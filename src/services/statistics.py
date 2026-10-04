"""PandaScore: общий дисковый кеш расписания и истории команд, запросы только по необходимости."""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from services.matches import date, team_key

logger = logging.getLogger(__name__)


def compact(row):
    return {
        key: row.get(key)
        for key in (
            "id",
            "begin_at",
            "end_at",
            "status",
            "winner_id",
            "forfeit",
            "results",
            "tournament_id",
            "serie_id",
        )
    } | {
        "teams": [
            dict(id=o["opponent"]["id"], name=o["opponent"]["name"])
            for o in row.get("opponents", [])
            if o.get("opponent")
        ],
        "sport": row.get("videogame", {}).get("slug"),
    }


def form(rows, team_id, cutoff, now, tournament_id):
    played = [
        r
        for r in rows
        if r["status"] == "finished"
        and not r["forfeit"]
        and r["winner_id"]
        and date(r["begin_at"])
        and cutoff <= date(r["begin_at"]) < now
        and date(r["end_at"])
        and date(r["end_at"]) <= now
        and len(r["teams"]) == 2
        and team_id in [t["id"] for t in r["teams"]]
    ]
    played.sort(key=lambda r: date(r["begin_at"]), reverse=True)
    recent = played[:5]
    return {
        "games": len(recent),
        "wins": sum(r["winner_id"] == team_id for r in recent),
        "recent": [
            dict(
                date=r["begin_at"],
                won=r["winner_id"] == team_id,
                opponent=next(t["name"] for t in r["teams"] if t["id"] != team_id),
            )
            for r in recent
        ],
        "tournament_games": sum(r["tournament_id"] == tournament_id for r in played) if tournament_id else None,
        "tournament_wins": sum(r["tournament_id"] == tournament_id and r["winner_id"] == team_id for r in played)
        if tournament_id
        else None,
    }


class Statistics:
    def __init__(self, settings, path: Path):
        self.settings, self.path = settings, path
        self.lock = asyncio.Lock()
        try:
            self.state = json.loads(path.read_text())
        except (OSError, ValueError):
            self.state = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, ensure_ascii=False))
        temp.replace(self.path)

    async def get(self, route, ttl):
        async with self.lock:
            now = time.time()
            cache = self.state.setdefault("cache", {})
            entry = cache.get(route)
            if entry and now - entry["fetched_at"] < ttl:
                return entry
            today = datetime.now(UTC).date().isoformat()
            budget = self.state.setdefault("budget", {})
            if budget.get("day") != today:
                budget.update(day=today, used=0)
            if budget["used"] >= self.settings.pandascore_daily_limit or now < self.state.get("retry_at", 0):
                return None
            budget["used"] += 1  # Ошибочные попытки тоже расходуют лимит; сохраняем до запроса.
            self.save()
            params = {"page[size]": 100, "sort": "begin_at" if route.endswith("upcoming") else "-begin_at"}
            if route.startswith("/teams/"):
                params["filter[status]"] = "finished"
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get(
                        "https://api.pandascore.co" + route,
                        params=params,
                        headers={"Authorization": "Bearer " + self.settings.pandascore_api_key.get_secret_value()},
                    )
                if response.status_code == 429:
                    retry = response.headers.get("Retry-After", "")
                    try:
                        delay = int(retry) if retry.isdigit() else parsedate_to_datetime(retry).timestamp() - now
                    except (ValueError, TypeError):
                        delay = 3600
                    self.state["retry_at"] = now + max(3600, delay)
                elif response.status_code in (401, 403):
                    self.state["retry_at"] = now + 3600
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
                    raise ValueError("Некорректный ответ PandaScore")
                rows = [compact(row) for row in payload]
                cache[route] = dict(fetched_at=now, rows=rows)
                if response.headers.get("x-rate-limit-remaining") == "0":
                    self.state["retry_at"] = now + 3600
                # Ограничиваем размер кеша; свежие популярные команды остаются.
                self.state["cache"] = dict(sorted(cache.items(), key=lambda item: item[1]["fetched_at"])[-200:])
                self.save()
                return cache[route]
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                self.state["retry_at"] = max(self.state.get("retry_at", 0), now + 300)
                self.save()
                logger.warning("Статистика PandaScore временно недоступна")
                return None

    async def describe(self, match):
        key = self.settings.pandascore_api_key
        if not key or not key.get_secret_value():
            return None
        game = {"cs2": "csgo", "dota2": "dota2"}[match["sport"]]
        upcoming = await self.get(f"/{game}/matches/upcoming", 3600)
        if not upcoming:
            return None
        sport = "cs-go" if match["sport"] == "cs2" else "dota-2"
        # Команды из уже загруженной истории тоже подходят: дополнительных поисковых запросов нет.
        known = [
            row for entry in self.state.get("cache", {}).values() for row in entry["rows"] if row["sport"] == sport
        ]
        teams = {t["id"]: t for r in [*known, *upcoming["rows"]] for t in r["teams"]}
        selected = [
            [t for t in teams.values() if team_key(t["name"]) == team_key(match[side])] for side in ("team1", "team2")
        ]
        if any(len(candidates) != 1 for candidates in selected):
            return None  # Не расходуем лимит на нечёткий поиск и не путаем Academy с основным составом.
        first, second = [candidates[0] for candidates in selected]
        ids = {first["id"], second["id"]}
        event = next(
            (
                r
                for r in upcoming["rows"]
                if {t["id"] for t in r["teams"]} == ids
                and date(r["begin_at"])
                and date(match.get("starts_at"))
                and abs((date(r["begin_at"]) - date(match["starts_at"])).total_seconds()) <= 300
            ),
            None,
        )
        histories = [await self.get(f"/teams/{team['id']}/matches", 6 * 3600) for team in (first, second)]
        if any(h is None for h in histories):
            return None
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=180)
        rows = [[r for r in h["rows"] if r["sport"] == sport] for h in histories]
        tournament_id = event["tournament_id"] if event else None
        summaries = [
            form(history, team["id"], cutoff, now, tournament_id)
            for history, team in zip(rows, (first, second), strict=True)
        ]
        if not all(s["games"] for s in summaries):
            return None
        h2h = {
            r["id"]: r
            for history in rows
            for r in history
            if {t["id"] for t in r["teams"]} == ids
            and r["status"] == "finished"
            and not r["forfeit"]
            and r["winner_id"] in ids
            and date(r["end_at"])
            and date(r["end_at"]) <= now
            and date(r["begin_at"])
            and cutoff <= date(r["begin_at"]) < now
        }
        return {
            "source": "PandaScore",
            "updated_at": min(h["fetched_at"] for h in histories),
            "team1": summaries[0],
            "team2": summaries[1],
            "head_to_head": {
                "games": len(h2h),
                "team1_wins": sum(r["winner_id"] == first["id"] for r in h2h.values()),
                "period_days": 180,
                "scope": "доступная выборка, до 100 последних матчей каждой команды",
            },
            "roster_changes_verified": False,
        }
