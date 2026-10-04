from datetime import UTC, datetime, timedelta

from services.notifications import scheduled


def event(now):
    return dict(
        id="a",
        sport="cs2",
        status="prematch",
        team1="A",
        team2="B",
        starts_at=(now + timedelta(minutes=20)).isoformat(),
        offers=[dict(p1=2, p2=2)],
    )


def test_notifications_require_opt_in_and_reminder_requires_fresh_event():
    now = datetime(2026, 10, 4, 9, tzinfo=UTC)
    match = event(now)
    assert list(scheduled({}, [match], now)) == []
    user = dict(reminders={"a": dict(starts_at=match["starts_at"])})
    assert list(scheduled(user, [], now)) == []
    assert list(scheduled(user, [match], now))[0][0] == "remind:a"


def test_daily_is_dated_and_does_not_replay_after_restart():
    now = datetime(2026, 10, 4, 9, tzinfo=UTC)
    user = dict(notifications=True, sport="cs2")
    message = list(scheduled(user, [event(now)], now))[0]
    assert message[0] == "2026-10-04:12"
    assert list(scheduled(user, [event(now)], now + timedelta(minutes=11))) == []


async def test_delivery_is_not_repeated_after_restart(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, Mock

    import pytest

    from services import notifications
    from services.users import Users

    now = datetime(2026, 10, 4, 9, tzinfo=UTC)
    monkeypatch.setattr(notifications, "datetime", Mock(now=Mock(return_value=now)))
    monkeypatch.setattr(notifications.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    users = Users(tmp_path / "users.json")
    users.update(42, notifications=True, sport="cs2")
    bot = Mock(send_message=AsyncMock())
    matches = Mock(read=Mock(return_value=[event(now)]))
    with pytest.raises(asyncio.CancelledError):
        await notifications.run(bot, users, matches)
    with pytest.raises(asyncio.CancelledError):
        await notifications.run(bot, Users(users.path), matches)
    bot.send_message.assert_awaited_once()
