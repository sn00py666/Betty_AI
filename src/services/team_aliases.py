"""Кеш решений о названиях; применения всегда ограничены контекстом конкретного матча."""

import hashlib
import json
import re
from pathlib import Path

from services.team_names import team_key

VERSION = 1
ROSTER_WORDS = {
    "academy",
    "junior",
    "youngsters",
    "youth",
    "nxt",
    "ares",
    "force",
    "green",
    "red",
    "women",
    "female",
    "fe",
    "ii",
    "2",
    "ac",
    "ex",
    "академия",
    "женщины",
}


def roster_markers(name):
    return set(re.findall(r"\w+", name.casefold())) & ROSTER_WORDS


def alias_key(sport, first, second):
    value = [VERSION, sport, *sorted((team_key(first), team_key(second)))]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def load_aliases(path: Path, now):
    try:
        data = json.loads(path.read_text())
        if data.get("version") != VERSION or not isinstance(data.get("decisions"), dict):
            return {}
        return {
            key: row
            for key, row in data["decisions"].items()
            if isinstance(row, dict) and isinstance(row.get("expires_at"), int | float) and row["expires_at"] > now
        }
    except (OSError, ValueError, AttributeError):
        return {}


def accepted_alias(first, second, sport, aliases):
    if not aliases or roster_markers(first) != roster_markers(second):
        return False
    row = aliases.get(alias_key(sport, first, second), {})
    return row.get("relation") == "same_team" and row.get("evidence") in (
        "spelling",
        "abbreviation",
        "transliteration",
        "known_alias",
    )
