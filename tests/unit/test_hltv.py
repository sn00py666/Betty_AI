import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from config import Settings
from integrations.errors import StopSource
from integrations.hltv import parse_match, parse_schedule, parse_team, url
from services.ai import Analyst, facts
from services.analysis import render
from services.health import read, write
from services.hltv import PROFILE_MAX_AGE, HltvCache, enrich_statistics
from services.hltv_collection import DISCOVERY, collect_hltv, next_job

FIXTURES = Path(__file__).parents[1] / "fixtures" / "hltv"
SPIRIT = "https://www.hltv.org/team/7020/spirit"
AURORA = "https://www.hltv.org/team/11861/aurora"
MATCH = "https://www.hltv.org/matches/2398756/aurora-vs-spirit-esl-pro-league-season-24"
NOW = datetime(2026, 10, 15, tzinfo=UTC).timestamp()


def fixture(name):
    return (FIXTURES / f"{name}.html").read_text()


@pytest.fixture
def cache(tmp_path):
    cache = HltvCache(tmp_path / "hltv")
    cache.store(SPIRIT, fixture("team"), NOW - 10)
    cache.store(AURORA, fixture("aurora"), NOW - 10)
    cache.store(MATCH, fixture("match"), NOW - 10)
    return cache


@pytest.fixture
def match():
    return {
        "id": "test",
        "sport": "cs2",
        "team1": "Team Spirit",
        "team2": "Aurora Gaming",
        "status": "prematch",
        "starts_at": "2026-10-16T10:00:00+00:00",
        "tournament": "test",
        "offers": [],
    }


def test_real_profile_preserves_tabs_periods_and_rating_versions():
    data = parse_team(fixture("team"), SPIRIT + "#tab-rosterBox")
    assert data["url"] == SPIRIT and data["country"] == "RU"
    assert data["rankings"]["hltv"]["rank"] == 1
    assert data["rankings"]["valve"]["rank"] == 3
    assert len(data["coverage"]["tabs"]) == 7
    players = {p["name"]: p for p in data["roster"]}
    assert players["chopper"]["status"] == "BENCHED"
    assert players["donk"]["rating_version"] == "Rating 2.0"
    assert players["sh1ro"]["rating_version"] == "Rating 3.0"
    ancient = data["maps"][0]
    assert (ancient["wins"], ancient["losses"], ancient["win_percent"]) == (9, 1, 90)
    assert ancient["period"] == {"startDate": "2026-07-15", "endDate": "2026-10-15"}
    assert ancient["veto"]["Picks"] == "19% of 36"
    assert data["news"][0]["date"] and data["transfers"][0]["date"]
    assert "AI GENERATED" not in json.dumps(data)
    assert data["results"][0]["scores"] == [0, 2]
    assert data["results"][0]["teams"][0]["id"] == 7020


def test_real_match_preserves_orientation_unplayed_maps_and_actual_lineup():
    data = parse_match(fixture("match"), MATCH)
    assert data["status"] == "finished"
    assert [t["id"] for t in data["teams"]] == [11861, 7020]
    assert data["maps"][0]["scores"] == [13, 9]
    assert data["maps"][2]["scores"] == [None, None]
    assert data["maps"][0]["picked_by"] == ["Spirit"]
    assert len(data["player_stats"]) == 10  # Not all-map + per-map + CT/T duplicated.
    assert data["player_stats"][0]["metrics"]["adr"] == "98.6"  # Not eco-adjusted ADR.
    assert len(data["lineups"][0]["players"]) == 5


def test_schedule_finds_teams_without_profile_links():
    rows = parse_schedule(fixture("matches"))
    assert rows[0]["names"] == ["JOGA DE TERNO", "A NATA"]
    assert rows[0]["live"]
    assert all(row["url"].startswith("https://www.hltv.org/matches/") for row in rows)


@pytest.mark.parametrize(
    "address", ["http://www.hltv.org/team/1/x", "https://evil.test/team/1/x", "https://www.hltv.org@evil.test/team/1/x"]
)
def test_external_or_plain_http_urls_rejected(address):
    with pytest.raises(ValueError):
        url(address)


