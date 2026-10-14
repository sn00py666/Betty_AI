"""Отчёт по локальным состояниям; никаких запросов к источникам ради /status."""

import logging
import shutil
import time
from datetime import UTC, datetime
from html import escape

from aiogram.exceptions import TelegramAPIError

from services.dota_rankings import SOURCES as DOTA_SOURCES
from services.dota_rankings import available_teams, enabled_sources, load_source, select_ranking
from services.health import read, safe_error, write
from services.matches import BOOKMAKERS, MOSCOW, date

logger = logging.getLogger(__name__)
PROBLEMS = {"error", "blocked", "limited", "stale", "offline"}
ICONS = {"ok": "🟢", "idle": "⚪", "disabled": "⚪", "limited": "🟠", "stale": "🟠"}
LABELS = {
    "ok": "работает",
    "idle": "ожидается первый сигнал",
    "disabled": "выключен",
    "limited": "лимит запросов",
    "stale": "данные устарели",
    "offline": "процесс недоступен",
    "blocked": "доступ ограничен",
    "error": "ошибка",
}


def stamp(value):
    if not value:
        return "—"
    return datetime.fromtimestamp(value, UTC).astimezone(MOSCOW).strftime("%d.%m %H:%M")


def classify(state):
    error = state.get("error")
    if not error:
        return "ok"
    code = state.get("http_status")
    if code in (401, 403, 451) or any(f"HTTP {x}" in str(error) for x in (401, 403, 451)):
        return "blocked"
    if code == 429 or "HTTP 429" in str(error):
        return "limited"
    return "error"


def report_parts(text):
    """Режем по строкам, сохраняя HTML-теги; оставляем запас до лимита Telegram."""
    parts, lines, size = [], [], 0
    for line in text.splitlines():
        length = len(line.encode("utf-16-le")) // 2 + 1
        if lines and size + length > 3500:
            parts.append("\n".join(lines))
            lines, size = [], 0
        lines.append(line)
        size += length
    if lines:
        parts.append("\n".join(lines))
    return parts


