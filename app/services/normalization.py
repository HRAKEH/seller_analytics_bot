"""Pure conversion of marketplace payloads into explicit internal metrics.

No HTTP and no database access here. This makes business semantics independently testable.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from app.storage.models import MetricPoint
from .numeric import finite_number

class NormalizationError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def normalize_wb_orders(payload: Any, connection_id: int, data_date: str) -> list[MetricPoint]:
    """Normalize WB operational orders for one day.

    One response row is treated as one ordered item. Cancelled rows remain part of
    gross ordered_units and are also counted separately as cancellations_units.
    This keeps the gross order flow observable instead of silently deleting cancels.
    """
    if not isinstance(payload, list):
        raise NormalizationError('WB orders payload must be a list')
    if any(not isinstance(r,dict) for r in payload):
        raise NormalizationError('WB orders contains a non-object row')
    rows = [r for r in payload if str(r.get('date', ''))[:10] == data_date]
    ordered_units = len(rows)
    cancellations = sum(1 for r in rows if bool(r.get('isCancel')))
    order_revenue = 0.0
    for r in rows:
        raw = r.get('priceWithDisc')
        if raw is None:
            raw = r.get('finishedPrice')
        if raw is None:
            raw = r.get('totalPrice')
        try:
            order_revenue += finite_number(raw)
        except (TypeError, ValueError):
            raise NormalizationError('WB orders contains non-numeric order price')
    as_of_values = [str(r.get('lastChangeDate')) for r in rows if r.get('lastChangeDate')]
    as_of = max(as_of_values) if as_of_values else _now()
    return [
        MetricPoint(connection_id, data_date, 'ordered_units', float(ordered_units), 'units', True, as_of),
        MetricPoint(connection_id, data_date, 'ordered_revenue', order_revenue, 'RUB', True, as_of),
        MetricPoint(connection_id, data_date, 'cancellations_units', float(cancellations), 'units', True, as_of),
    ]


def normalize_ozon_order_analytics(payload: Any, connection_id: int, data_date: str) -> list[MetricPoint]:
    """Normalize Ozon analytics ordered_units + revenue for one day.

    `ordered_units` is the common cross-marketplace operational metric. Revenue is
    stored as `ordered_revenue` and deliberately not aggregated with WB money until
    an equivalent WB order-money definition is implemented and verified.
    """
    if not isinstance(payload, dict):
        raise NormalizationError('Ozon analytics payload must be an object')
    result = payload.get('result') or {}
    if not isinstance(result,dict):
        raise NormalizationError('Ozon analytics result must be an object')
    rows = result.get('data') or result.get('rows') or []
    metrics = result.get('totals')
    if not isinstance(metrics, list) or len(metrics) < 2:
        # SKU/day queries return many rows. Summing them is valid for the two
        # explicitly requested additive metrics and avoids the old first-row bug.
        units = 0.0
        revenue = 0.0
        if not isinstance(rows,list) or not rows:
            raise NormalizationError('Ozon analytics has no ordered_units/revenue metrics')
        try:
            for row in rows:
                values = row.get('metrics') if isinstance(row, dict) else None
                if not isinstance(values, list) or len(values) < 2:
                    raise NormalizationError('Ozon analytics row has no ordered_units/revenue metrics')
                units += finite_number(values[0])
                revenue += finite_number(values[1])
        except (TypeError, ValueError) as exc:
            raise NormalizationError('Ozon analytics metrics are not numeric') from exc
    else:
        try:
            units = finite_number(metrics[0])
            revenue = finite_number(metrics[1])
        except (TypeError, ValueError) as exc:
            raise NormalizationError('Ozon analytics metrics are not numeric') from exc
    now = _now()
    return [
        MetricPoint(connection_id, data_date, 'ordered_units', units, 'units', True, now),
        MetricPoint(connection_id, data_date, 'ordered_revenue', revenue, 'RUB', True, now),
    ]


def split_ozon_daily_analytics(payload: Any, connection_id: int) -> dict[str, list[MetricPoint]]:
    """Split an Ozon analytics response requested with dimension=['day']."""
    if not isinstance(payload, dict):
        raise NormalizationError('Ozon analytics payload must be an object')
    result = payload.get('result') or {}
    rows = result.get('data') or result.get('rows') or []
    if not isinstance(rows, list):
        raise NormalizationError('Ozon analytics rows must be a list')
    out: dict[str, list[MetricPoint]] = {}
    now = _now()
    for row in rows:
        if not isinstance(row, dict): continue
        dims = row.get('dimensions') or []
        metrics = row.get('metrics') or []
        if not dims or len(metrics) < 2: continue
        d0 = dims[0] if isinstance(dims[0], dict) else {}
        raw_day = d0.get('id') or d0.get('value') or d0.get('name') or ''
        day = str(raw_day)[:10]
        if len(day) != 10: continue
        try:
            units, revenue = finite_number(metrics[0]), finite_number(metrics[1])
        except (TypeError, ValueError) as exc:
            raise NormalizationError(f'Ozon non-numeric metrics for {day}') from exc
        out[day] = [
            MetricPoint(connection_id, day, 'ordered_units', units, 'units', True, now),
            MetricPoint(connection_id, day, 'ordered_revenue', revenue, 'RUB', True, now),
        ]
    return out
