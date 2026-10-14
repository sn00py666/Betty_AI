"""Disk-only HLTV context for forecasts. The bot never opens a browser."""

import copy
import gzip
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from integrations.hltv import parse_match, parse_team, url
from services.health import read, write
from services.matches import date
from services.team_names import team_key

PROFILE_TTL = 6 * 3600
PROFILE_MAX_AGE = 12 * 3600
MATCH_TTL = 300


def page_key(address):
    return hashlib.sha256(url(address).encode()).hexdigest()


class HltvCache:
    def __init__(self, directory: Path):
        self.directory = directory

    def load(self, address):
        try:
            return read(self.directory / "pages" / (page_key(address) + ".json"))
        except (OSError, ValueError):
            return {}

    def index(self):
        try:
            return read(self.directory / "index.json")
        except (OSError, ValueError):
            return {}

    def store(self, address, html, fetched_at=None):
        """Validate first; never replace good cache with a challenge/partial document."""
        address = url(address)
        parser = parse_team if "/team/" in address else parse_match
        data = parser(html, address)
        now = time.time() if fetched_at is None else fetched_at
        entry = {"fetched_at": now, "data": data}
        folder = self.directory / "pages"
        folder.mkdir(parents=True, exist_ok=True)
        key = page_key(address)
        raw = folder / (key + ".html.gz")
        temporary = raw.with_suffix(".tmp")
        temporary.write_bytes(gzip.compress(html.encode()))
        temporary.replace(raw)
        previous = self.load(address)
        write(folder / (key + ".json"), entry)
        if data["kind"] == "team":
            index = self.index()
            index[str(data["id"])] = {"id": data["id"], "name": data["name"], "url": address}
            write(self.directory / "index.json", index)
            # Keep bounded dated snapshots for audit/roster changes. Raw HTML is latest-only.
            history = self.directory / "history" / str(data["id"])
            if previous.get("data") != data:
                history.mkdir(parents=True, exist_ok=True)
                snapshot = history / f"{int(now)}.json.gz"
                temporary = snapshot.with_suffix(".tmp")
                temporary.write_bytes(gzip.compress(json.dumps(entry, ensure_ascii=False).encode()))
                temporary.replace(snapshot)
                for old in sorted(history.glob("*.json.gz"))[:-30]:
                    old.unlink()
        return entry

    def register(self, teams):
        index = self.index()
        for team in teams:
            if team and team.get("name"):
                index[str(team["id"])] = team
        write(self.directory / "index.json", index)

    def team(self, name):
        candidates = [t for t in self.index().values() if team_key(t["name"]) == team_key(name)]
        return candidates[0] if len(candidates) == 1 else None

    def wanted(self, names):
        """Independent per-name request files avoid bot/collector read-modify-write races."""
        for name in names:
            key = hashlib.sha256(team_key(name).encode()).hexdigest()
            write(self.directory / "wanted" / f"{key}.json", {"name": name, "requested_at": time.time()})

    def context(self, match, now=None):
        if match["sport"] != "cs2":
            return None
        now = time.time() if now is None else now
        cutoff = datetime.fromtimestamp(now, UTC)
        start = date(match.get("starts_at"))
        if match.get("status") == "prematch" and start:
            cutoff = min(cutoff, start)
        result = {"source": "HLTV", "teams": {}, "match": None}
        for side in ("team1", "team2"):
            team = self.team(match[side])
            if not team:
                continue
            entry = self.load(team["url"])
            fetched = entry.get("fetched_at", 0)
            if not 0 <= now - fetched <= PROFILE_MAX_AGE or fetched > cutoff.timestamp():
                continue
            data = copy.deepcopy(entry.get("data", {}))
            if not data or data.get("id") != team["id"]:
                continue
            # Keep complete parsed data on disk, send relevant windows to the model.
            data.pop("ranking_history", None)
            data["results"] = [
                r
                for r in data["results"]
                if date(r.get("starts_at")) and cutoff - timedelta(days=180) <= date(r["starts_at"]) < cutoff
            ]
            details = []
            for record in sorted(data["results"], key=lambda r: r["starts_at"], reverse=True)[:10]:
                detail = self.load(record["url"]).get("data", {})
                if (
                    detail.get("status") == "finished"
                    and detail.get("starts_at") == record["starts_at"]
                    and {t["id"] for t in detail.get("teams", [])} == {t["id"] for t in record["teams"]}
                ):
                    details.append(detail)
            data["recent_match_details"] = details
            data["news"] = [
                r
                for r in data["news"]
                if date(r.get("date")) and cutoff - timedelta(days=30) <= date(r["date"]) <= cutoff
            ][:20]
            data["transfers"] = [
                r
                for r in data["transfers"]
                if date(r.get("date")) and cutoff - timedelta(days=180) <= date(r["date"]) <= cutoff
            ][:20]
            data["achievements"] = data["achievements"][:20]
            data["events"] = data["events"][:20]
            data["fetched_at"] = fetched
            data["freshness"] = "fresh" if now - fetched <= PROFILE_TTL else "refresh_due"
            result["teams"][side] = data
        if not result["teams"]:
            return None
        if len(result["teams"]) == 2 and start:
            ids = {t["id"] for t in result["teams"].values()}
            candidates = {}
            for team in result["teams"].values():
                for record in team["upcoming"] + team["results"]:
                    observed = date(record.get("starts_at"))
                    if (
                        {t["id"] for t in record["teams"]} == ids
                        and observed
                        and abs((observed - start).total_seconds()) <= 300
                    ):
                        candidates[record["id"]] = record
            if len(candidates) == 1:
                record = next(iter(candidates.values()))
                entry = self.load(record["url"])
                data = entry.get("data", {})
                if (
                    data
                    and date(data.get("starts_at"))
                    and abs((date(data["starts_at"]) - start).total_seconds()) <= 300
                    and 0 <= now - entry.get("fetched_at", 0) <= MATCH_TTL
                    and entry["fetched_at"] <= cutoff.timestamp()
                    and {t["id"] for t in data.get("teams", [])} == ids
                    and data.get("status") == ("live" if match["status"] == "live" else "scheduled")
                ):
                    # Finished/current-match stats must never leak into a prematch forecast.
                    result["match"] = {
                        k: data[k] for k in ("id", "url", "teams", "starts_at", "event", "status", "veto", "lineups")
                    } | {"fetched_at": entry["fetched_at"]}
        return result


