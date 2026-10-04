import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery

router = Router(name="donations")
logger = logging.getLogger(__name__)
MAX_DONATION = 10_000


def matches_donation(payload: str, currency: str, amount: int, user_id: int) -> bool:
    return currency == "XTR" and 1 <= amount <= MAX_DONATION and payload == f"donation:{user_id}:{amount}"


@router.message(Command("donate"))
async def donate(message: Message, command: CommandObject) -> None:
    if message.chat.type != "private" or message.from_user is None:
        await message.answer("Для доната откройте личный чат с ботом.")
        return

    raw_amount = (command.args or "1").strip()
    if not raw_amount.isascii() or not raw_amount.isdecimal() or len(raw_amount) > 5:
        await message.answer("Укажите целое число от 1 до 10000. Например: /donate 50")
        return

    amount = int(raw_amount)
    if not 1 <= amount <= MAX_DONATION:
        await message.answer("Укажите целое число от 1 до 10000. Например: /donate 50")
        return

    await message.answer_invoice(
        title="Донат",
        description="Добровольный донат проекту. Покупка не открывает дополнительные функции.",
        payload=f"donation:{message.from_user.id}:{amount}",
        currency="XTR",
        provider_token="",
        prices=[LabeledPrice(label="Донат", amount=amount)],
        start_parameter="donate",
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    if matches_donation(query.invoice_payload, query.currency, query.total_amount, query.from_user.id):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Счёт не подходит для оплаты. Создайте новый через /donate.")


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    payment = message.successful_payment
    if payment is None or message.from_user is None:
        return
    if not matches_donation(payment.invoice_payload, payment.currency, payment.total_amount, message.from_user.id):
        logger.error("Получен платёж с неожиданными параметрами: %s", payment.telegram_payment_charge_id)
        return

    logger.info("Донат %s Stars принят: %s", payment.total_amount, payment.telegram_payment_charge_id)
    await message.answer("Ок, принят")
