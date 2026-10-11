import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from config import Settings
from services.ai import Analyst, Commentary, checked_commentary, facts, request_body
from services.analysis import render


@pytest.fixture(autouse=True)
def statistics(monkeypatch):
    monkeypatch.setattr(
        "services.ai.Statistics.describe",
        AsyncMock(
            return_value={
                "updated_at": 1791200880,
                "team1": {"games": 5, "wins": 3, "tournament_games": 2, "tournament_wins": 1},
                "team2": {"games": 5, "wins": 3, "tournament_games": 2, "tournament_wins": 1},
                "head_to_head": {"games": 1, "team1_wins": 1},
            }
        ),
    )


@pytest.fixture
def match():
    return dict(
        id="a",
        sport="cs2",
        team1="<NAVI>",
        team2="Spirit",
        tournament="ESL",
        status="prematch",
        starts_at="2026-10-05T10:00:00+00:00",
        offers=[dict(bookmaker="pari", p1=2.5, p2=1.5, url="https://pari.ru/", fetched_at="2026-10-04T20:00:00+00:00")],
    )


def settings():
    return Settings(_env_file=None, telegram_bot_token="123:abc", ai_api_key=SecretStr("test-key"), ai_daily_limit=2)


async def test_cache_reused_after_restart_and_refresh(tmp_path, monkeypatch, match):
    answer = json.dumps(dict(summary="Матч близкий, явного перевеса нет.", winner="unclear", probability=None))
    post = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": answer}}]},
            request=httpx.Request("POST", "https://openrouter.ai/"),
        )
    )
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    path = tmp_path / "analysis.json"
    result = await Analyst(settings(), path).explain(match)
    match["offers"][0]["fetched_at"] = "2026-10-04T20:15:00+00:00"
    assert await Analyst(settings(), path).explain(match) == result
    assert post.await_count == 1
    assert "&lt;NAVI&gt;" in render(match, result)
    assert len(render(match, result)) < 4096


async def test_missing_statistics_never_sent_to_model(tmp_path, monkeypatch, match):
    monkeypatch.setattr("services.ai.Statistics.describe", AsyncMock(return_value=None))
    post = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert await Analyst(settings(), tmp_path / "analysis.json").explain(match) is None
    post.assert_not_awaited()


async def test_provider_error_falls_back_and_backs_off(tmp_path, monkeypatch, match):
    post = AsyncMock(return_value=httpx.Response(429, request=httpx.Request("POST", "https://openrouter.ai/")))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    analyst = Analyst(settings(), tmp_path / "analysis.json")
    assert await analyst.explain(match) is None
    assert await analyst.explain(match) is None
    assert post.await_count == 1
    assert "AI-разбор сейчас недоступен" in render(match)


async def test_daily_request_limit_persists(tmp_path, monkeypatch, match):
    config = settings()
    config.ai_daily_limit = 0
    post = AsyncMock()
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    assert await Analyst(config, tmp_path / "analysis.json").explain(match) is None
    post.assert_not_awaited()


def test_facts_do_not_include_bookmaker_prices(match):
    original = facts(match)
    match["offers"][0]["p1"] = 10
    match["offers"][0]["p2"] = None
    assert facts(match) == original
    assert "offers" not in original and "market_probability_team1" not in original


def test_recent_results_reach_model_without_inventing_season_data(match):
    recent = [{"date": "2026-10-04T10:00:00+00:00", "won": True, "opponent": "Spirit"}]
    match["statistics"] = {"team1": {"games": 1, "wins": 1, "recent": recent}}
    payload = facts(match)
    assert payload["statistics"]["team1"]["recent"] == recent
    assert "season" not in payload["statistics"]["team1"]
    assert payload["recent_form"]["edge"] == "unknown"


def test_recent_comparison_uses_win_rates_and_is_separate_from_top10(match):
    match["statistics"] = {
        "team1": {"games": 5, "wins": 4},
        "team2": {"games": 3, "wins": 3},
    }
    payload = facts(match)
    assert payload["recent_form"]["edge"] == "team2"
    assert payload["recent_form"]["team1"] == {"wins": 4, "games": 5}
    assert payload["factors"][0]["edge"] == "unknown"


def test_conflicting_evidence_cannot_be_rendered_as_equal_form(match):
    match["statistics"] = {
        "team1": {"games": 5, "wins": 4, "tournament_games": 4, "tournament_wins": 1},
        "team2": {"games": 5, "wins": 2, "tournament_games": 4, "tournament_wins": 3},
    }
    wrong = Commentary(summary="Общая форма равна, Spirit сильнее.", winner="team2", probability=65)
    corrected = checked_commentary(match, wrong)
    assert corrected.winner == "unclear" and corrected.probability is None
    assert "4/5" in corrected.summary and "2/5" in corrected.summary
    assert "3/4" in corrected.summary and "1/4" in corrected.summary
    assert "равна" not in corrected.summary
    # Дополнительный независимый аргумент позволяет модели оценить баланс.
    match["statistics"]["team1"]["top10"] = {"games": 5, "wins": 1}
    match["statistics"]["team2"]["top10"] = {"games": 5, "wins": 4}
    assert checked_commentary(match, wrong) == wrong


async def test_configured_temperature_reaches_provider(tmp_path, monkeypatch, match):
    config = settings()
    config.ai_temperature = 0.65
    post = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                dict(
                                    summary="Последние результаты равны, явного фаворита нет.",
                                    winner="unclear",
                                    probability=None,
                                )
                            )
                        }
                    }
                ]
            },
            request=httpx.Request("POST", "https://openrouter.ai/"),
        )
    )
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    await Analyst(config, tmp_path / "analysis.json").explain(match)
    assert post.call_args.kwargs["json"]["temperature"] == 0.65
    assert request_body(config.ai_model, match)["temperature"] == 0.35


async def test_closed_market_can_be_analyzed_with_statistics(tmp_path, monkeypatch, match):
    match["offers"][0]["p1"] = None
    answer = json.dumps(dict(summary="Небольшой перевес у Spirit.", winner="team2", probability=58))
    post = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": answer}}]},
            request=httpx.Request("POST", "https://openrouter.ai/"),
        )
    )
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    result = await Analyst(settings(), tmp_path / "analysis.json").explain(match)
    assert result.probability == 58
    assert "Победа Spirit — 58%" in render(match, result)


@pytest.mark.parametrize(
    "winner,probability", [("team1", None), ("team2", 49), ("team1", 101), ("unclear", 58), ("team1", 58.5)]
)
def test_invalid_probability_rejected(winner, probability):
    from pydantic import ValidationError

    from services.ai import Commentary

    with pytest.raises(ValidationError):
        Commentary(summary="Оценка", winner=winner, probability=probability)
