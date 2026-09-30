"""Pure product-level normalization and analytics helpers."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class ProductNormalizationError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _num(value: Any, label: str) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError) as exc:
        raise ProductNormalizationError(f'{label} is not numeric') from exc


def _wb_scheme(row: dict[str, Any]) -> str:
    raw = ' '.join(str(row.get(k) or '') for k in ('warehouseType', 'deliveryType')).casefold()
    if 'продав' in raw or 'fbs' in raw:
        return 'FBS'
    if 'wb' in raw or 'вб' in raw or 'wildberries' in raw:
        return 'FBW'
    return 'UNKNOWN'


def _ozon_scheme(raw: Any) -> str:
    text = str(raw or '').upper()
    if 'FBO' in text:
        return 'FBO'
    if 'FBS' in text:
        return 'FBS'
    if 'FBP' in text:
        return 'FBP'
    return text or 'UNKNOWN'


@dataclass
class ProductDayObservation:
    data_date: str
    marketplace_sku: str
    name: str
    offer_id: str | None = None
    ordered_units: float = 0.0
    ordered_revenue: float = 0.0
    cancellations_units: float = 0.0
    fulfillment_units: dict[str, float] = field(default_factory=dict)
    as_of: str | None = None


@dataclass(frozen=True)
class StockObservation:
    marketplace_sku: str
    name: str
    available_units: float
    reserved_units: float
    fulfillment_scheme: str
    warehouse_name: str = 'ALL'
    offer_id: str | None = None
    as_of: str | None = None


def normalize_wb_product_orders(payload: Any, *, data_date: str | None = None) -> list[ProductDayObservation]:
    if not isinstance(payload, list):
        raise ProductNormalizationError('WB orders payload must be a list')
    grouped: dict[tuple[str, str], ProductDayObservation] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        day = str(row.get('date') or '')[:10]
        if data_date and day != data_date:
            continue
        if len(day) != 10:
            continue
        sku = str(row.get('nmId') or row.get('barcode') or row.get('supplierArticle') or '').strip()
        if not sku:
            continue
        supplier_article = str(row.get('supplierArticle') or '').strip()
        brand = str(row.get('brand') or '').strip()
        subject = str(row.get('subject') or '').strip()
        name = ' · '.join(x for x in (brand, subject, supplier_article) if x) or f'WB {sku}'
        key = (day, sku)
        obs = grouped.get(key)
        if obs is None:
            obs = grouped[key] = ProductDayObservation(day, sku, name, offer_id=supplier_article or None)
        obs.ordered_units += 1.0
        price = row.get('priceWithDisc')
        if price is None:
            price = row.get('finishedPrice')
        if price is None:
            price = row.get('totalPrice')
        obs.ordered_revenue += _num(price, 'WB order price')
        if bool(row.get('isCancel')):
            obs.cancellations_units += 1.0
        scheme = _wb_scheme(row)
        obs.fulfillment_units[scheme] = obs.fulfillment_units.get(scheme, 0.0) + 1.0
        changed = str(row.get('lastChangeDate') or '')
        if changed and (not obs.as_of or changed > obs.as_of):
            obs.as_of = changed
    return sorted(grouped.values(), key=lambda x: (x.data_date, x.marketplace_sku))


def _dimension_parts(row: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return (day, sku, name) from Ozon analytics dimensions, irrespective of order."""
    day = None
    sku = None
    name = None
    for dim in row.get('dimensions') or []:
        if not isinstance(dim, dict):
            continue
        raw = dim.get('id')
        if raw is None:
            raw = dim.get('value')
        if raw is None:
            raw = dim.get('name')
        value = str(raw or '').strip()
        if len(value) >= 10 and value[:10].count('-') == 2:
            candidate = value[:10]
            try:
                datetime.fromisoformat(candidate)
            except ValueError:
                pass
            else:
                day = candidate
                continue
        if sku is None and value:
            sku = value
            name = str(dim.get('name') or dim.get('value') or '').strip() or None
    return day, sku, name


def normalize_ozon_product_analytics(payload: Any, *, default_date: str | None = None) -> list[ProductDayObservation]:
    if not isinstance(payload, dict):
        raise ProductNormalizationError('Ozon analytics payload must be an object')
    result = payload.get('result') or {}
    rows = result.get('data') or result.get('rows') or []
    if not isinstance(rows, list):
        raise ProductNormalizationError('Ozon analytics rows must be a list')
    grouped: dict[tuple[str, str], ProductDayObservation] = {}
    now = _now()
    for row in rows:
        if not isinstance(row, dict):
            continue
        metrics = row.get('metrics') or []
        if len(metrics) < 2:
            continue
        day, sku, name = _dimension_parts(row)
        day = day or default_date
        if not day or not sku:
            continue
        units = _num(metrics[0], 'Ozon ordered_units')
        revenue = _num(metrics[1], 'Ozon revenue')
        key = (day, sku)
        obs = grouped.get(key)
        if obs is None:
            obs = grouped[key] = ProductDayObservation(day, sku, name or f'Ozon {sku}', as_of=now)
        obs.ordered_units += units
        obs.ordered_revenue += revenue
    return sorted(grouped.values(), key=lambda x: (x.data_date, x.marketplace_sku))


