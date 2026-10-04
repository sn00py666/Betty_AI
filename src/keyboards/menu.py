from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from content import MATCHES, OFFERS, SPORTS


def keyboard(buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Одна кнопка на строку — как в макете."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)] for text, data in buttons]
    )


def disciplines() -> InlineKeyboardMarkup:
    return keyboard([(title, f"sport:{sport}") for sport, title in SPORTS.items()])


def matches(sport: str) -> InlineKeyboardMarkup:
    buttons = [(match["title"], f"match:{match_id}") for match_id, match in MATCHES.items()] if sport == "cs2" else []
    return keyboard([*buttons, ("назад", "disciplines")])


def navigation(*, with_bonus: bool = False) -> InlineKeyboardMarkup:
    buttons = [("👀 Выбрать матчи", "matches")]
    if with_bonus:
        buttons.append(("🎁 Забрать бонус 80к", "bonus"))
    return keyboard(buttons)


def bonuses() -> InlineKeyboardMarkup:
    return keyboard([(label, f"offer:{index}") for index, label in enumerate(OFFERS)])
