import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from config import Settings
from services.ai import Analyst
from services.analysis import render


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
    answer = json.dumps(dict(summary="Рынок выделяет Spirit."))
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


async def test_closed_market_never_sent_to_model(tmp_path, monkeypatch, match):
    match["offers"][0]["p1"] = None
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