def normalize_wb_stocks(payload: Any, *, fulfillment_scheme: str) -> list[StockObservation]:
    if payload is None:
        return []
    if not isinstance(payload, dict):
        raise ProductNormalizationError('WB stock payload must be an object')
    data = payload.get('data') or {}
    items = (data.get('items') if isinstance(data, dict) else None) or payload.get('items') or []
    if not isinstance(items, list):
        raise ProductNormalizationError('WB stock items must be a list')
    now = _now()
    grouped: dict[tuple[str, str], float] = {}
    for row in items:
        if not isinstance(row, dict):
            continue
        sku = str(row.get('nmId') or '').strip()
        if not sku:
            continue
        warehouse = str(row.get('warehouseName') or row.get('warehouseId') or 'ALL')
        key = (sku, warehouse)
        grouped[key] = grouped.get(key, 0.0) + _num(row.get('quantity'), 'WB stock quantity')
    return [StockObservation(sku, f'WB {sku}', qty, 0.0, fulfillment_scheme, warehouse, as_of=now)
            for (sku, warehouse), qty in sorted(grouped.items())]


def normalize_ozon_stocks(payload: Any) -> list[StockObservation]:
    if not isinstance(payload, dict):
        raise ProductNormalizationError('Ozon stock payload must be an object')
    items = payload.get('items') or []
    if not isinstance(items, list):
        raise ProductNormalizationError('Ozon stock items must be a list')
    now = _now()
    grouped: dict[tuple[str, str | None, str], tuple[float, float]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        offer_id = str(item.get('offer_id') or '').strip() or None
        product_id = str(item.get('product_id') or '').strip()
        raw_stocks = item.get('stocks') or []
        stocks = list(raw_stocks.values()) if isinstance(raw_stocks, dict) else raw_stocks
        for stock in stocks:
            if not isinstance(stock, dict):
                continue
            sku = str(stock.get('sku') or product_id or offer_id or '').strip()
            if not sku:
                continue
            scheme = _ozon_scheme(stock.get('type') or stock.get('shipment_type'))
            key = (sku, offer_id, scheme)
            old = grouped.get(key, (0.0, 0.0))
            grouped[key] = (old[0] + _num(stock.get('present'), 'Ozon stock present'),
                            old[1] + _num(stock.get('reserved'), 'Ozon stock reserved'))
    return [StockObservation(sku, offer or f'Ozon {sku}', present, reserved, scheme,
                             'ALL', offer_id=offer, as_of=now)
            for (sku, offer, scheme), (present, reserved) in sorted(grouped.items(), key=lambda x: str(x[0]))]


def normalize_ozon_postings(payload: Any, *, fulfillment_scheme: str,
                             target_date: str | None = None) -> list[ProductDayObservation]:
    """Normalize current FBO/FBS posting lists into a scheme-only item flow.

    This helper is kept separate from order analytics: postings are useful for
    fulfillment split, but must not replace the common ``ordered_units`` metric.
    """
    if not isinstance(payload, dict):
        raise ProductNormalizationError('Ozon postings payload must be an object')
    rows = payload.get('postings') or (payload.get('result') or {}).get('postings') or []
    if not isinstance(rows, list):
        raise ProductNormalizationError('Ozon postings must be a list')
    grouped: dict[tuple[str, str], ProductDayObservation] = {}
    for posting in rows:
        if not isinstance(posting, dict):
            continue
        day = str(posting.get('created_at') or posting.get('in_process_at') or '')[:10]
        if target_date and day != target_date:
            continue
        if len(day) != 10:
            continue
        for product in posting.get('products') or []:
            if not isinstance(product, dict):
                continue
            sku = str(product.get('sku') or '').strip()
            if not sku:
                continue
            qty = _num(product.get('quantity') or 1, 'Ozon posting quantity')
            offer = str(product.get('offer_id') or '').strip() or None
            name = str(product.get('name') or offer or f'Ozon {sku}')
            key = (day, sku)
            obs = grouped.get(key)
            if obs is None:
                obs = grouped[key] = ProductDayObservation(day, sku, name, offer_id=offer, as_of=_now())
            obs.fulfillment_units[fulfillment_scheme] = obs.fulfillment_units.get(fulfillment_scheme, 0.0) + qty
    return sorted(grouped.values(), key=lambda x: (x.data_date, x.marketplace_sku))
