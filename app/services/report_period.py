"""Consistent period arguments for date-anchored report commands."""
from datetime import date, timedelta


def parse_report_period(text: str, default_days: int, today: date) -> tuple[int,date,date]:
    parts=text.split()
    if len(parts)>3:raise ValueError('Лишние аргументы')
    days=int(parts[1]) if len(parts)>1 else min(default_days,31)
    end=date.fromisoformat(parts[2]) if len(parts)>2 else today-timedelta(days=1)
    if not 1<=days<=31:raise ValueError('Период: от 1 до 31 дня')
    if end>today:raise ValueError('Будущая дата недоступна')
    return days,end-timedelta(days=days-1),end
