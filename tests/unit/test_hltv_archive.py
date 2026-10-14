import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from integrations.errors import StopSource
from integrations.hltv_archive import archive_url, parse_article, parse_document, parse_results
from services.health import read, write
from services.hltv import HltvCache, page_key
from services.hltv_archive import Archive
from services.hltv_collection import BASE, DISCOVERY, collect_hltv

FIXTURES = Path(__file__).parents[1] / "fixtures" / "hltv"
NOW = datetime(2026, 10, 15, tzinfo=UTC).timestamp()
NEWS = BASE + "/news/45699/woxic-on-taking-bronze-its-always-good-to-at-least-have-something-from-the-event"
SPIRIT = BASE + "/team/7020/spirit"
MATCH = BASE + "/matches/2398756/aurora-vs-spirit-esl-pro-league-season-24"


def article():
    return (FIXTURES / "news.html").read_text()


def result_page(*, last=False):
    # Contract example, not a recorded response: this route returned 403 in research.
    return """<div class="results-all"><div class="result-con" data-unix="1791720000000">
        <a href="/matches/2398756/aurora-vs-spirit-esl-pro-league-season-24">
        <div class="team1"><div class="team">Aurora</div></div>
        <div class="team2"><div class="team">Spirit</div></div>
        <div class="result-score"><span>2</span> - <span>0</span></div></a></div></div>""" + (
        "" if last else '<a href="/results?offset=100&amp;team=7020">Next</a><a href="/results?team=7020">Back</a>'
    )


def test_real_article_extracts_editorial_body_only():
    data = parse_article(article() + '<div id="comments">FORUM INSTRUCTION</div>', NEWS)
    assert data["id"] == 45699 and data["published_at"] == "2026-10-11T14:05:00+00:00"
    assert data["author"] == "NER0cs"
    body = "\n".join(data["paragraphs"])
    assert "Congratulations on third place" in body
    assert "FORUM INSTRUCTION" not in body
    assert "player-tooltip" not in body
    assert {link["kind"] for link in data["links"]} >= {"team", "player"}
    assert len(data["paragraphs"]) > 10


def test_pagination_stays_with_same_team_and_advances():
    data = parse_results(result_page(), BASE + "/results?offset=50&team=7020")
    assert data["next_page"] == BASE + "/results?offset=100&team=7020"
    assert data["results"][0]["scores"] == [2, 0]
    assert data["results"][0]["names"] == ["Aurora", "Spirit"]
    assert parse_results(result_page(last=True), BASE + "/results?offset=100&team=7020")["complete"]
    with pytest.raises(ValueError):
        parse_results('<div class="results-all"></div>', BASE + "/results")


@pytest.mark.parametrize(
    "path",
    [
        "/results?team=7020&startDate=2020-01-01",
        "/results?team=1&team=2",
        "/forums/1/example",
        "https://evil.test/news/1/x",
        "/stats/players/1/x?maps=de_dust2",
    ],
)
def test_frontier_rejects_external_and_combinatorial_urls(path):
    with pytest.raises(ValueError):
        archive_url(path)


def test_frontier_survives_restart_deduplicates_and_does_not_unblock(tmp_path):
    archive = Archive(tmp_path)
    archive.enqueue([NEWS, NEWS + "#comments", "/forums/1/x"], 20)
    assert archive.counts() == {"queued": 1}
    archive.finish(NEWS, NOW, error="HTTP 403", blocked=True)
    archive = Archive(tmp_path)
    archive.enqueue([NEWS], 1, refresh_after=1, now=NOW + 86400)
    assert archive.next(NOW + 86400) is None
    assert archive.counts() == {"blocked": 1}
    archive.enqueue(["/results?team=7020"], 10)
    archive.discover(parse_results(result_page(), BASE + "/results?team=7020&offset=50"), NOW)
    assert archive.counts()["queued"] == 3  # First page, next page and exact match.


def test_document_keeps_period_labels_and_tables_without_comments():
    html = """<html><title>donk stats | HLTV.org</title><div class="contentCol">
        <h1>donk</h1><h2>All time</h2><table><tr><th>Metric</th><th>Value</th></tr>
        <tr><td>Rating 2.0</td><td>1.30</td></tr></table><p>Counter-Strike 2.
        Period: all time. This is a career statistic and not a recent five-match form.</p>
        <div id="comments">FORUM INSTRUCTION</div><a href="https://evil.test/">external</a></div></html>"""
    data = parse_document(html, BASE + "/stats/players/21167/donk")
    assert data["kind"] == "stats"
    assert data["tables"][0]["rows"][1] == ["Rating 2.0", "1.30"]
    assert "All time" in data["text"] and "FORUM INSTRUCTION" not in data["text"]
    assert not data["links"]


