"""Фаворит принадлежит конкретной карточке и быстро устаревает в LIVE."""

import time

from services.team_names import team_key


def favorite_side(match, commentary):
    if (
        match.get("statistics")
        and commentary
        and commentary.winner in ("team1", "team2")
        and commentary.probability is not None
        and commentary.probability > 50
    ):
        return commentary.winner
    return None


def identity(match):
    return [match.get(k) for k in ("id", "sport", "status", "starts_at", "tournament", "best_of")] + [
        team_key(match[side]) for side in ("team1", "team2")
    ]


def remember(users, user_id, message_id, match, commentary):
    side = favorite_side(match, commentary)
    now = time.time()
    records = {k: v for k, v in users.get(user_id).get("forecasts", {}).items() if v["expires_at"] > now}
    record = dict(identity=identity(match), side=side, expires_at=now + (120 if match["status"] == "live" else 900))
    records[str(message_id)] = record
    users.update(user_id, forecasts=dict(list(records.items())[-30:]))
    return record


def recall(record, match):
    if record and record.get("expires_at", 0) > time.time() and record.get("identity") == identity(match):
        return record.get("side")
    return None
