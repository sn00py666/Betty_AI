"""Порядок матчей: LIVE, рейтинг дисциплины, охват источников, время."""

from datetime import UTC, datetime

from services.dota_rankings import dota_team_key, select_ranking
from services.matches import date, match_order
from services.rankings import ValveRankings
from services.team_names import team_key


def sort_matches(items, mode, rankings_path, *, dota_ranks=None):
    if mode == "time":
        return sorted(items, key=match_order)
    today = datetime.now(UTC).date()
    snapshots = ValveRankings(rankings_path).state().get("snapshots", {}).values()
    valid = [
        s
        for s in snapshots
        if date(s.get("date", "") + "T00:00:00+00:00")
        and 0 <= (today - date(s["date"] + "T00:00:00+00:00").date()).days <= 45
    ]
    ranks = max(valid, key=lambda s: s["date"])["teams"] if valid else {}
    if dota_ranks is None:
        _, dota_ranks, _ = select_ranking(rankings_path.parent)

    def order(match):
        ranking = ranks if match["sport"] == "cs2" else dota_ranks
        key = team_key if match["sport"] == "cs2" else dota_team_key
        teams = sorted(ranking.get(key(match[side]), {}).get("rank", 10000) for side in ("team1", "team2"))
        coverage = len({o["bookmaker"] for o in match["offers"]})
        live, starts, name = match_order(match)
        return live, *teams, -coverage, starts, name

    return sorted(items, key=order)
