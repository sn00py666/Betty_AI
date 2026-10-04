"""Один реальный матч из шести линий должен стать одной карточкой с шестью ценами."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from integrations.bookmakers import SOURCES, parse_response
from services.matches import Matches, team_key

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "dota2"


def test_same_dota_match_from_all_six_bookmakers(tmp_path):
    captured = json.loads((FIXTURES / "captured.json").read_text())
    now = datetime(2026, 10, 5, tzinfo=UTC)
    expected = {}
    for name in SOURCES:
        extension = "json" if name in ("pari", "fonbet") else "html"
        rows = parse_response(name, (FIXTURES / f"{name}.{extension}").read_text(), captured[name])
        match = next(m for m in rows if team_key(m["team1"]) == "yandex")
        assert match["sport"] == "dota2" and match["status"] == "prematch"
        assert (
            abs((datetime.fromisoformat(match["starts_at"]) - datetime(2026, 10, 8, 9, tzinfo=UTC)).total_seconds())
            < 60
        )
        assert match["p1"] > 1 and match["p2"] > 1
        expected[name] = (match["p1"], match["p2"])
        # Имитируем единый цикл получения сохранённых ответов, без запросов к сайтам.
        match["fetched_at"] = now.isoformat()
        folder = tmp_path / name
        folder.mkdir()
        (folder / "latest.json").write_text(
            json.dumps(
                dict(fetched_at=now.isoformat(), expires_at=(now + timedelta(minutes=30)).isoformat(), matches=[match])
            )
        )
    matches = Matches(tmp_path).read(now)
    assert len(matches) == 1
    assert {o["bookmaker"]: (o["p1"], o["p2"]) for o in matches[0]["offers"]} == expected


def test_browser_snapshots_deduplicate_and_keep_capture_time():
    captured = json.loads((FIXTURES / "captured.json").read_text())["betboom"]
    html = (FIXTURES / "betboom.html").read_text()
    page = dict(url=SOURCES["betboom"], captured_at=captured, html=html)
    newer = {**page, "captured_at": "2026-10-04T23:00:00+00:00"}
    rows = parse_response("betboom", json.dumps({"pages": [page, newer]}))
    assert len(rows) == 1
    assert rows[0]["fetched_at"] == newer["captured_at"]


def test_betboom_sticky_heading_cannot_change_tournament():
    from bs4 import BeautifulSoup

    captured = json.loads((FIXTURES / "captured.json").read_text())["betboom"]
    html = (FIXTURES / "betboom.html").read_text()
    scrolled = BeautifulSoup(html, "html.parser")
    scrolled.select_one("h3.bb-Ls").decompose()
    sticky = '<div class="bb-Ny"><h3 class="bb-Ls">Другой турнир</h3></div>'
    pages = [
        dict(url=SOURCES["betboom"], captured_at=captured, html=html),
        dict(url=SOURCES["betboom"], captured_at=captured, html=sticky + str(scrolled)),
    ]
    rows = parse_response("betboom", json.dumps({"pages": pages}))
    assert len(rows) == 1
    assert rows[0]["tournament"] == "BLAST SLAM VIII"


def test_betboom_outright_is_not_a_match():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup((FIXTURES / "betboom.html").read_text(), "html.parser")
    teams = soup.select(".bb-yO")
    teams[0].string = "Итоги BLAST SLAM VIII"
    teams[1].decompose()
    rows = parse_response("betboom", str(soup), "2026-10-04T23:00:00+00:00")
    assert rows == []
