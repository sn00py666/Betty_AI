"""Запуск: uv run python src/collect.py [fonbet marathon] [--watch]."""

import argparse
import asyncio
import fcntl
import gzip
import hashlib
import json
import random
import time
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from protego import Protego

from config import RankingSettings
from integrations.bookmakers import LIVE_URLS, SOURCES, parse_response
from integrations.browser import BROWSER_URLS, load_rendered, load_winline
from integrations.errors import StopSource
from services.collection_schedule import (
    BROWSER_SOURCES,
    FAST_INTERVAL,
    HTTP_LIVE_SOURCES,
    LIVE_DISCOVERY_INTERVAL,
    LIVE_SOURCES,
    hot_sports,
    next_start_check,
)
from services.dota_rankings import enabled_sources, run_dota_rankings
from services.freshness import LIVE_TTL, PREMATCH_TTL
from services.health import heartbeat
from services.hltv_collection import run_hltv
from services.rankings import run_rankings

DATA = Path(__file__).resolve().parent.parent / "data" / "odds"
# Профиль Chrome 154 на macOS (версия установленного Chrome при настройке).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
ROBOTS_AGENT = "BettyAI/0.1"  # Смена HTTP-заголовка не снимает ограничения для нашего сборщика.
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"}
INTERVAL = PREMATCH_TTL
MAX_BYTES = 20 * 1024 * 1024


def read_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def retry_after(value, now):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return 0


async def request(client, url, headers=None, *, robots=False):
    request_headers = {**HEADERS, "Accept": "text/plain,*/*;q=0.5" if robots else "*/*", **(headers or {})}
    async with client.stream("GET", url, headers=request_headers) as response:
        if response.status_code == 304:
            return response, ""
        if response.status_code in (401, 403, 451) or response.is_redirect:
            raise StopSource(f"HTTP {response.status_code}: доступ ограничен или адрес изменился")
        if robots and response.status_code in (404, 410):
            return response, ""
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise StopSource("Ответ превысил 20 МБ")
        text = body.decode("utf-8")
        lowered = text.lower()
        if any(marker in lowered for marker in ("cf-chl-", "verify you are human", "g-recaptcha", "hcaptcha")):
            raise StopSource("Получена страница проверки посетителя")
        return response, text


def schedule_snapshots(directory):
    snapshots = []
    for name in SOURCES:
        try:
            snapshot = read_json(directory / name / "latest.json", {})
            if isinstance(snapshot, dict) and isinstance(snapshot.get("matches", []), list):
                snapshots.append(
                    snapshot | {"matches": [m for m in snapshot.get("matches", []) if isinstance(m, dict)]}
                )
        except (OSError, ValueError):
            continue
    return snapshots


def preserved_matches(previous, updates, *, live_only=False, sports=None, keep_live=False):
    identities = {(m.get("sport", "cs2"), m["id"]) for m in updates}
    preserved = []
    for match in previous.get("matches", []):
        if (match.get("sport", "cs2"), match["id"]) in identities:
            continue
        keep = (
            (live_only and match["status"] == "prematch")
            or (sports and match.get("sport", "cs2") not in sports)
            or (keep_live and match["status"] == "live")
        )
        if keep:
            preserved.append(
                {
                    **match,
                    "fetched_at": match.get("fetched_at") or previous["fetched_at"],
                    "expires_at": match.get("expires_at") or previous["expires_at"],
                }
            )
    return preserved


