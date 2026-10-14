"""Свежие снимки букмекеров → список матчей с сопоставимыми коэффициентами."""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from integrations.bookmakers import SOURCES, price
from services.freshness import LIVE_TTL, PREMATCH_TTL
from services.team_aliases import accepted_alias, load_aliases
from services.team_names import branding_key, canonical_name, team_key

MOSCOW = ZoneInfo("Europe/Moscow")
BOOKMAKERS = {
    "fonbet": "Фонбет",
    "pari": "PARI",
    "marathon": "Марафон",
    "winline": "Winline",
    "leon": "Леон",
    "betboom": "BetBoom",
}
logger = logging.getLogger(__name__)


def date(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def team_alignment(left, right, *, allow_branding=False, aliases=None):
    """Порядок команд: 1 — прямой, -1 — обратный, None — нет однозначной пары."""
    for order, indices in ((1, (0, 1)), (-1, (1, 0))):
        exact = [left["teams"][i] == right["teams"][j] for i, j in enumerate(indices)]
        if all(exact):
            return order
        if (allow_branding or aliases) and any(exact):
            candidates = [
                exact[i] or accepted_alias(left[f"team{i + 1}"], right[f"team{j + 1}"], left["sport"], aliases) or (
                    allow_branding and branding_key(left[f"team{i + 1}"]) is not None
                    and branding_key(left[f"team{i + 1}"]) == branding_key(right[f"team{j + 1}"])
                )
                for i, j in enumerate(indices)
            ]
            if all(candidates):
                return order
    return None


def compatible(left, right, *, allow_unknown_live=False, allow_branding=False, aliases=None):
    if left["sport"] != right["sport"] or left["status"] != right["status"]:
        return False
    if team_alignment(left, right, allow_branding=allow_branding, aliases=aliases) is None:
        return False
    if left.get("best_of") and right.get("best_of") and left["best_of"] != right["best_of"]:
        return False
    a, b = date(left.get("starts_at")), date(right.get("starts_at"))
    if a and b:
        return abs((a - b).total_seconds()) <= 300
    return allow_unknown_live and left["status"] == "live"


def group_events(rows, aliases=None):
    """Сначала точные даты, затем однозначные пары среди свежих LIVE-записей."""
    groups = []
    for row in rows:
        candidates = [g for g in groups if all(compatible(member, row) for member in g)]
        group = candidates[0] if len(candidates) == 1 else None
        if group and any(member["bookmaker"] == row["bookmaker"] for member in group):
            group = None
        if group is None:
            groups.append([row])
        else:
            group.append(row)

    live_pairs = {}
    for index, group in enumerate(groups):
        first = group[0]
        if first["status"] == "live":
            key = first["sport"], tuple(sorted(first["teams"]))
            live_pairs.setdefault(key, []).append(index)
    for indices in live_pairs.values():
        if len(indices) < 2:
            continue
        members = [row for index in indices for row in groups[index]]
        # Две записи одной пары у одного букмекера означают неоднозначность.
        # Проверяем все записи, чтобы отсутствующие время/BO не скрыли конфликт.
        if len({row["bookmaker"] for row in members}) != len(members):
            continue
        if not all(compatible(a, b, allow_unknown_live=True) for a, b in combinations(members, 2)):
            continue
        groups[indices[0]] = members
        for index in indices[1:]:
            groups[index] = []
    return merge_branding_variants([group for group in groups if group], aliases)


def merge_branding_variants(groups, aliases=None):
    # Слабые варианты названий разрешены только с известным временем и точным соперником.
    # Рассматриваем всю компоненту кандидатов: два события одного букмекера запрещают слияние.
    def can_merge(a, b):
        # Для LIVE без даты нужны точные имена или отдельное подтверждённое решение LLM.
        known_names = team_alignment(a, b, aliases=aliases) is not None
        return compatible(
            a, b, allow_branding=True, aliases=aliases, allow_unknown_live=bool(aliases) and known_names,
        )

    neighbors = {i: set() for i in range(len(groups))}
    for i, j in combinations(neighbors, 2):
        if all(can_merge(a, b) for a in groups[i] for b in groups[j]):
            neighbors[i].add(j)
            neighbors[j].add(i)
    visited = set()
    result = []
    for index in neighbors:
        if index in visited:
            continue
        component, pending = set(), [index]
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(neighbors[current] - component)
        visited.update(component)
        members = [row for i in sorted(component) for row in groups[i]]
        if (
            len(component) > 1
            and len({row["bookmaker"] for row in members}) == len(members)
            and all(can_merge(a, b) for a, b in combinations(members, 2))
        ):
            result.append(members)
        else:
            result.extend(groups[i] for i in sorted(component))
    return result


def start_label(match, now=None):
    if match["status"] == "live":
        return "LIVE"
    start = date(match.get("starts_at"))
    if start is None:
        return "время уточняется"
    # Обратный отсчёт содержит только полные минуты: это нижняя граница времени старта.
    countdown = "через" in (match.get("start_label") or "").lower()
    seconds = 60 if countdown and (start.second or start.microsecond) else 0 if countdown else 30
    start = (start + timedelta(seconds=seconds)).replace(second=0, microsecond=0).astimezone(MOSCOW)
    today = (now or datetime.now(UTC)).astimezone(MOSCOW).date()
    days = (start.date() - today).days
    day = {0: "сегодня", 1: "завтра"}.get(days, start.strftime("%d.%m"))
    return f"{day} {start:%H:%M}"


def market_probability(match):
    pairs = [(o["p1"], o["p2"]) for o in match["offers"] if o["p1"] and o["p2"]]
    return median(b / (a + b) for a, b in pairs) if pairs else None


class Matches:
    def __init__(self, directory: Path):
        self.directory = directory

    def read(self, now=None, *, use_resolutions=True):
        now = now or datetime.now(UTC)
        aliases = load_aliases(self.directory.parent / "team_aliases.json", now.timestamp()) if use_resolutions else {}
        rows = []
        for bookmaker in SOURCES:
            path = self.directory / bookmaker / "latest.json"
            try:
                snapshot = json.loads(path.read_text())
                fetched = date(snapshot.get("fetched_at"))
                if not fetched or fetched > now:
                    continue
                for raw in snapshot["matches"]:
                    expires = date(raw.get("expires_at") or snapshot.get("expires_at"))
                    if not expires or expires <= now or raw.get("market") != "match_winner":
                        continue
                    status = raw.get("status")
                    if status not in ("live", "prematch"):
                        continue
                    observed = date(raw.get("fetched_at")) or fetched
                    if observed > now:
                        continue
                    age = (now - observed).total_seconds()
                    if age > (LIVE_TTL if status == "live" else PREMATCH_TTL):
                        continue
                    start = date(raw.get("starts_at"))
                    if status == "prematch" and start and start <= now:
                        continue
                    if not raw.get("team1") or not raw.get("team2"):
                        continue
                    source_host = urlsplit(SOURCES[bookmaker]).hostname
                    host = urlsplit(raw.get("url", "")).hostname
                    allowed = {source_host, "fon.bet" if bookmaker == "fonbet" else f"{bookmaker}.ru"}
                    if urlsplit(raw.get("url", "")).scheme != "https" or host not in allowed:
                        continue
                    rows.append(
                        {
                            **raw,
                            "team1": canonical_name(raw["team1"], raw.get("sport", "cs2")),
                            "team2": canonical_name(raw["team2"], raw.get("sport", "cs2")),
                            "bookmaker": bookmaker,
                            "sport": raw.get("sport", "cs2"),
                            "teams": [team_key(raw["team1"]), team_key(raw["team2"])],
                            "fetched_at": observed.isoformat(),
                            "p1": price(raw.get("p1")),
                            "p2": price(raw.get("p2")),
                        }
                    )
            except (OSError, ValueError, KeyError, TypeError):
                logger.warning("Не удалось прочитать снимок %s", bookmaker)
        # Точные даты первыми; одинаковый порядок сохраняет идентификатор при обновлении цен.
        rows.sort(key=lambda row: (not bool(date(row.get("starts_at"))), row["bookmaker"], row["id"]))
        groups = []
        for members in group_events(rows, aliases):
            first = members[0]
            identity = f"{first['sport']}:{first['bookmaker']}:{first['id']}"
            group = {**first, "id": hashlib.sha256(identity.encode()).hexdigest()[:16], "offers": []}
            # Время из расписания точнее оценки по обратному отсчёту. ID и порядок команд сохраняем.
            scheduled = next(
                (row for row in members if date(row.get("starts_at"))
                 and "через" not in (row.get("start_label") or "").lower()), None
            )
            if scheduled:
                group.update(starts_at=scheduled["starts_at"], start_label=scheduled.get("start_label"))
            groups.append(group)
            for row in members:
                p1, p2 = row["p1"], row["p2"]
                if team_alignment(group, row, allow_branding=True, aliases=aliases) == -1:
                    p1, p2 = p2, p1
                group["offers"].append(
                    {
                        "bookmaker": row["bookmaker"],
                        "p1": p1,
                        "p2": p2,
                        "url": row["url"],
                        "fetched_at": row["fetched_at"],
                    }
                )
        return sorted(groups, key=match_order)

    def get(self, match_id):
        return next((match for match in self.read() if match["id"] == match_id), None)


def match_order(match):
    return (match["status"] != "live", date(match.get("starts_at")) or datetime.max.replace(tzinfo=UTC), match["team1"])
