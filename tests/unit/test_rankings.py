from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from services.factors import factors
from services.health import read, write
from services.rankings import RAW, TREE, ValveRankings, parse_standings, retry_delay


def standings():
    rows = [f"| {i} | {2000 - i * 10} | Top{i} | a,b,c,d,e | details |" for i in range(1, 11)]
    rows += ["| 19 | 1527 | Liquid | a,b,c,d,e | details |", "| 37 | 1386 | BBL | a,b,c,d,e | details |"]
    return "### Standings as of 2026_10_05<br />\n" + "\n".join(rows)


def test_ambiguous_rosters_are_excluded_instead_of_picking_a_rank():
    text = standings() + "\n| 61 | 1201 | CYBERSHOKE | a,b,c,d,e | details |"
    text += "\n| 171 | 786 | CYBERSHOKE | f,g,h,i,j | details |"
    text += "\n| 172 | 780 | CYBERSHOKE | k,l,m,n,o | details |"
    result = parse_standings(text)
    assert "cybershoke" not in result["teams"]
    assert result["ambiguous"] == ["cybershoke"]
    assert result["teams"]["liquid"]["rank"] == 19
    with pytest.raises(ValueError):
        parse_standings(text.replace("| 1 |", "| 11 |"))


def test_historical_top10_uses_prior_ranking_and_excludes_walkovers(tmp_path):
    path = tmp_path / "valve.json"
    previous = dict(date="2026-09-07", teams={"spirit": {"rank": 1}, "liquid": {"rank": 20}})
    current = dict(date="2026-10-05", teams={"spirit": {"rank": 50}, "liquid": {"rank": 19}})
    write(path, dict(snapshots={"old": previous, "new": current}))
    now = datetime(2026, 10, 12, tzinfo=UTC)

    def match(day, **changes):
        return (
            dict(
                begin_at=f"2026-{day}T12:00:00Z",
                end_at=f"2026-{day}T14:00:00Z",
                status="finished",
                winner_id=1,
                teams=[dict(id=1, name="Team Liquid"), dict(id=2, name="Team Spirit")],
            )
            | changes
        )

    history = [
        match("10-04"),  # В тот день Spirit были топ-1, даже если сейчас топ-50.
        match("10-05"),  # Снимок дня матча не используется: время публикации неизвестно.
        match("10-06"),  # После публикации нового рейтинга это уже не топ-10.
        match("10-03", forfeit=True),
        match("10-13"),
        match("09-07"),  # Самый ранний снимок ещё не был известен.
        match("10-02", end_at=None),
    ]
    enriched = ValveRankings(path).enrich(history, 1, now - timedelta(days=180), now)
    assert enriched["top10"] == dict(games=2, wins=2, ranking="Valve VRS")
    assert enriched["valve_rank"]["rank"] == 19
    assert ValveRankings(path).enrich(history, 1, now - timedelta(days=180), now + timedelta(days=46)) == {}


async def test_refresh_once_per_day_and_reuses_downloaded_snapshots(tmp_path, monkeypatch):
    path = tmp_path / "valve.json"
    snapshot_path = "live/2026/standings_global_2026_10_05.md"

    async def reply(self, url, **kwargs):
        if url == TREE:
            return httpx.Response(200, json={"tree": [{"path": snapshot_path}]}, request=httpx.Request("GET", url))
        assert url == RAW + snapshot_path
        return httpx.Response(200, text=standings(), request=httpx.Request("GET", url))

    get = AsyncMock(side_effect=reply)
    monkeypatch.setattr(httpx.AsyncClient, "get", lambda self, url, **kwargs: get(self, url, **kwargs))
    await ValveRankings(path).refresh()
    await ValveRankings(path).refresh()
    assert get.await_count == 2
    assert read(tmp_path / "health" / "valve.json")["last_success"]
    state = read(path)
    state["next_request"] = 0
    write(path, state)
    await ValveRankings(path).refresh()
    assert get.await_count == 3  # Новый индекс, прежний файл не загружается повторно.


async def test_access_error_preserves_ratings_and_honors_http_date(tmp_path, monkeypatch):
    path = tmp_path / "valve.json"
    snapshots = {"old": parse_standings(standings())}
    write(path, dict(snapshots=snapshots))
    get = AsyncMock(return_value=httpx.Response(403, request=httpx.Request("GET", TREE)))
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    await ValveRankings(path).refresh()
    await ValveRankings(path).refresh()
    assert read(path)["snapshots"] == snapshots
    assert read(tmp_path / "health" / "valve.json")["http_status"] == 403
    assert get.await_count == 1
    assert retry_delay("Thu, 01 Oct 2026 12:00:00 GMT", 1790848800) == 7200
    assert retry_delay("invalid", 0) == 3600


def test_valve_factor_compares_points_not_direction_of_rank():
    match = dict(
        sport="cs2",
        team1="Liquid",
        team2="BBL",
        statistics=dict(
            team1=dict(valve_rank=dict(rank=19, points=1527), valve_date="2026-10-05"),
            team2=dict(valve_rank=dict(rank=37, points=1386), valve_date="2026-10-05"),
        ),
    )
    assert next(f for f in factors(match) if f["label"] == "Рейтинг Valve")["edge"] == "team1"
    match["statistics"]["team2"]["valve_date"] = "2026-09-07"
    assert not any(f["label"] == "Рейтинг Valve" for f in factors(match))
