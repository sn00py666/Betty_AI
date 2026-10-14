import json
from datetime import UTC, datetime, timedelta

import pytest

from services.freshness import LIVE_TTL
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
    assert len(Matches(tmp_path).read(NOW + timedelta(seconds=LIVE_TTL - 1))) == 2
    assert len(Matches(tmp_path).read(NOW + timedelta(seconds=LIVE_TTL + 1))) == 1
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


@pytest.mark.parametrize(
    "starts_at,expected",
    [
        ("2026-10-04T09:59:47.414043+00:00", "сегодня 13:00"),
        ("2026-10-04T10:00:12+00:00", "сегодня 13:00"),
        ("2026-10-04T10:30:00+00:00", "сегодня 13:30"),
        ("2026-10-04T20:59:47+00:00", "завтра 00:00"),
    ],
)
def test_display_time_rounds_to_nearest_minute(starts_at, expected):
    from services.matches import start_label

    match = dict(status="prematch", starts_at=starts_at)
    assert start_label(match, datetime(2026, 10, 4, 9, tzinfo=UTC)) == expected
    assert match["starts_at"] == starts_at


def test_envy_alias_merges_bookmakers_with_seconds_difference(tmp_path):
    start = NOW + timedelta(hours=2)
    snapshot(tmp_path, "pari", team1="Heroic", team2="Team Envy", starts_at=start.isoformat())
    snapshot(
        tmp_path,
        "leon",
        team1="ENVY",
        team2="HEROIC",
        starts_at=(start - timedelta(seconds=13)).isoformat(),
        p1=1.6,
        p2=2.6,
    )
    rows = Matches(tmp_path).read(NOW)
    assert len(rows) == 1
    assert (rows[0]["team1"], rows[0]["team2"]) == ("Envy", "HEROIC")
    assert [(o["p1"], o["p2"]) for o in rows[0]["offers"]] == [(1.6, 2.6), (1.5, 2.5)]


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
        fetched_at=(NOW - timedelta(seconds=LIVE_TTL + 1)).isoformat(),
        expires_at=(NOW + timedelta(seconds=60)).isoformat(),
    )
    assert Matches(tmp_path).read(NOW) == []


def test_live_matches_precede_earlier_upcoming_and_unknown_times(tmp_path):
    data = snapshot(tmp_path, "pari")
    base = data["matches"][0]
    data["matches"] = [
        dict(base, id="upcoming", starts_at=(NOW + timedelta(hours=1)).isoformat()),
        dict(base, id="live-unknown", status="live", starts_at=None),
        dict(base, id="live-later", status="live", starts_at=(NOW + timedelta(hours=2)).isoformat()),
        dict(base, id="live-earlier", status="live", starts_at=(NOW - timedelta(hours=1)).isoformat()),
    ]
    (tmp_path / "pari" / "latest.json").write_text(json.dumps(data))
    rows = Matches(tmp_path).read(NOW)
    assert [r["status"] for r in rows] == ["live", "live", "live", "prematch"]
    assert [r["starts_at"] for r in rows] == [data["matches"][i]["starts_at"] for i in (3, 2, 1, 0)]


def test_nuclear_fokus_live_merges_four_sources_without_start_times(tmp_path):
    # Реальный случай: Фонбет/PARI знают время, Леон/Марафон его не передают.
    common = dict(team1="Nuclear TigeRES", team2="FOKUS", status="live")
    snapshot(tmp_path, "fonbet", **common, url="https://fon.bet/live")
    snapshot(tmp_path, "pari", **common)
    snapshot(
        tmp_path,
        "leon",
        status="live",
        team1="FOKUS",
        team2="Nuclear Tigeres",
        starts_at=None,
        best_of=None,
        p1=1.8,
        p2=2.0,
        tournament="PGL Bucharest Open Qualifier",
    )
    snapshot(
        tmp_path,
        "marathon",
        **common,
        starts_at=None,
        tournament="CS 2. PGL. Бухарест. Открытая квалификация. Матч из 3-х карт",
    )
    rows = Matches(tmp_path).read(NOW)
    assert len(rows) == 1
    assert rows[0]["team1"] == "Nuclear TigeRES"
    offers = {offer["bookmaker"]: offer for offer in rows[0]["offers"]}
    assert set(offers) == {"fonbet", "pari", "leon", "marathon"}
    assert (offers["leon"]["p1"], offers["leon"]["p2"]) == (2.0, 1.8)
    assert rows[0]["starts_at"] is not None