class Monitor:
    def __init__(self, settings, directory):
        self.settings, self.directory = settings, directory
        self.started = time.time()
        self.path = directory / "monitor.json"
        try:
            self.sent = read(self.path)
        except (OSError, ValueError):
            self.sent = {}

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        items = []

        def add(name, code, detail):
            items.append(dict(name=name, code=code, detail=safe_error(detail)))

        def load(path, name):
            try:
                state = read(path)
                for key in ("updated_at", "started_at", "last_success", "next_request", "retry_at"):
                    if key in state and (not isinstance(state[key], int | float) or state[key] < 0):
                        raise ValueError("Неверная дата состояния")
                if "matches" in state and not isinstance(state["matches"], list):
                    raise ValueError("Неверный список матчей")
                if state.get("fetched_at") is not None and not isinstance(state["fetched_at"], str):
                    raise ValueError("Неверная дата снимка")
                for key in ("budget", "0"):
                    if key in state:
                        row = state[key]
                        if not isinstance(row, dict) or not isinstance(row.get("used", row.get("count", 0)), int):
                            raise ValueError("Неверный счётчик запросов")
                return state
            except (OSError, ValueError, TypeError, OverflowError):
                add(name, "error", "Файл состояния повреждён или недоступен")
                return None

        for role, label in (("bot", "Telegram-бот"), ("collector", "Сборщик")):
            state = load(self.directory / "health" / f"{role}.json", label)
            if state is None:
                continue
            seen = state.get("updated_at", 0)
            if seen and now - seen <= 180:
                add(label, "ok", f"Работает с {stamp(state.get('started_at'))}")
            elif now - self.started < 120 and not seen:
                add(label, "idle", "Ожидается запуск")
            else:
                add(label, "offline", f"Нет сигнала процесса; последний {stamp(seen)}")

        for name, label in BOOKMAKERS.items():
            folder = self.directory / "odds" / name
            state = load(folder / "state.json", label)
            latest = load(folder / "latest.json", label)
            if state is None or latest is None:
                continue
            fetched = date(latest.get("fetched_at"))
            success = fetched.timestamp() if fetched else state.get("last_success", 0)
            count = len(latest.get("matches", []))
            code = classify(state)
            if state.get("paused") and code == "ok":
                code = "error"
            if not state and not latest:
                code = "idle"
            elif code == "ok" and (not success or now - success > 3900):
                code = "stale"
            detail = f"{count} матчей; обновление {stamp(success)}"
            if state.get("error"):
                detail += f"; {state['error']}"
            if state.get("paused"):
                detail += "; остановлен до ручной проверки"
            elif state.get("next_request", 0) > now:
                detail += f"; следующий запрос {stamp(state['next_request'])}"
            add(label, code, detail)

        for name, configured in (
            ("PandaScore", bool(self.settings.pandascore_api_key)),
            ("OpenRouter AI", bool(self.settings.ai_api_key)),
            ("Valve VRS", self.settings.valve_rankings_enabled),
        ):
            filename = {"PandaScore": "pandascore", "OpenRouter AI": "ai", "Valve VRS": "valve"}[name]
            state = load(self.directory / "health" / f"{filename}.json", name)
            if state is None:
                continue
            code = classify(state) if configured else "disabled"
            if configured and not state:
                code = "idle"
            detail = "Выключен" if not configured else f"Последний успешный запрос {stamp(state.get('last_success'))}"
            if state.get("error"):
                detail += f"; {state['error']}"
            if state.get("retry_at", 0) > now:
                detail += f"; пауза до {stamp(state['retry_at'])}"
            if name == "PandaScore":
                cache = load(self.directory / "pandascore.json", "Кеш PandaScore")
                if cache:
                    budget = cache.get("budget", {})
                    used = (
                        budget.get("used", 0)
                        if budget.get("day") == datetime.fromtimestamp(now, UTC).date().isoformat()
                        else 0
                    )
                    detail += f"; запросы {used}/{self.settings.pandascore_daily_limit} за сутки"
                    if configured and used >= self.settings.pandascore_daily_limit and code in ("ok", "idle"):
                        code = "limited"
                        detail += "; дневной лимит исчерпан"
            elif name == "OpenRouter AI":
                detail += "; без дневного лимита запросов"
                usage = load(self.directory / "ai_usage.json", "Счётчик AI")
                if usage:
                    row = usage.get("0", {})
                    used = (
                        row.get("count", 0)
                        if row.get("date") == datetime.fromtimestamp(now, UTC).date().isoformat()
                        else 0
                    )
                    detail += f"; запросы {used} за сутки"
            elif name == "Valve VRS" and configured:
                cache = load(self.directory / "valve.json", "Кеш Valve")
                if cache:
                    snapshots = cache.get("snapshots", {})
                    try:
                        published = max(
                            (datetime.fromisoformat(row["date"]).replace(tzinfo=UTC) for row in snapshots.values()),
                            default=None,
                        )
                        if snapshots and published is None:
                            raise ValueError("Неверная дата рейтинга")
                    except (ValueError, TypeError, KeyError, AttributeError):
                        code = "error"
                        detail += "; повреждён кеш рейтингов"
                    else:
                        if published:
                            detail += f"; снимок {published.date().isoformat()}"
                        if not state:
                            code = classify(cache)
                        if code == "ok" and (
                            not published or (datetime.fromtimestamp(now, UTC).date() - published.date()).days > 45
                        ):
                            code = "stale"
                elif code == "idle" and now - self.started >= 120:
                    code = "error"
                    detail += "; сбор рейтинга не начался"
            add(name, code, detail)

        hltv = load(self.directory / "hltv" / "state.json", "HLTV")
        if hltv is not None:
            code = classify(hltv) if self.settings.hltv_enabled else "disabled"
            if self.settings.hltv_enabled and not hltv:
                code = "idle"
            if code == "ok" and hltv.get("last_success", 0) < now - 12 * 3600:
                code = "stale"
            detail = f"Последнее обновление {stamp(hltv.get('last_success'))}"
            if hltv.get("error"):
                detail += f"; {hltv['error']}"
            if hltv.get("paused"):
                detail += "; остановлен до ручной проверки"
                if code == "ok":
                    code = "blocked"
            if hltv.get("unresolved_teams"):
                detail += f"; не сопоставлено команд: {len(hltv['unresolved_teams'])}"
            add("HLTV", code, detail)

        sources = enabled_sources(self.settings)
        states = {source: load_source(self.directory, source) for source in sources}
        selected, teams, cached = select_ranking(self.directory, sources, now, states)

        def initial_wait(state):
            if not state:
                return now - self.started < 120
            return (
                not state.get("last_success")
                and not state.get("error")
                and 0 <= now - state.get("last_attempt", 0) < 120
            )

        for source, label in DOTA_SOURCES.items():
            name = f"{label} · Dota 2"
            if source not in sources:
                add(name, "disabled", "Выключен")
                continue
            state = states[source]
            usable = available_teams(source, state, now)
            code = classify(state)
            if initial_wait(state):
                code = "idle"
            elif not state:
                code = "error"
            elif code == "ok" and not usable:
                code = "stale"
            detail = f"{len(usable)} команд; обновление {stamp(state.get('last_success'))}"
            if state.get("published"):
                detail += f"; рейтинг от {state['published']}"
            if state.get("error"):
                detail += f"; {state['error']}"
            if state.get("next_request", 0) > now:
                detail += f"; следующий запрос {stamp(state['next_request'])}"
            if not state:
                detail += "; сбор ещё не начался"
            add(name, code, detail)
        if not sources:
            add("Рейтинг Dota 2", "disabled", "Источники выключены; порядок по букмекерам и времени")
        elif selected:
            mode = "последний пригодный кеш" if cached else ("основной" if selected == "opendota" else "резервный")
            add("Рейтинг Dota 2", "ok", f"{DOTA_SOURCES[selected]} ({mode}); {len(teams)} команд")
        else:
            waiting = any(initial_wait(state) for state in states.values())
            add(
                "Рейтинг Dota 2",
                "idle" if waiting else "stale",
                "Нет свежего рейтинга; порядок по букмекерам и времени",
            )

        disk = shutil.disk_usage(self.directory)
        free = disk.free / (1024**3)
        add("Диск", "error" if free < 0.5 else "ok", f"Свободно {free:.2f} ГБ")
        return items

    def report(self):
        lines = ["<b>Состояние Betty</b>", f"{stamp(time.time())} МСК", ""]
        for item in self.snapshot():
            icon = ICONS.get(item["code"], "🔴")
            lines.extend([f"{icon} <b>{escape(item['name'])}</b> — {LABELS[item['code']]}", escape(item["detail"])])
        return "\n".join(lines)

    async def check(self, bot):
        if not self.settings.admin_user_ids:
            return
        items = self.snapshot()
        now = time.time()
        for user_id in self.settings.admin_user_ids:
            previous = self.sent.setdefault(str(user_id), {})
            if now < previous.get("retry_at", 0):
                continue
            changes = [
                item
                for item in items
                if item["code"] != previous.get("states", {}).get(item["name"])
                and (item["code"] in PROBLEMS or previous.get("states", {}).get(item["name"]) in PROBLEMS)
            ]
            if changes:
                lines = ["<b>Алерт Betty</b>", f"{stamp(now)} МСК", ""]
                for item in changes:
                    icon = "✅" if item["code"] not in PROBLEMS else "⚠️"
                    lines.extend(
                        [f"{icon} <b>{escape(item['name'])}</b> — {LABELS[item['code']]}", escape(item["detail"])]
                    )
                try:
                    for part in report_parts("\n".join(lines)):
                        await bot.send_message(user_id, part, parse_mode="HTML")
                except TelegramAPIError as error:
                    previous["retry_at"] = now + 300
                    logger.warning("Алерт модератору не доставлен (%s)", type(error).__name__)
                    write(self.path, self.sent)
                    continue
            previous.update(states={item["name"]: item["code"] for item in items}, retry_at=0)
            write(self.path, self.sent)
