"""Факторы прогноза: только сравнения подтверждённых показателей."""


def wins_label(count, *, each=False):
    forms = {1: "победе" if each else "победа", 2: "победы", 3: "победы", 4: "победы"}
    word = "побед" if 11 <= count % 100 <= 14 else forms.get(count % 10, "побед")
    return f"{count} {word}"


def factors(match, *, compact=False):
    stats = match.get("statistics") or {}
    first, second = stats.get("team1", {}), stats.get("team2", {})
    a, b = match["team1"], match["team2"]
    result = []

    def add(label, text, left=None, right=None, *, values=None, display_label=None, equal_text=None, compact_text=None):
        edge = (
            "unknown"
            if left is None or right is None
            else ("team1" if left > right else "team2" if right > left else "neutral")
        )
        if compact and values is not None:
            first_value, second_value = reversed(values) if edge == "team2" else values
            comparison = equal_text if equal_text and values[0] == values[1] else f"{first_value} против {second_value}"
            text = f"{display_label or label}: {comparison}"
        elif compact and compact_text is not None:
            text = f"{display_label or label}: {compact_text}"
        result.append(dict(label=label, text=text, edge=edge))

    games_a, games_b = first.get("games", 0), second.get("games", 0)
    if games_a >= 3 and games_b >= 3:
        wins_a, wins_b = first["wins"], second["wins"]
        recent_text = (
            f"по {wins_label(wins_a, each=True)}" if wins_a == wins_b
            else f"{wins_label(max(wins_a, wins_b))} против {min(wins_a, wins_b)}"
        )
        add(
            "Последние игры",
            f"побед: {a} — {first['wins']}/{games_a}, {b} — {second['wins']}/{games_b}",
            first["wins"] / games_a,
            second["wins"] / games_b,
            values=None if games_a == games_b else (
                f"{first['wins']} из {games_a}", f"{second['wins']} из {games_b}"
            ),
            display_label=(
                f"Последние {games_a} {'игры' if games_a in (3, 4) else 'игр'}"
                if games_a == games_b else "Победы в последних играх"
            ),
            compact_text=recent_text,
        )

    hltv = stats.get("hltv", {}).get("teams", {})
    ranks = [hltv.get(side, {}).get("rankings", {}).get("hltv") for side in ("team1", "team2")]
    if all(ranks) and ranks[0]["url"].split("/details/")[0] == ranks[1]["url"].split("/details/")[0]:
        add(
            "Рейтинг HLTV",
            f"{a} — №{ranks[0]['rank']}, {b} — №{ranks[1]['rank']}",
            -ranks[0]["rank"],
            -ranks[1]["rank"],
            values=(f"№{ranks[0]['rank']}", f"№{ranks[1]['rank']}"),
        )

    top_a, top_b = first.get("top10", {}), second.get("top10", {})
    rank_a, rank_b = first.get("valve_rank"), second.get("valve_rank")
    if rank_a and rank_b and first.get("valve_date") == second.get("valve_date"):
        add(
            "Рейтинг Valve",
            f"{a} — №{rank_a['rank']}, {b} — №{rank_b['rank']} ({first['valve_date']})",
            rank_a["points"],
            rank_b["points"],
            values=(f"№{rank_a['rank']}", f"№{rank_b['rank']}"),
        )
    if top_a.get("games") and top_b.get("games"):
        add(
            "Форма против топов",
            f"против топ-10{' Valve' if top_a.get('ranking') == top_b.get('ranking') == 'Valve VRS' else ''}: "
            f"{a} — {top_a['wins']}/{top_a['games']}, {b} — {top_b['wins']}/{top_b['games']}",
            top_a["wins"] / top_a["games"],
            top_b["wins"] / top_b["games"],
            values=(f"{top_a['wins']} из {top_a['games']}", f"{top_b['wins']} из {top_b['games']}"),
            display_label="Победы над топ-10",
            equal_text=f"по {top_a['wins']} из {top_a['games']}",
        )
    else:
        add("Форма против топов", "нет данных о матчах против топ-10")

    if match["sport"] == "cs2":
        margin_a, margin_b = first.get("winning_round_margin"), second.get("winning_round_margin")
        if margin_a is not None and margin_b is not None:
            add(
                "Разрыв в счёте",
                f"в среднем в выигранных картах: {a} +{margin_a:g} раунда, {b} +{margin_b:g}",
                margin_a,
                margin_b,
                values=(f"+{margin_a:g}", f"+{margin_b:g}"),
                display_label="Средний отрыв в выигранных картах (раунды)",
                equal_text=f"по +{margin_a:g}",
            )
        else:
            add("Разрыв в счёте", "нет раундовой статистики")
        maps_a, maps_b = first.get("active_maps"), second.get("active_maps")
        if maps_a is not None and maps_b is not None:
            add(
                "Пул карт",
                f"активных карт: {a} — {len(set(maps_a))}, {b} — {len(set(maps_b))}",
                len(set(maps_a)),
                len(set(maps_b)),
                values=(len(set(maps_a)), len(set(maps_b))),
                display_label="Активных карт",
                equal_text=f"по {len(set(maps_a))}",
            )
        else:
            add("Пул карт", "нет данных о маппуле")

    games_a, games_b = first.get("tournament_games"), second.get("tournament_games")
    if games_a and games_b:
        wins_a, wins_b = first["tournament_wins"], second["tournament_wins"]
        record_a, record_b = f"{wins_a}-{games_a - wins_a}", f"{wins_b}-{games_b - wins_b}"
        text = f"обе идут со счётом {record_a}" if record_a == record_b else f"{a} — {record_a}, {b} — {record_b}"
        add(
            "На турнире", text, wins_a / games_a, wins_b / games_b,
            values=(record_a.replace("-", ":"), record_b.replace("-", ":")),
            equal_text=f"у обеих {record_a.replace('-', ':')}",
        )
    else:
        add("На турнире", "нет результатов текущего этапа")

    h2h = stats.get("head_to_head") or {}
    games = h2h.get("games", 0)
    if games:
        wins = h2h["team1_wins"]
        text = f"{wins}-{games - wins} ({a} / {b})"
        if games < 3:
            add(
                "Личные встречи", text + ", слишком мало для выводов", 0, 0,
                compact_text=f"{wins}:{games - wins}" + (", мало матчей" if wins != games - wins else ""),
            )
        else:
            add(
                "Личные встречи", text, wins, games - wins,
                compact_text=f"{max(wins, games - wins)}:{min(wins, games - wins)}",
            )
    else:
        add("Личные встречи", "нет данных об очных встречах")
    return result


def factor_summary(items, winner, teams):
    parts = []
    # Сначала факторы выбранного фаворита, затем противоположной команды.
    sides = (winner, "team2" if winner == "team1" else "team1") if winner != "unclear" else ("team1", "team2")
    for side in sides:
        count = sum(item["edge"] == side for item in items)
        if count:
            parts.append(
                f"{count} {'фактор' if count == 1 else 'фактора' if count < 5 else 'факторов'} за {teams[side]}"
            )
    neutral = sum(item["edge"] == "neutral" for item in items)
    if neutral:
        parts.append(f"{neutral} {'нейтральный' if neutral == 1 else 'нейтральных'}")
    return ", ".join(parts)
