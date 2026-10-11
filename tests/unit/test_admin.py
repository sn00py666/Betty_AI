from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.methods import SendMessage
from aiogram.types import Message, Update

from handlers import admin


@pytest.fixture(scope="module")
def dispatcher():
    dispatcher = Dispatcher()
    dispatcher.include_router(admin.router)
    return dispatcher


@pytest.mark.parametrize(
    "user_id,chat_type,allowed", [(850362750, "private", True), (1, "private", False), (850362750, "group", False)]
)
async def test_status_requires_configured_admin_in_private_chat(dispatcher, user_id, chat_type, allowed):
    monitor = SimpleNamespace(report=lambda: "REPORT")
    settings = SimpleNamespace(admin_user_ids=[850362750])
    update = Update.model_validate(
        dict(
            update_id=1,
            message=dict(
                message_id=1,
                date=1,
                chat=dict(id=user_id, type=chat_type),
                text="/status",
                **{"from": dict(id=user_id, is_bot=False, first_name="Test")},
            ),
        )
    )
    async with Bot("123456:TEST_TOKEN", default=DefaultBotProperties(parse_mode="HTML")) as bot:
        bot.session.make_request = AsyncMock(
            return_value=Message(message_id=2, date=1, chat=dict(id=user_id, type=chat_type))
        )
        await dispatcher.feed_update(bot, update, settings=settings, monitor=monitor)
        sent = [c.args[1] for c in bot.session.make_request.call_args_list if isinstance(c.args[1], SendMessage)]
    assert any(m.text == "REPORT" for m in sent) == allowed
    if chat_type == "group":
        assert not sent
