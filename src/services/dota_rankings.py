"""Независимые рейтинги Dota 2: OpenDota и публичная таблица CyberScore."""

import asyncio
import json
import logging
import math
import re
import time
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup
from protego import Protego

from services.health import read, safe_error, write
from services.rankings import retry_delay
from services.team_names import team_key

SOURCES = {"opendota": "OpenDota", "cyberscore": "CyberScore"}
URLS = {
    "opendota": "https://api.opendota.com/api/teams",
    "cyberscore": "https://cyberscore.live/en/teams/",
}
ROBOTS = "https://cyberscore.live/robots.txt"
AGENT = "BettyAI/0.1"
INTERVAL = 86400
MAX_AGE = 3 * INTERVAL
ACTIVE_AGE = 90 * INTERVAL
PUBLISHED_AGE = 45 * INTERVAL
MAX_BYTES = 4 * 1024 * 1024
logger = logging.getLogger(__name__)


def dota_team_key(name):
    key = team_key(name)
    return {"1winteam": "1win", "tundraesports": "tundra"}.get(key, key)


def enabled_sources(settings=None):
    return tuple(name for name in SOURCES if getattr(settings, f"dota_{name}_enabled", True))


def source_path(directory, source):
    return directory / "dota_rankings" / f"{source}.json"


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def load_source(directory, source):
    try:
        state = read(source_path(directory, source))
        for key in ("last_attempt", "last_success", "next_request"):
            if key in state and (not number(state[key]) or not 0 <= state[key] < 253402300800):
                raise ValueError("Неверная дата")
        if type(state.get("failures", 0)) is not int or not 0 <= state.get("failures", 0) <= 6:
            raise ValueError("Неверный счётчик ошибок")
        teams = state.get("teams", {})
        if not isinstance(teams, dict):
            raise ValueError("Неверный список команд")
        if state.get("last_success") and not teams:
            raise ValueError("Отсутствуют команды успешного снимка")
        for key, row in teams.items():
            if (
                not isinstance(row, dict)
                or not key
                or not isinstance(row.get("name"), str)
                or dota_team_key(row["name"]) != key
                or type(row.get("rank")) is not int
                or not 1 <= row["rank"] <= 10000
                or (source == "opendota" and not number(row.get("last_match_time")))
            ):
                raise ValueError("Неверная команда")
        if source == "cyberscore" and teams:
            datetime.strptime(state["published"], "%Y-%m-%d")
        return state
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return {"error": "Повреждён или недоступен кеш рейтинга", "invalid": True}


