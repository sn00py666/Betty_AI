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
        offers=[dict(bookmaker="marathon", p1=2, p2=2)],
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
    matches = Mock(directory=tmp_path / "odds", read=Mock(return_value=[event(now)]))
    with pytest.raises(asyncio.CancelledError):
        await notifications.run(bot, users, matches)
    with pytest.raises(asyncio.CancelledError):
        await notifications.run(bot, Users(users.path), matches)
    bot.send_message.assert_awaited_once()


async def test_daily_push_sorts_before_top_five_for_both_sports(tmp_path, monkeypatch):
    import asyncio
    import time
    from unittest.mock import AsyncMock, Mock

    import pytest

    from services import notifications
    from services.health import write
    from services.users import Users

    now = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(notifications, "datetime", Mock(now=Mock(return_value=now)))

    # Finish after all subscribers have received their messages, not after the first one.
    async def sleep(seconds):
        if seconds == 30:
            raise asyncio.CancelledError

    monkeypatch.setattr(notifications.asyncio, "sleep", sleep)
    ranks = {f"club{i}": dict(name=f"Club {i}", rank=i, last_match_time=time.time() - 10) for i in range(1, 8)}
    write(tmp_path / "valve.json", dict(snapshots={"today": dict(date=now.date().isoformat(), teams=ranks)}))
    write(tmp_path / "dota_rankings" / "opendota.json", dict(last_success=time.time() - 1, teams=ranks))
    items = []
    for sport in ("cs2", "dota2"):
        for i in range(7, 0, -1):
            items.append(
                event(now)
                | dict(
                    id=f"{sport}-{i}",
                    sport=sport,
                    team1=f"Club {i}",
                    starts_at=(now + timedelta(hours=8 - i)).isoformat(),
                )
            )
        # A top-ranked live game and tomorrow's game must not occupy today's five slots.
        items.extend(
            [
                items[-1] | dict(id=f"{sport}-live", status="live"),
                items[-1] | dict(id=f"{sport}-tomorrow", starts_at=(now + timedelta(days=1)).isoformat()),
            ]
        )
    users = Users(tmp_path / "users.json")
    for uid, sport in ((1, "cs2"), (2, "dota2")):
        users.update(uid, notifications=True, sport=sport, match_sort="time")
    bot = Mock(send_message=AsyncMock())
    matches = Mock(directory=tmp_path / "odds", read=Mock(return_value=items))
    with pytest.raises(asyncio.CancelledError):
        await notifications.run(bot, users, matches)
    assert bot.send_message.await_count == 2
    for call, sport in zip(bot.send_message.call_args_list, ("cs2", "dota2"), strict=True):
        markup = call.kwargs["reply_markup"]
        assert [row[0].callback_data for row in markup.inline_keyboard] == [f"match:{sport}-{i}" for i in range(1, 6)]
    assert items[0]["id"] == "cs2-7"


def test_evening_push_still_selects_market_favorite():
    now = datetime(2026, 10, 22, 15, tzinfo=UTC)
    balanced = event(now)
    favorite = event(now) | dict(id="favorite", offers=[dict(bookmaker="marathon", p1=1.1, p2=8)])
    message = list(scheduled(dict(notifications=True, sport="cs2"), [balanced, favorite], now))[0]
    assert message[2].inline_keyboard[0][0].callback_data == "match:favorite"
    assert len(message[2].inline_keyboard) == 1
