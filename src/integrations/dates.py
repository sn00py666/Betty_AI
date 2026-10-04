"""Даты линии: только с известным часовым поясом и временем получения страницы."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

MONTHS = "янв фев мар апр май июн июл авг сен окт ноя дек".split()


def start_time(label, captured_at, timezone="Europe/Moscow"):
    if not label or not captured_at:
        return None
    anchor = datetime.fromisoformat(captured_at)
    if anchor.tzinfo is None:
        return None
    anchor = anchor.astimezone(ZoneInfo(timezone))
    label = " ".join(label.lower().split())
    if "через" in label:
        minutes = re.search(r"(\d+)\s*мин", label)
        if not minutes:
            return None  # Отсчёт только в часах слишком неточен для сопоставления.
        days = re.search(r"(\d+)\s*(?:дн|день)", label)
        hours = re.search(r"(\d+)\s*час", label)
        delta = timedelta(
            days=int(days[1]) if days else 0, hours=int(hours[1]) if hours else 0, minutes=int(minutes[1])
        )
        start = anchor + delta
    else:
        clock = re.search(r"(\d{1,2}):(\d{2})", label)
        if not clock:
            return None
        start = anchor.replace(hour=int(clock[1]), minute=int(clock[2]), second=0, microsecond=0)
        if "завтра" in label:
            start += timedelta(days=1)
        elif "сегодня" not in label:
            day = re.search(r"(\d{1,2})\s+(янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)", label)
            numeric = re.search(r"(\d{1,2})\.(\d{2})(?:\.(\d{4}|\d{2}))?", label)
            if day:
                month = 5 if day[2] in ("май", "мая") else MONTHS.index(day[2]) + 1
                start = start.replace(month=month, day=int(day[1]))
            elif numeric:
                start = start.replace(
                    year=(int(numeric[3]) + (2000 if len(numeric[3]) == 2 else 0)) if numeric[3] else anchor.year,
                    month=int(numeric[2]),
                    day=int(numeric[1]),
                )
            else:
                return None
            if not numeric or not numeric[3]:
                if (start - anchor).days < -180:
                    start = start.replace(year=start.year + 1)
                elif (start - anchor).days > 180:
                    start = start.replace(year=start.year - 1)
    return start.astimezone(UTC).isoformat()
