"""Historical CBR reference rates; never an assertion about Ozon's FX rate."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re
import xml.etree.ElementTree as ET

import httpx

CBR_URL = 'https://www.cbr.ru/scripts/XML_daily.asp'


class CurrencyRateError(ValueError):
    pass


@dataclass(frozen=True)
class CurrencyQuote:
    currency: str
    nominal: int
    value: Decimal

    @property
    def rub_per_unit(self) -> Decimal:
        return self.value / self.nominal


@dataclass(frozen=True)
class CurrencyRates:
    requested_date: str
    effective_date: str
    quotes: tuple[CurrencyQuote, ...]

    def rate(self, currency: str) -> Decimal | None:
        if currency == 'RUB':
            return Decimal(1)
        return next((q.rub_per_unit for q in self.quotes if q.currency == currency), None)


def parse_cbr_rates(payload: bytes, day: date) -> CurrencyRates:
    if len(payload) > 2_000_000 or b'<!DOCTYPE' in payload.upper() or b'<!ENTITY' in payload.upper():
        raise CurrencyRateError('Некорректный ответ ЦБ')
    try:
        root = ET.fromstring(payload)
        effective = datetime.strptime(root.attrib['Date'], '%d.%m.%Y').date()
        if root.tag != 'ValCurs' or effective > day:
            raise ValueError('wrong rate date')
        quotes = []
        seen = set()
        for row in root.findall('Valute'):
            currency = (row.findtext('CharCode') or '').strip()
            nominal = int(row.findtext('Nominal') or '')
            value = Decimal((row.findtext('Value') or '').replace(',', '.'))
            if (not re.fullmatch(r'[A-Z]{3}', currency) or currency in seen or
                    nominal <= 0 or not value.is_finite() or value <= 0):
                raise ValueError('invalid rate')
            seen.add(currency)
            quotes.append(CurrencyQuote(currency, nominal, value))
        if not quotes:
            raise ValueError('no rates')
    except (ET.ParseError, KeyError, ValueError, InvalidOperation) as exc:
        raise CurrencyRateError('ЦБ не вернул корректные курсы для выбранной даты') from exc
    return CurrencyRates(day.isoformat(), effective.isoformat(), tuple(quotes))


def saved_cbr_rates(repo, day: date) -> CurrencyRates | None:
    row = repo.currency_rate_snapshot(day.isoformat())
    if row is None:
        return None
    try:
        rates = parse_cbr_rates(row['payload'], day)
        if rates.effective_date != row['effective_date']:
            return None
        return rates
    except CurrencyRateError:
        return None


async def ensure_cbr_rates(repo, day: date, *, transport=None, force: bool = False) -> CurrencyRates:
    """Fetch once per requested day and keep the original response in backups.

    Historical dates use their saved snapshot. Today's snapshot can be renewed
    after an hour. A failed renewal does not delete a valid saved rate.
    """
    saved = saved_cbr_rates(repo, day)
    row = repo.currency_rate_snapshot(day.isoformat()) if saved else None
    if saved and not force:
        fetched = datetime.fromisoformat(row['fetched_at'])
        now = datetime.now(timezone.utc)
        if day < now.date() or (now - fetched).total_seconds() < 3600:
            return saved
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=False) as client:
            response = await client.get(CBR_URL, params={'date_req': day.strftime('%d/%m/%Y')})
            response.raise_for_status()
            rates = parse_cbr_rates(response.content, day)
    except (httpx.HTTPError, CurrencyRateError) as exc:
        if saved:
            return saved
        raise CurrencyRateError('Курс ЦБ для даты отчёта пока недоступен') from exc
    repo.save_currency_rate_snapshot(day.isoformat(), rates.effective_date, response.content)
    return rates
