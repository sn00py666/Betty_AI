import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from services.health import api_state, read, safe_error, write
from services.matches import BOOKMAKERS
from services.monitoring import Monitor


@pytest.fixture
def settings():
    return SimpleNamespace(
        admin_user_ids=[850362750],
        pandascore_api_key="test",
        ai_api_key="test",
        pandascore_daily_limit=100,
        valve_rankings_enabled=True,
    )


def healthy(directory):
    now = time.time()
    for name in ("bot", "collector"):
        write(directory / "health" / f"{name}.json", dict(updated_at=now, started_at=now))
    for name in BOOKMAKERS:
        write(directory / "odds" / name / "state.json", dict(next_request=now + 1800))
        write(directory / "odds" / name / "latest.json", dict(fetched_at=datetime.now(UTC).isoformat(), matches=[]))
    for name in ("pandascore", "ai", "valve"):
        api_state(directory / "health" / f"{name}.json", status=200)
    today = datetime.now(UTC).date().isoformat()
    write(directory / "valve.json", dict(snapshots={"new": dict(date=today)}))


def states(monitor):
    return {item["name"]: item for item in monitor.snapshot()}


def test_report_all_sources_no_network_or_credentials(tmp_path, settings):
    healthy(tmp_path)
    monitor = Monitor(settings, tmp_path)
    state_path = tmp_path / "odds" / "betboom" / "state.json"
    write(state_path, dict(paused=True, error="HTTP 401: https://example.test/?token=secret"))
    items = states(monitor)
    assert items["BetBoom"]["code"] == "blocked"
    assert "secret" not in items["BetBoom"]["detail"]
    assert all(items[name]["code"] == "ok" for name in ("Telegram-бот", "Сборщик", "PandaScore", "Valve VRS"))
    assert set(BOOKMAKERS.values()) <= items.keys()
    assert len(monitor.report()) < 4096
    assert "доступ ограничен" in monitor.report()
    assert "123456:ABCdef1234567890_ABCdef1234" not in safe_error("123456:ABCdef1234567890_ABCdef1234")


def test_stale_offline_budget_and_corrupt_state_are_visible(tmp_path, settings):
    healthy(tmp_path)
    monitor = Monitor(settings, tmp_path)
    write(tmp_path / "health" / "bot.json", dict(updated_at=time.time() - 200))
    write(tmp_path / "odds" / "pari" / "latest.json", dict(fetched_at="2020-01-01T00:00:00Z", matches=[]))
    (tmp_path / "odds" / "leon" / "state.json").write_text("{invalid")
    write(tmp_path / "health" / "collector.json", dict(updated_at="invalid"))
    write(tmp_path / "pandascore.json", dict(budget=dict(day=datetime.now(UTC).date().isoformat(), used=100)))
    api_state(tmp_path / "health" / "ai.json", error="HTTP 429", status=429, retry_at=time.time() + 3600)
    write(tmp_path / "valve.json", dict(snapshots={"old": dict(date="2020-01-01")}))
    items = states(monitor)
    assert items["Telegram-бот"]["code"] == "offline"
    assert items["Сборщик"]["code"] == "error"
    assert items[BOOKMAKERS["pari"]]["code"] == "stale"
    assert items[BOOKMAKERS["leon"]]["code"] == "error"
    assert items["PandaScore"]["code"] == "limited"
    assert items["OpenRouter AI"]["code"] == "limited"
    assert items["Valve VRS"]["code"] == "stale"


def test_ai_usage_has_no_daily_quota(tmp_path, settings):
    healthy(tmp_path)
    write(tmp_path / "ai_usage.json", {"0": {"date": datetime.now(UTC).date().isoformat(), "count": 1000000}})
    item = states(Monitor(settings, tmp_path))["OpenRouter AI"]
    assert item["code"] == "ok"
    assert "1000000" in item["detail"]
    assert "без дневного лимита" in item["detail"]
    assert "исчерпан" not in item["detail"]


async def test_problem_once_after_restart_and_recovery_once(tmp_path, settings):
    healthy(tmp_path)
    bot = SimpleNamespace(send_message=AsyncMock())
    path = tmp_path / "odds" / "betboom" / "state.json"
    write(path, dict(paused=True, error="HTTP 403"))
    await Monitor(settings, tmp_path).check(bot)
    await Monitor(settings, tmp_path).check(bot)
    assert bot.send_message.await_count == 1
    assert bot.send_message.call_args.args[0] == 850362750
    write(path, dict(error=None))
    await Monitor(settings, tmp_path).check(bot)
    await Monitor(settings, tmp_path).check(bot)
    assert bot.send_message.await_count == 2
    assert "✅" in bot.send_message.call_args.args[1]


async def test_delivery_failure_keeps_pending_alert(tmp_path, settings):
    healthy(tmp_path)
    write(tmp_path / "odds" / "betboom" / "state.json", dict(error="HTTP 403"))
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=TelegramForbiddenError(method=SendMessage(chat_id=850362750, text="test"), message="Forbidden")
        )
    )
    await Monitor(settings, tmp_path).check(bot)
    await Monitor(settings, tmp_path).check(bot)
    assert bot.send_message.await_count == 1
    saved = read(tmp_path / "monitor.json")
    assert "states" not in saved["850362750"]
    saved["850362750"]["retry_at"] = 0
    write(tmp_path / "monitor.json", saved)
    bot.send_message.side_effect = None
    await Monitor(settings, tmp_path).check(bot)
    assert bot.send_message.await_count == 2


def test_startup_grace_and_disabled_sources(tmp_path, settings):
    settings.valve_rankings_enabled = False
    monitor = Monitor(settings, tmp_path)
    assert states(monitor)["Сборщик"]["code"] == "idle"
    assert states(monitor)["Valve VRS"]["code"] == "disabled"
    monitor.started -= 121
    assert states(monitor)["Сборщик"]["code"] == "offline"
