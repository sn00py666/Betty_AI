import gzip
import json
import time
from pathlib import Path

import httpx
import pytest

from collect import INTERVAL, collect_one, read_json, retry_after, save_json
from integrations.bookmakers import (
    PARSERS,
    parse_betboom,
    parse_fonbet,
    parse_leon,
    parse_marathon,
    parse_pari,
    parse_winline,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_pari_real_response_and_own_links():
    matches = parse_pari((FIXTURES / "pari.json").read_text())
    assert matches
    assert all(m["url"].startswith("https://pari.ru/") for m in matches)
    assert all(m["status"] == "prematch" and m["market"] == "match_winner" for m in matches)
    assert matches[0]["team1"] == "ShindeN"


def test_winline_series_odds_exclude_player_duels():
    matches = parse_winline((FIXTURES / "winline.html").read_text())
    assert len(matches) == 2
    assert matches[0]["p1"] == 3.37  # Не 2.02 от отдельной карты.
    assert matches[0]["p2"] == 1.33
    assert matches[0]["status"] == "live"
    assert matches[0]["tournament"] == "Counter-Strike | Ranked"
    assert matches[0]["starts_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [403, 429])
async def test_browser_failure_uses_persistent_cooldown(tmp_path, monkeypatch, code):
    folder = tmp_path / "winline"
    folder.mkdir()
    save_json(folder / "state.json", {"robots": "User-agent: *\nAllow: /", "robots_checked": time.time()})
    calls = []

    async def browser(url, robots=""):
        calls.append(url)
        return httpx.Response(code, headers={"Retry-After": "7200"}, request=httpx.Request("GET", url)), ""

    monkeypatch.setattr("collect.load_winline", browser)
    async with httpx.AsyncClient() as client:
        await collect_one("winline", client, tmp_path)
        await collect_one("winline", client, tmp_path)
    state = read_json(folder / "state.json", {})
    assert len(calls) == 1
    if code == 403:
        assert state["paused"]
    else:
        assert state["next_request"] >= time.time() + 7199


def test_fonbet_series_only_and_suspended_prices():
    data = json.loads((FIXTURES / "fonbet.json").read_text())
    matches = parse_fonbet(json.dumps(data))
    assert matches
    assert all(m["market"] == "match_winner" and m["best_of"] == 3 for m in matches)
    assert all(m["starts_at"].endswith("+00:00") for m in matches)
    event = next(e for e in data["events"] if str(e["id"]) == matches[0]["id"])
    event["blocked"] = True
    blocked = parse_fonbet(json.dumps(data))[0]
    assert blocked["p1"] is None and blocked["p2"] is None
    event["level"] = 2
    assert blocked["id"] not in {m["id"] for m in parse_fonbet(json.dumps(data))}


def test_marathon_real_html():
    matches = parse_marathon((FIXTURES / "marathon.html").read_text())
    assert matches[0]["team1"] == "Team Spirit"
    assert matches[0]["team2"] == "ShindeN"
    assert matches[0]["p1"] == 1.025
    assert matches[0]["p2"] == 10.75
    assert matches[0]["best_of"] == 3
    assert matches[0]["starts_at"] is None  # В этом фрагменте нет даты с часовым поясом.
    closed = parse_marathon((FIXTURES / "marathon.html").read_text().replace("active-selection", "disabled"))
    assert closed[0]["p1"] is None


@pytest.fixture
def directory(tmp_path):
    folder = tmp_path / "fonbet"
    folder.mkdir()
    save_json(folder / "state.json", {"robots": "User-agent: *\nAllow: /", "robots_checked": time.time()})
    return tmp_path


async def execute(directory, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await collect_one("fonbet", client, directory)
    return read_json(directory / "fonbet" / "state.json", {})


@pytest.mark.asyncio
async def test_cache_and_restart_cooldown(directory):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=(FIXTURES / "fonbet.json").read_text(), headers={"ETag": '"v1"'})

    state = await execute(directory, handler)
    assert state["next_request"] > time.time() + INTERVAL - 2
    await execute(directory, handler)
    assert len(calls) == 1
    assert (directory / "fonbet" / "latest.json").exists()
    assert gzip.decompress((directory / "fonbet" / "response.gz").read_bytes())
    state["next_request"] = 0
    save_json(directory / "fonbet" / "state.json", state)

    def unchanged(request):
        assert request.headers["If-None-Match"] == '"v1"'
        return httpx.Response(304)

    await execute(directory, unchanged)
    assert len((directory / "fonbet" / "history.jsonl").read_text().splitlines()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(403, ""), (451, ""), (200, "<html>verify you are human</html>"), (302, "")])
async def test_access_denied_stops_even_after_restart(directory, status, body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=body)

    state = await execute(directory, handler)
    assert state["paused"]
    state["next_request"] = 0
    save_json(directory / "fonbet" / "state.json", state)
    await execute(directory, handler)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_429_honors_retry_after_without_retry(directory):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "100000"})

    state = await execute(directory, handler)
    assert state["next_request"] >= time.time() + 99999
    await execute(directory, handler)
    assert len(calls) == 1


def test_retry_after_http_date():
    assert retry_after("Thu, 01 Oct 2026 12:00:00 GMT", 1790852400) == 3600
    assert retry_after("invalid", time.time()) == 0


