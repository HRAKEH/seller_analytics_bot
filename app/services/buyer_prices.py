"""Buyer order prices from captured postings, separate from seller/net money."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re

from app.services.currency import CurrencyRates, saved_cbr_rates
from app.services.ozon_dates import posting_day


@dataclass(frozen=True)
class CurrencyTotal:
    currency: str
    amount: Decimal
    units: int


@dataclass(frozen=True)
class BuyerPriceTotals:
    totals: tuple[CurrencyTotal, ...]
    priced_units: int
    posting_units: int
    expected_units: int | None
    complete: bool
    freshness: str | None
    warnings: tuple[str, ...] = ()
    rates: CurrencyRates | None = None

    @property
    def foreign_currencies(self) -> tuple[str, ...]:
        return tuple(t.currency for t in self.totals if t.currency != 'RUB')

    @property
    def missing_rates(self) -> tuple[str, ...]:
        return tuple(c for c in self.foreign_currencies
                     if self.rates is None or self.rates.rate(c) is None)

    @property
    def rub_total(self) -> Decimal | None:
        if not self.complete or self.missing_rates:
            return None
        total = Decimal(0)
        for item in self.totals:
            rate = Decimal(1) if item.currency == 'RUB' else self.rates.rate(item.currency)
            total += item.amount * rate
        # Round only the final combined amount, not each item or FX rate.
        return total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _number(value) -> Decimal | None:
    if value is None or value == '' or isinstance(value, (bool, dict, list)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def _quantity(value) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number == number.to_integral_value() else None


def _sku(value) -> str | None:
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) else None


def _payload(raw) -> dict:
    try:
        parsed = json.loads(raw or '{}', parse_float=Decimal)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _price(product) -> tuple[Decimal, str] | None:
    raw = product.get('customer_price')
    if isinstance(raw, dict):
        amount = _number(raw.get('amount'))
        currency = raw.get('currency') or product.get('customer_currency_code')
    else:
        amount = _number(raw)
        currency = product.get('customer_currency_code')
    # currency_code is the seller currency; it must never substitute for a
    # missing buyer currency. Missing prices are distinct from genuine zero.
    if amount is None or not isinstance(currency, str) or not re.fullmatch(r'[A-Z]{3}', currency):
        return None
    return amount, currency


def _analytics_quantities(repo, connection_id: int, ds: str) -> dict[str, int] | None:
    metric = repo.latest_metric(connection_id, ds, 'ordered_units')
    if metric is None:
        return None
    body = _payload(repo.raw_payload_for_run(metric['source_run_id']))
    result = body.get('result')
    if not isinstance(result, dict):
        return None
    rows = result.get('data', result.get('rows'))
    if not isinstance(rows, list):
        return None
    expected = defaultdict(int)
    for row in rows:
        if not isinstance(row, dict):
            return None
        dimensions, metrics = row.get('dimensions'), row.get('metrics')
        if not isinstance(dimensions, list) or not dimensions or not isinstance(dimensions[0], dict):
            return None
        sku = _sku(dimensions[0].get('id'))
        qty = _quantity(metrics[0]) if isinstance(metrics, list) and metrics else None
        if sku is None or qty is None:
            return None
        if qty:
            expected[sku] += qty
    return dict(expected)


def build_buyer_prices(repo, connection_id: int, day: date, expected_units) -> BuyerPriceTotals | None:
    ds = day.isoformat()
    sources = {scheme: repo.latest_raw_source(connection_id, ds, (f'postings/{scheme}',))
               for scheme in ('fbo', 'fbs')}
    if not any(sources.values()):
        return None
    details = repo.latest_raw_source(connection_id, ds, ('postings/fbo/details',))
    detail_payload = _payload(details['payload_json']) if details else {}
    fbo_source = sources['fbo']
    detail_rows = {}
    if fbo_source and detail_payload.get('list_source_run_id') == fbo_source['id']:
        for item in detail_payload.get('responses', []):
            if not isinstance(item, dict) or item.get('http_status') != 200:
                continue
            response = item.get('response')
            result = response.get('result') if isinstance(response, dict) else None
            if isinstance(result, dict) and result.get('posting_number') == item.get('posting_number'):
                detail_rows[item['posting_number']] = result

    amounts, priced_quantities = defaultdict(Decimal), defaultdict(int)
    posting_quantities = defaultdict(int)
    priced_units = posting_units = 0
    reliable = True
    seen = {}
    freshness = max((s['finished_at'] for s in sources.values() if s), default=None)
    if details and detail_rows:
        freshness = max(freshness or '', details['finished_at'])
    for scheme, source in sources.items():
        if source is None or source['status'] != 'success':
            reliable = False
            continue
        raw = _payload(source['payload_json'])
        postings = raw.get('postings')
        if not isinstance(postings, list):
            reliable = False
            continue
        for posting in postings:
            if not isinstance(posting, dict):
                reliable = False
                continue
            number = posting.get('posting_number')
            stamp = posting.get('created_at') or posting.get('in_process_at')
            if not isinstance(number, str) or not number or posting_day(stamp or '') != ds:
                reliable = False
                continue
            if number in seen:
                if seen[number] != (scheme, posting):
                    reliable = False
                continue
            seen[number] = (scheme, posting)
            products = posting.get('products')
            if not isinstance(products, list) or not products:
                reliable = False
                continue
            quantities = defaultdict(int)
            for product in products:
                if not isinstance(product, dict):
                    reliable = False
                    continue
                sku, quantity = _sku(product.get('sku')), _quantity(product.get('quantity'))
                if sku is None or quantity is None or quantity <= 0:
                    reliable = False
                    continue
                quantities[sku] += quantity
            financial_posting = detail_rows.get(number, {}) if scheme == 'fbo' else posting
            financial = financial_posting.get('financial_data')
            financial_products = financial.get('products') if isinstance(financial, dict) else []
            prices = {}
            if isinstance(financial_products, list):
                for product in financial_products:
                    if not isinstance(product, dict):
                        continue
                    sku = _sku(product.get('product_id'))
                    if sku not in quantities:
                        continue
                    price = _price(product)
                    if 'quantity' in product and _quantity(product['quantity']) != quantities[sku]:
                        price = None
                    if sku in prices and prices[sku] != price:
                        reliable = False
                        prices[sku] = None
                    else:
                        prices[sku] = price
            for sku, quantity in quantities.items():
                posting_units += quantity
                posting_quantities[sku] += quantity
                price = prices.get(sku)
                if price is None:
                    continue
                amount, currency = price
                amounts[currency] += amount * quantity
                priced_quantities[currency] += quantity
                priced_units += quantity
    expected = _quantity(expected_units)
    analytics = _analytics_quantities(repo, connection_id, ds)
    same_skus = analytics is not None and analytics == dict(posting_quantities)
    complete = reliable and same_skus and expected == posting_units == priced_units
    warnings = []
    if any((last := repo.latest_run(connection_id, endpoint, ds)) is not None
           and last.status in {'failed', 'partial'}
           for endpoint in ('postings/fbo', 'postings/fbs', 'postings/fbo/details')):
        warnings.append('Последнее обновление цен было неполным; показаны сохранённые данные.')
    if not reliable:
        warnings.append('Списки отправлений загружены не полностью или содержат неоднозначные данные.')
    if not same_skus or expected != posting_units:
        warnings.append('Состав отправлений пока не совпадает с заказами из аналитики.')
    if priced_units != posting_units:
        warnings.append('Для части отправлений цена покупателя или её валюта отсутствует.')
    if complete and not amounts:
        amounts['RUB'] = Decimal(0)
    totals = tuple(CurrencyTotal(c, amounts[c], priced_quantities[c])
                   for c in sorted(amounts, key=lambda c: (c != 'RUB', c)))
    return BuyerPriceTotals(totals, priced_units, posting_units, expected, complete, freshness,
                            tuple(warnings), saved_cbr_rates(repo, day) if any(c != 'RUB' for c in amounts) else None)
