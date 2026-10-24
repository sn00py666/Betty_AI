from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendAnimation,
    SendMessage,
    SendPhoto,
)
from aiogram.types import Message, Update

from config import Settings
from handlers import menu
from services.ai import Commentary
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
            if isinstance(method, SendMessage | EditMessageText | SendAnimation | SendPhoto):
                return Message(message_id=10, date=1, chat={"id": method.chat_id, "type": "private"}).as_(bot)
            return True

        instance.session.make_request = AsyncMock(side_effect=reply)
        monkeypatch.setattr(menu.asyncio, "sleep", AsyncMock())
        yield instance


def incoming(*, text=None, callback=None, chat_type="private", message_id=1):
    user = {"id": 42, "is_bot": False, "first_name": "Test"}
    message = {
        "message_id": message_id,
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
    data = snapshot(tmp_path, "marathon", starts_at=(now + timedelta(hours=2)).isoformat())
    data["fetched_at"] = now.isoformat()
    data["expires_at"] = (now + timedelta(minutes=30)).isoformat()
    import json

    (tmp_path / "marathon" / "latest.json").write_text(json.dumps(data))
    return Matches(tmp_path)


@pytest.fixture
def analyst():
    return AsyncMock(explain=AsyncMock(return_value=None))


async def test_first_and_repeated_start_survive_restart(dispatcher, bot, users):
    await dispatcher.feed_update(bot, incoming(text="/start"), users=users)
    first = requests(bot, SendAnimation)[0]
    assert "привет" in first.caption and "Демо" not in first.caption
    assert first.animation.path.is_file()
    assert first.animation.path.name == "betty-start-motion-v3.mp4"
    assert [row[0].text for row in first.reply_markup.inline_keyboard] == ["CS-2", "Dota 2"]
    await dispatcher.feed_update(bot, incoming(text="/start"), users=Users(users.path))
    repeated = requests(bot, SendAnimation)[-1]
    assert repeated.caption == "<b>Выбери дисциплину:</b>"
    assert repeated.animation.path == first.animation.path
    assert repeated.reply_markup == first.reply_markup
    assert not requests(bot, SendMessage)


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
    assert "Прогноз недоступен" in requests(bot, EditMessageText)[0].text
    assert requests(bot, SendMessage)[0].text == "Анализирую текущую форму команд…"
    assert "80 000" not in requests(bot, EditMessageText)[0].text
    rows = requests(bot, EditMessageText)[0].reply_markup.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1
    assert rows[0][0].text == "🔔 Напомнить за 30 минут"
    menu.asyncio.sleep.assert_awaited_once_with(6)
    offers = requests(bot, SendPhoto)[-1]
    assert offers.photo.path.is_file()
    assert offers.photo.path.name == "betty-freebets-21111.png"
    assert "Нет актуального фаворита AI" in offers.caption
    assert offers.reply_markup.inline_keyboard[0][0].url == "https://bett1.pro/click?o=354&a=53074&tsource=1015"


@pytest.mark.parametrize("callback", ["match:missing", "sport:missing", "offer:0", "remind:missing"])
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


async def test_reminder_edits_card_and_remains_confirmed_when_reopened(dispatcher, bot, users, matches, analyst):
    match = matches.read()[0]
    update = incoming(callback="remind:" + match["id"])
    await dispatcher.feed_update(bot, update, users=users, matches=matches)
    saved = Users(users.path).get(42)["reminders"]
    assert saved[match["id"]]["starts_at"] == match["starts_at"]
    edits = requests(bot, EditMessageReplyMarkup)
    assert len(edits) == 1 and edits[0].message_id == 1
    markup = edits[0].reply_markup
    assert len(markup.inline_keyboard) == 1
    assert markup.inline_keyboard[0][0].text == "✅ Напоминание установлено"
    assert not requests(bot, SendMessage) and not requests(bot, EditMessageText)
    # Повторное нажатие не редактирует уже подтверждённую кнопку и не дублирует напоминание.
    data = update.model_dump()
    data["callback_query"]["message"]["reply_markup"] = markup.model_dump()
    update = Update.model_validate(data)
    await dispatcher.feed_update(bot, update, users=users, matches=matches)
    assert len(requests(bot, EditMessageReplyMarkup)) == 1
    assert users.get(42)["reminders"] == saved
    await dispatcher.feed_update(
        bot, incoming(callback="match:" + match["id"]), users=users, matches=matches, analyst=analyst
    )
    assert requests(bot, EditMessageText)[0].reply_markup.model_dump() == markup.model_dump()


@pytest.mark.parametrize("changes", [{"status": "live"}, {"starts_at": None}])
def test_analysis_without_reminder_has_no_buttons(changes):
    match = {"id": "a", "status": "prematch", "starts_at": "2026-10-15T13:00:00+00:00"} | changes
    assert menu.menu.match_actions(match) is None


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
    assert [b.callback_data for b in rows[5]] == ["page:cs2:0", "page_current", "page:cs2:2"]
    assert [b.text for b in rows[5]] == ["←", "2/3", "→"]
    assert "2/3" not in message.text
    assert "Время МСК" not in message.text
    assert "Сначала команды" not in message.text
    assert "лучший коэффициент" in message.text
    await dispatcher.feed_update(bot, incoming(callback="page:cs2:2"), users=users, matches=matches)
    rows = requests(bot, EditMessageText)[-1].reply_markup.inline_keyboard
    assert rows[0][0].callback_data == "match:10"
    assert [b.callback_data for b in rows[1]] == ["page:cs2:1", "page_current"]
    assert [b.text for b in rows[1]] == ["←", "3/3"]
    await dispatcher.feed_update(bot, incoming(callback="page:cs2:0"), users=users, matches=matches)
    rows = requests(bot, EditMessageText)[-1].reply_markup.inline_keyboard
    assert [b.text for b in rows[5]] == ["1/3", "→"]
    assert [b.callback_data for b in rows[5]] == ["page_current", "page:cs2:1"]


async def test_time_sort_keeps_live_matches_on_first_pages(dispatcher, bot, users, matches, monkeypatch):
    users.update(42, sport="cs2", match_sort="time")
    base = matches.read()[0]
    upcoming = [dict(base, id=f"prematch-{i}", status="prematch") for i in range(6)]
    live = [dict(base, id=f"live-{i}", status="live", starts_at=None) for i in range(7)]
    monkeypatch.setattr(matches, "read", lambda: [*upcoming, *live])
    await dispatcher.feed_update(bot, incoming(callback="page:cs2:0"), users=users, matches=matches)
    rows = requests(bot, EditMessageText)[-1].reply_markup.inline_keyboard
    assert [r[0].callback_data for r in rows[:5]] == [f"match:live-{i}" for i in range(5)]
    await dispatcher.feed_update(bot, incoming(callback="page:cs2:1"), users=users, matches=matches)
    rows = requests(bot, EditMessageText)[-1].reply_markup.inline_keyboard
    assert [r[0].callback_data for r in rows[:5]] == [
        "match:live-5",
        "match:live-6",
        "match:prematch-0",
        "match:prematch-1",
        "match:prematch-2",
    ]


async def test_sorting_selection_persists_and_returns_to_page_one(dispatcher, bot, users, matches):
    await dispatcher.feed_update(bot, incoming(callback="sort:cs2:time"), users=users, matches=matches)
    assert Users(users.path).get(42)["match_sort"] == "time"
    message = requests(bot, EditMessageText)[-1]
    assert "1/1" not in message.text
    assert any(b.text == "1/1" for row in message.reply_markup.inline_keyboard for b in row)
    assert any(b.text == "✓ По времени" for row in message.reply_markup.inline_keyboard for b in row)


async def test_bonus_command_and_old_button_open_separate_screen(dispatcher, bot):
    settings = Settings(_env_file=None, telegram_bot_token="123:TEST", bonus_offers=[])
    for update in (incoming(text="/bonus"), incoming(callback="bonus")):
        await dispatcher.feed_update(bot, update, settings=settings)
        text = requests(bot, SendMessage)[-1].text
        assert "Бонусы букмекеров" in text and "нет опубликованных" in text and "Ближайшие матчи" not in text


async def test_favorite_survives_odds_button_but_not_changed_match(
    dispatcher, bot, users, matches, analyst, monkeypatch
):
    async def explain(match):
        match["statistics"] = {"updated_at": 1791200880, "team1": {}, "team2": {}}
        return Commentary(summary="Перевес второй команды.", winner="team2", probability=60)

    analyst.explain.side_effect = explain
    match = matches.read()[0]
    await dispatcher.feed_update(
        bot, incoming(callback="match:" + match["id"]), users=users, matches=matches, analyst=analyst
    )
    assert "— фаворит против" in requests(bot, SendPhoto)[-1].caption
    update = incoming(callback="odds:" + match["id"], message_id=10)
    await dispatcher.feed_update(bot, update, users=users, matches=matches)
    assert "— фаворит против" in requests(bot, SendPhoto)[-1].caption
    monkeypatch.setattr(matches, "get", lambda _: dict(match, status="live"))
    await dispatcher.feed_update(bot, update, users=users, matches=matches)
    assert "Нет актуального фаворита AI" in requests(bot, SendPhoto)[-1].caption


async def test_long_offers_keep_full_text_below_photo(monkeypatch):
    text = "Длинное название команды " * 100
    monkeypatch.setattr(menu, "render_offers", lambda *args: text)
    message = AsyncMock()
    await menu.show_offers(message, {"offers": []})
    message.answer_photo.assert_awaited_once()
    assert "caption" not in message.answer_photo.call_args.kwargs
    assert message.answer.call_args.args == (text,)
    assert message.answer.call_args.kwargs["reply_markup"].inline_keyboard