def enrich_statistics(statistics, context):
    """Keep sources distinct. HLTV supplies a fallback only with both teams' history."""
    if not context:
        return statistics
    stats = copy.deepcopy(statistics) if statistics else None
    teams = context["teams"]
    if stats is None:
        if set(teams) != {"team1", "team2"} or not all(t["results"] for t in teams.values()):
            return None
        stats = {"source": "HLTV", "updated_at": min(t["fetched_at"] for t in teams.values())}
        for side, team in teams.items():
            played = sorted(team["results"], key=lambda r: r["starts_at"], reverse=True)
            recent = []
            for row in played:
                index = next(i for i, t in enumerate(row["teams"]) if t["id"] == team["id"])
                if row["scores"][0] == row["scores"][1]:
                    continue
                recent.append(
                    {
                        "date": row["starts_at"],
                        "won": row["scores"][index] > row["scores"][1 - index],
                        "opponent": row["teams"][1 - index]["name"],
                        "url": row["url"],
                    }
                )
            stats[side] = {
                "games": len(recent[:5]),
                "wins": sum(r["won"] for r in recent[:5]),
                "recent": recent[:5],
                "country": team["country"],
            }
        ids = {t["id"] for t in teams.values()}
        h2h = {
            r["id"]: r
            for t in teams.values()
            for r in t["results"]
            if {op["id"] for op in r["teams"]} == ids and r["scores"][0] != r["scores"][1]
        }
        stats["head_to_head"] = {
            "games": len(h2h),
            "team1_wins": sum(
                r["teams"][r["scores"].index(max(r["scores"]))]["id"] == teams["team1"]["id"] for r in h2h.values()
            ),
            "period_days": 180,
            "scope": "HLTV profile results, series not maps",
        }
    stats["hltv"] = context
    return stats