@pytest.mark.asyncio
async def test_robots_wildcard_blocks_without_request(directory):
    save_json(
        directory / "fonbet" / "state.json",
        {
            "robots": "User-agent: *\nDisallow: /*?",
            "robots_checked": time.time(),
        },
    )

    def handler(request):
        pytest.fail("Запрещённый robots.txt URL не должен запрашиваться")

    state = await execute(directory, handler)
    assert state["paused"]


@pytest.mark.asyncio
async def test_bad_response_keeps_last_snapshot(directory):
    folder = directory / "fonbet"
    save_json(folder / "latest.json", {"fetched_at": "old", "matches": ["old"]})
    state = await execute(directory, lambda _: httpx.Response(200, json={"error": "unavailable"}))
    assert state["paused"]
    assert read_json(folder / "latest.json", {})["fetched_at"] == "old"


def test_leon_series_market_and_locked_prices():
    html = (FIXTURES / "leon.html").read_text()
    match = parse_leon(html)[0]
    assert (match["team1"], match["team2"], match["p1"], match["p2"]) == ("Sashi", "Acend", 1.75, 1.95)
    assert match["status"] == "prematch" and match["starts_at"] is None
    assert match["id"] == "1970324853713054"
    assert parse_leon(html.replace('locked="false"', 'locked="true"'))[0]["p1"] is None
    assert parse_leon(html.replace("Победитель", "Победитель 1 карты"))[0]["p1"] is None
    assert parse_leon(html.replace("/bets/esports/cs2/", "/bets/esports/dota-2/"))[0]["sport"] == "dota2"
    live = parse_leon(html.replace('live="false"', 'live="true"'))[0]
    assert live["status"] == "live" and live["start_label"] is None


def test_betboom_primary_market_only_and_disabled_prices():
    html = (FIXTURES / "betboom.html").read_text()
    match = parse_betboom(html)[0]
    assert (match["team1"], match["team2"], match["p1"], match["p2"]) == ("Nemiga Gaming", "Astralis", 3.6, 1.26)
    assert match["id"] == "6089418" and match["status"] == "live"
    assert match["live_period"] == "4-я карта" and match["start_label"] is None
    assert parse_betboom(html.replace("<button ", "<button disabled "))[0]["p1"] is None
    extra = '<button><span class="bb-Uu">П1</span><span class="bb-Vu">9.99</span></button>'
    assert parse_betboom(html + extra)[0]["p1"] == 3.6
    assert parse_betboom(html.replace("CS2", "Dota 2"))[0]["sport"] == "dota2"
    with pytest.raises(ValueError):
        parse_betboom(html.replace('aria-checked="true"', 'aria-checked="false"'))


@pytest.mark.parametrize("name", ["leon", "betboom"])
def test_browser_parsers_reject_challenge(name):
    with pytest.raises(ValueError):
        PARSERS[name]("<html><body>Проверка браузера</body></html>")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["leon", "betboom"])
@pytest.mark.parametrize("code", [200, 403, 429])
async def test_new_browser_collectors_save_or_pause(tmp_path, monkeypatch, name, code):
    calls = []

    async def browser(source, url, robots):
        calls.append(source)
        return (
            httpx.Response(code, request=httpx.Request("GET", url), headers={"Retry-After": "7200"}),
            (FIXTURES / f"{name}.html").read_text(),
            "User-agent: *\nAllow: /",
        )

    monkeypatch.setattr("collect.load_rendered", browser)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Не нужен прямой HTTP"))
    ) as client:
        await collect_one(name, client, tmp_path)
        await collect_one(name, client, tmp_path)
    state = read_json(tmp_path / name / "state.json", {})
    assert calls == [name]
    if code == 200:
        snapshot = read_json(tmp_path / name / "latest.json", {})
        assert snapshot["matches"] and state["robots_checked"]
    elif code == 403:
        assert state["paused"]
    else:
        assert state["next_request"] >= time.time() + 7199


def test_dota_series_is_included_but_draw_market_is_excluded():
    data = {
        "sports": [{"id": 1, "name": "Dota 2. BLAST Slam. Bo3"}],
        "events": [
            {
                "id": 2,
                "sportId": 1,
                "level": 1,
                "place": "line",
                "team1": "Team A",
                "team2": "Team B",
                "startTime": 1791450000,
            }
        ],
        "customFactors": [{"e": 2, "factors": [{"f": 921, "v": 1.5}, {"f": 923, "v": 2.5}]}],
    }
    match = parse_fonbet(json.dumps(data))[0]
    assert match["sport"] == "dota2" and match["best_of"] == 3
    data["customFactors"][0]["factors"].append({"f": 922, "v": 3})
    assert parse_fonbet(json.dumps(data)) == []


def test_marathon_today_after_moscow_midnight():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup((FIXTURES / "marathon.html").read_text(), "html.parser")
    row = soup.select_one('[data-event-name][data-live="false"]')
    row.select_one(".date").string = "14:30"
    row.select_one(".member-name")["class"].append("today-member-name")
    html = '<script>initData={"timeZoneId":"Europe/Moscow"}</script>' + str(soup)
    match = parse_marathon(html, "2026-10-04T22:00:00+00:00")[0]
    assert match["starts_at"] == "2026-10-05T11:30:00+00:00"


def test_draw_market_is_excluded_even_when_draw_is_blocked():
    data = json.loads((FIXTURES / "fonbet.json").read_text())
    target = parse_fonbet(json.dumps(data))[0]["id"]
    market = next(f for f in data["customFactors"] if str(f["e"]) == target)
    market["factors"].append({"f": 922, "v": 3.2, "blocked": True})
    assert target not in {m["id"] for m in parse_fonbet(json.dumps(data))}
