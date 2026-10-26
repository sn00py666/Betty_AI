from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from content import SPORTS
from services.emojis import bookmaker_icon
from services.matches import BOOKMAKERS, start_label
from services.partners import FREEBET_LABELS, PARTNER_URLS, public_offers

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
    return keyboard([("👀 Выбрать матчи", "matches"), ("🎁 Бонусы", "bonus")])


def match_actions(match, reminder_set=False):
    if match.get("starts_at") and match["status"] == "prematch":
        text = "✅ Напоминание установлено" if reminder_set else "🔔 Напомнить за 30 минут"
        return keyboard([(text, f"remind:{match['id']}")])
    return None


def offers(match, favorite=None):
    rows = []
    side = "p2" if favorite == "team2" else "p1"
    for offer in sorted(public_offers(match["offers"]), key=lambda o: o[side] or 0, reverse=True):
        if offer["p1"] is None and offer["p2"] is None:
            continue
        label = BOOKMAKERS[offer["bookmaker"]]
        if freebet := FREEBET_LABELS.get(offer["bookmaker"]):
            label += f" · {freebet}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
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
                text=BOOKMAKERS[o.bookmaker],
                url=PARTNER_URLS[o.bookmaker],
                icon_custom_emoji_id=bookmaker_icon(o.bookmaker),
            )
        ]
        for o in offers
        if o.bookmaker in PARTNER_URLS and o.enabled
    ]
    rows.extend(navigation().inline_keyboard[:1])
    return InlineKeyboardMarkup(inline_keyboard=rows)
