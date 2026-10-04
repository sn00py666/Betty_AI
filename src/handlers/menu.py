import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, Message, ReactionTypeEmoji

import content
from config import PROJECT_ROOT
from keyboards import menu
from services.users import Users

router = Router(name="menu")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
logger = logging.getLogger(__name__)


async def show_matches(message: Message, user_id: int, users: Users) -> None:
    sport = users.get(user_id).get("sport")
    if sport not in content.SPORTS:
        await message.answer("<b>Выбери дисциплину:</b>", reply_markup=menu.disciplines())
        return
    text = content.DEMO + content.MATCH_LIST if sport == "cs2" else "Матчей Dota 2 в демо пока нет."
    await message.answer(text, reply_markup=menu.matches(sport))


async def show_bonus(message: Message) -> None:
    sent = await message.answer_photo(
        FSInputFile(PROJECT_ROOT / "assets" / "bonus.png"),
        caption="<i>Демо: предложения и ссылки пока не подключены.</i>\n\n" + content.BONUS,
        reply_markup=menu.bonuses(),
    )
    try:
        await sent.react([ReactionTypeEmoji(emoji="🔥")])
    except TelegramBadRequest:
        logger.debug("Telegram не разрешил реакцию на сообщение с бонусом.")


@router.message(CommandStart())
async def start(message: Message, users: Users) -> None:
    first_visit = not users.get(message.from_user.id).get("started")
    users.update(message.from_user.id, started=True)
    text = content.DEMO + content.WELCOME if first_visit else "<b>Выбери дисциплину:</b>"
    await message.answer(text, reply_markup=menu.disciplines())


@router.message(Command("info"))
async def info(message: Message) -> None:
    await message.answer(content.DEMO + content.INFO, reply_markup=menu.navigation())


@router.message(Command("bonus"))
async def bonus(message: Message) -> None:
    await show_bonus(message)


@router.message(Command("askbetty"))
async def ask_betty(message: Message, users: Users) -> None:
    # В Figma указан только пункт меню; свободный AI-чат ещё не подключён.
    await message.answer("Пока могу показать примеры разборов из макета. Выбери матч 👇")
    await show_matches(message, message.from_user.id, users)


@router.callback_query(F.data == "disciplines")
async def choose_discipline(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("<b>Выбери дисциплину:</b>", reply_markup=menu.disciplines())


@router.callback_query(F.data.startswith("sport:"))
async def select_discipline(callback: CallbackQuery, users: Users) -> None:
    sport = callback.data.removeprefix("sport:")
    if sport not in content.SPORTS:
        await callback.answer("Дисциплина не найдена.", show_alert=True)
        return
    await callback.answer()
    users.update(callback.from_user.id, sport=sport)
    await show_matches(callback.message, callback.from_user.id, users)


@router.callback_query(F.data == "matches")
async def open_matches(callback: CallbackQuery, users: Users) -> None:
    await callback.answer()
    await show_matches(callback.message, callback.from_user.id, users)


@router.callback_query(F.data.startswith("match:"))
async def open_match(callback: CallbackQuery) -> None:
    match = content.MATCHES.get(callback.data.removeprefix("match:"))
    if match is None:
        await callback.answer("Матч не найден. Открой список заново.", show_alert=True)
        return
    await callback.answer()
    if "analysis" not in match:
        await callback.message.answer(
            f"<b>{match['title']}</b>\n\nРазбора этого матча в макете пока нет.", reply_markup=menu.navigation()
        )
        return
    await callback.message.answer(content.DEMO + match["analysis"])
    await asyncio.sleep(3)
    await show_bonus(callback.message)


@router.callback_query(F.data == "bonus")
async def open_bonus(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_bonus(callback.message)


@router.callback_query(F.data.startswith("offer:"))
async def open_offer(callback: CallbackQuery) -> None:
    await callback.answer("Это пример из Figma. Бонусная ссылка пока не подключена.", show_alert=True)


@router.message(Command("preview"))
async def preview(message: Message, command: CommandObject) -> None:
    """Показать экран уведомления только тому, кто вызвал команду."""
    name = command.args or ""
    if name not in content.NOTIFICATIONS:
        await message.answer("Примеры: /preview daily, /preview favorite, /preview reminder, /preview result")
        return
    await message.answer(
        content.DEMO + content.NOTIFICATIONS[name],
        reply_markup=menu.navigation(with_bonus=name in {"reminder", "result"}),
    )
