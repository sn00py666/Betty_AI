import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import collect
from config import RankingSettings
from handlers.menu import show_matches
from services.discovery import sort_matches
from services.dota_rankings import (
    ACTIVE_AGE,
    INTERVAL,
    MAX_AGE,
    ROBOTS,
    SOURCES,
    URLS,
    DotaRankings,
    enabled_sources,
    load_source,
    parse_cyberscore,
    parse_opendota,
    select_ranking,
    source_path,
)
from services.health import read, write
from services.monitoring import Monitor, report_parts

FIXTURES = Path(__file__).parents[1] / "fixtures" / "dota_rankings"
CAPTURED = datetime(2026, 10, 14, tzinfo=UTC).timestamp()


def opendota_rows(now):
    return [dict(name=f"Team {i}", rating=2000 - i, last_match_time=now - 10, team_id=i) for i in range(1, 21)]


def cyber_html(now):
    return (
        (FIXTURES / "cyberscore.html")
        .read_text()
        .replace("13.10.2026", datetime.fromtimestamp(now, UTC).strftime("%d.%m.%Y"))
    )


def cache(directory, source, now, **extra):
    snapshot = (
        parse_opendota(opendota_rows(now), now) if source == "opendota" else parse_cyberscore(cyber_html(now), now)
    )
    state = dict(snapshot, last_success=now, next_request=now + INTERVAL, error=None, http_status=200) | extra
    write(source_path(directory, source), state)
    return state


def test_recorded_responses_and_active_team_filter():
    rows = json.loads((FIXTURES / "opendota.json").read_text())
    result = parse_opendota(rows, CAPTURED)
    assert result["teams"]["yandex"]["rank"] == 1
    cyber = parse_cyberscore((FIXTURES / "cyberscore.html").read_text(), CAPTURED)
    assert len(cyber["teams"]) == 20
    assert cyber["teams"]["parivision"]["rank"] == 1
    assert cyber["teams"]["1win"]["rank"] == 11  # Значок изменения места не часть ранга.
    assert cyber["published"] == "2026-10-13"
    rows = opendota_rows(CAPTURED) + [
        dict(name="Old", rating=9999, last_match_time=CAPTURED - ACTIVE_AGE - 1),
        dict(name="Future", rating=9999, last_match_time=CAPTURED + 1),
        dict(name="NaN", rating=float("nan"), last_match_time=CAPTURED),
        dict(name="Empty", rating=0, last_match_time=CAPTURED),
        dict(name="Team 1", rating=999, last_match_time=CAPTURED - 1),
        None,
    ]
    teams = parse_opendota(rows, CAPTURED)["teams"]
    assert len(teams) == 19
    assert not {"old", "future", "nan", "empty", "team1"} & teams.keys()


@pytest.mark.parametrize(
    "replace",
    [
        ("13.10.2026", "01.01.2020"),
        ("13.10.2026", "15.10.2026"),
        ("sub-item--rank", "changed"),
        ("Dota 2 team ranking", "Other page"),
    ],
)
def test_cyberscore_rejects_stale_future_or_changed_page(replace):
    with pytest.raises(ValueError):
        parse_cyberscore((FIXTURES / "cyberscore.html").read_text().replace(*replace), CAPTURED)


def test_failover_recovery_cache_expiry_and_disabled_sources(tmp_path):
    now = time.time()
    primary = cache(tmp_path, "opendota", now)
    cache(tmp_path, "cyberscore", now)
    assert select_ranking(tmp_path, now=now)[0] == "opendota"
    write(source_path(tmp_path, "opendota"), primary | dict(error="HTTP 403", http_status=403))
    assert select_ranking(tmp_path, now=now)[0] == "cyberscore"
    cache(tmp_path, "cyberscore", now, error="HTTP 429", http_status=429)
    assert select_ranking(tmp_path, now=now)[::2] == ("opendota", True)
    write(source_path(tmp_path, "opendota"), primary)
    assert select_ranking(tmp_path, now=now)[::2] == ("opendota", False)
    assert select_ranking(tmp_path, now=now + MAX_AGE + 1) == (None, {}, False)
    assert select_ranking(tmp_path, sources=(), now=now) == (None, {}, False)
    assert select_ranking(tmp_path, sources=("cyberscore",), now=now)[0] == "cyberscore"
    assert select_ranking(tmp_path, now=now - 1) == (None, {}, False)


@pytest.mark.parametrize(
    "value",
    [
        "{broken",
        "[]",
        '{"teams": []}',
        '{"last_success": "wrong"}',
        '{"next_request": NaN}',
        '{"failures": "wrong"}',
        '{"teams": {"x": {"name": "X", "rank": -1}}}',
    ],
)
def test_corrupt_cache_never_breaks_sorting(tmp_path, value):
    path = source_path(tmp_path, "opendota")
    path.parent.mkdir(parents=True)
    path.write_text(value)
    assert load_source(tmp_path, "opendota")["invalid"]
    assert select_ranking(tmp_path) == (None, {}, False)


