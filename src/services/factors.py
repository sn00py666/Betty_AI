"""Факторы прогноза: только сравнения подтверждённых показателей."""


def factors(match):
    stats = match.get("statistics") or {}
    first, second = stats.get("team1", {}), stats.get("team2", {})
    a, b = match["team1"], match["team2"]
    result = []

    def add(label, text, left=None, right=None):
        edge = (
            "unknown"
            if left is None or right is None
            else ("team1" if left > right else "team2" if right > left else "neutral")
        )
        result.append(dict(label=label, text=text, edge=edge))

    games_a, games_b = first.get("games", 0), second.get("games", 0)
    if games_a >= 3 and games_b >= 3:
        add(
            "Последние игры",
            f"побед: {a} — {first['wins']}/{games_a}, {b} — {second['wins']}/{games_b}",
            first["wins"] / games_a,
            second["wins"] / games_b,
        )

    top_a, top_b = first.get("top10", {}), second.get("top10", {})
    rank_a, rank_b = first.get("valve_rank"), second.get("valve_rank")
    if rank_a and rank_b and first.get("valve_date") == second.get("valve_date"):
        add(
            "Рейтинг Valve",
            f"{a} — №{rank_a['rank']}, {b} — №{rank_b['rank']} ({first['valve_date']})",
            rank_a["points"],
            rank_b["points"],
        )
    if top_a.get("games") and top_b.get("games"):
        add(
            "Форма против топов",
            f"против топ-10{' Valve' if top_a.get('ranking') == top_b.get('ranking') == 'Valve VRS' else ''}: "
            f"{a} — {top_a['wins']}/{top_a['games']}, {b} — {top_b['wins']}/{top_b['games']}",
            top_a["wins"] / top_a["games"],
            top_b["wins"] / top_b["games"],
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
            )
        else:
            add("Пул карт", "нет данных о маппуле")

    games_a, games_b = first.get("tournament_games"), second.get("tournament_games")
    if games_a and games_b:
        wins_a, wins_b = first["tournament_wins"], second["tournament_wins"]
        record_a, record_b = f"{wins_a}-{games_a - wins_a}", f"{wins_b}-{games_b - wins_b}"
        text = f"обе идут со счётом {record_a}" if record_a == record_b else f"{a} — {record_a}, {b} — {record_b}"
        add("На турнире", text, wins_a / games_a, wins_b / games_b)
    else:
        add("На турнире", "нет результатов текущего этапа")

    h2h = stats.get("head_to_head") or {}
    games = h2h.get("games", 0)
    if games:
        wins = h2h["team1_wins"]
        text = f"{wins}-{games - wins} ({a} / {b})"
        if games < 3:
            add("Личные встречи", text + ", слишком мало для выводов", 0, 0)
        else:
            add("Личные встречи", text, wins, games - wins)
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
