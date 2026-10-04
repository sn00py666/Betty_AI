"""Проверяемый разбор коэффициентов. Не выдаём рыночную вероятность за AI-модель."""

from html import escape

from content import DISCLAIMER
from services.matches import BOOKMAKERS, MOSCOW, date, market_probability, start_label


def render(match, commentary=None):
    first, second = escape(match["team1"]), escape(match["team2"])
    lines = [f"<b>{first} vs {second}</b>", f"{escape(match['tournament'])} · {start_label(match)}", "━━━━━━━━━━━━━━"]
    probability = market_probability(match)
    if probability is None:
        lines.append("Рынок победителя сейчас закрыт. Оценить фаворита по доступным котировкам нельзя.")
    elif abs(probability - 0.5) < 0.01:
        lines.append("<b>Рыночная оценка:</b> шансы команд примерно равны.")
    else:
        favorite = first if probability > 0.5 else second
        chance = max(probability, 1 - probability)
        lines.append(f"<b>Фаворит по коэффициентам:</b> {favorite} · около {chance:.0%}.")
    lines.append("Это оценка из коэффициентов без маржи, не независимый AI-прогноз и не гарантия результата.")
    if commentary:
        lines.extend(["\n<b>Комментарий AI</b>", escape(commentary.summary), escape(commentary.risk)])
    else:
        lines.append("AI-комментарий сейчас недоступен; ниже — проверяемое сравнение котировок.")
    lines.append("\n<b>Коэффициенты на победителя матча</b>")
    for offer in match["offers"]:
        p1 = f"{offer['p1']:.2f}" if offer["p1"] else "закрыт"
        p2 = f"{offer['p2']:.2f}" if offer["p2"] else "закрыт"
        updated = date(offer["fetched_at"]).astimezone(MOSCOW).strftime("%H:%M")
        lines.append(f"{BOOKMAKERS[offer['bookmaker']]}: П1 {p1} · П2 {p2} <i>({updated} МСК)</i>")
    for side, team in (("p1", first), ("p2", second)):
        available = [o for o in match["offers"] if o[side]]
        if available:
            best = max(available, key=lambda o: o[side])
            lines.append(f"\nЛучший на {team}: <b>{best[side]:.2f}</b> — {BOOKMAKERS[best['bookmaker']]}.")
    lines.append("\nСравнение только среди показанных источников. Коэффициент на сайте мог измениться.")
    lines.append("\n" + DISCLAIMER)
    return "\n".join(lines)
