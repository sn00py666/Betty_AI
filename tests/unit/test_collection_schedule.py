import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

import collect
from services.collection_schedule import FAST_INTERVAL, hot_sports, next_start_check
from services.freshness import LIVE_TTL, PREMATCH_TTL

NOW = datetime(2026, 10, 15, 10, tzinfo=UTC)


def snapshot(*matches):
    return {"fetched_at": NOW.isoformat(), "matches": list(matches)}


@pytest.mark.parametrize(
    "status,start_offset,age,expected",
    [
        ("prematch", 3600, 0, set()),
        ("prematch", 600, 0, {"cs2"}),
        ("prematch", -60, 0, {"cs2"}),
        ("prematch", -1801, 0, set()),
        ("prematch", 0, PREMATCH_TTL + 1, set()),
        ("live", None, LIVE_TTL - 1, {"cs2"}),
        ("live", None, LIVE_TTL + 1, set()),
    ],
)
def test_hot_window_and_stale_data(status, start_offset, age, expected):
    match = {
        "status": status,
        "sport": "cs2",
        "starts_at": (NOW + timedelta(seconds=start_offset)).isoformat() if start_offset is not None else None,
        "fetched_at": (NOW - timedelta(seconds=age)).isoformat(),
    }
    assert hot_sports([snapshot(match)], NOW.timestamp()) == expected


def test_wake_up_before_start_instead_of_waiting_full_interval():
    data = snapshot({"status": "prematch", "starts_at": (NOW + timedelta(minutes=20)).isoformat()})
    assert next_start_check([data], NOW.timestamp()) == NOW.timestamp() + 600


@pytest.mark.parametrize("failures", [0, 1])
async def test_fast_pass_preserves_other_sport_and_error_backoff(tmp_path, monkeypatch, failures):
    clock = NOW.timestamp()
    monkeypatch.setattr(collect.time, "time", lambda: clock)
    folder = tmp_path / "winline"
    folder.mkdir()
    old = (NOW - timedelta(minutes=5)).isoformat()
    untouched = {
        "id": "dota",
        "sport": "dota2",
        "status": "prematch",
        "fetched_at": old,
        "starts_at": (NOW + timedelta(hours=2)).isoformat(),
        "expires_at": "old-expiry",
    }
    collect.save_json(
        folder / "latest.json",
        snapshot(
            {"id": "cs", "sport": "cs2", "status": "prematch", "starts_at": NOW.isoformat()},
            untouched,
        ),
    )
    collect.save_json(
        folder / "state.json",
        {
            "robots": "User-agent: *\nAllow: /",
            "robots_checked": clock,
            "last_success": clock - 120,
            "last_full_success": clock - 120,
            "next_request": clock + 1000,
            "failures": failures,
        },
    )
    browser = AsyncMock(return_value=(httpx.Response(200, request=httpx.Request("GET", "https://winline.ru")), "page"))
    monkeypatch.setattr(collect, "load_winline", browser)
    monkeypatch.setattr(
        collect,
        "parse_response",
        lambda *args: [
            {
                "id": "cs",
                "sport": "cs2",
                "team1": "A",
                "team2": "B",
                "status": "live",
                "fetched_at": NOW.isoformat(),
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        await collect.collect_one("winline", client, tmp_path)
    if failures:
        browser.assert_not_awaited()
        assert collect.read_json(folder / "state.json", {})["next_request"] == clock + 1000
        return
    assert browser.call_args.kwargs == {"sports": {"cs2"}}
    data = collect.read_json(folder / "latest.json", {})
    assert data["matches"][1] == untouched
    assert datetime.fromisoformat(data["matches"][0]["expires_at"]).timestamp() == clock + LIVE_TTL
    state = collect.read_json(folder / "state.json", {})
    assert state["next_request"] == clock + FAST_INTERVAL
    assert state["last_full_success"] == clock - 120
    assert state["refresh_mode"] == "fast"
    # Перезапуск до срока не создаёт повторный запрос.
    async with httpx.AsyncClient() as client:
        await collect.collect_one("winline", client, tmp_path)
    assert browser.await_count == 1


async def test_slow_browser_does_not_block_http_sources(monkeypatch):
    browser_started = asyncio.Event()
    http_finished = asyncio.Event()

    async def collect_one(name, client):
        if name == "winline":
            browser_started.set()
            await asyncio.wait_for(http_finished.wait(), 1)
        else:
            await asyncio.wait_for(browser_started.wait(), 1)
            http_finished.set()

    monkeypatch.setattr(collect, "collect_one", collect_one)
    await collect.collect_loop(["winline", "fonbet"], False)
    assert http_finished.is_set()


@pytest.mark.parametrize("crawl_delay,last_attempt,wait", [(0, 0, False), (120, 0, True), (0, 1, True)])
async def test_full_refresh_crawl_delay_and_crash_reservation(tmp_path, monkeypatch, crawl_delay, last_attempt, wait):
    clock = NOW.timestamp()
    monkeypatch.setattr(collect.time, "time", lambda: clock)
    folder = tmp_path / "winline"
    folder.mkdir()
    collect.save_json(folder / "latest.json", snapshot({"sport": "cs2", "status": "live"}))
    collect.save_json(
        folder / "state.json",
        {
            "robots": f"User-agent: *\nAllow: /\nCrawl-delay: {crawl_delay}",
            "robots_checked": clock,
            "last_success": clock - 60,
            "last_full_success": clock - PREMATCH_TTL - 1,
            "last_attempt": clock if last_attempt else clock - 60,
            "next_request": clock + 30,
        },
    )
    browser = AsyncMock(return_value=(httpx.Response(200, request=httpx.Request("GET", "https://winline.ru")), "page"))
    monkeypatch.setattr(collect, "load_winline", browser)
    monkeypatch.setattr(
        collect,
        "parse_response",
        lambda *args: [
            {
                "id": "cs",
                "sport": "cs2",
                "team1": "A",
                "team2": "B",
                "status": "prematch",
                "starts_at": (NOW + timedelta(hours=2)).isoformat(),
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        await collect.collect_one("winline", client, tmp_path)
    if wait:
        browser.assert_not_awaited()
    else:
        browser.assert_awaited_once()
        assert browser.call_args.kwargs == {}  # Полный сбор, даже если ранее был LIVE.
        state = collect.read_json(folder / "state.json", {})
        assert state["last_full_success"] == clock
        assert state["refresh_mode"] == "prematch"
        assert state["next_request"] == clock + PREMATCH_TTL
