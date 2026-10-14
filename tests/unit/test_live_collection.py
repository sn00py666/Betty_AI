import gzip
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from collect import collect_one, read_json, save_json
from integrations.bookmakers import LIVE_URLS, SOURCES, parse_marathon, parse_response
from services.collection_schedule import FAST_INTERVAL, LIVE_DISCOVERY_INTERVAL
from services.freshness import LIVE_TTL

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.mark.parametrize("name", ["fonbet", "pari"])
def test_real_live_json_has_series_prices_and_excludes_child_markets(name):
    raw = (FIXTURES / f"{name}-live.json").read_text()
    matches = parse_response(name, raw)
    assert len(matches) == 2
    assert {m["sport"] for m in matches} == {"cs2", "dota2"}
    assert all(m["status"] == "live" and m["best_of"] == 3 for m in matches)
    factors = {str(f["e"]): {v["f"]: v["v"] for v in f["factors"]} for f in json.loads(raw)["customFactors"]}
    for m in matches:
        assert (m["p1"], m["p2"]) == (factors[m["id"]][921], factors[m["id"]][923])
        assert m["url"] == ("https://fon.bet/live" if name == "fonbet" else "https://pari.ru/live")
    data = json.loads(raw)
    data["events"][0]["place"] = "notActive"
    assert len(parse_response(name, json.dumps(data))) == 1
    data["events"][1]["blocked"] = True
    closed = parse_response(name, json.dumps(data))[0]
    assert closed["p1"] is None and closed["p2"] is None


def test_marathon_real_live_page_reads_series_and_ignores_other_sports():
    raw = (FIXTURES / "marathon-live.html").read_text()
    matches = parse_marathon(raw)
    assert len(matches) == 4
    assert {m["sport"] for m in matches} == {"cs2", "dota2"}
    assert all(m["status"] == "live" and m["starts_at"] is None and m["best_of"] == 3 for m in matches)
    priced = next(m for m in matches if m["team1"] == "Zero Tenacity")
    assert (priced["p1"], priced["p2"]) == (1.31, 3.02)
    assert priced["url"] == "https://www.marathonbet.ru/su/live/32549650"
    # Рынок карты на месте рынка серии не должен стать коэффициентом на матч.
    map_only = parse_marathon(raw.replace("@Match_Result.", "@Map_2_Result."))
    assert all(m["p1"] is None and m["p2"] is None for m in map_only)
    assert parse_marathon('<div data-live-events-container="true"></div>') == []
    with pytest.raises(ValueError):
        parse_marathon("<html>неполная загрузка</html>")


@pytest.mark.parametrize("name", ["fonbet", "pari", "marathon"])
@pytest.mark.parametrize("empty", [False, True])
async def test_live_refresh_promotes_event_removes_finished_and_preserves_prematch(tmp_path, name, empty):
    now = time.time()
    observed = datetime.fromtimestamp(now - 120, UTC).isoformat()
    expiry = datetime.fromtimestamp(now + 1600, UTC).isoformat()
    raw = (FIXTURES / f"{name}-live.{'html' if name == 'marathon' else 'json'}").read_text()
    promoted_id = parse_response(name, raw)[0]["id"]
    if empty:
        raw = (
            '<div data-live-events-container="true"></div>'
            if name == "marathon"
            else json.dumps(
                {
                    "sports": [],
                    "events": [],
                    "customFactors": [],
                }
            )
        )
    folder = tmp_path / name
    folder.mkdir()
    old_match = {
        "id": "future",
        "sport": "cs2",
        "team1": "A",
        "team2": "B",
        "status": "prematch",
        "starts_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "fetched_at": observed,
        "expires_at": expiry,
    }
    save_json(
        folder / "latest.json",
        {
            "fetched_at": observed,
            "expires_at": expiry,
            "matches": [old_match, {**old_match, "id": promoted_id}, {**old_match, "id": "finished", "status": "live"}],
        },
    )
    save_json(
        folder / "state.json",
        {
            "robots": "User-agent: *\nAllow: /",
            "robots_checked": now,
            "last_success": now - 120,
            "last_full_success": now - 120,
        },
    )
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await collect_one(name, client, tmp_path)
    data = read_json(folder / "latest.json", {})
    state = read_json(folder / "state.json", {})
    assert not state.get("error")
    assert calls == [LIVE_URLS.get(name, SOURCES[name])]
    assert next(m for m in data["matches"] if m["id"] == "future") == old_match
    assert all(m["id"] != "finished" for m in data["matches"])
    assert state["last_full_success"] == now - 120
    live = [m for m in data["matches"] if m["status"] == "live"]
    if empty:
        assert not live and state["refresh_mode"] == "prematch"
        assert state["next_request"] >= now + LIVE_DISCOVERY_INTERVAL
    else:
        assert live and state["refresh_mode"] == "fast"
        assert len([m for m in data["matches"] if m["id"] == promoted_id]) == 1
        assert state["next_request"] >= now + FAST_INTERVAL
        for m in live:
            age = datetime.fromisoformat(m["expires_at"]) - datetime.fromisoformat(m["fetched_at"])
            assert age.total_seconds() == LIVE_TTL


async def test_marathon_live_cache_is_separate_and_checks_robots(tmp_path):
    now = time.time()
    folder = tmp_path / "marathon"
    folder.mkdir()
    (folder / "response.gz").write_bytes(gzip.compress(b"prematch cache"))
    save_json(
        folder / "state.json",
        {
            "robots": "User-agent: *\nDisallow: /su/live/",
            "robots_checked": now,
            "last_full_success": now,
            "etag": "prematch-etag",
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("robots disallows LIVE"))
    ) as client:
        await collect_one("marathon", client, tmp_path)
    assert read_json(folder / "state.json", {})["paused"]
    assert gzip.decompress((folder / "response.gz").read_bytes()) == b"prematch cache"


async def test_marathon_live_revalidation_never_uses_prematch_cache(tmp_path):
    now = time.time()
    folder = tmp_path / "marathon"
    folder.mkdir()
    (folder / "response.gz").write_bytes(gzip.compress(b"prematch cache"))
    save_json(
        folder / "state.json",
        {
            "robots": "User-agent: *\nAllow: /",
            "robots_checked": now,
            "last_full_success": now,
            "etag": "prematch-etag",
        },
    )
    raw = (FIXTURES / "marathon-live.html").read_text()

    def first(request):
        assert "If-None-Match" not in request.headers
        return httpx.Response(200, text=raw, headers={"ETag": "live-etag"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(first)) as client:
        await collect_one("marathon", client, tmp_path)
    assert gzip.decompress((folder / "response.gz").read_bytes()) == b"prematch cache"
    assert gzip.decompress((folder / "live-response.gz").read_bytes()).decode() == raw
    state = read_json(folder / "state.json", {})
    state["next_request"] = 0
    save_json(folder / "state.json", state)

    def unchanged(request):
        assert request.headers["If-None-Match"] == "live-etag"
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unchanged)) as client:
        await collect_one("marathon", client, tmp_path)
    assert read_json(folder / "state.json", {})["error"] is None
    assert any(m["status"] == "live" for m in read_json(folder / "latest.json", {})["matches"])
