from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerPreCheckoutQuery, SendInvoice, SendMessage
from aiogram.types import Update

from handlers.donate import router


@pytest.fixture(scope="module")
def dispatcher():
    instance = Dispatcher()
    instance.include_router(router)
    return instance


@pytest.fixture
async def bot():
    async with Bot(token="123456:TEST_TOKEN") as instance:
        instance.session.make_request = AsyncMock(return_value=True)
        yield instance


def message_update(**fields):
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                **fields,
            },
        }
    )


@pytest.mark.parametrize("command,amount", [("/donate", 1), ("/donate 50", 50), ("/donate 10000", 10000)])
async def test_invoice(dispatcher, bot, command, amount):
    await dispatcher.feed_update(bot, message_update(text=command))
    bot.session.make_request.assert_awaited_once()
    method = bot.session.make_request.call_args.args[1]
    assert isinstance(method, SendInvoice)
    assert method.currency == "XTR"
    assert method.provider_token == ""
    assert len(method.prices) == 1
    assert method.prices[0].amount == amount
    assert method.payload == f"donation:42:{amount}"
    assert method.start_parameter


@pytest.mark.parametrize("amount", ["0", "-1", "1.5", "abc", "10001", "50 60", "9" * 5000])
async def test_invalid_amount_does_not_send_invoice(dispatcher, bot, amount):
    await dispatcher.feed_update(bot, message_update(text=f"/donate {amount}"))
    bot.session.make_request.assert_awaited_once()
    assert isinstance(bot.session.make_request.call_args.args[1], SendMessage)


@pytest.mark.parametrize(
    "currency,amount,payload,accepted",
    [
        ("XTR", 50, "donation:42:50", True),
        ("USD", 50, "donation:42:50", False),
        ("XTR", 51, "donation:42:50", False),
        ("XTR", 50, "donation:99:50", False),
        ("XTR", 0, "donation:42:0", False),
        ("XTR", 50, "unknown", False),
    ],
)
async def test_checkout_validation(dispatcher, bot, currency, amount, payload, accepted):
    update = Update.model_validate(
        {
            "update_id": 2,
            "pre_checkout_query": {
                "id": "checkout-1",
                "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                "currency": currency,
                "total_amount": amount,
                "invoice_payload": payload,
            },
        }
    )
    await dispatcher.feed_update(bot, update)
    bot.session.make_request.assert_awaited_once()
    method = bot.session.make_request.call_args.args[1]
    assert isinstance(method, AnswerPreCheckoutQuery)
    assert method.ok is accepted
    assert bool(method.error_message) is not accepted


async def test_successful_payment_sends_exact_reply(dispatcher, bot):
    update = message_update(
        successful_payment={
            "currency": "XTR",
            "total_amount": 50,
            "invoice_payload": "donation:42:50",
            "telegram_payment_charge_id": "charge-1",
            "provider_payment_charge_id": "",
        }
    )
    await dispatcher.feed_update(bot, update)
    bot.session.make_request.assert_awaited_once()
    method = bot.session.make_request.call_args.args[1]
    assert isinstance(method, SendMessage)
    assert method.text == "Ок, принят"


async def test_regular_message_does_not_confirm_payment(dispatcher, bot):
    await dispatcher.feed_update(bot, message_update(text="оплатил"))
    bot.session.make_request.assert_not_awaited()
