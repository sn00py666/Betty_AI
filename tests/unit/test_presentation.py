import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from pydantic import ValidationError

from config import Settings
from keyboards import menu
from services.ai import Commentary
from services.analysis import render_offers
from services.bonuses import ordered_offers, render_bonus
from services.discovery import sort_matches
from services.emojis import EmojiFallback, bookmaker_name, registry, team_emoji
from services.forecasts import recall, remember
from services.users import Users


@pytest.fixture
def match():
    now = datetime.now(UTC)
    return dict(
        id="test",
        sport="cs2",
        team1="Aurora",
        team2="NAVI",
        status="prematch",
        tournament="Test Bo3",
        best_of=3,
        starts_at=(now + timedelta(hours=2)).isoformat(),
        statistics={"updated_at": now.timestamp()},
        offers=[
            dict(bookmaker="pari", p1=3.0, p2=1.5, url="https://pari.ru/", fetched_at=now.isoformat()),
            dict(bookmaker="fonbet", p1=1.6, p2=2.4, url="https://fon.bet/", fetched_at=now.isoformat()),
        ],
    )


def test_popularity_mixes_live_and_prematch_by_rank(tmp_path, match):
    now = datetime.now(UTC)
    path = tmp_path / "valve.json"
    path.write_text(
        json.dumps(
            {
                "snapshots": {
                    "current": {
                        "date": now.date().isoformat(),
                        "teams": {
                            "navi": {"rank": 2},
                            "aurora": {"rank": 10},
                        },
                    }
                }
            }
        )
    )
    unknown = dict(match, id="unknown", team1="Other", team2="Unknown", starts_at=now.isoformat())
    live = dict(unknown, id="live", status="live")
    items = [unknown, match, live]
    assert [m["id"] for m in sort_matches(items, "popular", path)] == ["test", "unknown", "live"]
    assert [m["id"] for m in sort_matches(items, "time", path)] == ["live", "unknown", "test"]
    path.write_text(json.dumps({"snapshots": {"old": {"date": "2000-01-01", "teams": {"navi": {"rank": 1}}}}}))
    assert [m["id"] for m in sort_matches(items, "popular", path)] == ["unknown", "live", "test"]


def test_popularity_uses_unique_bookmakers_when_no_rank(tmp_path, match):
    other = dict(match, id="other", status="live", offers=[match["offers"][0]] * 3)
    assert sort_matches([other, match], "popular", tmp_path / "absent.json")[0]["id"] == "test"


@pytest.mark.parametrize("live_id", ["top", "middle", "unknown"])
def test_popularity_order_does_not_change_when_a_match_goes_live(tmp_path, match, live_id):
    path = tmp_path / "valve.json"
    path.write_text(
        json.dumps(
            {
                "snapshots": {
                    "current": {
                        "date": datetime.now(UTC).date().isoformat(),
                        "teams": {"navi": {"rank": 2}, "aurora": {"rank": 10}},
                    }
                }
            }
        )
    )
    items = [
        dict(match, id="unknown", team1="Other", team2="Unknown"),
        dict(match, id="middle", team1="Aurora", team2="Unknown"),
        dict(match, id="top", team1="NAVI", team2="Unknown"),
    ]
    expected = ["top", "middle", "unknown"]
    assert [m["id"] for m in sort_matches(items, "popular", path)] == expected
    for item in items:
        if item["id"] == live_id:
            item.update(status="live", starts_at=None)
    assert [m["id"] for m in sort_matches(items, "popular", path)] == expected


def test_team2_favorite_best_price_and_button_order(match):
    text = render_offers(match, "team2")
    assert "Фаворит AI: <b>NAVI</b>" in text
    assert "Лучший коэффициент на фаворита: <b>2.40</b>" in text
    assert "Лучший на Aurora" not in text
    assert menu.offers(match, "team2").inline_keyboard[0][0].url == "https://fon.bet/"
    match["offers"][0]["p2"] = 2.4
    highlight = render_offers(match, "team2").split("Лучший коэффициент на фаворита:")[1].split("\n")[0]
    assert "PARI" in highlight and "Фонбет" in highlight


