import asyncio

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, Message

import content
from config import PROJECT_ROOT, Settings
from keyboards import menu
from services.ai import Analyst, facts
from services.analysis import render, render_offers
from services.bonuses import ordered_offers, render_bonus
from services.discovery import sort_matches
from services.dota_rankings import enabled_sources, select_ranking
from services.forecasts import recall, remember
from services.matches import Matches, date
from services.users import Users

router = Router(name="menu")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


async def show_matches(message, user_id, users, matches, page=0, edit=False, settings=None):
    sport = users.get(user_id).get("sport")
    if sport not in content.SPORTS:
        await message.answer("<b>Выбери дисциплину:</b>", reply_markup=menu.disciplines())
        return
    mode = users.get(user_id).get("match_sort", "popular")
    _, dota_ranks, _ = select_ranking(matches.directory.parent, enabled_sources(settings))
    items = sort_matches(
        [m for m in matches.read() if m["sport"] == sport],
        mode,
        matches.directory.parent / "valve.json",
        dota_ranks=dota_ranks,
    )
    page = min(max(0, page), max(0, (len(items) - 1) // menu.PAGE_SIZE))
    text = content.MATCH_LIST if items else "Свежих матчей этой дисциплины сейчас нет. Попробуй позже."
    markup = menu.matches(items, sport, page, mode)
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as error:
            if "message is not modified" not in error.message.lower():
                raise
    else:
        await message.answer(text, reply_markup=markup)


async def show_offers(message, match, favorite=None):
    await message.answer(render_offers(match, favorite), reply_markup=menu.offers(match, favorite))


@router.message(CommandStart())
async def start(message: Message, users: Users):
    first_visit = not users.get(message.from_user.id).get("started")
    users.update(message.from_user.id, started=True, username=message.from_user.username)
    await message.answer_animation(
        FSInputFile(PROJECT_ROOT / "assets" / "betty-start-motion-v3.mp4"),
        caption=content.WELCOME if first_visit else "<b>Выбери дисциплину:</b>",
        reply_markup=menu.disciplines(),
    )


@router.message(Command("info"))
async def info(message: Message):
    await message.answer(content.INFO, reply_markup=menu.navigation())


@router.message(Command("askbetty"))
async def ask_betty(message: Message, users: Users, matches: Matches, settings: Settings = None):
    await show_matches(message, message.from_user.id, users, matches, settings=settings)


@router.message(Command("bonus"))
async def bonus(message: Message, settings: Settings):
    offers = ordered_offers(settings)
    await message.answer(render_bonus(offers), reply_markup=menu.bonuses(offers))


@router.callback_query(F.data == "disciplines")
async def choose_discipline(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("<b>Выбери дисциплину:</b>", reply_markup=menu.disciplines())


@router.callback_query(F.data.startswith("sport:"))
async def select_discipline(callback: CallbackQuery, users: Users, matches: Matches, settings: Settings = None):
    sport = callback.data.removeprefix("sport:")
    if sport not in content.SPORTS:
        await callback.answer("Дисциплина не найдена.", show_alert=True)
        return
    await callback.answer()
    users.update(callback.from_user.id, sport=sport)
    await show_matches(callback.message, callback.from_user.id, users, matches, settings=settings)


@router.callback_query(F.data == "matches")
async def open_matches(callback: CallbackQuery, users: Users, matches: Matches, settings: Settings = None):
    await callback.answer()
    await show_matches(callback.message, callback.from_user.id, users, matches, settings=settings)


@router.callback_query(F.data == "page_current")
async def current_page(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("page:"))
async def change_page(callback: CallbackQuery, users: Users, matches: Matches, settings: Settings = None):
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[1] not in content.SPORTS or not parts[2].isdigit():
        await callback.answer("Открой список заново.", show_alert=True)
        return
    await callback.answer()
    users.update(callback.from_user.id, sport=parts[1])
    await show_matches(
        callback.message, callback.from_user.id, users, matches, int(parts[2]), edit=True, settings=settings
    )


@router.callback_query(F.data.startswith("sort:"))
async def change_sort(callback: CallbackQuery, users: Users, matches: Matches, settings: Settings = None):
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[1] not in content.SPORTS or parts[2] not in ("popular", "time"):
        await callback.answer("Открой список заново.", show_alert=True)
        return
    await callback.answer()
    users.update(callback.from_user.id, sport=parts[1], match_sort=parts[2])
    await show_matches(callback.message, callback.from_user.id, users, matches, edit=True, settings=settings)


@router.callback_query(F.data.startswith("match:"))
async def open_match(callback: CallbackQuery, users: Users, matches: Matches, analyst: Analyst):
    match_id = callback.data.removeprefix("match:")
    match = matches.get(match_id)
    if match is None:
        await callback.answer("Матч завершился или данные устарели. Обнови список.", show_alert=True)
        return
    await callback.answer()
    users.update(callback.from_user.id, last_match=match_id)
    pending = await callback.message.answer("Анализирую текущую форму команд…")
    try:
        async with asyncio.timeout(25):
            commentary = await analyst.explain(match)
    except TimeoutError:
        commentary = None
    fresh = matches.get(match_id)
    if fresh is None:
        await pending.edit_text("За время разбора котировки устарели. Обнови список матчей.")
        return
    fresh["statistics"] = match.get("statistics")
    if facts(fresh) != facts(match):
        commentary = None
    await pending.edit_text(render(fresh, commentary), reply_markup=menu.match_actions(fresh))
    forecast = remember(users, callback.from_user.id, pending.message_id, fresh, commentary)
    await asyncio.sleep(3)
    if fresh := matches.get(match_id):
        await show_offers(callback.message, fresh, recall(forecast, fresh))


@router.callback_query(F.data.startswith("odds:"))
async def open_odds(callback: CallbackQuery, matches: Matches, users: Users):
    match = matches.get(callback.data.removeprefix("odds:"))
    if match is None:
        await callback.answer("Коэффициенты устарели. Обнови список.", show_alert=True)
        return
    await callback.answer()
    forecast = users.get(callback.from_user.id).get("forecasts", {}).get(str(callback.message.message_id))
    await show_offers(callback.message, match, recall(forecast, match))


@router.message(Command("notifications"))
async def notifications(message: Message, users: Users):
    enabled = users.get(message.from_user.id).get("notifications", False)
    text = "Уведомления включены." if enabled else "Уведомления выключены."
    await message.answer(
        text + "\nДневная подборка — в 12:00, фаворит рынка — в 18:00 МСК.",
        reply_markup=menu.keyboard(
            [
                ("Включить подборки", "notifications:on"),
                ("Выключить всё, включая напоминания", "notifications:off"),
            ]
        ),
    )


@router.callback_query(F.data.in_({"notifications:on", "notifications:off"}))
async def set_notifications(callback: CallbackQuery, users: Users):
    enabled = callback.data.endswith(":on")
    users.update(callback.from_user.id, notifications=enabled, **({} if enabled else {"reminders": {}}))
    await callback.answer("Уведомления включены" if enabled else "Уведомления и напоминания выключены", show_alert=True)


@router.callback_query(F.data.startswith("remind:"))
async def remind(callback: CallbackQuery, users: Users, matches: Matches):
    match = matches.get(callback.data.removeprefix("remind:"))
    if not match or not date(match.get("starts_at")) or match["status"] != "prematch":
        await callback.answer("Нет подтверждённого времени начала. Обнови список.", show_alert=True)
        return
    reminders = dict(users.get(callback.from_user.id).get("reminders", {}))
    if len(reminders) >= 20 and match["id"] not in reminders:
        await callback.answer("Можно сохранить до 20 напоминаний.", show_alert=True)
        return
    reminders[match["id"]] = {"starts_at": match["starts_at"], "title": f"{match['team1']} vs {match['team2']}"}
    users.update(callback.from_user.id, reminders=reminders)
    await callback.answer("Напомню за 30 минут. Отключить: /notifications", show_alert=True)


@router.callback_query(F.data == "bonus")
async def old_bonus(callback: CallbackQuery, settings: Settings):
    await callback.answer()
    await bonus(callback.message, settings)


@router.callback_query()
async def old_button(callback: CallbackQuery):
    await callback.answer("Эта кнопка устарела. Открой /start.", show_alert=True)
