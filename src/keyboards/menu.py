from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from content import SPORTS
from services.emojis import bookmaker_icon
from services.matches import BOOKMAKERS, start_label

PAGE_SIZE = 5


def keyboard(buttons):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)] for text, data in buttons]
    )


def disciplines():
    return keyboard([(title, f"sport:{sport}") for sport, title in SPORTS.items()])


def matches(items, sport, page, mode="popular"):
    buttons = [
        (f"{m['team1']} vs {m['team2']} · {start_label(m)}"[:110], f"match:{m['id']}")
        for m in items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    ]
    markup = keyboard(buttons)
    arrows = []
    if page:
        arrows.append(InlineKeyboardButton(text="←", callback_data=f"page:{sport}:{page - 1}"))
    if items:
        total_pages = (len(items) - 1) // PAGE_SIZE + 1
        arrows.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="page_current"))
    if (page + 1) * PAGE_SIZE < len(items):
        arrows.append(InlineKeyboardButton(text="→", callback_data=f"page:{sport}:{page + 1}"))
    if arrows:
        markup.inline_keyboard.append(arrows)
    markup.inline_keyboard.append(
        [
            InlineKeyboardButton(text=("✓ " if mode == value else "") + title, callback_data=f"sort:{sport}:{value}")
            for value, title in (("popular", "По популярности"), ("time", "По времени"))
        ]
    )
    markup.inline_keyboard.extend(keyboard([("назад", "disciplines")]).inline_keyboard)
    return markup


def navigation():
    return keyboard([("👀 Выбрать матчи", "matches"), ("🎁 Бонусы букмекеров", "bonus")])


def match_actions(match):
    buttons = [("Обновить разбор", f"match:{match['id']}"), ("Сравнить коэффициенты", f"odds:{match['id']}")]
    if match.get("starts_at") and match["status"] == "prematch":
        buttons.append(("🔔 Напомнить за 30 минут", f"remind:{match['id']}"))
    return keyboard([*buttons, ("👀 Выбрать матчи", "matches")])


def offers(match, favorite=None):
    rows = []
    side = "p2" if favorite == "team2" else "p1"
    for offer in sorted(match["offers"], key=lambda o: o[side] or 0, reverse=True):
        if offer["p1"] is None and offer["p2"] is None:
            continue
        first_price, second_price = (offer["p2"], offer["p1"]) if favorite == "team2" else (offer["p1"], offer["p2"])
        p1 = f"{first_price:.2f}" if first_price else "закрыт"
        p2 = f"{second_price:.2f}" if second_price else "закрыт"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{BOOKMAKERS[offer['bookmaker']]} · П1 {p1} / П2 {p2}",
                    url=offer["url"],
                    icon_custom_emoji_id=bookmaker_icon(offer["bookmaker"]),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="👀 Выбрать матчи", callback_data="matches")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bonuses(offers):
    rows = [
        [
            InlineKeyboardButton(
                text=BOOKMAKERS[o.bookmaker], url=str(o.url), icon_custom_emoji_id=bookmaker_icon(o.bookmaker)
            )
        ]
        for o in offers
    ]
    rows.extend(navigation().inline_keyboard[:1])
    return InlineKeyboardMarkup(inline_keyboard=rows)
