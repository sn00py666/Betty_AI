"""Общие сроки свежести и приоритеты обновления линии."""

from services.freshness import LIVE_TTL, PREMATCH_TTL
from services.matches import date

FAST_INTERVAL = 30
START_WINDOW = 600
START_GRACE = 1800
BROWSER_SOURCES = frozenset({"winline", "leon", "betboom"})
HTTP_LIVE_SOURCES = frozenset({"fonbet", "pari", "marathon"})
LIVE_SOURCES = BROWSER_SOURCES | HTTP_LIVE_SOURCES
LIVE_DISCOVERY_INTERVAL = 300


def hot_sports(snapshots, now):
    sports = set()
    for snapshot in snapshots:
        for match in snapshot.get("matches", []):
            observed = date(match.get("fetched_at") or snapshot.get("fetched_at"))
            if observed is None or not 0 <= now - observed.timestamp() <= PREMATCH_TTL:
                continue
            sport = match.get("sport", "cs2")
            if match.get("status") == "live" and now - observed.timestamp() <= LIVE_TTL:
                sports.add(sport)
            start = date(match.get("starts_at"))
            if match.get("status") == "prematch" and start and -START_GRACE <= start.timestamp() - now <= START_WINDOW:
                sports.add(sport)
    return sports & {"cs2", "dota2"}


def next_start_check(snapshots, now):
    starts = [
        start.timestamp() - START_WINDOW
        for snapshot in snapshots
        for match in snapshot.get("matches", [])
        if match.get("status") == "prematch"
        and (start := date(match.get("starts_at")))
        and start.timestamp() - START_WINDOW > now
    ]
    return min(starts, default=now + PREMATCH_TTL)
