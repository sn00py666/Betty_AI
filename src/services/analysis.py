"""Короткая карточка прогноза и отдельное сравнение коэффициентов."""

import re
from datetime import UTC, datetime
from html import escape

from content import DISCLAIMER
from services.emojis import bookmaker_name, team_emoji
from services.factors import factor_summary, factors
from services.forecasts import favorite_side
from services.matches import BOOKMAKERS, MOSCOW, date, start_label
from services.team_names import canonical_match, canonical_text


def flag(country):
    code = (country or "").upper()
    if len(code) == 2 and code.isascii() and code.isalpha():
        return "".join(chr(127397 + ord(letter)) for letter in code) + " "
    return ""


def render(match, commentary=None):
    match = canonical_match(match)
    statistics = match.get("statistics") or {}
    teams = {side: escape(match[side]) for side in ("team1", "team2")}
    names = {
        side: (
            team_emoji(match[side], match["sport"])
            or flag(statistics.get(side, {}).get("country") or match.get(side + "_country"))
        )
        + teams[side]
        for side in teams
    }
    tournament = re.sub(r"^(?:Counter-Strike|CS-?2|Dota 2)\.\s*", "", match["tournament"], flags=re.I)
    tournament = re.sub(r"\bBo([135])\b", r"Bo\1", tournament, flags=re.I)
    tournament = re.sub(r"\.\s+(?!Bo\d+\b)", " · ", tournament, count=1)
    if match.get("best_of") and not re.search(r"\bBo\d+\b", tournament, re.I):
        tournament += f". Bo{match['best_of']}"
    lines = [
        f"<b>{names['team1']} vs {names['team2']}</b>",
        f"{escape(tournament)} · {start_label(match)}",
        "",
        "━━━━━━━━━━━━━━",
        "",
    ]
    valid = favorite_side(match, commentary)
    if valid:
        lines.append(f"<b>Победа {teams[commentary.winner]} — {commentary.probability}%</b>")
    else:
        lines.append("<b>Явного фаворита нет</b>" if commentary and statistics else "<b>Прогноз недоступен</b>")
    items = [item for item in factors(match) if item["edge"] != "unknown"]
    winner = commentary.winner if valid else "unclear"
    if items:
        lines.extend([f"<i>{factor_summary(items, winner, teams)}</i>", ""])
        lines.extend(f"{item['label']} — {escape(item['text'])}" for item in items)
        lines.append("")
    else:
        lines.append("")
    if commentary and statistics:
        lines.append(escape(canonical_text(without_links(commentary.summary), match["sport"])))
    elif statistics:
        lines.append("AI-разбор сейчас недоступен. Показаны подтверждённые факторы матча.")
    else:
        lines.append("Разбор этого матча пока не сформирован.")
    lines.extend(["", "━━━━━━━━━━━━━━"])
    if statistics:
        updated = datetime.fromtimestamp(statistics["updated_at"], UTC).astimezone(MOSCOW).strftime("%d.%m %H:%M")
        lines.append(f"PandaScore, {updated} МСК")
        if statistics.get("ranking_source"):
            lines.append("Рейтинг и топ-10: Valve VRS")
    lines.append("Не является финансовым советом. 18+")
    return prevent_auto_links("\n".join(lines))


def render_offers(match, favorite=None):
    match = canonical_match(match)
    favorite = favorite if favorite in ("team1", "team2") else None
    first, second = escape(match["team1"]), escape(match["team2"])
    lines = [f"<b>{first} vs {second}</b>", "\n<b>Коэффициенты на победителя матча</b>"]
    if favorite in ("team1", "team2"):
        side = "p1" if favorite == "team1" else "p2"
        available = [o for o in match["offers"] if o[side]]
        lines.append(f"\nФаворит AI: <b>{escape(match[favorite])}</b>")
        if available:
            best_price = max(o[side] for o in available)
            names = ", ".join(
                bookmaker_name(o["bookmaker"], BOOKMAKERS[o["bookmaker"]]) for o in available if o[side] == best_price
            )
            lines.append(f"Лучший коэффициент на фаворита: <b>{best_price:.2f}</b> — {names}.")
        else:
            lines.append("Ставки на фаворита сейчас закрыты во всех показанных источниках.")
        lines.append("")
    else:
        lines.append("\nНет актуального фаворита AI — сравнение обоих исходов.\n")
    for offer in match["offers"]:
        p1 = f"{offer['p1']:.2f}" if offer["p1"] else "закрыт"
        p2 = f"{offer['p2']:.2f}" if offer["p2"] else "закрыт"
        updated = date(offer["fetched_at"]).astimezone(MOSCOW).strftime("%H:%M")
        name = bookmaker_name(offer["bookmaker"], BOOKMAKERS[offer["bookmaker"]])
        lines.append(f"{name}: П1 {p1} · П2 {p2} <i>({updated} МСК)</i>")
    sides = () if favorite else (("p1", first), ("p2", second))
    for side, team in sides:
        available = [o for o in match["offers"] if o[side]]
        if available:
            best = max(available, key=lambda o: o[side])
            lines.append(f"\nЛучший на {team}: <b>{best[side]:.2f}</b> — {BOOKMAKERS[best['bookmaker']]}.")
    lines.append("\nСравнение только среди показанных источников. Коэффициент на сайте мог измениться.")
    lines.append("\n" + DISCLAIMER)
    return prevent_auto_links("\n".join(lines))


def without_links(text):
    # Сохраняем подпись ссылки, удаляем адрес и внешнюю разметку AI.
    text = re.sub(r"<a\b[^>]*>(.*?)</a\s*>", r"\1", text, flags=re.I | re.S)
    text = re.sub(r"\[([^]\n]+)\]\([^\n)]*\)", r"\1", text)
    text = re.sub(r"(?:https?://|www\.)[^\s<>]+", "", text, flags=re.I)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def prevent_auto_links(text):
    # Telegram автоматически превращает Virtus.pro / BC.Game и домены в ссылки.
    # WORD JOINER не виден и разрывает распознавание домена, сохраняя название.
    return re.sub(r"(?<=[^\W\d_])\.(?=[^\W\d_])", ".\u2060", text)
