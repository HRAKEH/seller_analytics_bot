"""Per-shop runtime preferences with environment defaults and validation."""
from __future__ import annotations
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

from app.config import Settings
from app.storage import Repository
from app.storage.models import ShopPreferences


def defaults_from_settings(settings: Settings) -> dict[str, Any]:
    return {
        'timezone': settings.timezone,
        'report_time': settings.report_time,
        'product_report_days': settings.product_report_days,
        'stock_velocity_days': settings.stock_velocity_days,
        'stock_risk_days': settings.stock_risk_days,
        'finance_lookback_days': settings.finance_lookback_days,
        'alerts_enabled': settings.alerts_enabled,
        'alerts_interval_minutes': settings.alerts_interval_minutes,
        'alert_order_drop_pct': settings.alert_order_drop_pct,
        'alert_order_lookback_days': settings.alert_order_lookback_days,
        'alert_api_stale_hours': settings.alert_api_stale_hours,
        'alert_drr_pct': settings.alert_drr_pct,
        'alert_cooldown_minutes': settings.alert_cooldown_minutes,
        'demo_mode': False,
        'onboarding_version': '',
    }


def validate_timezone(value: str) -> str:
    clean=(value or '').strip()
    if not clean:
        raise ValueError('Часовой пояс не может быть пустым.')
    try: ZoneInfo(clean)
    except ZoneInfoNotFoundError as exc:
        raise ValueError('Неизвестный часовой пояс. Пример: Europe/Moscow') from exc
    return clean


def validate_report_time(value: str) -> str:
    clean=(value or '').strip()
    try:
        hh,mm=clean.split(':',1); time(hour=int(hh),minute=int(mm))
    except (ValueError,TypeError) as exc:
        raise ValueError('Время должно быть в формате HH:MM, например 09:00.') from exc
    return f'{int(hh):02d}:{int(mm):02d}'


def _bounded_int(value: Any, low: int, high: int, label: str) -> int:
    try: n=int(value)
    except (TypeError,ValueError) as exc: raise ValueError(f'{label}: требуется целое число.') from exc
    if not low <= n <= high: raise ValueError(f'{label}: допустимо {low}–{high}.')
    return n


def _bounded_float(value: Any, low: float, high: float, label: str) -> float:
    try: n=float(str(value).replace(',','.'))
    except (TypeError,ValueError) as exc: raise ValueError(f'{label}: требуется число.') from exc
    if not low <= n <= high: raise ValueError(f'{label}: допустимо {low:g}–{high:g}.')
    return n


def update_validated(repo: Repository, shop_id: int, **changes: Any) -> ShopPreferences:
    clean=dict(changes)
    if 'timezone' in clean: clean['timezone']=validate_timezone(str(clean['timezone']))
    if 'report_time' in clean: clean['report_time']=validate_report_time(str(clean['report_time']))
    rules={
        'product_report_days':(1,30,'Дней товарного отчёта'),
        'stock_velocity_days':(3,60,'Окно скорости продаж'),
        'stock_risk_days':(1,90,'Порог запаса'),
        'finance_lookback_days':(1,90,'Финансовый период'),
        'alerts_interval_minutes':(15,1440,'Интервал алертов'),
        'alert_order_lookback_days':(3,60,'Окно сравнения заказов'),
        'alert_cooldown_minutes':(30,43200,'Cooldown алерта'),
    }
    for key,(lo,hi,label) in rules.items():
        if key in clean: clean[key]=_bounded_int(clean[key],lo,hi,label)
    floats={
        'alert_order_drop_pct':(1,95,'Порог падения заказов'),
        'alert_api_stale_hours':(1,720,'Порог свежести API'),
        'alert_drr_pct':(1,500,'Порог ДРР'),
    }
    for key,(lo,hi,label) in floats.items():
        if key in clean: clean[key]=_bounded_float(clean[key],lo,hi,label)
    return repo.update_shop_preferences(shop_id,**clean)
