"""Readable display dates; API arguments and persisted dates stay unchanged."""
from __future__ import annotations

from contextvars import ContextVar
from datetime import date, datetime, timezone
from functools import wraps
import re
from zoneinfo import ZoneInfo

display_timezone = ContextVar('display_timezone', default='Europe/Moscow')
_ISO = re.compile(r'(?<![\w/])\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?(?![\w])')
_PROTECTED = re.compile(r'(<(?:code|pre)\b[^>]*>.*?</(?:code|pre)>|https?://[^\s<>]+|(?<!\w)/[a-z_]+(?:@[A-Za-z0-9_]+)?[^\n<]*)', re.S)


def display_day(value) -> str:
    try:
        day = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
        return day.strftime('%d.%m.%Y')
    except (ValueError, TypeError):
        return str(value or '—')


def display_time(value, *, tz: str | None = None) -> str:
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(ZoneInfo(tz or display_timezone.get())).strftime('%d.%m.%Y %H:%M')
    except (ValueError, TypeError):
        return str(value or '—')


def readable_text(text: str, *, tz: str | None = None) -> str:
    """Format complete ISO values, preserving copyable commands and URLs."""
    def replace(match):
        value = match.group()
        return display_time(value, tz=tz) if len(value) > 10 else display_day(value)
    return ''.join(part if i % 2 else _ISO.sub(replace, part)
                   for i, part in enumerate(_PROTECTED.split(text)))


def readable_dates(formatter):
    @wraps(formatter)
    def render(*args, **kwargs):
        tz = kwargs.pop('timezone', None)
        token = display_timezone.set(tz) if tz else None
        try:
            return readable_text(formatter(*args, **kwargs))
        finally:
            if token is not None:
                display_timezone.reset(token)
    return render