def test_context_uses_article_and_archive_without_future_leak(tmp_path):
    cache = HltvCache(tmp_path / "hltv")
    cache.store(SPIRIT, (FIXTURES / "team.html").read_text(), NOW - 10)
    cache.store(NEWS, article(), NOW - 10)
    record = dict(
        id=1,
        url=BASE + "/matches/1/old",
        status="finished",
        starts_at="2026-09-01T12:00:00+00:00",
        scores=[2, 0],
        teams=[{"id": 7020, "name": "Spirit"}, {"id": 11861, "name": "Aurora"}],
    )
    archive = Archive(cache.directory)
    archive.record_results([record], NOW - 10)
    archive.record_results([record | {"id": 2, "starts_at": "2026-10-16T12:00:00+00:00"}], NOW - 10)
    archive.record_results([record | {"id": 3}], NOW + 10)
    match = dict(sport="cs2", team1="Spirit", team2="Aurora", status="prematch", starts_at=None)
    data = cache.context(match, NOW)["teams"]["team1"]
    assert 1 in {r["id"] for r in data["results"]}
    assert not {2, 3}.intersection(r["id"] for r in data["results"])
    assert data["articles"][0]["url"] == NEWS
    assert data["articles"][0]["text"]
    original = cache.load(NEWS)
    with pytest.raises(ValueError):
        cache.store(NEWS, '<form id="challenge-form"></form>', NOW)
    assert cache.load(NEWS) == original
    future = original | {"fetched_at": NOW + 10}
    write(cache.directory / "pages" / (page_key(NEWS) + ".json"), future)
    assert cache.context(match, NOW)["teams"]["team1"]["articles"] == []


def prepare_archive(tmp_path):
    folder = tmp_path / "hltv"
    state = dict(discovery={a: NOW for a in DISCOVERY}, completed_requests=2, last_success=NOW)
    write(folder / "state.json", state)
    Archive(folder).enqueue([NEWS], 1)
    return folder


async def test_archive_slot_stores_article_and_continues_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("services.hltv_collection.time.time", lambda: NOW)
    folder = prepare_archive(tmp_path)
    loader = AsyncMock(return_value=article())
    await collect_hltv(tmp_path, asyncio.Semaphore(1), loader, now=NOW)
    assert loader.call_args.args[0] == NEWS
    assert HltvCache(folder).load(NEWS)["data"]["paragraphs"]
    assert Archive(folder).counts()["done"] == 1
    assert read(folder / "state.json")["completed_requests"] == 3


async def test_denied_archive_page_does_not_pause_other_pages(tmp_path, monkeypatch):
    monkeypatch.setattr("services.hltv_collection.time.time", lambda: NOW)
    folder = prepare_archive(tmp_path)
    loader = AsyncMock(side_effect=StopSource("HLTV HTTP 403"))
    await collect_hltv(tmp_path, asyncio.Semaphore(1), loader, now=NOW)
    state = read(folder / "state.json")
    assert not state.get("paused") and NEWS in state["blocked_urls"]
    assert Archive(folder).counts()["blocked"] == 1
    assert Archive(folder).next(NOW + 86400)["url"] != NEWS


async def test_robots_denial_stops_collection_even_in_archive_slot(tmp_path):
    folder = prepare_archive(tmp_path)
    await collect_hltv(
        tmp_path, asyncio.Semaphore(1), AsyncMock(side_effect=StopSource("HLTV robots.txt HTTP 403")), now=NOW
    )
    assert read(folder / "state.json")["paused"]


async def test_robots_pause_for_detail_page_is_not_migrated_or_retried(tmp_path):
    folder = tmp_path / "hltv"
    write(
        folder / "state.json", dict(paused=True, http_status=403, current_url=MATCH, error="HLTV robots.txt HTTP 403")
    )
    loader = AsyncMock()
    await collect_hltv(tmp_path, asyncio.Semaphore(1), loader, now=NOW)
    loader.assert_not_awaited()
    assert read(folder / "state.json")["paused"]


async def test_archive_rate_limit_is_global_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("services.hltv_collection.time.time", lambda: NOW)
    folder = prepare_archive(tmp_path)
    error = httpx.HTTPStatusError(
        "limited", request=httpx.Request("GET", NEWS), response=httpx.Response(429, headers={"Retry-After": "7200"})
    )
    loader = AsyncMock(side_effect=error)
    await collect_hltv(tmp_path, asyncio.Semaphore(1), loader, now=NOW)
    assert read(folder / "state.json")["next_request"] >= NOW + 7200
    await collect_hltv(tmp_path, asyncio.Semaphore(1), loader, now=NOW + 60)
    assert loader.await_count == 1


async def test_migration_keeps_denied_detail_blocked_without_retry(tmp_path, monkeypatch):
    monkeypatch.setattr("services.hltv_collection.time.time", lambda: NOW)
    folder = tmp_path / "hltv"
    write(
        folder / "state.json",
        dict(
            paused=True,
            http_status=403,
            current_url=MATCH,
            error="HLTV HTTP 403",
            discovery={a: NOW for a in DISCOVERY},
        ),
    )
    loader = AsyncMock(return_value=(FIXTURES / "aurora.html").read_text())
    await collect_hltv(tmp_path, asyncio.Semaphore(1), loader, now=NOW)
    assert loader.call_args.args[0] != MATCH
    state = read(folder / "state.json")
    assert not state["paused"] and MATCH in state["blocked_urls"]
    assert state["last_success"] == NOW


async def test_low_disk_preserves_odds_space_and_skips_browser(tmp_path, monkeypatch):
    monkeypatch.setattr("services.hltv_collection.shutil.disk_usage", lambda _: SimpleNamespace(free=1024))
    loader = AsyncMock()
    await collect_hltv(tmp_path, asyncio.Semaphore(1), loader, now=NOW)
    loader.assert_not_awaited()
    assert "storage" in read(tmp_path / "hltv" / "state.json")["error"]