def test_dota_sorting_uses_one_provider_live_first_and_never_cs2_rating(tmp_path):
    now = time.time()
    cache(tmp_path, "cyberscore", now)
    write(
        tmp_path / "valve.json",
        dict(snapshots={"today": dict(date=datetime.now(UTC).date().isoformat(), teams={"unknown": {"rank": 1}})}),
    )

    def match(identifier, team, status="prematch", hours=0):
        return dict(
            id=identifier,
            sport="dota2",
            team1=team,
            team2="Other",
            status=status,
            starts_at=datetime.fromtimestamp(now + hours * 3600, UTC).isoformat(),
            offers=[],
        )

    matches = [
        match("unknown", "Unknown"),
        match("ranked", "Team Spirit", hours=1),
        match("live", "Unknown", status="live"),
    ]
    assert [m["id"] for m in sort_matches(matches, "popular", tmp_path / "valve.json")] == ["live", "ranked", "unknown"]
    assert [m["id"] for m in sort_matches(matches, "time", tmp_path / "valve.json")] == ["live", "unknown", "ranked"]
    assert [m["id"] for m in sort_matches(matches, "popular", tmp_path / "valve.json", dota_ranks={})] == [
        "live",
        "unknown",
        "ranked",
    ]


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    monkeypatch.setattr("services.dota_rankings.asyncio.sleep", AsyncMock())


@pytest.mark.parametrize("source", SOURCES)
async def test_refresh_success_is_persisted_and_not_repeated_after_restart(tmp_path, monkeypatch, source):
    now = time.time()
    calls = []

    def respond(request):
        calls.append(str(request.url))
        assert request.headers["user-agent"] == "BettyAI/0.1"
        if str(request.url) == ROBOTS:
            return httpx.Response(200, text="User-agent: *\nAllow: /en/teams/\nCrawl-delay: 10")
        return (
            httpx.Response(200, json=opendota_rows(now))
            if source == "opendota"
            else httpx.Response(200, text=cyber_html(now))
        )

    mock_http(monkeypatch, respond)
    await DotaRankings(tmp_path, source).refresh()
    await DotaRankings(tmp_path, source).refresh()
    assert calls == ([ROBOTS] if source == "cyberscore" else []) + [URLS[source]]
    state = read(source_path(tmp_path, source))
    assert state["last_success"] >= now and state["error"] is None
    assert len(state["teams"]) == 20


@pytest.mark.parametrize("code", [403, 429, 500])
async def test_errors_keep_cache_and_honor_retry_after(tmp_path, monkeypatch, code):
    now = time.time()
    old = cache(tmp_path, "opendota", now - 100, next_request=0)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(code, headers={"Retry-After": "172800"})

    mock_http(monkeypatch, respond)
    await DotaRankings(tmp_path, "opendota").refresh()
    await DotaRankings(tmp_path, "opendota").refresh()
    state = read(source_path(tmp_path, "opendota"))
    assert state["teams"] == old["teams"] and state["last_success"] == old["last_success"]
    assert state["http_status"] == code and state["next_request"] >= now + 172800
    assert len(calls) == 1


async def test_robots_denial_never_fetches_ranking(tmp_path, monkeypatch):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="User-agent: *\nDisallow: /en/teams/")

    mock_http(monkeypatch, respond)
    await DotaRankings(tmp_path, "cyberscore").refresh()
    assert calls == [ROBOTS]
    assert "robots.txt" in read(source_path(tmp_path, "cyberscore"))["error"]