def available_teams(source, state, now):
    success = state.get("last_success", 0)
    if not success or not 0 <= now - success <= MAX_AGE:
        return {}
    teams = state.get("teams", {})
    if source == "cyberscore":
        published = datetime.strptime(state["published"], "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        return teams if 0 <= now - published <= PUBLISHED_AGE else {}
    return {key: row for key, row in teams.items() if 0 <= now - row["last_match_time"] <= ACTIVE_AGE}


def select_ranking(directory, sources=None, now=None, states=None):
    """Один рейтинг на весь список: баллы/места разных поставщиков не смешиваются."""
    now = time.time() if now is None else now
    sources = tuple(SOURCES) if sources is None else sources
    states = states if states is not None else {s: load_source(directory, s) for s in sources}
    candidates = []
    for priority, source in enumerate(sources):
        state = states[source]
        teams = available_teams(source, state, now)
        if teams:
            candidates.append((bool(state.get("error")), priority, source, teams))
    if not candidates:
        return None, {}, False
    cached, _, source, teams = min(candidates, key=lambda row: row[:2])
    return source, teams, cached


def unique_teams(rows):
    teams, ambiguous = {}, set()
    for row in rows:
        key = dota_team_key(row["name"])
        if not key:
            continue
        if key in teams or key in ambiguous:
            teams.pop(key, None)
            ambiguous.add(key)
        else:
            teams[key] = row
    if len(teams) < 10:
        raise ValueError("Неполный рейтинг: меньше 10 однозначных команд")
    return teams


def parse_opendota(payload, now):
    if not isinstance(payload, list):
        raise ValueError("Неверный формат OpenDota")
    rows = []
    for row in payload:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip():
            continue
        rating, played = row.get("rating"), row.get("last_match_time")
        if not number(rating) or rating <= 0 or not number(played) or not 0 <= now - played <= ACTIVE_AGE:
            continue
        rows.append(dict(name=row["name"].strip(), rating=rating, last_match_time=played))
    rows.sort(key=lambda row: (-row["rating"], row["name"]))
    rank, previous = 0, None
    for index, row in enumerate(rows, 1):
        if row["rating"] != previous:
            rank = index
        row["rank"], previous = rank, row["rating"]
    return dict(teams=unique_teams(rows))


def parse_cyberscore(html, now):
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    if not heading or heading.get_text(strip=True) != "Dota 2 team ranking":
        raise ValueError("Не найдена таблица Dota 2 CyberScore")
    stamp = re.search(r"Updated on:\s*(\d{2}\.\d{2}\.\d{4})", soup.get_text(" ", strip=True))
    if not stamp:
        raise ValueError("Не найдена дата рейтинга CyberScore")
    published = datetime.strptime(stamp[1], "%d.%m.%Y").replace(tzinfo=UTC)
    if not 0 <= now - published.timestamp() <= PUBLISHED_AGE:
        raise ValueError("Устаревшая или будущая дата рейтинга CyberScore")
    rows = []
    for link in soup.select('a[href^="/en/teams/"]'):
        rank = link.select_one(".sub-item--rank > div")
        name = link.select_one(".team-name .name-wrapper")
        rating = link.select_one(".sub-item--rating")
        if rank and name and rating:
            rows.append(
                dict(
                    name=name.get_text(" ", strip=True),
                    rank=int(rank.get_text(strip=True)),
                    rating=float(rating.get_text(strip=True)),
                )
            )
    # Берём только публичную первую страницу: запросы с пагинацией запрещены robots.txt.
    if [row["rank"] for row in rows] != list(range(1, 21)):
        raise ValueError("Неполный топ-20 CyberScore")
    if any(not number(row["rating"]) or row["rating"] <= 0 for row in rows):
        raise ValueError("Неверные баллы CyberScore")
    return dict(teams=unique_teams(rows), published=published.date().isoformat())


async def fetch(client, url, *, robots=False):
    async with client.stream("GET", url) as response:
        if robots and response.status_code in (404, 410):
            return ""
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise ValueError("Ответ рейтинга превысил 4 МБ")
        return body.decode("utf-8")


class DotaRankings:
    def __init__(self, directory, source):
        self.directory, self.source = directory, source

    async def refresh(self):
        state = load_source(self.directory, self.source)
        now = time.time()
        if now < state.get("next_request", 0):
            return
        state.update(last_attempt=now, next_request=now + INTERVAL)
        path = source_path(self.directory, self.source)
        write(path, state)  # Пауза сохраняется до сети и переживает перезапуск.
        try:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": AGENT}, follow_redirects=False) as client:
                if self.source == "cyberscore":
                    rules = Protego.parse(await fetch(client, ROBOTS, robots=True))
                    if not rules.can_fetch(URLS[self.source], AGENT):
                        raise ValueError("robots.txt запрещает рейтинг CyberScore")
                    delay = max(10, rules.crawl_delay(AGENT) or 0)
                    state["next_request"] = max(state["next_request"], now + delay)
                    write(path, state)
                    await asyncio.sleep(delay)
                text = await fetch(client, URLS[self.source])
            now = time.time()
            snapshot = (
                parse_opendota(json.loads(text), now) if self.source == "opendota" else parse_cyberscore(text, now)
            )
            state.update(snapshot, last_success=now, error=None, http_status=200, failures=0)
            state.pop("invalid", None)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError) as error:
            code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            failures = min(int(state.get("failures", 0)) + 1, 6)
            delay = min(INTERVAL, 3600 * 2 ** (failures - 1))
            if isinstance(error, httpx.HTTPStatusError):
                delay = max(delay, retry_delay(error.response.headers.get("Retry-After"), time.time()))
            if code in (401, 403, 451):
                delay = max(delay, INTERVAL)
            state.update(
                failures=failures,
                next_request=time.time() + delay,
                http_status=code,
                error=f"HTTP {code}" if code else safe_error(str(error)) or type(error).__name__,
            )
        write(path, state)


async def run_dota_rankings(directory, source):
    rankings = DotaRankings(directory, source)
    while True:
        try:
            await rankings.refresh()
        except OSError:
            # Отказ записи станет виден по устареванию кеша, другие источники продолжают работу.
            logger.exception("Не удалось сохранить рейтинг %s", source)
        await asyncio.sleep(60)