def test_live_pair_merges_when_neither_source_knows_start(tmp_path):
    snapshot(tmp_path, "leon", status="live", starts_at=None, best_of=None)
    snapshot(tmp_path, "marathon", status="live", starts_at=None)
    rows = Matches(tmp_path).read(NOW)
    assert len(rows) == 1
    assert len(rows[0]["offers"]) == 2


@pytest.mark.parametrize("conflict", ["start", "best_of", "bookmaker"])
def test_unknown_live_fields_do_not_bridge_conflicting_events(tmp_path, conflict):
    snapshot(tmp_path, "leon", status="live", starts_at=None, best_of=None)
    snapshot(tmp_path, "fonbet", status="live", url="https://fon.bet/live")
    changes = {"status": "live"}
    if conflict == "start":
        changes["starts_at"] = (NOW + timedelta(hours=4)).isoformat()
    elif conflict == "best_of":
        changes["best_of"] = 5
    if conflict == "bookmaker":
        path = tmp_path / "fonbet" / "latest.json"
        data = json.loads(path.read_text())
        data["matches"].append(dict(data["matches"][0], id="second-event"))
        path.write_text(json.dumps(data))
    else:
        snapshot(tmp_path, "pari", **changes)
    assert len(Matches(tmp_path).read(NOW)) == 3


def test_live_fallback_never_merges_prematch_or_other_discipline(tmp_path):
    snapshot(tmp_path, "leon", status="live", starts_at=None)
    snapshot(tmp_path, "marathon", status="prematch", starts_at=None)
    snapshot(tmp_path, "pari", status="live", sport="dota2", starts_at=None)
    assert len(Matches(tmp_path).read(NOW)) == 3


def test_stale_live_offer_is_excluded_from_merged_match(tmp_path):
    snapshot(tmp_path, "leon", status="live", starts_at=None)
    snapshot(
        tmp_path,
        "marathon",
        status="live",
        starts_at=None,
        fetched_at=(NOW - timedelta(seconds=LIVE_TTL + 1)).isoformat(),
    )
    rows = Matches(tmp_path).read(NOW)
    assert len(rows) == 1
    assert [offer["bookmaker"] for offer in rows[0]["offers"]] == ["leon"]


def test_hltv_names_normalized_before_merging_sources(tmp_path):
    snapshot(tmp_path, "pari", team1="Ninjas in Pyjamas", team2="BB Team")
    snapshot(tmp_path, "fonbet", team1="BETBOOM", team2="NIP", p1=1.6, p2=2.6, url="https://fon.bet/sports/10")
    rows = Matches(tmp_path).read(NOW)
    assert len(rows) == 1
    assert (rows[0]["team1"], rows[0]["team2"]) == ("BETBOOM", "NIP")
    assert [(o["p1"], o["p2"]) for o in rows[0]["offers"]] == [(1.6, 2.6), (1.5, 2.5)]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Natus Vincere", "NAVI"),
        ("Ninja in Pyjamas", "NIP"),
        ("Team Spirit", "Spirit"),
        ("Team Envy", "Envy"),
        ("Team Falcons", "Falcons"),
        ("Aurora Gaming", "Aurora"),
        ("BB Team", "BETBOOM"),
        ("BetBoom Team", "BETBOOM"),
        ("TheMongolz", "The MongolZ"),
        ("Virtus Pro", "Virtus.pro"),
        ("Furia Esports", "FURIA"),
        ("Faze Clan", "FaZe"),
        ("Pain Gaming", "paiN"),
        ("Famalicão", "Famalicão"),
        ("Spirit Academy", "Spirit Academy"),
        ("Unknown Academy", "Unknown Academy"),
    ],
)
def test_reference_team_names(raw, expected):
    from services.team_names import canonical_name

    assert canonical_name(raw) == expected
    assert canonical_name(raw, "dota2") == raw
