"""Свежие снимки букмекеров → список матчей с сопоставимыми коэффициентами."""

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from integrations.bookmakers import SOURCES, price

MOSCOW = ZoneInfo("Europe/Moscow")
BOOKMAKERS = {
    "fonbet": "Фонбет",
    "pari": "PARI",
    "marathon": "Марафон",
    "winline": "Winline",
    "leon": "Леон",
    "betboom": "BetBoom",
}
# Только известные варианты, без нечёткого сравнения и удаления Academy/Youngsters.
ALIASES = {
    "teamspirit": "spirit",
    "g2esports": "g2",
    "natusvincere": "navi",
    "nemigagaming": "nemiga",
    "bbteam": "betboomteam",
}
logger = logging.getLogger(__name__)


def team_key(name):
    key = re.sub(r"[^\w]", "", name.casefold())
    return ALIASES.get(key, key)


def date(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def compatible(left, right):
    if left["sport"] != right["sport"] or left["status"] != right["status"]:
        return False
    if sorted(left["teams"]) != sorted(right["teams"]):
        return False
    if left.get("best_of") and right.get("best_of") and left["best_of"] != right["best_of"]:
        return False
    a, b = date(left.get("starts_at")), date(right.get("starts_at"))
    if a and b:
        return abs((a - b).total_seconds()) <= 300
    # Без точного времени не объединяем разные события на основании одних имён.
    return False


def start_label(match):
    if match["status"] == "live":
        return "LIVE"
    start = date(match.get("starts_at"))
    return start.astimezone(MOSCOW).strftime("%d.%m %H:%M МСК") if start else "время уточняется"


def market_probability(match):
    pairs = [(o["p1"], o["p2"]) for o in match["offers"] if o["p1"] and o["p2"]]
    return median(b / (a + b) for a, b in pairs) if pairs else None


class Matches:
    def __init__(self, directory: Path):
        self.directory = directory

    def read(self, now=None):
        now = now or datetime.now(UTC)
        rows = []
        for bookmaker in SOURCES:
            path = self.directory / bookmaker / "latest.json"
            try:
                snapshot = json.loads(path.read_text())
                fetched = date(snapshot.get("fetched_at"))
                if not fetched or fetched > now:
                    continue
                for raw in snapshot["matches"]:
                    expires = date(raw.get("expires_at") or snapshot.get("expires_at"))
                    if not expires or expires <= now or raw.get("market") != "match_winner":
                        continue
                    status = raw.get("status")
                    if status not in ("live", "prematch"):
                        continue
                    age = (now - fetched).total_seconds()
                    if age > (60 if status == "live" else 1800):
                        continue
                    start = date(raw.get("starts_at"))
                    if status == "prematch" and start and start <= now:
                        continue
                    if not raw.get("team1") or not raw.get("team2"):
                        continue
                    source_host = urlsplit(SOURCES[bookmaker]).hostname
                    host = urlsplit(raw.get("url", "")).hostname
                    allowed = {source_host, "fon.bet" if bookmaker == "fonbet" else f"{bookmaker}.ru"}
                    if urlsplit(raw.get("url", "")).scheme != "https" or host not in allowed:
                        continue
                    rows.append(
                        {
                            **raw,
                            "bookmaker": bookmaker,
                            "sport": raw.get("sport", "cs2"),
                            "teams": [team_key(raw["team1"]), team_key(raw["team2"])],
                            "fetched_at": fetched.isoformat(),
                            "p1": price(raw.get("p1")),
                            "p2": price(raw.get("p2")),
                        }
                    )
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning("Не удалось прочитать снимок %s", bookmaker)
        # Точные даты первыми; одинаковый порядок сохраняет идентификатор при обновлении цен.
        rows.sort(key=lambda row: (not bool(date(row.get("starts_at"))), row["bookmaker"], row["id"]))
        groups = []
        for row in rows:
            candidates = [g for g in groups if compatible(g, row)]
            group = candidates[0] if len(candidates) == 1 else None
            if group and any(o["bookmaker"] == row["bookmaker"] for o in group["offers"]):
                group = None
            if group is None:
                identity = f"{row['sport']}:{row['bookmaker']}:{row['id']}"
                group = {**row, "id": hashlib.sha256(identity.encode()).hexdigest()[:16], "offers": []}
                groups.append(group)
            p1, p2 = row["p1"], row["p2"]
            if row["teams"] != group["teams"]:
                p1, p2 = p2, p1
            group["offers"].append(
                {
                    "bookmaker": row["bookmaker"],
                    "p1": p1,
                    "p2": p2,
                    "url": row["url"],
                    "fetched_at": row["fetched_at"],
                }
            )
        return sorted(groups, key=lambda m: (m["status"] != "live", m.get("starts_at") or "9999", m["team1"]))

    def get(self, match_id):
        return next((match for match in self.read() if match["id"] == match_id), None)
