"""Проверенные логотипы из паков Динара; ID хранятся строками без потери точности."""

import json
import re
from functools import lru_cache
from html import escape
from pathlib import Path

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest

from services.team_names import team_key


@lru_cache(maxsize=1)
def registry():
    path = Path(__file__).resolve().parents[2] / "assets" / "custom-emoji.json"
    return json.loads(path.read_text(encoding="utf-8"))


def tag(entry):
    return f'<tg-emoji emoji-id="{entry["id"]}">{escape(entry["emoji"])}</tg-emoji> '


def team_emoji(name, sport):
    entry = registry()["teams"].get(team_key(name)) if sport == "cs2" else None
    return tag(entry) if entry else ""


def bookmaker_icon(bookmaker):
    return registry()["bookmakers"].get(bookmaker, {}).get("id")


def bookmaker_name(bookmaker, name):
    entry = registry()["bookmakers"].get(bookmaker)
    return (tag(entry) if entry else "") + escape(name)


def plain_method(method):
    result = method.model_copy(deep=True)
    for field in ("text", "caption"):
        text = getattr(result, field, None)
        if text:
            setattr(result, field, re.sub(r"<tg-emoji\b[^>]*>.*?</tg-emoji>\s*", "", text))
    markup = getattr(result, "reply_markup", None)
    if markup and hasattr(markup, "inline_keyboard"):
        for row in markup.inline_keyboard:
            for button in row:
                button.icon_custom_emoji_id = None
    return result


class EmojiFallback(BaseRequestMiddleware):
    def __init__(self, enabled=True):
        self.enabled = enabled

    async def __call__(self, make_request, bot, method):
        if not self.enabled:
            return await make_request(bot, plain_method(method))
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as error:
            if "emoji" not in error.message.lower():
                raise
            plain = plain_method(method)
            if plain == method:
                raise
            # Нет права на Premium-эмодзи или ID удалён: сохраняем рабочую карточку.
            return await make_request(bot, plain)
