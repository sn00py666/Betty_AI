import json
from datetime import UTC, datetime, timedelta

import pytest

from services.matches import Matches, market_probability

NOW = datetime(2026, 10, 4, 20, tzinfo=UTC)


def snapshot(folder, bookmaker, **overrides):
    match = dict(
        id="10",
        sport="cs2",
        team1="NAVI",
        team2="Team Spirit",
        tournament="ESL",
        best_of=3,
        status="prematch",
        market="match_winner",
        starts_at=(NOW + timedelta(hours=2)).isoformat(),
        p1=2.5,
        p2=1.5,
        url=f"https://{bookmaker}.ru/sports/10",
    )
    match.update(overrides)
    directory = folder / bookmaker
    directory.mkdir(exist_ok=True, parents=True)
    data = dict(fetched_at=NOW.isoformat(), expires_at=(NOW + timedelta(minutes=30)).isoformat(), matches=[match])
    (directory / "latest.json").write_text(json.dumps(data))
    return data


def test_reversed_teams_and_aliases_keep_correct_prices(tmp_path):
    snapshot(tmp_path, "pari")
    snapshot(tmp_path, "fonbet", team1="Spirit", team2="Natus Vincere", p1=1.6, p2=2.6, url="https://fon.bet/sports/10")
    matches = Matches(tmp_path).read(NOW)
    assert len(matches) == 1
    match = matches[0]
    assert match["team1"] == "Spirit"
    assert [(o["p1"], o["p2"]) for o in match["offers"]] == [(1.6, 2.6), (1.5, 2.5)]
    assert market_probability(match) == pytest.approx((2.6 / 4.2 + 2.5 / 4) / 2)


@pytest.mark.parametrize(
    "changes",
    [
        dict(starts_at=None),
        dict(sport="dota2"),
        dict(team2="Spirit Academy"),
        dict(best_of=5),
        dict(starts_at=(NOW + timedelta(days=1)).isoformat()),
    ],
)
def test_uncertain_or_different_events_are_not_merged(tmp_path, changes):
    snapshot(tmp_path, "pari")
    snapshot(tmp_path, "fonbet", url="https://fon.bet/sports/10", **changes)
    assert len(Matches(tmp_path).read(NOW)) == 2


@pytest.mark.parametrize(
    "changes",
    [
        dict(url="https://evil.example/"),
        dict(p1="nan", p2="inf"),
        dict(starts_at=NOW.isoformat()),
        dict(market="map_winner"),
    ],
)
def test_invalid_or_closed_markets(tmp_path, changes):
    snapshot(tmp_path, "pari", **changes)
    items = Matches(tmp_path).read(NOW)
    if "p1" in changes:
        assert market_probability(items[0]) is None
    else:
        assert items == []


def test_stale_prematch_and_live_hidden(tmp_path):
    snapshot(tmp_path, "pari")
    snapshot(tmp_path, "fonbet", url="https://fon.bet/sports/10", status="live")
    assert len(Matches(tmp_path).read(NOW + timedelta(seconds=61))) == 1
    assert Matches(tmp_path).read(NOW + timedelta(minutes=31)) == []


def test_per_match_expiry_preserves_prematch_in_mixed_snapshot(tmp_path):
    data = snapshot(tmp_path, "pari", expires_at=(NOW + timedelta(minutes=30)).isoformat())
    data["expires_at"] = (NOW + timedelta(seconds=60)).isoformat()
    (tmp_path / "pari" / "latest.json").write_text(json.dumps(data))
    assert len(Matches(tmp_path).read(NOW + timedelta(minutes=5))) == 1


def test_dates_use_moscow_day_at_utc_midnight_boundary():
    from services.matches import start_label

    now = datetime(2026, 10, 4, 22, tzinfo=UTC)  # Уже 5 октября в Москве.
    match = dict(status="prematch", starts_at="2026-10-04T23:00:00+00:00")
    assert start_label(match, now) == "сегодня 02:00"
    match["starts_at"] = "2026-10-05T23:00:00+00:00"
    assert start_label(match, now) == "завтра 02:00"


def test_chronological_order_puts_unknown_times_last(tmp_path):
    data = snapshot(tmp_path, "pari")
    original = data["matches"][0]
    data["matches"] = [
        dict(original, id="late", starts_at=(NOW + timedelta(hours=3)).isoformat()),
        dict(original, id="unknown", starts_at=None),
        dict(original, id="early", starts_at=(NOW + timedelta(hours=1)).isoformat()),
    ]
    (tmp_path / "pari" / "latest.json").write_text(json.dumps(data))
    rows = Matches(tmp_path).read(NOW)
    assert [r["starts_at"] for r in rows] == [data["matches"][i]["starts_at"] for i in (2, 0, 1)]


def test_browser_capture_time_is_not_extended_by_later_pages(tmp_path):
    snapshot(
        tmp_path,
        "pari",
        status="live",
        fetched_at=(NOW - timedelta(seconds=61)).isoformat(),
        expires_at=(NOW + timedelta(seconds=60)).isoformat(),
    )
    assert Matches(tmp_path).read(NOW) == []
