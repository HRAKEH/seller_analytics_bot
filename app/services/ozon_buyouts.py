"""Check captured buyout rows against exact posting/SKU quantities.

These are diagnostic observations, not the cabinet's realization metric. Keep
price currency unknown when the response does not explicitly identify it.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from html import escape
import re

from .buyer_prices import _number, _quantity, _sku, _payload
from .ozon_dates import posting_day


@dataclass(frozen=True)
class BuyoutMatch:
    posting_number: str
    sku: str
    quantity: int
    price: Decimal
    amount: Decimal
    currency: str | None
    report_from: str
    report_to: str


@dataclass(frozen=True)
class BuyoutCheck:
    matches: tuple[BuyoutMatch, ...]
    expected_units: int
    matched_units: int
    postings_complete: bool
    report_complete: bool
    freshness: str | None
    report_from: str | None
    report_to: str | None
    warnings: tuple[str, ...] = ()
    report_required: bool = True


def buyout_expectations(repo, connection_id: int, day: date):
    """Prove which products Ozon itself bought, using both complete posting lists."""
    ds=day.isoformat()
    expected=defaultdict(int); seen={}; conflicts=set(); reliable=True
    for scheme in ('fbo','fbs'):
        source=repo.latest_raw_source(connection_id,ds,(f'postings/{scheme}',))
        if source is None or source['status']!='success':
            reliable=False
            continue
        attempt=repo.latest_run(connection_id,f'postings/{scheme}',ds)
        if attempt and attempt.status=='failed' and attempt.id>source['id']:
            reliable=False
        postings=_payload(source['payload_json']).get('postings')
        if not isinstance(postings,list):
            reliable=False
            continue
        for posting in postings:
            if not isinstance(posting,dict):
                reliable=False
                continue
            number=posting.get('posting_number')
            if (not isinstance(number,str) or not number.strip() or
                posting_day(posting.get('created_at') or posting.get('in_process_at'))!=ds):
                reliable=False
                continue
            if number in seen:
                if seen[number]!=(scheme,posting):
                    conflicts.add(number); reliable=False
                continue
            seen[number]=(scheme,posting)
            products=posting.get('products')
            if not isinstance(products,list) or not products:
                reliable=False
                continue
            flags={}
            for product in products:
                if not isinstance(product,dict):
                    reliable=False
                    continue
                sku=_sku(product.get('sku')); qty=_quantity(product.get('quantity'))
                flag=product.get('is_marketplace_buyout')
                if sku is None or qty is None or qty<=0 or not isinstance(flag,bool):
                    reliable=False
                    continue
                if sku in flags and flags[sku]!=flag:
                    conflicts.add(number); reliable=False
                flags[sku]=flag
                if flag:expected[(number,sku)]+=qty
    return dict(expected),reliable,conflicts


def build_buyout_check(repo, connection_id: int, day: date) -> BuyoutCheck | None:
    ds=day.isoformat()
    expected,reliable,conflicts=buyout_expectations(repo,connection_id,day)
    if reliable and not expected:
        # This optional report is about Ozon buying goods itself, not every
        # customer's delivery. Old failed probes are irrelevant to this cohort.
        return BuyoutCheck((),0,0,True,True,None,None,None,report_required=False)
    capture=repo.latest_buyout_capture(connection_id,ds)
    latest=repo.latest_run(connection_id,'finance/products/buyout',ds)
    if capture is None and latest is None:
        return None
    candidates={}; duplicates=set(); invalid=set()
    payload=_payload(capture['payload_json']) if capture else {}
    reports=payload.get('reports',[])
    report_complete=bool(capture and capture['status']=='success' and isinstance(reports,list) and reports)
    if not isinstance(reports,list):reports=[]
    for report in reports:
        if not isinstance(report,dict) or report.get('ok') is not True:
            report_complete=False
            continue
        body=report.get('response')
        rows=body.get('products') if isinstance(body,dict) else None
        try:
            first=date.fromisoformat(report['date_from']); last=date.fromisoformat(report['date_to'])
            if not 0<=(last-first).days<31:raise ValueError('invalid buyout period')
        except (ValueError,KeyError,TypeError):
            report_complete=False
            continue
        if not isinstance(rows,list):
            report_complete=False
            continue
        for row in rows:
            if not isinstance(row,dict):
                report_complete=False
                continue
            number=row.get('posting_number')
            if not isinstance(number,str):continue
            key=(number,_sku(row.get('sku')))
            # Offer/name matching is deliberately insufficient.
            if key not in expected:continue
            if key in candidates or key in invalid:duplicates.add(key)
            qty=_quantity(row.get('quantity')); price=_number(row.get('buyout_price')); amount=_number(row.get('amount'))
            currency=row.get('currency') or row.get('currency_code') or body.get('currency')
            if currency is not None and (not isinstance(currency,str) or not re.fullmatch('[A-Z]{3}',currency)):
                invalid.add(key)
                continue
            consistent=False
            if qty==expected[key] and price is not None and amount is not None:
                try:
                    consistent=((price*qty).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)==
                                amount.quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
                except DecimalException:
                    pass
            if not consistent:
                invalid.add(key)
                continue
            candidates[key]=BuyoutMatch(key[0],key[1],qty,price,amount,currency,
                                        first.isoformat(),last.isoformat())
    matches=tuple(candidates[key] for key in sorted(candidates)
                  if key not in duplicates|invalid and key[0] not in conflicts)
    matched=sum(row.quantity for row in matches)
    expected_units=sum(expected.values())
    warnings=[]
    if not reliable:warnings.append('Списки отправлений или признаки выкупа пока неполные.')
    failed_latest=bool(latest and latest.status=='failed' and
                      (capture is None or latest.finished_at>=capture['finished_at']))
    if not report_complete and not failed_latest:
        warnings.append('Отдельный отчёт о выкупе товаров самим Ozon пока неполный.')
    if duplicates or invalid or conflicts:
        warnings.append('Неоднозначные строки и несовпадения количества исключены из сверки.')
    if matched!=expected_units:
        warnings.append('Для части товаров с признаком выкупа цена ещё не найдена; это не нулевая цена.')
    if failed_latest:
        if latest.http_status==429:
            warning='Последний запрос выкупов ограничен Ozon (429). Дождитесь снятия лимита и обновите отчёты; менять токен из-за этого не требуется.'
        elif latest.http_status in {401,402,403}:
            warning='Последний запрос выкупов отклонён Ozon; проверьте права API к этому отдельному отчёту.'
        else:
            warning='Последний запрос выкупов не удался; повторите обновление позже.'
        if matches:warning+=' Ниже показаны сохранённые цены, обновить их пока не удалось.'
        warnings.append(warning)
    return BuyoutCheck(matches,expected_units,matched,reliable,report_complete,
                       capture['finished_at'] if capture else None,
                       payload.get('report_date_from'),payload.get('report_date_to'),tuple(warnings))


def format_buyout_check(check: BuyoutCheck | None) -> list[str]:
    if check is None:
        return ['Выкуп товаров самим Ozon: ⏳ данные для сверки ещё не загружены.']
    lines=['<b>Выкуп товаров самим Ozon</b>']
    if not check.report_required:
        return lines+['В загруженных отправлениях этого дня таких товаров нет. Отдельный отчёт не требуется.']
    lines.append('Это отдельные сделки с Ozon, а не все продажи покупателям.')
    if not check.postings_complete:
        lines.append(f'Сопоставлено: {check.matched_units} шт.; состав товаров с признаком выкупа пока неполный.')
    elif check.expected_units:
        lines.append(f'По номеру отправления, SKU и количеству: {check.matched_units}/{check.expected_units} шт. с признаком выкупа.')
    else:
        lines.append('Товары с признаком выкупа в этих отправлениях не найдены.')
    for row in check.matches:
        price=f'{row.price:,.2f}'.replace(',', ' ').replace('.', ',')
        unit=' ₽' if row.currency=='RUB' else (' '+row.currency if row.currency else ' (валюта в ответе не указана)')
        lines.append('Отправление '+escape(row.posting_number[:70])+', Ozon · SKU <code>'+escape(row.sku)+'</code>'+
                     f': {row.quantity} шт. · цена выкупа {price}{unit} за шт.')
    if check.report_from and check.report_to:
        lines.append('Период отчёта о выкупах: '+escape(str(check.report_from)[:10])+' — '+escape(str(check.report_to)[:10])+'.')
    if check.freshness:lines.append('Выкупы обновлены: '+escape(check.freshness))
    for warning in check.warnings:lines.append('⚠️ '+escape(warning))
    lines.append('Цена выкупа — отдельный показатель. Совпадение с ценой реализации в кабинете ещё требует сверки.')
    return lines
