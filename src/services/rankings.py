"""Бесплатные публичные снимки Valve VRS: рейтинг и исторический топ-10."""

import asyncio
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from services.health import api_state, read, write
from services.matches import date
from services.team_names import team_key

REPOSITORY = "ValveSoftware/counter-strike_regional_standings"
RAW = f"https://raw.githubusercontent.com/{REPOSITORY}/main/"
TREE = f"https://api.github.com/repos/{REPOSITORY}/git/trees/main?recursive=1"


def parse_standings(text):
    stamp = re.search(r"Standings as of (\d{4}_\d{2}_\d{2})", text)
    if not stamp:
        raise ValueError("Не найдена дата рейтинга Valve")
    teams = {}
    ambiguous = set()
    for line in text.splitlines():
        cells = [part.strip() for part in line.strip().split("|")]
        if len(cells) < 6 or not cells[1].isdigit() or not cells[2].isdigit():
            continue
        key = team_key(cells[3])
        if key in teams or key in ambiguous:
            # Valve ранжирует составы; одинаковое название не определяет состав.
            teams.pop(key, None)
            ambiguous.add(key)
            continue
        teams[key] = dict(rank=int(cells[1]), points=int(cells[2]), name=cells[3])
    if len(teams) < 10 or sum(t["rank"] <= 10 for t in teams.values()) != 10:
        raise ValueError("Неполный топ-10 в рейтинге Valve")
    return dict(date=stamp[1].replace("_", "-"), teams=teams, ambiguous=sorted(ambiguous))


def retry_delay(value, now):
    try:
        return max(3600, int(value))
    except (ValueError, TypeError):
        try:
            return max(3600, parsedate_to_datetime(value).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 3600


class ValveRankings:
    def __init__(self, path):
        self.path = path

    def state(self):
        try:
            return read(self.path)
        except (OSError, ValueError):
            return {}

    async def refresh(self):
        state = self.state()
        now = time.time()
        if now < state.get("next_request", 0):
            return
        state["next_request"] = now + 86400
        write(self.path, state)  # Один индекс в сутки, включая перезапуски.
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(TREE, headers={"User-Agent": "BettyAI/0.1", "Accept": "application/json"})
                response.raise_for_status()
                tree = response.json()
                if tree.get("truncated"):
                    raise ValueError("Неполный индекс снимков Valve")
                paths = sorted(
                    row["path"]
                    for row in tree["tree"]
                    if re.fullmatch(r"live/\d{4}/standings_global_\d{4}_\d{2}_\d{2}\.md", row["path"])
                    and row["path"].split("standings_global_")[1][:-3].replace("_", "-")
                    <= datetime.now(UTC).date().isoformat()
                )[-8:]
                if not paths:
                    raise ValueError("Не найдены снимки рейтинга Valve")
                snapshots = dict(state.get("snapshots", {}))
                for path in paths:
                    if path not in snapshots:
                        response = await client.get(RAW + path)
                        response.raise_for_status()
                        snapshots[path] = parse_standings(response.text)
                state.update(snapshots={p: snapshots[p] for p in paths}, last_success=time.time(), error=None)
                write(self.path, state)
                api_state(self.path.parent / "health" / "valve.json", status=200)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            delay = 3600
            if isinstance(error, httpx.HTTPStatusError):
                delay = retry_delay(error.response.headers.get("Retry-After"), time.time())
            state.update(next_request=time.time() + delay, error=f"HTTP {code}" if code else type(error).__name__)
            write(self.path, state)
            api_state(
                self.path.parent / "health" / "valve.json",
                error=state["error"],
                status=code,
                retry_at=state["next_request"],
            )

    def enrich(self, history, team_id, cutoff, now):
        snapshots = sorted(self.state().get("snapshots", {}).values(), key=lambda row: row["date"])
        # Без надёжного снимка нет подстановки старого рейтинга как текущего.
        valid = [s for s in snapshots if 0 <= (now.date() - datetime.fromisoformat(s["date"]).date()).days <= 45]
        if not valid:
            return {}
        current = valid[-1]
        name = next((t["name"] for r in history for t in r["teams"] if t["id"] == team_id), None)
        result = {"valve_rank": current["teams"].get(team_key(name or "")), "valve_date": current["date"]}
        games, wins = 0, 0
        for row in history:
            played, ended = date(row.get("begin_at")), date(row.get("end_at"))
            if (
                row["status"] != "finished"
                or row.get("forfeit")
                or not row.get("winner_id")
                or len(row["teams"]) != 2
                or not played
                or not ended
                or not cutoff <= played < now
                or ended > now
            ):
                continue
            opponent = next((t for t in row["teams"] if t["id"] != team_id), None)
            if not opponent or not any(t["id"] == team_id for t in row["teams"]):
                continue
            # Только рейтинг, опубликованный ДО этого матча, без утечки будущих результатов.
            prior = [s for s in snapshots if datetime.fromisoformat(s["date"]).date() < played.date()]
            if not prior or (played.date() - datetime.fromisoformat(prior[-1]["date"]).date()).days > 45:
                continue
            ranking = prior[-1]["teams"].get(team_key(opponent["name"]))
            if ranking and ranking["rank"] <= 10:
                games += 1
                wins += row["winner_id"] == team_id
        if games:
            result["top10"] = dict(games=games, wins=wins, ranking="Valve VRS")
        return result


async def run_rankings(path):
    rankings = ValveRankings(path)
    while True:
        await rankings.refresh()
        await asyncio.sleep(60)
