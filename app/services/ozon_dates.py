"""Align UTC Ozon posting timestamps with Moscow calendar reporting days."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo('Europe/Moscow')


def posting_day(value: object) -> str | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        # Date-only imported fixtures already denote a reporting day.
        if len(text) == 10:
            return date.fromisoformat(text).isoformat()
        stamp = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(MOSCOW).date().isoformat()
    except ValueError:
        return None


def posting_range(start: date, end: date) -> tuple[str, str]:
    if end < start:
        raise ValueError('end must not be before start')
    first = datetime.combine(start, time.min, MOSCOW)
    last = datetime.combine(end + timedelta(days=1), time.min, MOSCOW) - timedelta(milliseconds=1)
    return tuple(stamp.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
                 for stamp in (first, last))
