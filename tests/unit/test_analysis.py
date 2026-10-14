from copy import deepcopy

import pytest

from services.ai import Commentary
from services.analysis import render, render_offers


@pytest.fixture
def match():
    return {
        "sport": "cs2",
        "team1": "Aurora",
        "team2": "BB Team",
        "tournament": "Counter-Strike. ESL Pro League. Групповой этап. Bo3",
        "best_of": 3,
        "status": "prematch",
        "starts_at": "2026-10-09T16:30:00+00:00",
        "offers": [{"bookmaker": "pari", "p1": 1.5, "p2": 2.5, "fetched_at": "2026-10-09T12:00:00+00:00"}],
        "statistics": {
            "updated_at": 1791200880,
            "team1": {
                "country": "TR",
                "top10": {"games": 5, "wins": 3},
                "winning_round_margin": 3,
                "active_maps": ["Ancient", "Mirage", "Nuke"],
                "tournament_games": 2,
                "tournament_wins": 1,
            },
            "team2": {
                "country": "RU",
                "top10": {"games": 5, "wins": 2},
                "winning_round_margin": 1,
                "active_maps": ["Ancient", "Mirage"],
                "tournament_games": 2,
                "tournament_wins": 1,
            },
            "head_to_head": {"games": 1, "team1_wins": 1},
        },
    }


def test_requested_card_layout(match):
    text = render(match, Commentary(summary="Перевес Aurora небольшой, матч близкий.", winner="team1", probability=58))
    assert text.startswith('<b><tg-emoji emoji-id="5291987642450228345">🇹🇷</tg-emoji> Aurora vs ')
    assert '<tg-emoji emoji-id="5298896909094657296">🇷🇺</tg-emoji> BETBOOM</b>' in text
    assert "ESL Pro League · Групповой этап. Bo3" in text
    assert "<b>Победа Aurora — 58%</b>\n<i>3 фактора за Aurora, 2 нейтральных</i>" in text
    assert text.index("Победа Aurora") < text.index("Форма против топов") < text.index("Перевес Aurora")
    assert "На турнире — обе идут со счётом 1-1" in text
    assert "Личные встречи — 1-0 (Aurora / BETBOOM), слишком мало для выводов" in text
    assert text.endswith("МСК\nНе является финансовым советом. 18+")
    for phrase in ("Counter-Strike", "Изменения составов", "доступной выборке", "котиров", "Вероятный исход"):
        assert phrase not in text
    assert text.count("━━━━━━━━━━━━━━") == 2
    assert len(text) < 4096


def test_unknown_factors_are_not_neutral(match):
    for side in ("team1", "team2"):
        for key in ("top10", "winning_round_margin", "active_maps", "country"):
            match["statistics"][side].pop(key)
    text = render(match, Commentary(summary="Матч близкий.", winner="team1", probability=52))
    assert "2 нейтральных" in text
    assert "без данных" not in text and "нет данных" not in text
    assert "Разрыв в счёте" not in text and "Пул карт" not in text
    assert 'emoji-id="5291987642450228345"' in text  # Логотип не зависит от доступности страны в статистике.
    assert "+3" not in text


def test_counts_factors_for_both_teams(match):
    match["statistics"]["team2"]["winning_round_margin"] = 4
    text = render(match, Commentary(summary="Матч близкий.", winner="team1", probability=55))
    assert "2 фактора за Aurora, 1 фактор за BETBOOM, 2 нейтральных" in text


def test_no_statistics_has_no_market_prediction(match):
    match.pop("statistics")
    text = render(match)
    assert "Прогноз недоступен" in text
    assert "%" not in text and "Победа Aurora" not in text
    assert "Разбор этого матча пока не сформирован" in text
    assert "<i></i>" not in text and "без данных" not in text


def test_sparse_card_shows_actual_reason_for_prediction(match):
    match["statistics"]["team1"] = {"games": 5, "wins": 4}
    match["statistics"]["team2"] = {"games": 5, "wins": 3}
    text = render(match, Commentary(summary="Последние результаты Aurora лучше.", winner="team1", probability=55))
    assert "1 фактор за Aurora, 1 нейтральный" in text
    assert "Последние игры — побед: Aurora — 4/5, BETBOOM — 3/5" in text
    assert "Форма против топов" not in text
    assert "На турнире" not in text
    assert "нет данных" not in text and "без данных" not in text


def test_escaping_and_separate_bookmaker_offers(match):
    match["team1"] = "<Aurora>"
    text = render(match, Commentary(summary="Перевес <Aurora> небольшой.", winner="team1", probability=58))
    assert "<Aurora>" not in text
    assert "&lt;Aurora&gt;" in text
    assert "PARI" not in text
    assert "PARI: П1 1.50 • П2 2.50" in render_offers(match)


def test_dota_does_not_use_rounds_or_map_pool(match):
    match["sport"] = "dota2"
    text = render(match)
    assert "Разрыв в счёте" not in text and "Пул карт" not in text


def test_no_duplicate_bo_and_independent_of_odds(match):
    first = render(match)
    changed = deepcopy(match)
    changed["offers"][0]["p1"] = 10
    assert render(changed) == first
    assert first.count("Bo3") == 1


def test_report_removes_ai_links_and_domain_autolinks(match):
    match["team1"] = "Virtus.pro"
    match["team2"] = "BC.Game"
    commentary = Commentary(
        summary='[Virtus.pro](https://example.com/source) сильнее. <a href="https://example.com">BC.Game</a> '
        "могут навязать борьбу. https://example.com www.example.org",
        winner="team1",
        probability=58,
    )
    text = render(match, commentary)
    assert "https://" not in text and "www." not in text and "href" not in text
    assert "[Virtus" not in text and "&lt;a" not in text
    assert "Virtus.\u2060pro" in text and "BC.\u2060Game" in text


def test_ai_commentary_uses_canonical_team_names(match):
    match["team1"] = "Natus Vincere"
    text = render(match, Commentary(summary="Natus Vincere сильнее BB Team.", winner="team1", probability=58))
    assert "Победа NAVI — 58%" in text
    assert "NAVI сильнее BETBOOM." in text
    assert "Natus Vincere" not in text and "BB Team" not in text
