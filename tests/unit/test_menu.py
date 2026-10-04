from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Message, Update

from handlers import menu
from services.users import Users


@pytest.fixture(scope="module")
def dispatcher():
    instance = Dispatcher()
    instance.include_router(menu.router)
    return instance


@pytest.fixture
def users(tmp_path):
    return Users(tmp_path / "users.json")


@pytest.fixture
async def bot(monkeypatch):
    async with Bot(token="123456:TEST_TOKEN", default=DefaultBotProperties(parse_mode="HTML")) as instance:

        async def reply(bot, method, **kwargs):
            if isinstance(method, SendMessage | EditMessageText):
                return Message(message_id=10, date=1, chat={"id": method.chat_id, "type": "private"}).as_(bot)
            return True

        instance.session.make_request = AsyncMock(side_effect=reply)
        monkeypatch.setattr(menu.asyncio, "sleep", AsyncMock())
        yield instance


def incoming(*, text=None, callback=None, chat_type="private"):
    user = {"id": 42, "is_bot": False, "first_name": "Test"}
    message = {
        "message_id": 1,
        "date": 1,
        "chat": {"id": 42, "type": chat_type},
        "from": user,
        "text": text,
    }
    if callback is None:
        return Update.model_validate({"update_id": 1, "message": message})
    message["from"] = {"id": 123456, "is_bot": True, "first_name": "Betty"}
    return Update.model_validate(
        {
            "update_id": 2,
            "callback_query": {
                "id": "button-1",
                "from": user,
                "chat_instance": "chat",
                "message": message,
                "data": callback,
            },
        }
    )


def requests(bot, method_type):
    return [call.args[1] for call in bot.session.make_request.call_args_list if isinstance(call.args[1], method_type)]


@pytest.fixture
def matches(tmp_path):
    from datetime import UTC, datetime, timedelta

    from test_matches import snapshot

    from services.matches import Matches

    now = datetime.now(UTC)
    data = snapshot(tmp_path, "pari", starts_at=(now + timedelta(hours=2)).isoformat())
    data["fetched_at"] = now.isoformat()
    data["expires_at"] = (now + timedelta(minutes=30)).isoformat()
    import json

    (tmp_path / "pari" / "latest.json").write_text(json.dumps(data))
    return Matches(tmp_path)


@pytest.fixture
def analyst():
    return AsyncMock(explain=AsyncMock(return_value=None))


async def test_first_and_repeated_start_survive_restart(dispatcher, bot, users):
    await dispatcher.feed_update(bot, incoming(text="/start"), users=users)
    first = requests(bot, SendMessage)[0]
    assert "привет" in first.text and "Демо" not in first.text
    assert [row[0].text for row in first.reply_markup.inline_keyboard] == ["CS-2", "Dota 2"]
    await dispatcher.feed_update(bot, incoming(text="/start"), users=Users(users.path))
    assert requests(bot, SendMessage)[-1].text == "<b>Выбери дисциплину:</b>"


async def test_selection_persists_real_matches(dispatcher, bot, users, matches):
    await dispatcher.feed_update(bot, incoming(callback="sport:cs2"), users=users, matches=matches)
    assert Users(users.path).get(42)["sport"] == "cs2"
    assert not users.get(123456)
    buttons = requests(bot, SendMessage)[-1].reply_markup.inline_keyboard
    assert buttons[0][0].callback_data == "match:" + matches.read()[0]["id"]
    assert "NAVI" in buttons[0][0].text


async def test_dota_empty_state(dispatcher, bot, users, matches):
    await dispatcher.feed_update(bot, incoming(callback="sport:dota2"), users=users, matches=matches)
    assert "Свежих матчей" in requests(bot, SendMessage)[-1].text


async def test_real_analysis_then_actual_bookmaker_link(dispatcher, bot, users, matches, analyst):
    await dispatcher.feed_update(
        bot, incoming(callback="match:" + matches.read()[0]["id"]), users=users, matches=matches, analyst=analyst
    )
    assert "Вероятный исход по текущей линии" in requests(bot, EditMessageText)[0].text
    assert "80 000" not in requests(bot, EditMessageText)[0].text
    menu.asyncio.sleep.assert_awaited_once_with(3)
    offers = requests(bot, SendMessage)[-1]
    assert offers.reply_markup.inline_keyboard[0][0].url == "https://pari.ru/sports/10"


@pytest.mark.parametrize("callback", ["match:missing", "sport:missing", "offer:0"])
async def test_stale_buttons(dispatcher, bot, users, matches, analyst, callback):
    await dispatcher.feed_update(bot, incoming(callback=callback), users=users, matches=matches, analyst=analyst)
    assert requests(bot, AnswerCallbackQuery)[0].show_alert
    analyst.explain.assert_not_awaited()


async def test_info_and_askbetty(dispatcher, bot, users, matches):
    users.update(42, sport="cs2")
    await dispatcher.feed_update(bot, incoming(text="/info"), users=users)
    assert "Как это работает" in requests(bot, SendMessage)[-1].text
    await dispatcher.feed_update(bot, incoming(text="/askbetty"), users=users, matches=matches)
    assert "Ближайшие матчи" in requests(bot, SendMessage)[-1].text


async def test_disable_notifications_clears_reminders(dispatcher, bot, users):
    users.update(42, notifications=True, reminders={"a": {}})
    await dispatcher.feed_update(bot, incoming(callback="notifications:off"), users=users)
    assert not users.get(42)["notifications"] and not users.get(42)["reminders"]


async def test_menu_does_not_respond_in_groups(dispatcher, bot, users):
    await dispatcher.feed_update(bot, incoming(text="/start", chat_type="group"), users=users)
    bot.session.make_request.assert_not_awaited()


async def test_pagination_has_five_matches_and_edits_same_message(dispatcher, bot, users, matches, monkeypatch):
    users.update(42, sport="cs2")
    base = matches.read()[0]
    items = [dict(base, id=str(i)) for i in range(11)]
    monkeypatch.setattr(matches, "read", lambda: items)
    await dispatcher.feed_update(bot, incoming(callback="page:cs2:1"), users=users, matches=matches)
    assert not requests(bot, SendMessage)
    message = requests(bot, EditMessageText)[0]
    rows = message.reply_markup.inline_keyboard
    assert [r[0].callback_data for r in rows[:5]] == [f"match:{i}" for i in range(5, 10)]
    assert [b.callback_data for b in rows[5]] == ["page:cs2:0", "page:cs2:2"]
    assert "2/3" in message.text
    await dispatcher.feed_update(bot, incoming(callback="page:cs2:2"), users=users, matches=matches)
    rows = requests(bot, EditMessageText)[-1].reply_markup.inline_keyboard
    assert rows[0][0].callback_data == "match:10"
    assert [b.callback_data for b in rows[1]] == ["page:cs2:1"]
