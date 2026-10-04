"""Подборки по подписке и запрошенные напоминания. Время — московское."""

import asyncio
import logging
from datetime import UTC, datetime
from html import escape

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from keyboards.menu import keyboard
from services.matches import MOSCOW, date, market_probability, start_label

logger = logging.getLogger(__name__)


def scheduled(user, items, now):
    """Возвращает сообщения с ключами для защиты от повторной отправки."""
    available = {m["id"]: m for m in items}
    for match_id, reminder in user.get("reminders", {}).items():
        match = available.get(match_id)
        start = date(match["starts_at"] if match else reminder["starts_at"])
        if match and start and 0 < (start - now).total_seconds() <= 1800:
            text = f"<b>Скоро матч</b>\n{escape(match['team1'])} vs {escape(match['team2'])}\n{start_label(match)}"
            yield f"remind:{match_id}", text, keyboard([("Открыть матч", f"match:{match_id}")])
    if not user.get("notifications"):
        return
    local = now.astimezone(MOSCOW)
    # Короткое окно: после долгого простоя не рассылаем пропущенные подборки.
    if local.hour not in (12, 18) or local.minute >= 10:
        return
    chosen = [m for m in items if m["sport"] == user.get("sport", "cs2") and m["status"] == "prematch"]
    chosen = [
        m for m in chosen if date(m.get("starts_at")) and date(m["starts_at"]).astimezone(MOSCOW).date() == local.date()
    ]
    if not chosen:
        return
    key = f"{local.date()}:{local.hour}"
    if local.hour == 18:
        chosen = [m for m in chosen if market_probability(m) is not None]
        if not chosen:
            return
        chosen = [max(chosen, key=lambda m: abs(market_probability(m) - 0.5))]
    title = "Матчи сегодня" if local.hour == 12 else "Самый выраженный фаворит рынка в сегодняшней линии"
    text = f"<b>{title}</b>\nВыбери матч для разбора."
    buttons = [(f"{m['team1']} — {m['team2']}", f"match:{m['id']}") for m in chosen[:5]]
    yield key, text, keyboard(buttons)


async def run(bot, users, matches):
    while True:
        now = datetime.now(UTC)
        items = matches.read()
        for user_id, user in list(users.data.items()):
            sent = list(user.get("sent_notifications", []))
            for key, text, markup in scheduled(user, items, now):
                if key in sent:
                    continue
                # Резервируем до запроса: неизвестный исход доставки не вызывает дубликаты.
                sent.append(key)
                users.update(int(user_id), sent_notifications=sent[-100:])
                try:
                    await bot.send_message(int(user_id), text, reply_markup=markup)
                except TelegramForbiddenError:
                    users.update(int(user_id), notifications=False, reminders={})
                    break
                except TelegramAPIError as error:
                    logger.warning("Уведомление не доставлено (%s)", type(error).__name__)
                await asyncio.sleep(0.1)
            reminders = {
                k: v
                for k, v in user.get("reminders", {}).items()
                if date(v.get("starts_at")) and date(v["starts_at"]) > now
            }
            if reminders != user.get("reminders", {}):
                users.update(int(user_id), reminders=reminders)
        await asyncio.sleep(30)
