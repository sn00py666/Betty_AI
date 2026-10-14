"""Targeted, restart-safe HLTV collection; one browser slot shared with bookmakers."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from protego import Protego

from integrations.errors import StopSource
from integrations.hltv import BASE, parse_schedule
from integrations.hltv_browser import AGENT, load_page
from services.health import read, write
from services.hltv import MATCH_TTL, PROFILE_TTL, HltvCache
from services.matches import Matches, date
from services.team_names import team_key

logger = logging.getLogger(__name__)
MIN_INTERVAL = 30
DISCOVERY = {BASE + "/matches": 900}


def targets(directory, now):
    matches = [m for m in Matches(directory / "odds").read(datetime.fromtimestamp(now, UTC)) if m["sport"] == "cs2"]
    names = set()
    for match in matches:
        start = date(match.get("starts_at"))
        if match["status"] == "live" or (start and start.timestamp() <= now + 7 * 86400):
            names.update((match["team1"], match["team2"]))
    for path in (directory / "hltv" / "wanted").glob("*.json"):
        try:
            row = read(path)
            if 0 <= now - row.get("requested_at", 0) < 86400:
                names.add(row["name"])
            elif now - row.get("requested_at", 0) >= 86400:
                path.unlink()
        except (ValueError, OSError, KeyError):
            continue
    return matches, names


def next_job(cache, state, matches, names, now):
    jobs = []
    for address, ttl in DISCOVERY.items():
        due = state.get("discovery", {}).get(address, 0) + ttl
        jobs.append((due, address))
    keys = {team_key(name) for name in names}
    schedule = read(cache.directory / "schedule.json")
    if now - schedule.get("fetched_at", 0) < 1800:
        for record in schedule.get("matches", []):
            if not keys.intersection(team_key(name) for name in record["names"]):
                continue
            entry = cache.load(record["url"])
            start = date(record.get("starts_at"))
            ttl = MATCH_TTL if record["live"] or (start and start.timestamp() <= now + 3600) else 3600
            jobs.append((entry.get("fetched_at", 0) + ttl, record["url"]))
    unresolved = []
    for name in sorted(names):
        team = cache.team(name)
        if not team:
            unresolved.append(name)
            continue
        entry = cache.load(team["url"])
        related = [m for m in matches if team_key(name) in (team_key(m["team1"]), team_key(m["team2"]))]
        imminent = any(
            m["status"] == "live" or (date(m.get("starts_at")) and 0 <= date(m["starts_at"]).timestamp() - now <= 3600)
            for m in related
        )
        ttl = 3600 if imminent else PROFILE_TTL
        jobs.append((entry.get("fetched_at", 0) + ttl, team["url"]))
        if entry:
            data = entry["data"]
            # Match pages for the profile's recent history and upcoming schedule.
            for record in data["results"] + data["upcoming"]:
                start = date(record.get("starts_at"))
                if not start or not now - 90 * 86400 <= start.timestamp() <= now + 7 * 86400:
                    continue
                detail = cache.load(record["url"])
                if detail.get("data", {}).get("status") == "finished":
                    ttl_match = 7 * 86400
                else:
                    ttl_match = MATCH_TTL if start.timestamp() <= now + 3600 else 3600
                # Prioritize team profiles, then imminent matches, then backfill history.
                due = detail.get("fetched_at", 0) + ttl_match
                priority = 0 if record in data["upcoming"] else 1
                jobs.append((max(due, now - 1) if priority else due, record["url"]))
    state["unresolved_teams"] = unresolved
    due = [(due, address) for due, address in jobs if due <= now]
    return min(due)[1] if due else None


async def collect_hltv(directory, browser_slot, loader=load_page, now=None):
    now = time.time() if now is None else now
    folder = directory / "hltv"
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "state.json"
    try:
        state = read(state_path)
    except (OSError, ValueError):
        logger.error("HLTV state unreadable; collection remains stopped")
        return
    if state.get("paused") or now < state.get("next_request", 0):
        return
    cache = HltvCache(folder)
    matches, names = targets(directory, now)
    address = next_job(cache, state, matches, names, now)
    if address is None:
        write(state_path, state)
        return
    # Persist reservation before waiting for Chrome: a restart cannot bypass the delay.
    state.update(last_attempt=now, next_request=now + MIN_INTERVAL, current_url=address)
    write(state_path, state)
    try:
        async with browser_slot:
            html = await loader(address, folder, state)
        fetched_at = time.time()
        if address in DISCOVERY:
            if address == BASE + "/matches":
                write(folder / "schedule.json", {"fetched_at": fetched_at, "matches": parse_schedule(html)})
            state.setdefault("discovery", {})[address] = fetched_at
        else:
            entry = cache.store(address, html, fetched_at)
            if entry["data"]["kind"] == "team":
                cache.register(
                    [team for row in entry["data"]["results"] + entry["data"]["upcoming"] for team in row["teams"]]
                )
            else:
                cache.register(entry["data"]["teams"])
        delay = max(MIN_INTERVAL, Protego.parse(state.get("robots", "")).crawl_delay(AGENT) or 0)
        state.update(last_success=fetched_at, error=None, failures=0, http_status=200, next_request=fetched_at + delay)
    except StopSource as error:
        state.update(paused=True, error=str(error))
    except (httpx.HTTPError, ValueError, OSError, KeyError, TypeError) as error:
        code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
        failures = state.get("failures", 0) + 1
        delay = min(86400, 300 * 2 ** min(failures, 8))
        if code == 429:
            retry = error.response.headers.get("Retry-After", "")
            try:
                delay = max(
                    delay, int(retry) if retry.isdigit() else parsedate_to_datetime(retry).timestamp() - time.time()
                )
            except (ValueError, TypeError, OverflowError):
                pass
            delay = max(3600, delay)
        state.update(
            failures=failures,
            error=f"HTTP {code}" if code else type(error).__name__,
            next_request=time.time() + delay,
            http_status=code,
        )
        logger.warning("HLTV collection failed: %s", state["error"])
    finally:
        write(state_path, state)


async def run_hltv(directory, browser_slot):
    while True:
        try:
            await collect_hltv(directory, browser_slot)
        except Exception:
            # A broken external source must not terminate live bookmaker collection.
            logger.exception("HLTV worker failed")
        await asyncio.sleep(30)
