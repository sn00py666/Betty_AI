"""Публичные букмекеры и ссылки не зависят от старых снимков или BONUS_OFFERS."""

import re
from unittest.mock import AsyncMock

import pytest
from test_matches import NOW, snapshot

import collect
from config import Settings
from integrations.bookmakers import SOURCES
from keyboards import menu
from services.analysis import render_offers
from services.bonuses import ordered_offers, render_bonus
from services.matches import BOOKMAKERS, Matches
from services.partners import PARTNER_URLS
from services.team_matching import candidates

EXPECTED = {
    "marathon": "https://bett1.pro/click?o=354&a=53074&tsource=1015",
    "winline": "https://bett1.pro/click?o=159&a=53074&tsource=1015&link_id=5155",
}


def test_old_six_source_cache_only_exposes_partners_and_their_matches(tmp_path):
    for name in SOURCES:
        snapshot(tmp_path, name, url=SOURCES[name])
    snapshot(tmp_path, "leon", team1="Disabled-only team", url=SOURCES["leon"])
    rows = Matches(tmp_path).read(NOW)
    assert len(rows) == 1
    assert {o["bookmaker"] for o in rows[0]["offers"]} == set(EXPECTED)
    assert not candidates(rows)
    assert (tmp_path / "leon" / "latest.json").exists()
    buttons = menu.offers(rows[0]).inline_keyboard
    assert {b.url for row in buttons for b in row if b.url} == set(EXPECTED.values())


@pytest.mark.parametrize("favorite", [None, "team1", "team2"])
def test_render_and_keyboard_reject_inactive_best_price_and_replace_all_urls(favorite):
    match = dict(
        sport="cs2",
        team1="PARIVISION",
        team2="BETBOOM",
        offers=[
            dict(
                bookmaker=n,
                p1=2 if n in EXPECTED else 99,
                p2=3 if n in EXPECTED else 99,
                url="https://old.example/match",
            )
            for n in SOURCES
        ],
    )
    text = render_offers(match, favorite)
    assert "PARIVISION" in text and "BETBOOM" in text
    for name in set(SOURCES) - set(EXPECTED):
        assert not re.search(r"\b" + re.escape(BOOKMAKERS[name]) + r"\b", text)
    assert "99.00" not in text
    urls = [b.url for row in menu.offers(match, favorite).inline_keyboard for b in row if b.url]
    assert len(urls) == 2 and set(urls) == set(EXPECTED.values())
    assert all(o["url"] == "https://old.example/match" for o in match["offers"])


def test_old_bonus_configuration_cannot_restore_disabled_books_or_old_urls():
    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:TEST",
        bonus_offers=[
            dict(bookmaker=n, priority=i, description="Условия акции", url="https://old.example/bonus", enabled=True)
            for i, n in enumerate(SOURCES)
        ],
    )
    offers = ordered_offers(settings)
    assert {o.bookmaker for o in offers} == set(EXPECTED)
    # Both renderers also protect callers that bypass ordered_offers.
    text = render_bonus(settings.bonus_offers)
    assert all(BOOKMAKERS[n] not in text for n in set(SOURCES) - set(EXPECTED))
    urls = [b.url for row in menu.bonuses(settings.bonus_offers).inline_keyboard for b in row if b.url]
    assert len(urls) == 2 and set(urls) == set(EXPECTED.values())
    assert PARTNER_URLS == EXPECTED


def test_default_collector_only_starts_partners(tmp_path, monkeypatch):
    worker = AsyncMock()
    monkeypatch.setattr(collect, "run", worker)
    monkeypatch.setattr(collect, "DATA", tmp_path)
    monkeypatch.setattr("sys.argv", ["collect.py", "--watch"])
    collect.main()
    worker.assert_awaited_once_with(["marathon", "winline"], True)


def test_collector_rejects_disabled_bookmaker(tmp_path, monkeypatch):
    worker = AsyncMock()
    monkeypatch.setattr(collect, "run", worker)
    monkeypatch.setattr("sys.argv", ["collect.py", "leon", "--watch"])
    with pytest.raises(SystemExit) as error:
        collect.main()
    assert error.value.code == 2
    worker.assert_not_called()


def test_old_inactive_live_cache_does_not_trigger_fast_collection(tmp_path):
    snapshot(tmp_path, "leon", status="live")
    assert not collect.hot_sports(collect.schedule_snapshots(tmp_path), NOW.timestamp())
