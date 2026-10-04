from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
from pydantic import SecretStr

from config import Settings
from services.statistics import Statistics, compact, form


def config(limit=100):
    return Settings(
        _env_file=None, telegram_bot_token="123:abc", pandascore_api_key=SecretStr("test"), pandascore_daily_limit=limit
    )


def record(now, upcoming=False):
    start = now + timedelta(days=1) if upcoming else now - timedelta(days=1)
    return dict(
        id=10,
        begin_at=start.isoformat(),
        end_at=None if upcoming else now.isoformat(),
        status="not_started" if upcoming else "finished",
        winner_id=None if upcoming else 1,
        forfeit=False,
        results=[],
        tournament_id=9,
        serie_id=8,
        videogame={"slug": "cs-go"},
        opponents=[{"opponent": {"id": i, "name": name}} for i, name in [(1, "A"), (2, "B")]],
    )


def match(now):
    return dict(sport="cs2", team1="A", team2="B", starts_at=(now + timedelta(days=1)).isoformat())


async def test_cache_shared_across_matches_and_restarts(tmp_path, monkeypatch):
    now = datetime.now(UTC)

    async def reply(self, url, **kwargs):
        return httpx.Response(200, json=[record(now, url.endswith("upcoming"))], request=httpx.Request("GET", url))

    get = AsyncMock(side_effect=reply)
    monkeypatch.setattr(httpx.AsyncClient, "get", lambda self, url, **kw: get(self, url, **kw))
    path = tmp_path / "panda.json"
    result = await Statistics(config(), path).describe(match(now))
    assert result["team1"]["wins"] == 1 and result["team2"]["wins"] == 0
    assert result["head_to_head"]["games"] == 1
    assert get.await_count == 3
    again = await Statistics(config(), path).describe(match(now))
    assert again == result and get.await_count == 3
    # Один просроченный ответ обновляется, остальные переиспользуются.
    cache = Statistics(config(), path)
    cache.state["cache"]["/teams/1/matches"]["fetched_at"] -= 7 * 3600
    cache.save()
    await Statistics(config(), path).describe(match(now))
    assert get.await_count == 4


async def test_daily_budget_survives_restart(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    get = AsyncMock(
        return_value=httpx.Response(
            200, json=[record(now, True)], request=httpx.Request("GET", "https://api.pandascore.co/")
        )
    )
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    path = tmp_path / "panda.json"
    assert await Statistics(config(1), path).describe(match(now)) is None
    assert await Statistics(config(1), path).describe(match(now)) is None
    assert get.await_count == 1


async def test_rate_limit_persists_and_prevents_retries(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    get = AsyncMock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "7200"}, request=httpx.Request("GET", "https://api.pandascore.co/")
        )
    )
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    path = tmp_path / "panda.json"
    assert await Statistics(config(), path).describe(match(now)) is None
    assert await Statistics(config(), path).describe(match(now)) is None
    assert get.await_count == 1


def test_form_excludes_walkovers_and_unfinished_results():
    now = datetime.now(UTC)
    rows = [compact(record(now)), compact(record(now, True)), compact(dict(record(now), forfeit=True))]
    result = form(rows, 1, now - timedelta(days=180), now, 9)
    assert result["games"] == 1 and result["wins"] == 1 and result["tournament_games"] == 1
