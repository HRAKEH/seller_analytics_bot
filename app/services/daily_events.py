"""Daily events, independent of the creation-day order cohort and net accruals.

Read the latest raw snapshot rather than summing historical observations: a
corrected or explicitly empty response replaces the previous daily result.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP, localcontext
import json

from .ozon_dates import MOSCOW, posting_day

WB_SALES = 'statistics/sales/reconciliation'
WB_CANCELS = 'statistics/cancellations/day'
OZON_RETURNS = 'returns/customer/day'
OZON_REALIZATION = 'finance/realization/by-day'


class DailyEventError(ValueError):
    pass


@dataclass(frozen=True)
class DayEvent:
    units: int | None = None
    amount: Decimal | None = None
    source: str = ''
    freshness: str | None = None
    warning: str | None = None
    available_units: int = 0
    price_note: str | None = None
    load_failed: bool = False
    unavailable_note: str | None = None


@dataclass(frozen=True)
class DailyEvents:
    buyouts: DayEvent
    returns: DayEvent
    cancellations: DayEvent


def wb_event_day(value) -> str | None:
    """WB dates without an offset are already Moscow dates, unlike Ozon UTC."""
    try:
        text = str(value or '')
        if len(text) == 10:
            return date.fromisoformat(text).isoformat()
        stamp = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(MOSCOW)
        return stamp.date().isoformat()
    except (ValueError, TypeError):
        return None


def _number(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and abs(amount) <= Decimal('1e18') else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _quantity(value) -> int | None:
    number = _number(value)
    if number is None or not 0 <= number <= 1_000_000_000 or number != number.to_integral_value():
        return None
    return int(number)


def _money(value: Decimal) -> Decimal:
    try:
        with localcontext() as context:
            context.prec=50
            return value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    except DecimalException as exc:
        raise DailyEventError('Источник передал некорректную денежную сумму.') from exc


def _rows(payload, key: str | None = None) -> list[dict]:
    rows = payload.get(key) if key and isinstance(payload, dict) else payload
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise DailyEventError('Источник событий вернул ответ неизвестного формата.')
    return rows


def _unique(rows: list[dict], key: str, *, revision: str | None = None) -> list[dict]:
    chosen = {}
    for row in rows:
        raw_identifier=row.get(key)
        if key=='id':
            if (isinstance(raw_identifier,bool) or not isinstance(raw_identifier,(str,int))
                    or not str(raw_identifier).isdigit() or int(raw_identifier)<=0):
                raise DailyEventError('В строке возврата нет корректного идентификатора.')
        elif not isinstance(raw_identifier,str):
            raise DailyEventError('В строке события нет корректного идентификатора.')
        identifier = str(raw_identifier or '').strip()
        if not identifier:
            raise DailyEventError('В строке события нет уникального идентификатора.')
        old = chosen.get(identifier)
        if old is not None and old != row:
            old_revision = str(old.get(revision) or '') if revision else ''
            new_revision = str(row.get(revision) or '') if revision else ''
            if not old_revision or not new_revision or old_revision == new_revision:
                raise DailyEventError('Источник вернул противоречивые версии одного события.')
            if new_revision < old_revision:
                continue
        chosen[identifier] = row
    return list(chosen.values())


def normalize_wb_sales_day(payload, day: str) -> tuple[DayEvent, DayEvent]:
    rows = _unique(_rows(payload), 'saleID', revision='lastChangeDate')
    counts = {'S': 0, 'R': 0}
    amounts = {'S': Decimal(0), 'R': Decimal(0)}
    priced = {'S': True, 'R': True}
    for row in rows:
        ds = wb_event_day(row.get('date'))
        if ds is None or ds<'2000-01-01':
            raise DailyEventError('WB не передал дату продажи или возврата.')
        if ds != day:
            continue
        kind = str(row['saleID'])[:1].upper()
        if kind not in counts:
            raise DailyEventError('WB передал неизвестный вид продажи или возврата.')
        counts[kind] += 1  # WB: one row is one sold/returned item.
        amount = _number(row.get('finishedPrice'))
        # These fields are filled asynchronously; zero with actual items is
        # not proof of a free purchase. Never replace it with priceWithDisc.
        if amount is None or amount == 0 or kind == 'S' and amount < 0:
            priced[kind] = False
        else:
            amounts[kind] += abs(amount)
    result = []
    for kind in ('S', 'R'):
        result.append(DayEvent(counts[kind], _money(amounts[kind]) if priced[kind] else None,
            'WB · продажи и возвраты по дате операции', available_units=counts[kind],
            price_note='Цена покупателя из WB, без скидки WB Кошелька.' if priced[kind]
                else 'WB ещё не заполнил все цены покупателей; сумма не рассчитана.'))
    return tuple(result)


def split_event_rows(payload, source: str, start: str, end: str) -> tuple[dict[str, list[dict]], int]:
    """Keep exact event dates; undated relevant rows make the range partial."""
    if source == WB_CANCELS:
        rows = _unique(_rows(payload), 'srid', revision='lastChangeDate')
    elif source == OZON_RETURNS:
        rows = _unique(_rows(payload, 'returns'), 'id')
    else:
        raise ValueError('Unsupported daily event source')
    grouped = {}
    undated = 0
    for row in rows:
        if source == WB_CANCELS:
            flag = row.get('isCancel')
            if flag is False:
                continue
            if flag is not True:
                undated += 1
                continue
            ds = wb_event_day(row.get('cancelDate'))
            # Do not substitute lastChangeDate or the order date.
            if ds is not None and ds < '2000-01-01':
                ds = None
        else:
            kind = row.get('type')
            if kind in {'Cancellation', 'FullReturn', 'PartialReturn', 'Unknown'}:
                continue
            if kind != 'ClientReturn':
                undated += 1
                continue
            logistic = row.get('logistic')
            ds = posting_day(logistic.get('return_date')) if isinstance(logistic, dict) else None
            if ds is not None and ds<'2000-01-01':
                ds=None
        if ds is None:
            undated += 1
        elif start <= ds <= end:
            grouped.setdefault(ds, []).append(row)
    return grouped, undated


def normalize_wb_cancellations_day(payload, day: str) -> DayEvent:
    rows = _rows(payload, 'orders')
    grouped, undated = split_event_rows(rows, WB_CANCELS, day, day)
    count = len(grouped.get(day, []))
    partial = undated or payload.get('undated_rows', 0)
    return DayEvent(None if partial else count, source='WB · дата отмены cancelDate',
        available_units=count, warning='Для части отмен WB не указал дату; итог неполный.' if partial else None)


def normalize_ozon_returns_day(payload, day: str) -> DayEvent:
    grouped, undated = split_event_rows(payload, OZON_RETURNS, day, day)
    count = 0
    amount = Decimal(0)
    priced = True
    partial = bool(undated or payload.get('undated_rows', 0))
    for row in grouped.get(day, []):
        product = row.get('product')
        quantity = _quantity(product.get('quantity')) if isinstance(product, dict) else None
        if quantity is None or quantity <= 0:
            partial = True
            continue
        count += quantity
        price = product.get('price')
        value = _number(price.get('price')) if isinstance(price, dict) else None
        currency = price.get('currency_code') if isinstance(price, dict) else None
        if currency not in {'RUB', 'RUR'} or value is None or value <= 0:
            priced = False
        else:
            amount += value * quantity
    return DayEvent(None if partial else count, _money(amount) if not partial and priced else None,
        'Ozon · клиентские возвраты после вручения, дата return_date', available_units=count,
        warning='У части клиентских возвратов нет даты или количества; итог неполный.' if partial else None,
        price_note='Стоимость товаров в клиентских возвратах по API Ozon, до удержаний.' if priced
            else 'Не все цены возвратов подтверждены в рублях; сумма не рассчитана.')


def normalize_ozon_realization_day(payload) -> tuple[DayEvent, DayEvent]:
    rows = _rows(payload, 'rows')
    # This daily method does not document a currency header. Retain quantities,
    # but only display money as RUB if the response explicitly confirms it.
    currency = payload.get('currency') or payload.get('currency_code')
    header = payload.get('header')
    if isinstance(header, dict):
        currency = currency or header.get('currency_sys_name')
    counts = [0, 0]
    amounts = [Decimal(0), Decimal(0)]
    complete = [True, True]
    priced = [currency in {'RUB', 'RUR'}, currency in {'RUB', 'RUR'}]
    seen = set()
    for row in rows:
        item = row.get('item')
        sku = str(item.get('sku') or '') if isinstance(item, dict) else ''
        row_number=row.get('rowNumber',row.get('row_number'))
        identifier=('row',str(row_number)) if row_number is not None else ('sku',sku)
        if not sku or identifier in seen:
            raise DailyEventError('Дневная реализация Ozon содержит отсутствующий SKU или неоднозначные строки.')
        seen.add(identifier)
        for index, field in enumerate(('delivery_commission', 'return_commission')):
            component = row.get(field)
            # A missing component is not an explicit zero for this SKU.
            qty = _quantity(component.get('quantity')) if isinstance(component, dict) else None
            if qty is None:
                complete[index] = False
                continue
            counts[index] += qty
            if qty == 0:
                continue
            price = _number(component.get('price_per_instance'))
            amount = _number(component.get('amount'))
            if price is None or price <= 0 or amount is None or _money(abs(amount)) != _money(price * qty):
                priced[index] = False
            else:
                amounts[index] += abs(amount)
    result = []
    for index in range(2):
        if counts[index] == 0 and complete[index]:
            priced[index] = True
        result.append(DayEvent(counts[index] if complete[index] else None,
            _money(amounts[index]) if complete[index] and priced[index] else None,
            'Ozon · отчёт о реализации за день', available_units=counts[index],
            warning='В дневной реализации нет полного количества; итог неполный.' if not complete[index] else None,
            price_note='По реализации за день, до удержаний.' if priced[index]
                else 'Валюту и полную сумму реализации в рублях источник не подтвердил.'))
    return tuple(result)


def _read(repo, connection_id, day, endpoint, normalizer, *, index=None) -> DayEvent:
    saved = repo.latest_raw_source(connection_id, day, (endpoint,))
    attempt = repo.latest_run(connection_id, endpoint, day)
    reading = DayEvent(source={OZON_REALIZATION:'Ozon · дневная реализация',
                               OZON_RETURNS:'Ozon · клиентские возвраты'}.get(endpoint,endpoint))
    if saved:
        try:
            reading = normalizer(json.loads(saved['payload_json']))
            if index is not None:
                reading = reading[index]
            if saved['status'] != 'success':
                reading = replace(reading, units=None, amount=None,
                    warning=reading.warning or 'Источник вернул неполные данные.')
            reading = replace(reading, freshness=saved['finished_at'])
        except (DailyEventError, ValueError, TypeError, KeyError):
            reading = DayEvent(source=endpoint, warning='Сохранённые данные не подтверждают полный итог событий.')
    if attempt and attempt.status == 'failed' and (not saved or attempt.id > saved['id']):
        if not saved and attempt.http_status in {401, 402, 403}:
            premium=endpoint==OZON_REALIZATION and 'premium' in (attempt.error or '').lower()
            warning = ('Ozon отклонил дневной отчёт: требуется подписка Premium Plus.' if premium else
                'Нет доступа к дневной реализации: проверьте права API и подписку Premium Plus/Pro.'
                    if endpoint==OZON_REALIZATION else 'Нет доступа к источнику событий; проверьте права API.')
            reading=replace(reading,unavailable_note='⏳ нужна Premium Plus' if premium else '⏳ нет доступа к API')
        else:
            warning = 'Последняя загрузка не удалась; показаны сохранённые данные.' if saved else 'Источник событий не загружен: последний запрос не удался.'
        reading = replace(reading, warning=' '.join(filter(None, (reading.warning, warning))),
            load_failed=True)
    return reading


def build_daily_events(repo, connection_id: int, marketplace: str, day: date) -> DailyEvents:
    ds = day.isoformat()
    if marketplace == 'wildberries':
        sales = lambda payload: normalize_wb_sales_day(payload, ds)
        return DailyEvents(
            _read(repo, connection_id, ds, WB_SALES, sales, index=0),
            _read(repo, connection_id, ds, WB_SALES, sales, index=1),
            _read(repo, connection_id, ds, WB_CANCELS, lambda payload: normalize_wb_cancellations_day(payload, ds)))
    if marketplace == 'ozon':
        buyouts = _read(repo, connection_id, ds, OZON_REALIZATION, normalize_ozon_realization_day, index=0)
        returns = _read(repo, connection_id, ds, OZON_RETURNS, lambda payload: normalize_ozon_returns_day(payload, ds))
        # Avoid switching between a physical customer-return day and a finance
        # realization day; these are distinct report sources and can differ.
        push = repo.ozon_cancellations_day(connection_id,ds) if hasattr(repo,'ozon_cancellations_day') else None
        if push:
            units = int(push['units'])
            warning = ('Учтены полученные уведомления FBO/FBS по дате отмены, время Москвы. '
                       'Полнота за день не подтверждена: старые и пропущенные уведомления могут отсутствовать.')
            if push['issues']:
                warning += ' Есть уведомления с ошибками или разными версиями; спорные количества исключены.'
            cancellations = DayEvent(source='Ozon · Push уведомления об отменах FBO/FBS',available_units=units,
                unavailable_note=f'≥ {units} шт. · по уведомлениям' if units else '⏳ отмены за день не подтверждены',
                freshness=push['freshness'],warning=warning,
                price_note='Сумма отмен не приходит в уведомлениях; в рубли не подставляется.')
            return DailyEvents(buyouts,returns,cancellations)
        return DailyEvents(buyouts, returns, DayEvent(source='Ozon · отмены',
            unavailable_note='⏳ дата отмены не получена',
            warning='В полученных ответах Ozon нет даты отмены. Точный итог отмен за день не подтверждён.'))
    raise ValueError('Unknown marketplace')
