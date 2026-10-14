import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from test_matches import NOW, snapshot

from config import AISettings
from services.health import read, write
from services.matches import Matches
from services.team_aliases import VERSION, alias_key, load_aliases
from services.team_matching import TeamMatcher, candidates


@pytest.fixture
def settings():
    return SimpleNamespace(
        ai_api_key=SecretStr("test-key"),
        ai_base_url="https://example.test/v1",
        ai_model="test-model",
    )


def client(relation="same_team", evidence="abbreviation", *, content=None, status=200, headers=None):
    value = (
        content
        if content is not None
        else json.dumps(dict(relation=relation, evidence=evidence, reason="Вариант имени"))
    )
    response = httpx.Response(
        status,
        json={"choices": [{"message": {"content": value}}]},
        headers=headers,
        request=httpx.Request("POST", "https://example.test/v1/chat/completions"),
    )
    return SimpleNamespace(post=AsyncMock(return_value=response))


def pair(folder, **changes):
    odds = folder / "odds"
    snapshot(odds, "leon", team1="Eastern Dragons", team2="Opponent")
    snapshot(odds, "winline", **(dict(team1="Opponent", team2="EDG", p1=1.8, p2=2.2) | changes))
    return Matches(odds)


async def test_learns_alias_then_bot_merges_correct_sides_without_network(tmp_path, settings):
    matches = pair(tmp_path)
    assert len(matches.read(NOW)) == 2
    provider = client()
    state = await TeamMatcher(settings, tmp_path).scan(provider, NOW.timestamp())
    assert state["accepted"] == 1 and state["unresolved"] == 0 and state["resolved"] == 1
    rows = Matches(tmp_path / "odds").read(NOW)
    assert len(rows) == 1
    assert (rows[0]["team1"], rows[0]["team2"]) == ("Eastern Dragons", "Opponent")
    assert [(o["p1"], o["p2"]) for o in rows[0]["offers"]] == [(2.5, 1.5), (2.2, 1.8)]
    # Перезапуск сборщика и изменение коэффициентов не требуют повторной оплаты решения.
    pair(tmp_path, p1=1.9)
    await TeamMatcher(settings, tmp_path).scan(provider, NOW.timestamp() + 1)
    assert provider.post.await_count == 1
    payload = provider.post.call_args.kwargs["json"]
    assert "p1" not in payload["messages"][1]["content"]
    assert payload["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"sport": "dota2"},
        {"status": "live"},
        {"starts_at": None},
        {"best_of": 5},
        {"starts_at": (NOW + timedelta(days=1)).isoformat()},
        {"team1": "Other opponent"},
        {"team2": "EDG Academy"},
        {"team2": "EDG Junior"},
        {"team2": "EDG Women"},
        {"team2": "EDG II"},
        {"team2": "ex-EDG"},
    ],
)
async def test_invalid_context_never_reaches_llm_even_with_positive_cached_alias(tmp_path, settings, changes):
    matches = pair(tmp_path, **changes)
    key = alias_key("cs2", "Eastern Dragons", changes.get("team2", "EDG"))
    write(
        tmp_path / "team_aliases.json",
        {
            "version": VERSION,
            "decisions": {
                key: dict(relation="same_team", evidence="known_alias", expires_at=NOW.timestamp() + 1000),
            },
        },
    )
    provider = client()
    await TeamMatcher(settings, tmp_path).scan(provider, NOW.timestamp())
    provider.post.assert_not_awaited()
    assert len(matches.read(NOW)) == 2


@pytest.mark.parametrize(
    "relation,evidence,unresolved",
    [
        ("different_team", "none", 0),
        ("uncertain", "none", 1),
        ("same_team", "none", 1),
    ],
)
async def test_uncertain_or_rejected_names_are_cached_without_merging(
    tmp_path, settings, relation, evidence, unresolved
):
    matches = pair(tmp_path)
    provider = client(relation, evidence)
    first = await TeamMatcher(settings, tmp_path).scan(provider, NOW.timestamp())
    await TeamMatcher(settings, tmp_path).scan(provider, NOW.timestamp() + 1)
    assert first["unresolved"] == unresolved
    assert len(matches.read(NOW)) == 2
    assert provider.post.await_count == 1