def test_partial_or_challenge_does_not_replace_good_cache(cache):
    before = cache.load(SPIRIT)
    for html in ('<form id="challenge-form"></form>', '<h1 class="profile-team-name">Spirit</h1>'):
        with pytest.raises(ValueError):
            cache.store(SPIRIT, html)
        assert cache.load(SPIRIT) == before
    # Hidden login recaptcha exists on the real public page; it is not a blocking challenge.
    assert parse_team(fixture("team") + '<div class="g-recaptcha" id="login-recaptcha"></div>', SPIRIT)


def test_context_aliases_cache_restart_and_no_dota(cache, match):
    context = HltvCache(cache.directory).context(match, NOW)
    assert context["teams"]["team1"]["id"] == 7020
    assert len(context["teams"]["team1"]["recent_match_details"]) == 1
    assert context["match"] is None
    assert cache.context(match | {"sport": "dota2"}, NOW) is None
    assert cache.context(match, NOW + PROFILE_MAX_AGE + 1) is None
    assert cache.context(match | {"starts_at": "2026-10-10T00:00:00+00:00"}, NOW) is None


def test_ambiguous_team_identity_is_not_guessed(cache, match):
    cache.register([{"id": 999, "name": "Team Spirit", "url": "https://www.hltv.org/team/999/spirit"}])
    assert cache.team("Spirit") is None
    assert "team1" not in cache.context(match, NOW)["teams"]
    assert cache.team("Spirit Academy") is None


def test_source_fallback_and_reversed_h2h_series(cache, match):
    context = cache.context(match, NOW)
    stats = enrich_statistics(None, context)
    assert stats["source"] == "HLTV"
    assert stats["team1"]["games"] == 5 and stats["team1"]["wins"] == 2
    assert stats["head_to_head"]["games"] == 1  # Series duplicated across profiles counted once.
    assert stats["head_to_head"]["team1_wins"] == 0
    existing = {"source": "PandaScore", "updated_at": NOW, "team1": {"games": 5, "wins": 4}}
    assert enrich_statistics(existing, context)["team1"]["wins"] == 4
    assert "hltv" not in existing
    assert "HLTV," in render(match | {"statistics": stats})
    assert "PandaScore" not in render(match | {"statistics": stats})


def test_exact_current_match_and_no_finished_outcome_leak(cache, match):
    start = "2026-10-16T10:00:00+00:00"
    entry = cache.load(AURORA)
    row = copy.deepcopy(entry["data"]["upcoming"][0])
    row.update(
        url=MATCH, id=2398756, starts_at=start, teams=[{"id": 7020, "name": "Spirit"}, {"id": 11861, "name": "Aurora"}]
    )
    entry["data"]["upcoming"] = [row]
    from services.hltv import page_key

    write(cache.directory / "pages" / (page_key(AURORA) + ".json"), entry)
    assert cache.context(match, NOW)["match"] is None  # Finished fixture cannot represent future match.
    detail = cache.load(MATCH)
    detail["data"].update(status="scheduled", starts_at=start)
    write(cache.directory / "pages" / (page_key(MATCH) + ".json"), detail)
    context = cache.context(match, NOW)
    assert context["match"]["id"] == 2398756
    assert "player_stats" not in context["match"] and "maps" not in context["match"]
    assert cache.context(match | {"starts_at": None}, NOW)["match"] is None


async def test_llm_receives_hltv_without_pandascore(cache, match, tmp_path, monkeypatch):
    monkeypatch.setattr("services.ai.Statistics.describe", AsyncMock(return_value=None))
    monkeypatch.setattr("services.hltv.time.time", lambda: NOW)
    post = AsyncMock(
        return_value=httpx.Response(
            200,
            request=httpx.Request("POST", "https://example.test"),
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"winner": "unclear", "probability": None, "summary": "Матч близкий."}
                            )
                        }
                    }
                ]
            },
        )
    )
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    settings = Settings(_env_file=None, telegram_bot_token="123:abc", ai_api_key="test")
    analyst = Analyst(settings, tmp_path / "analysis.json")
    assert await analyst.explain(match)
    payload = json.loads(post.call_args.kwargs["json"]["messages"][1]["content"])
    assert payload["hltv"]["teams"]["team1"]["maps"][0]["wins"] == 9
    assert "offers" not in payload
    assert facts(match)["hltv"]