@pytest.mark.parametrize("kind", ["empty", "timeout"])
async def test_empty_and_timeout_preserve_good_snapshot(tmp_path, monkeypatch, kind):
    old = cache(tmp_path, "opendota", time.time(), next_request=0)

    def respond(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("Timed out", request=request)
        return httpx.Response(200, json=[])

    mock_http(monkeypatch, respond)
    await DotaRankings(tmp_path, "opendota").refresh()
    state = read(source_path(tmp_path, "opendota"))
    assert state["teams"] == old["teams"] and state["error"]


def monitor_settings(**changes):
    return SimpleNamespace(
        admin_user_ids=[1],
        pandascore_api_key=None,
        ai_api_key=None,
        pandascore_daily_limit=100,
        ai_daily_limit=500,
        valve_rankings_enabled=False,
        dota_opendota_enabled=True,
        dota_cyberscore_enabled=True,
        **changes,
    )


def test_monitor_shows_both_sources_active_fallback_stale_and_disabled(tmp_path):
    now = time.time()
    cache(tmp_path, "opendota", now, error="HTTP 403", http_status=403)
    cache(tmp_path, "cyberscore", now)
    settings = monitor_settings()
    monitor = Monitor(settings, tmp_path)
    items = {row["name"]: row for row in monitor.snapshot(now=now)}
    assert items["OpenDota · Dota 2"]["code"] == "blocked"
    assert items["CyberScore · Dota 2"]["code"] == "ok"
    assert "CyberScore (резервный)" in items["Рейтинг Dota 2"]["detail"]
    items = {row["name"]: row for row in monitor.snapshot(now=now + MAX_AGE + 1)}
    assert items["CyberScore · Dota 2"]["code"] == items["Рейтинг Dota 2"]["code"] == "stale"
    settings.dota_opendota_enabled = settings.dota_cyberscore_enabled = False
    items = {row["name"]: row for row in monitor.snapshot(now=now)}
    assert all(
        items[name]["code"] == "disabled" for name in ("OpenDota · Dota 2", "CyberScore · Dota 2", "Рейтинг Dota 2")
    )


async def test_monitor_alerts_once_per_failure_and_recovery_across_restart(tmp_path):
    from test_monitoring import healthy

    healthy(tmp_path)
    now = time.time()
    cache(tmp_path, "opendota", now)
    cache(tmp_path, "cyberscore", now)
    settings = monitor_settings()
    bot = SimpleNamespace(send_message=AsyncMock())
    await Monitor(settings, tmp_path).check(bot)
    assert bot.send_message.await_count == 0
    cache(tmp_path, "opendota", now, error="HTTP 429", http_status=429)
    await Monitor(settings, tmp_path).check(bot)
    await Monitor(settings, tmp_path).check(bot)
    assert bot.send_message.await_count == 1
    assert "OpenDota" in bot.send_message.call_args.args[1] and "лимит запросов" in bot.send_message.call_args.args[1]
    cache(tmp_path, "opendota", now)
    await Monitor(settings, tmp_path).check(bot)
    await Monitor(settings, tmp_path).check(bot)
    assert bot.send_message.await_count == 2
    assert "✅" in bot.send_message.call_args.args[1]


def test_monitor_missing_source_alerts_after_startup_grace_and_report_is_split(tmp_path):
    monitor = Monitor(monitor_settings(), tmp_path)
    monitor.started -= 121
    items = {row["name"]: row for row in monitor.snapshot()}
    assert items["OpenDota · Dota 2"]["code"] == "error"
    assert items["CyberScore · Dota 2"]["code"] == "error"
    text = "\n".join(["<b>Source</b>\n" + "&amp;🔴" * 90] * 20)
    parts = report_parts(text)
    assert len(parts) > 1 and "\n".join(parts) == text
    assert all(len(part.encode("utf-16-le")) // 2 <= 3500 for part in parts)


def test_first_request_in_progress_has_grace_even_if_monitor_already_running(tmp_path):
    now = time.time()
    for source in SOURCES:
        write(source_path(tmp_path, source), dict(last_attempt=now, next_request=now + INTERVAL))
    monitor = Monitor(monitor_settings(), tmp_path)
    monitor.started -= 500
    names = ("OpenDota · Dota 2", "CyberScore · Dota 2", "Рейтинг Dota 2")
    items = {row["name"]: row for row in monitor.snapshot(now=now)}
    assert all(items[name]["code"] == "idle" for name in names)
    items = {row["name"]: row for row in monitor.snapshot(now=now + 121)}
    assert all(items[name]["code"] == "stale" for name in names)


async def test_collector_starts_independent_enabled_ranking_workers(tmp_path, monkeypatch):
    started, ready = [], asyncio.Event()

    async def worker(*args):
        started.append(args)
        if len(started) == 4:
            ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(collect, "DATA", tmp_path / "odds")
    monkeypatch.setattr(
        collect,
        "RankingSettings",
        lambda: SimpleNamespace(valve_rankings_enabled=False, dota_opendota_enabled=True, dota_cyberscore_enabled=True),
    )
    monkeypatch.setattr(collect, "heartbeat", lambda *args, **kwargs: worker("heartbeat"))
    monkeypatch.setattr(collect, "collect_loop", worker)
    monkeypatch.setattr(collect, "run_dota_rankings", worker)
    task = asyncio.create_task(collect.run([], True))
    try:
        await asyncio.wait_for(ready.wait(), 1)
        assert (tmp_path, "opendota") in started and (tmp_path, "cyberscore") in started
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_ranking_settings_do_not_require_bot_token(monkeypatch):
    monkeypatch.setenv("DOTA_OPENDOTA_ENABLED", "false")
    settings = RankingSettings(_env_file=None)
    assert enabled_sources(settings) == ("cyberscore",)


@pytest.mark.parametrize("enabled", [True, False])
async def test_match_list_names_selected_source_and_respects_disabled_setting(tmp_path, enabled):
    cache(tmp_path, "cyberscore", time.time())
    users = SimpleNamespace(get=lambda _: dict(sport="dota2", match_sort="popular"))
    match = dict(
        id="one",
        sport="dota2",
        team1="Team Spirit",
        team2="PARIVISION",
        status="prematch",
        starts_at=datetime.now(UTC).isoformat(),
        offers=[],
    )
    matches = SimpleNamespace(directory=tmp_path / "odds", read=lambda: [match])
    message = SimpleNamespace(answer=AsyncMock())
    settings = SimpleNamespace(dota_opendota_enabled=False, dota_cyberscore_enabled=enabled)
    await show_matches(message, 1, users, matches, settings=settings)
    text = message.answer.call_args.args[0]
    assert ("CyberScore" in text) is enabled
    assert ("Рейтинг временно недоступен" in text) is not enabled
    assert "Valve" not in text