async def test_ambiguous_candidates_remain_separate_after_llm_accepts_names(tmp_path, settings):
    matches = pair(tmp_path)
    path = tmp_path / "odds" / "winline" / "latest.json"
    data = json.loads(path.read_text())
    data["matches"].append(dict(data["matches"][0], id="different-series"))
    path.write_text(json.dumps(data))
    state = await TeamMatcher(settings, tmp_path).scan(client(), NOW.timestamp())
    assert len(matches.read(NOW)) == 3
    assert state["unresolved"] > 0


async def test_unknown_live_time_requires_accepted_names_and_unique_sources(tmp_path, settings):
    odds = tmp_path / "odds"
    snapshot(odds, "leon", team1="Eastern Dragons", team2="Opponent", status="live", starts_at=None)
    snapshot(odds, "winline", team1="EDG", team2="Opponent", status="live", starts_at=None)
    provider = client()
    state = await TeamMatcher(settings, tmp_path).scan(provider, NOW.timestamp())
    provider.post.assert_awaited_once()
    assert state["unresolved"] == 0
    assert len(Matches(odds).read(NOW)) == 1
    data = json.loads((odds / "winline" / "latest.json").read_text())
    data["matches"].append(dict(data["matches"][0], id="another-live"))
    (odds / "winline" / "latest.json").write_text(json.dumps(data))
    assert len(Matches(odds).read(NOW)) == 3


@pytest.mark.parametrize("provider", [client(content="not json"), client(status=429, headers={"Retry-After": "120"})])
async def test_provider_error_preserves_matches_and_persistent_backoff(tmp_path, settings, provider):
    matches = pair(tmp_path)
    state = await TeamMatcher(settings, tmp_path).scan(provider, NOW.timestamp())
    assert state["error"] and state["retry_at"] >= NOW.timestamp() + 60
    await TeamMatcher(settings, tmp_path).scan(provider, NOW.timestamp() + 1)
    assert provider.post.await_count == 1
    assert len(matches.read(NOW)) == 2
    assert not load_aliases(tmp_path / "team_aliases.json", NOW.timestamp())


async def test_expired_alias_is_revalidated_and_missing_key_still_reports_candidates(tmp_path, settings):
    matches = pair(tmp_path)
    provider = client()
    await TeamMatcher(settings, tmp_path).scan(provider, NOW.timestamp())
    data = read(tmp_path / "team_aliases.json")
    for row in data["decisions"].values():
        row["expires_at"] = NOW.timestamp()
    write(tmp_path / "team_aliases.json", data)
    assert len(matches.read(NOW)) == 2
    settings.ai_api_key = None
    state = await TeamMatcher(settings, tmp_path).scan(provider, NOW.timestamp())
    assert not state["configured"] and state["pending"] == 1 and state["unresolved"] == 1
    assert provider.post.await_count == 1
    settings.ai_api_key = SecretStr("test-key")
    await TeamMatcher(settings, tmp_path).scan(provider, NOW.timestamp())
    assert provider.post.await_count == 2


def test_corrupt_alias_cache_does_not_break_match_list(tmp_path):
    matches = pair(tmp_path)
    for value in ("not json", "[]", '{"version":1,"decisions":[]}'):
        (tmp_path / "team_aliases.json").write_text(value)
        assert len(matches.read(NOW)) == 2


def test_candidate_cache_is_sport_specific(tmp_path):
    matches = pair(tmp_path)
    candidate = candidates(matches.read(NOW))[0]
    assert candidate["key"] != alias_key("dota2", *candidate["names"])


def test_collector_ai_settings_do_not_require_telegram_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    settings = AISettings(_env_file=None, ai_api_key="test-key")
    assert settings.team_matching_enabled
