from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto, SetMessageReaction
from aiogram.types import Message, Update

from config import PROJECT_ROOT
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
            if isinstance(method, SendMessage | SendPhoto):
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


async def test_first_and_repeated_start_survive_restart(dispatcher, bot, users):
    await dispatcher.feed_update(bot, incoming(text="/start"), users=users)
    first = requests(bot, SendMessage)[0]
    assert "привет" in first.text and "Демо" in first.text
    assert [row[0].text for row in first.reply_markup.inline_keyboard] == ["CS-2", "Dota 2"]
    await dispatcher.feed_update(bot, incoming(text="/start"), users=Users(users.path))
    assert requests(bot, SendMessage)[-1].text == "<b>Выбери дисциплину:</b>"


async def test_selection_persists_and_back_works(dispatcher, bot, users):
    await dispatcher.feed_update(bot, incoming(callback="sport:cs2"), users=users)
    assert Users(users.path).get(42)["sport"] == "cs2"
    assert not users.get(123456)  # Сохраняем пользователя, а не автора сообщения с кнопкой.
    buttons = requests(bot, SendMessage)[-1].reply_markup.inline_keyboard
    assert len(buttons) == 5
    assert buttons[0][0].callback_data == "match:navi-faze"
    await dispatcher.feed_update(bot, incoming(callback="disciplines"), users=users)
    assert requests(bot, SendMessage)[-1].text == "<b>Выбери дисциплину:</b>"


async def test_dota_has_honest_empty_state(dispatcher, bot, users):
    await dispatcher.feed_update(bot, incoming(callback="sport:dota2"), users=users)
    message = requests(bot, SendMessage)[-1]
    assert "Матчей Dota 2 в демо пока нет" in message.text
    assert len(message.reply_markup.inline_keyboard) == 1


async def test_prediction_then_bonus_after_three_seconds(dispatcher, bot, users):
    await dispatcher.feed_update(bot, incoming(callback="match:navi-faze"), users=users)
    assert isinstance(bot.session.make_request.call_args_list[0].args[1], AnswerCallbackQuery)
    assert "победа NAVI 2:1" in requests(bot, SendMessage)[0].text
    menu.asyncio.sleep.assert_awaited_once_with(3)
    photo = requests(bot, SendPhoto)[0]
    assert "Демо" in photo.caption
    assert photo.photo.path == PROJECT_ROOT / "assets" / "bonus.png"
    assert photo.photo.path.stat().st_size > 0
    assert len(photo.reply_markup.inline_keyboard) == 6
    assert requests(bot, SetMessageReaction)[0].reaction[0].emoji == "🔥"


async def test_missing_analysis_does_not_invent_prediction(dispatcher, bot, users):
    await dispatcher.feed_update(bot, incoming(callback="match:vitality-g2"), users=users)
    assert "Разбора этого матча в макете пока нет" in requests(bot, SendMessage)[0].text
    assert not requests(bot, SendPhoto)


@pytest.mark.parametrize("callback", ["match:missing", "sport:missing", "offer:0"])
async def test_unavailable_buttons_answer_without_crashing(dispatcher, bot, users, callback):
    await dispatcher.feed_update(bot, incoming(callback=callback), users=users)
    assert requests(bot, AnswerCallbackQuery)[0].show_alert


async def test_info_and_askbetty_use_saved_discipline(dispatcher, bot, users):
    users.update(42, sport="cs2")
    await dispatcher.feed_update(bot, incoming(text="/info"), users=users)
    assert "Как это работает" in requests(bot, SendMessage)[-1].text
    await dispatcher.feed_update(bot, incoming(text="/askbetty"), users=users)
    assert "Ближайшие матчи" in requests(bot, SendMessage)[-1].text


@pytest.mark.parametrize("name", ["daily", "favorite", "reminder", "result"])
async def test_notification_previews_only_send_to_caller(dispatcher, bot, users, name):
    await dispatcher.feed_update(bot, incoming(text=f"/preview {name}"), users=users)
    message = requests(bot, SendMessage)[0]
    assert message.chat_id == 42 and "Демо" in message.text


async def test_bonus_still_works_when_reactions_are_disabled(dispatcher, bot, users):
    original = bot.session.make_request.side_effect

    async def without_reactions(bot, method, **kwargs):
        if isinstance(method, SetMessageReaction):
            raise TelegramBadRequest(method=method, message="REACTION_INVALID")
        return await original(bot, method, **kwargs)

    bot.session.make_request.side_effect = without_reactions
    await dispatcher.feed_update(bot, incoming(text="/bonus"), users=users)
    assert requests(bot, SendPhoto)


async def test_menu_does_not_respond_in_groups(dispatcher, bot, users):
    await dispatcher.feed_update(bot, incoming(text="/start", chat_type="group"), users=users)
    bot.session.make_request.assert_not_awaited()
