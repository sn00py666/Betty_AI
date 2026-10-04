from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from content import SPORTS
from services.matches import BOOKMAKERS, start_label

PAGE_SIZE = 5


def keyboard(buttons):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)] for text, data in buttons]
    )


def disciplines():
    return keyboard([(title, f"sport:{sport}") for sport, title in SPORTS.items()])


def matches(items, sport, page):
    buttons = [
        (f"{m['team1']} vs {m['team2']} · {start_label(m)}"[:110], f"match:{m['id']}")
        for m in items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    ]
    markup = keyboard(buttons)
    arrows = []
    if page:
        arrows.append(InlineKeyboardButton(text="←", callback_data=f"page:{sport}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < len(items):
        arrows.append(InlineKeyboardButton(text="→", callback_data=f"page:{sport}:{page + 1}"))
    if arrows:
        markup.inline_keyboard.append(arrows)
    markup.inline_keyboard.extend(keyboard([("назад", "disciplines")]).inline_keyboard)
    return markup


def navigation():
    return keyboard([("👀 Выбрать матчи", "matches")])


def match_actions(match):
    buttons = [("Обновить разбор", f"match:{match['id']}"), ("Сравнить коэффициенты", f"odds:{match['id']}")]
    if match.get("starts_at") and match["status"] == "prematch":
        buttons.append(("🔔 Напомнить за 30 минут", f"remind:{match['id']}"))
    return keyboard([*buttons, ("👀 Выбрать матчи", "matches")])


def offers(match):
    rows = []
    for offer in sorted(match["offers"], key=lambda o: o["p1"] or 0, reverse=True):
        if offer["p1"] is None and offer["p2"] is None:
            continue
        p1 = f"{offer['p1']:.2f}" if offer["p1"] else "закрыт"
        p2 = f"{offer['p2']:.2f}" if offer["p2"] else "закрыт"
        rows.append(
            [InlineKeyboardButton(text=f"{BOOKMAKERS[offer['bookmaker']]} · П1 {p1} / П2 {p2}", url=offer["url"])]
        )
    rows.append([InlineKeyboardButton(text="👀 Выбрать матчи", callback_data="matches")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