async def collect_one(name, client, directory=DATA):
    folder = directory / name
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "state.json"
    state = read_json(state_path, {})
    now = time.time()
    if state.get("paused"):
        return
    snapshots = schedule_snapshots(directory)
    active = hot_sports(snapshots, now) if name in LIVE_SOURCES else set()
    state.setdefault("last_full_success", state.get("last_success", 0))
    full_due = state["last_full_success"] + INTERVAL
    live_only = name in HTTP_LIVE_SOURCES and now < full_due
    sports = active if name in BROWSER_SOURCES and active and now < full_due else None
    rules = Protego.parse(state.get("robots", ""))
    crawl_delay = rules.crawl_delay(ROBOTS_AGENT) or 0
    # Быстрый режим может сократить обычную паузу, но никогда — backoff после ошибки.
    if active and state.get("last_success") and not state.get("failures") and not state.get("error"):
        state["next_request"] = min(
            state.get("next_request", 0),
            max(state["last_success"], state.get("last_attempt", 0)) + max(FAST_INTERVAL, crawl_delay),
        )
    if now < state.get("next_request", 0):
        return
    # Записываем ДО сети: авария или перезапуск не должны создавать частые запросы.
    interval = FAST_INTERVAL if active else LIVE_DISCOVERY_INTERVAL if name in HTTP_LIVE_SOURCES else INTERVAL
    state["next_request"] = now + max(interval, crawl_delay)
    state["last_attempt"] = now
    save_json(state_path, state)
    url = LIVE_URLS[name] if live_only and name in LIVE_URLS else SOURCES[name]
    text = None
    try:
        rendered = name in ("leon", "betboom")
        if rendered:
            cached_rules = state.get("robots") if now - state.get("robots_checked", 0) < 86400 else None
            options = {"sports": sports} if sports else {}
            response, text, robot_text = await load_rendered(name, url, cached_rules, **options)
            if response.status_code in (401, 403, 451):
                raise StopSource(f"HTTP {response.status_code}: доступ ограничен")
            response.raise_for_status()
            state.update(robots=robot_text, robots_checked=now if cached_rules is None else state["robots_checked"])
        elif now - state.get("robots_checked", 0) >= 86400:
            origin = urlsplit(url)
            _, rules = await request(client, f"{origin.scheme}://{origin.netloc}/robots.txt", robots=True)
            state.update(robots=rules, robots_checked=now)
            save_json(state_path, state)
            # Пауза между robots.txt и линией того же сервера.
            await asyncio.sleep(max(10, Protego.parse(rules).crawl_delay(ROBOTS_AGENT) or 0))
        rules = Protego.parse(state.get("robots", ""))
        if any(not rules.can_fetch(address, ROBOTS_AGENT) for address in BROWSER_URLS.get(name, [url])):
            raise StopSource("robots.txt запрещает этот адрес")
        state["next_request"] = max(state["next_request"], time.time() + (rules.crawl_delay(ROBOTS_AGENT) or 0))
        save_json(state_path, state)
        headers = {
            "Accept": "application/json, text/plain, */*"
            if name in ("fonbet", "pari")
            else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        separate_live_cache = live_only and name in LIVE_URLS
        cache_prefix = "live_" if separate_live_cache else ""
        raw_path = folder / ("live-response.gz" if separate_live_cache else "response.gz")
        if raw_path.exists():
            if state.get(cache_prefix + "etag"):
                headers["If-None-Match"] = state[cache_prefix + "etag"]
            if state.get(cache_prefix + "last_modified"):
                headers["If-Modified-Since"] = state[cache_prefix + "last_modified"]
        if rendered:
            pass  # Линия уже загружена вместе с robots.txt в одном браузерном сеансе.
        elif name == "winline":
            options = {"sports": sports} if sports else {}
            response, text = await load_winline(url, state.get("robots", ""), **options)
            if response.status_code in (401, 403, 451):
                raise StopSource(f"HTTP {response.status_code}: доступ ограничен")
            response.raise_for_status()
        else:
            response, text = await request(client, url, headers)
        if response.status_code == 304:
            if not raw_path.exists():
                raise StopSource("HTTP 304 без локального ответа")
            text = gzip.decompress(raw_path.read_bytes()).decode()
        matches = parse_response(name, text, datetime.now(UTC).isoformat())
        if live_only:
            matches = [m for m in matches if m["status"] == "live"]
        # Пустой список после непустого может означать изменение формата, а не исчезновение линии.
        if not matches and not live_only:
            raise ValueError("Нет подходящих матчей; старый снимок не обновлён")
        if any(
            not isinstance(match.get(side), str) or not match[side].strip()
            for match in matches
            for side in ("team1", "team2")
        ):
            raise ValueError("Не прочитаны названия команд; старый снимок не обновлён")
        fetched = time.time()
        for match in matches:
            match.setdefault("sport", "cs2")
        # Срок хранения отдельно для каждого матча: live не сокращает срок прематча того же источника.
        timed_matches = [
            {
                **match,
                "fetched_at": match.get("fetched_at") or datetime.fromtimestamp(fetched, UTC).isoformat(),
                "expires_at": datetime.fromtimestamp(
                    (datetime.fromisoformat(match["fetched_at"]).timestamp() if match.get("fetched_at") else fetched)
                    + (LIVE_TTL if match["status"] == "live" else INTERVAL),
                    UTC,
                ).isoformat(),
            }
            for match in matches
        ]
        if sports or live_only or name == "marathon":
            # Частичный проход не освежает сохранённый прематч или другую дисциплину.
            previous = read_json(folder / "latest.json", {})
            timed_matches.extend(
                preserved_matches(
                    previous,
                    timed_matches,
                    sports=sports,
                    live_only=live_only,
                    keep_live=name == "marathon" and not live_only,
                )
            )
        snapshot = {
            "bookmaker": name,
            "fetched_at": datetime.fromtimestamp(fetched, UTC).isoformat(),
            "expires_at": datetime.fromtimestamp(fetched + INTERVAL, UTC).isoformat(),
            "matches": timed_matches,
        }
        content = [{k: v for k, v in match.items() if k not in ("fetched_at", "expires_at")} for match in timed_matches]
        digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        if digest != state.get("digest"):
            with (folder / "history.jsonl").open("a") as history:
                history.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
        if response.status_code != 304:
            temporary = raw_path.with_suffix(".tmp")
            temporary.write_bytes(gzip.compress(text.encode()))
            temporary.replace(raw_path)
            state[cache_prefix + "etag"] = response.headers.get("etag")
            state[cache_prefix + "last_modified"] = response.headers.get("last-modified")
        save_json(folder / "latest.json", snapshot)
        state.update(
            digest=digest, failures=0, error=None, paused=False, last_success=fetched, http_status=response.status_code
        )
        if not sports and not live_only:
            state["last_full_success"] = fetched
        snapshots = schedule_snapshots(directory)
        # ISO timestamps round to microseconds and can be just ahead of the raw float.
        active = hot_sports(snapshots, time.time()) if name in LIVE_SOURCES else set()
        delay = (
            FAST_INTERVAL
            if active
            else LIVE_DISCOVERY_INTERVAL
            if name in HTTP_LIVE_SOURCES
            else INTERVAL + random.uniform(0, 60)
        )
        due = fetched + delay
        if name in LIVE_SOURCES:
            due = min(due, state["last_full_success"] + INTERVAL, next_start_check(snapshots, fetched))
        state["next_request"] = max(due, fetched + (rules.crawl_delay(ROBOTS_AGENT) or 0))
        state["refresh_mode"] = "fast" if active else "prematch"
        state["fast_sports"] = sorted(active)
        print(f"{name}: {len(matches)} матчей → {folder / 'latest.json'}")
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        state["http_status"] = code
        if code == 429 or code >= 500:
            failures = state.get("failures", 0) + 1
            delay = min(86400, INTERVAL * 2 ** min(failures, 6))
            delay = max(delay, retry_after(error.response.headers.get("retry-after"), time.time()))
            state.update(failures=failures, next_request=time.time() + delay, error=f"HTTP {code}")
        else:
            state.update(paused=True, error=f"HTTP {code}: нужна ручная проверка")
        print(f"{name}: {state['error']}")
    except httpx.RequestError as error:
        state["http_status"] = None
        failures = state.get("failures", 0) + 1
        state.update(failures=failures, next_request=time.time() + min(86400, INTERVAL * 2 ** min(failures, 6)))
        state["error"] = type(error).__name__
        print(f"{name}: ошибка сети, пауза увеличена")
    except StopSource as error:
        state["http_status"] = None
        state.update(paused=True, error=str(error))
        print(f"{name}: остановлен — {error}")
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        # Неполная загрузка и временно некорректная карточка не останавливают сбор навсегда.
        failures = state.get("failures", 0) + 1
        delay = min(86400, INTERVAL * 2 ** min(failures, 6))
        state.update(
            paused=False,
            failures=failures,
            next_request=time.time() + delay,
            error=str(error),
            http_status=None,
        )
        if text:
            temporary = folder / "failed-response.tmp"
            temporary.write_bytes(gzip.compress(text.encode()))
            temporary.replace(folder / "failed-response.gz")
        print(f"{name}: {error}; повтор через {delay // 60} мин")
    finally:
        save_json(state_path, state)


async def collect_loop(names, watch, *, hltv_enabled=False):
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=3),
    ) as client:
        browser_slot = asyncio.Semaphore(1)

        async def worker(name):
            while True:
                if name in BROWSER_SOURCES:
                    # На сервере одновременно работает не более одного Chrome.
                    async with browser_slot:
                        await collect_one(name, client)
                else:
                    await collect_one(name, client)
                if not watch:
                    return
                await asyncio.sleep(5)

        async with asyncio.TaskGroup() as group:
            for name in names:
                group.create_task(worker(name))
            if watch and hltv_enabled:
                group.create_task(run_hltv(DATA.parent, browser_slot))


async def run(names, watch):
    settings = RankingSettings()
    tasks = [
        asyncio.create_task(heartbeat(DATA.parent / "health" / "collector.json", sources=names)),
        asyncio.create_task(collect_loop(names, watch, hltv_enabled=settings.hltv_enabled)),
    ]
    if settings.valve_rankings_enabled:
        tasks.append(asyncio.create_task(run_rankings(DATA.parent / "valve.json")))
    for source in enabled_sources(settings):
        tasks.append(asyncio.create_task(run_dota_rankings(DATA.parent, source)))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


def main():
    parser = argparse.ArgumentParser(description="Адаптивный сбор прематча и LIVE CS2 / Dota 2")
    parser.add_argument("bookmakers", nargs="*", help="fonbet marathon pari betboom leon winline")
    parser.add_argument("--watch", action="store_true", help="оставаться запущенным и соблюдать сохранённые паузы")
    args = parser.parse_args()
    names = list(dict.fromkeys(args.bookmakers or SOURCES))
    if unknown := set(names) - SOURCES.keys():
        parser.error(f"Неизвестные букмекеры: {', '.join(sorted(unknown))}")
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "Сборщик уже запущен.\n")
        try:
            asyncio.run(run(names, args.watch))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
