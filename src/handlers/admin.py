"""Команда отчёта только для указанных в конфигурации модераторов."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="admin")
router.message.filter(F.chat.type == "private")


@router.message(Command("myid"))
async def my_id(message: Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("status"))
async def status(message: Message, settings, monitor):
    if message.from_user.id not in settings.admin_user_ids:
        await message.answer("Эта команда доступна модератору.")
        return
    await message.answer(monitor.report())