def test_closed_favorite_and_unclear_do_not_recommend_other_team(match):
    for offer in match["offers"]:
        offer["p2"] = None
    text = render_offers(match, "team2")
    assert "на фаворита сейчас закрыты" in text and "Лучший коэффициент" not in text
    text = render_offers(match)
    assert "Нет актуального фаворита AI" in text and "Фаворит AI:" not in text
    assert "Лучший на Aurora" in text


def test_forecast_is_bound_to_card_event_and_time(tmp_path, match, monkeypatch):
    users = Users(tmp_path / "users.json")
    record = remember(users, 1, 55, match, Commentary(summary="NAVI сильнее.", winner="team2", probability=60))
    assert recall(Users(users.path).get(1)["forecasts"]["55"], match) == "team2"
    for changed in (dict(match, status="live"), dict(match, team1="NAVI", team2="Aurora"), dict(match, id="another")):
        assert recall(record, changed) is None
    monkeypatch.setattr(time, "time", lambda: record["expires_at"])
    assert recall(record, match) is None


@pytest.mark.parametrize("winner,probability", [("unclear", None), ("team1", 50)])
def test_even_probability_does_not_select_a_favorite(tmp_path, match, winner, probability):
    record = remember(
        Users(tmp_path / "users.json"),
        1,
        1,
        match,
        Commentary(summary="Матч близкий.", winner=winner, probability=probability),
    )
    assert recall(record, match) is None


def test_bonus_priority_enabled_validation_and_plain_text():
    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:TEST",
        bonus_offers=[
            dict(bookmaker="pari", priority=20, description="Условия <акции>", url="https://pari.ru/", enabled=True),
            dict(bookmaker="fonbet", priority=1, description="Оффер", url="https://fon.bet/", enabled=True),
            dict(bookmaker="leon", priority=0, description="Черновик", url="https://leon.ru/"),
        ],
    )
    offers = ordered_offers(settings)
    assert [o.bookmaker for o in offers] == ["fonbet", "pari"]
    assert "&lt;акции&gt;" in render_bonus(offers)
    assert "Черновик" not in render_bonus(offers)
    assert "нет опубликованных" in render_bonus([])
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            telegram_bot_token="123:TEST",
            bonus_offers=[dict(bookmaker="pari", priority=1, description="Test", url="http://pari.ru/", enabled=True)],
        )


def test_logo_alias_unknown_team_and_all_six_bookmakers():
    assert team_emoji("Natus Vincere", "cs2") == team_emoji("NAVI", "cs2")
    assert team_emoji("Unknown", "cs2") == team_emoji("NAVI", "dota2") == ""
    assert len(registry()["bookmakers"]) == 6
    for entry in [*registry()["teams"].values(), *registry()["bookmakers"].values()]:
        assert isinstance(entry["id"], str) and entry["id"].isdigit() and entry["emoji"]


async def test_telegram_emoji_rejection_falls_back_without_mutating_original(match):
    method = SendMessage(chat_id=1, text=bookmaker_name("pari", "PARI"), reply_markup=menu.offers(match))
    request = AsyncMock(side_effect=[TelegramBadRequest(method=method, message="CUSTOM_EMOJI_INVALID"), True])
    assert await EmojiFallback()(request, None, method)
    fallback = request.call_args_list[1].args[1]
    assert fallback.text == "PARI"
    assert all(b.icon_custom_emoji_id is None for row in fallback.reply_markup.inline_keyboard for b in row)
    assert "tg-emoji" in method.text
    assert method.reply_markup.inline_keyboard[0][0].icon_custom_emoji_id
    request = AsyncMock(side_effect=TelegramBadRequest(method=method, message="chat not found"))
    with pytest.raises(TelegramBadRequest):
        await EmojiFallback()(request, None, method)
    assert request.call_count == 1
