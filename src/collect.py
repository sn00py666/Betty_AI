"""Запуск: uv run python src/collect.py [fonbet marathon] [--watch]."""

import argparse
import asyncio
import fcntl
import gzip
import hashlib
import json
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from protego import Protego

from integrations.bookmakers import PARSERS, SOURCES
from integrations.browser import load_rendered, load_winline

DATA = Path(__file__).resolve().parent.parent / "data" / "odds"
# Профиль Chrome 154 на macOS (версия установленного Chrome при настройке).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
ROBOTS_AGENT = "BettyAI/0.1"  # Смена HTTP-заголовка не снимает ограничения для нашего сборщика.
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"}
INTERVAL = 1800  # Один снимок раз в полчаса. Это архив прематча, не live-фид.
MAX_BYTES = 20 * 1024 * 1024


def read_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


class StopSource(Exception):
    """Прекратить запросы до ручной проверки источника."""


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
        if response.status_code in (401, 403, 451) or response.is_redirect:
            raise StopSource(f"HTTP {response.status_code}: доступ ограничен или адрес изменился")
        if robots and response.status_code in (404, 410):
            return response, ""
        if response.status_code == 304:
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


async def collect_one(name, client, directory=DATA):
    folder = directory / name
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "state.json"
    state = read_json(state_path, {})
    now = time.time()
    if state.get("paused"):
        print(f"{name}: остановлен — {state.get('error')}")
        return
    if now < state.get("next_request", 0):
        print(f"{name}: пауза ещё {int(state['next_request'] - now)} с")
        return
    # Записываем ДО сети: авария или перезапуск не должны создавать частые запросы.
    state["next_request"] = now + INTERVAL + random.uniform(0, 60)
    save_json(state_path, state)
    url = SOURCES[name]
    try:
        rendered = name in ("leon", "betboom")
        if rendered:
            cached_rules = state.get("robots") if now - state.get("robots_checked", 0) < 86400 else None
            response, text, robot_text = await load_rendered(name, url, cached_rules)
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
        if not rules.can_fetch(url, ROBOTS_AGENT):
            raise StopSource("robots.txt запрещает этот адрес")
        state["next_request"] = max(state["next_request"], time.time() + (rules.crawl_delay(ROBOTS_AGENT) or 0))
        save_json(state_path, state)
        headers = {
            "Accept": "application/json, text/plain, */*"
            if name in ("fonbet", "pari")
            else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        raw_path = folder / "response.gz"
        if raw_path.exists():
            if state.get("etag"):
                headers["If-None-Match"] = state["etag"]
            if state.get("last_modified"):
                headers["If-Modified-Since"] = state["last_modified"]
        if rendered:
            pass  # Линия уже загружена вместе с robots.txt в одном браузерном сеансе.
        elif name == "winline":
            response, text = await load_winline(url)
            if response.status_code in (401, 403, 451):
                raise StopSource(f"HTTP {response.status_code}: доступ ограничен")
            response.raise_for_status()
        else:
            response, text = await request(client, url, headers)
        if response.status_code == 304:
            if not raw_path.exists():
                raise StopSource("HTTP 304 без локального ответа")
            text = gzip.decompress(raw_path.read_bytes()).decode()
        matches = PARSERS[name](text)
        # Пустой список после непустого может означать изменение формата, а не исчезновение линии.
        if not matches:
            raise StopSource("Нет подходящих матчей: требуется проверить источник; старый снимок не обновлён")
        fetched = time.time()
        for match in matches:
            match.setdefault("sport", "cs2")
        # Срок хранения отдельно для каждого матча: live не сокращает срок прематча того же источника.
        timed_matches = [
            {
                **match,
                "expires_at": datetime.fromtimestamp(
                    fetched + (60 if match["status"] == "live" else INTERVAL), UTC
                ).isoformat(),
            }
            for match in matches
        ]
        snapshot = {
            "bookmaker": name,
            "fetched_at": datetime.fromtimestamp(fetched, UTC).isoformat(),
            "expires_at": datetime.fromtimestamp(
                fetched + (60 if any(m["status"] == "live" for m in matches) else INTERVAL), UTC
            ).isoformat(),
            "matches": timed_matches,
        }
        digest = hashlib.sha256(json.dumps(matches, sort_keys=True).encode()).hexdigest()
        if digest != state.get("digest"):
            with (folder / "history.jsonl").open("a") as history:
                history.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
        if response.status_code != 304:
            temporary = raw_path.with_suffix(".tmp")
            temporary.write_bytes(gzip.compress(text.encode()))
            temporary.replace(raw_path)
            state.update(etag=response.headers.get("etag"), last_modified=response.headers.get("last-modified"))
        save_json(folder / "latest.json", snapshot)
        state.update(digest=digest, failures=0, error=None)
        print(f"{name}: {len(matches)} матчей → {folder / 'latest.json'}")
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        if code == 429 or code >= 500:
            failures = state.get("failures", 0) + 1
            delay = min(86400, INTERVAL * 2 ** min(failures, 6))
            delay = max(delay, retry_after(error.response.headers.get("retry-after"), time.time()))
            state.update(failures=failures, next_request=time.time() + delay, error=f"HTTP {code}")
        else:
            state.update(paused=True, error=f"HTTP {code}: нужна ручная проверка")
        print(f"{name}: {state['error']}")
    except httpx.RequestError as error:
        failures = state.get("failures", 0) + 1
        state.update(failures=failures, next_request=time.time() + min(86400, INTERVAL * 2 ** min(failures, 6)))
        state["error"] = type(error).__name__
        print(f"{name}: ошибка сети, пауза увеличена")
    except (StopSource, ValueError, KeyError, TypeError, OverflowError) as error:
        state.update(paused=True, error=str(error))
        print(f"{name}: остановлен — {error}")
    finally:
        save_json(state_path, state)


async def run(names, watch):
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=1),
    ) as client:
        while True:
            for name in names:
                await collect_one(name, client)
            if not watch:
                return
            await asyncio.sleep(60)


def main():
    parser = argparse.ArgumentParser(description="Редкие снимки линии CS2 / Dota 2")
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
