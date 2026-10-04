"""Проверяемый разбор коэффициентов. Не выдаём рыночную вероятность за AI-модель."""

from datetime import UTC, datetime
from html import escape

from content import DISCLAIMER
from services.matches import BOOKMAKERS, MOSCOW, date, market_probability, start_label


def statistics_text(match):
    stats = match["statistics"]
    paragraphs = []
    for side in ("team1", "team2"):
        team, form = escape(match[side]), stats[side]
        recent = form["recent"][0]
        result = "победа над" if recent["won"] else "поражение от"
        paragraphs.append(
            f"{team}: побед {form['wins']} из {form['games']} последних доступных игр. "
            f"Последняя игра — {result} {escape(recent['opponent'])}."
        )
    h2h = stats["head_to_head"]
    if h2h["games"]:
        score = f"{h2h['team1_wins']}:{h2h['games'] - h2h['team1_wins']}"
        record = f"{score} по победам ({escape(match['team1'])} / {escape(match['team2'])})"
        meetings = f"В доступной выборке за 180 дней очные встречи — {record}."
    else:
        meetings = "В доступной выборке за 180 дней очных встреч не найдено."
    tournament = []
    for side in ("team1", "team2"):
        form = stats[side]
        if form["tournament_games"] is not None:
            tournament.append(f"{escape(match[side])} — побед {form['tournament_wins']} из {form['tournament_games']}")
    if tournament:
        meetings += " На текущем этапе турнира: " + "; ".join(tournament) + "."
    return " ".join(paragraphs) + "\n\n" + meetings


def render(match, commentary=None):
    first, second = escape(match["team1"]), escape(match["team2"])
    lines = [f"<b>{first} vs {second}</b>", f"{escape(match['tournament'])} · {start_label(match)}", "━━━━━━━━━━━━━━"]
    probability = market_probability(match)
    if match.get("statistics"):
        lines.append("\n" + statistics_text(match))
    if commentary:
        lines.append("\n" + escape(commentary.summary))
    elif probability is None:
        lines.append("\nРынок победителя сейчас закрыт. Данных для оценки исхода недостаточно.")
    else:
        lines.append("\nAI-разбор сейчас недоступен. Сравнение актуальных котировок — ниже.")
    statistics = match.get("statistics")
    if commentary and statistics:
        outcome = (
            f"победа {first if commentary.winner == 'team1' else second}"
            if commentary.winner != "unclear"
            else "явного фаворита нет"
        )
    elif probability is None:
        outcome = "недостаточно данных"
    elif abs(probability - 0.5) < 0.01:
        outcome = "явного фаворита нет"
    else:
        favorite = first if probability > 0.5 else second
        outcome = f"победа {favorite}"
    label = "Вероятный исход" if commentary and statistics else "Вероятный исход по текущей линии"
    lines.append(f"\n<b>{label}:</b> {outcome}.")
    if statistics:
        updated = datetime.fromtimestamp(statistics["updated_at"], UTC).astimezone(MOSCOW).strftime("%d.%m %H:%M")
        lines.append(f"\n<i>Статистика: PandaScore, {updated} МСК. Изменения составов не проверены.</i>")
    else:
        lines.append("\n<i>Основа разбора — котировки. Статистика команд недоступна.</i>")
    lines.append("\n<i>Не является финансовым советом. 18+</i>")
    return "\n".join(lines)


def render_offers(match):
    first, second = escape(match["team1"]), escape(match["team2"])
    lines = [f"<b>{first} vs {second}</b>", "\n<b>Коэффициенты на победителя матча</b>"]
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