def test_scheduler_finds_unranked_team_through_match_directory(tmp_path):
    cache = HltvCache(tmp_path / "hltv")
    rows = parse_schedule(fixture("matches"))
    write(cache.directory / "schedule.json", {"fetched_at": NOW, "matches": rows})
    state = {"discovery": {address: NOW for address in DISCOVERY}}
    job = next_job(cache, state, [], {"Joga de terno"}, NOW)
    assert job == rows[0]["url"]
    assert state["unresolved_teams"] == ["Joga de terno"]


async def test_collector_persists_pause_and_never_overwrites_good_data(tmp_path):
    folder = tmp_path / "hltv"
    loader = AsyncMock(side_effect=StopSource("HLTV HTTP 403"))
    await collect_hltv(tmp_path, asyncio.Semaphore(1), loader, now=NOW)
    state = read(folder / "state.json")
    assert state["paused"] and state["next_request"] > NOW
    await collect_hltv(tmp_path, asyncio.Semaphore(1), loader, now=NOW + 86400)
    assert loader.await_count == 1


async def test_retry_after_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("services.hltv_collection.time.time", lambda: NOW)
    error = httpx.HTTPStatusError(
        "limited", request=httpx.Request("GET", SPIRIT), response=httpx.Response(429, headers={"Retry-After": "7200"})
    )
    loader = AsyncMock(side_effect=error)
    await collect_hltv(tmp_path, asyncio.Semaphore(1), loader, now=NOW)
    state = read(tmp_path / "hltv" / "state.json")
    assert state["next_request"] >= NOW + 7200 and not state.get("paused")
    await collect_hltv(tmp_path, asyncio.Semaphore(1), loader, now=NOW + 10)
    assert loader.await_count == 1


async def test_successful_discovery_and_profile_collection_are_cached(tmp_path, monkeypatch):
    monkeypatch.setattr("services.hltv_collection.time.time", lambda: NOW)
    loader = AsyncMock(return_value=fixture("matches"))
    await collect_hltv(tmp_path, asyncio.Semaphore(1), loader, now=NOW)
    folder = tmp_path / "hltv"
    assert len(read(folder / "schedule.json")["matches"]) == 5
    assert read(folder / "state.json")["last_success"] == NOW
    cache = HltvCache(folder)
    cache.register([{"id": 7020, "name": "Spirit", "url": SPIRIT}])
    cache.wanted(["Team Spirit"])
    loader.return_value = fixture("team")
    monkeypatch.setattr("services.hltv_collection.time.time", lambda: NOW + 60)
    await collect_hltv(tmp_path, asyncio.Semaphore(1), loader, now=NOW + 60)
    assert cache.load(SPIRIT)["data"]["id"] == 7020
    assert list((folder / "pages").glob("*.html.gz"))
    assert read(folder / "state.json")["last_success"] == NOW + 60


def test_postponed_match_is_not_scheduled_or_live():
    html = fixture("match").replace("Match over", "Match postponed")
    assert parse_match(html, MATCH)["status"] == "unavailable"


def test_hltv_ranking_factor_requires_same_snapshot(cache, match):
    from services.factors import factors

    stats = enrich_statistics(None, cache.context(match, NOW))
    assert any(f["label"] == "Рейтинг HLTV" for f in factors(match | {"statistics": stats}))
    stats["hltv"]["teams"]["team2"]["rankings"]["hltv"]["url"] = (
        "https://www.hltv.org/ranking/teams/2026/october/5/details/11861"
    )
    assert not any(f["label"] == "Рейтинг HLTV" for f in factors(match | {"statistics": stats}))


def test_partial_live_score_in_upcoming_table_is_not_a_finished_series():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(fixture("aurora"), "html.parser")
    row = soup.select_one("#matchesBox .team-row")
    scores = row.select(".score")
    scores[0].string = "1"
    scores[1].string = "0"
    data = parse_team(str(soup), AURORA)
    assert len(data["results"]) == 16
    assert data["upcoming"][0]["scores"] is None
