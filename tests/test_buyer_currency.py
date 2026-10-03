"""Reconcile order identities/quantities before converting buyer prices."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.reports import build_daily_report, format_daily
from app.reports.daily import build_daily_report_with_currency
from app.services.currency import CurrencyRateError, ensure_cbr_rates, parse_cbr_rates
from app.services.backups import BackupService
from app.storage import Database, MetricPoint, Repository

DAY = date(2026, 10, 1)
XML = b'''<?xml version="1.0" encoding="utf-8"?>
<ValCurs Date="01.10.2026"><Valute><CharCode>BYN</CharCode><Nominal>1</Nominal>
<Value>25,1250</Value></Valute><Valute><CharCode>KZT</CharCode><Nominal>100</Nominal>
<Value>15,7732</Value></Valute></ValCurs>'''


def posting(number, sku, quantity, *, cancelled=False):
    return {'posting_number': number, 'created_at': '2026-09-30T22:30:00Z',
            'status': 'cancelled' if cancelled else 'delivering',
            'products': [{'sku': sku, 'quantity': quantity, 'price': '1000.00'}]}


def fixture(tmp_path):
    db = Database(tmp_path / 'currency.sqlite3')
    db.initialize()
    repo = Repository(db)
    shop = repo.ensure_shop(repo.ensure_seller(1).id)
    conn = repo.ensure_connection(shop.id, 'ozon', 'Ozon')
    analytics = {'result': {'data': [
        {'dimensions': [{'id': sku}], 'metrics': [qty, 1000 * qty]}
        for sku, qty in [('9001', 2), ('9002', 1), ('9003', 3), ('9004', 1)]
    ]}}
    repo.record_success(conn.id, 'analytics/orders', DAY.isoformat(), analytics, [
        MetricPoint(conn.id, DAY.isoformat(), 'ordered_units', 7, 'units'),
        MetricPoint(conn.id, DAY.isoformat(), 'ordered_revenue', 7000, 'RUB')])
    repo.record_success(conn.id, 'finance/accrual/by-day', DAY.isoformat(), {'net': '1000.25'}, [
        MetricPoint(conn.id, DAY.isoformat(), 'marketplace_net', 1000.25, 'RUB')])
    fbo = [posting('fbo-a', 9001, 2), posting('fbo-b', 9002, 1, cancelled=True)]
    fbs = [posting('fbs-a', 9003, 3), posting('fbs-b', 9004, 1)]
    for row, amount, currency in zip(fbs, ['10.000', '45.670'], ['BYN', 'RUB']):
        row['financial_data'] = {'products': [{
            'product_id': row['products'][0]['sku'],
            'quantity': row['products'][0]['quantity'],
            'customer_price': {'amount': amount, 'currency': currency},
            'currency_code': 'RUB', 'price': '1000.00'}]}
    list_id = repo.record_success(conn.id, 'postings/fbo', DAY.isoformat(), {'postings': fbo}, [])
    repo.record_success(conn.id, 'postings/fbs', DAY.isoformat(), {'postings': fbs}, [])
    details = {'list_source_run_id': list_id, 'complete': True, 'responses': [
        {'posting_number': row['posting_number'], 'http_status': 200, 'response': {'result': {
            'posting_number': row['posting_number'],
            # Detail timestamp differs; the list owns the order date.
            'created_at': '2026-09-29T20:00:00Z',
            'financial_data': {'products': [{'product_id': row['products'][0]['sku'],
                'customer_price': amount, 'customer_currency_code': 'RUB',
                'currency_code': 'RUB', 'price': 1000}]}}}}
        for row, amount in zip(fbo, [123.455, 0])
    ]}
    repo.record_success(conn.id, 'postings/fbo/details', DAY.isoformat(), details, [])
    return repo, shop, conn, fbo, fbs, details


def test_cbr_uses_nominal_and_actual_effective_date():
    rates = parse_cbr_rates(XML, DAY)
    assert rates.rate('BYN') == Decimal('25.1250')
    assert rates.rate('KZT') == Decimal('0.157732')
    assert rates.rate('RUB') == 1 and rates.rate('XTS') is None
    weekend = parse_cbr_rates(XML, date(2026, 10, 4))
    assert weekend.requested_date == '2026-10-04' and weekend.effective_date == '2026-10-01'


@pytest.mark.parametrize('payload', [
    XML.replace(b'01.10.2026', b'02.10.2026'),
    XML.replace(b'25,1250', b'NaN'),
    XML.replace(b'25,1250', b'-1'),
    XML.replace(b'<Nominal>1</Nominal>', b'<Nominal>0</Nominal>'),
    XML.replace(b'KZT', b'BYN'),
    b'<ValCurs Date="01.10.2026"></ValCurs>',
    b'<html>unavailable</html>', b'<!DOCTYPE bad>' + XML,
])
def test_invalid_cbr_response_never_becomes_a_zero_or_future_rate(payload):
    with pytest.raises(CurrencyRateError):
        parse_cbr_rates(payload, DAY)


@pytest.mark.asyncio
async def test_full_buyer_prices_include_quantity_cancelled_zero_and_auto_fx(tmp_path):
    repo, shop, conn, *_ = fixture(tmp_path)
    seen = []
    def handler(request):
        seen.append(request.url.params['date_req'])
        assert request.url.host == 'www.cbr.ru'
        assert 'Api-Key' not in request.headers and 'Client-Id' not in request.headers
        return httpx.Response(200, content=XML)
    report = await build_daily_report_with_currency(repo, shop.id, DAY,
        transport=httpx.MockTransport(handler))
    prices = report.sources[0].buyer_prices
    assert prices.complete and prices.priced_units == prices.posting_units == 7
    assert [(t.currency, t.amount, t.units) for t in prices.totals] == [
        ('RUB', Decimal('292.580'), 4), ('BYN', Decimal('30.000'), 3)]
    assert prices.rub_total == Decimal('1046.33')
    assert report.sources[0].ordered_revenue == 7000 and report.sources[0].marketplace_net == 1000.25
    text = format_daily(report)
    assert '292.58 ₽ + 30.00 BYN' in text
    assert '≈ 1 046.33 ₽' in text and 'для 2026-10-01' in text
    assert 'курс ЦБ: 1 BYN = 25.125 ₽' in text
    assert 'предельной цене' in text and '1 000.25 ₽' in text
    assert seen == ['01/10/2026']
    backup = BackupService(repo.db, repo, tmp_path / 'backups').create()
    restored = Repository(Database(backup.path))
    assert restored.currency_rate_snapshot(DAY.isoformat())['payload'] == XML
    async def should_not_fetch(request):
        raise AssertionError('Historical snapshot must be reused after restart/backup')
    again = await build_daily_report_with_currency(restored, shop.id, DAY,
        transport=httpx.MockTransport(should_not_fetch))
    assert again.sources[0].buyer_prices.rub_total == prices.rub_total


@pytest.mark.asyncio
async def test_ruble_only_prices_do_not_need_cbr(tmp_path):
    repo, shop, conn, _, fbs, _ = fixture(tmp_path)
    fbs[0]['financial_data']['products'][0]['customer_price']['currency'] = 'RUB'
    repo.record_success(conn.id, 'postings/fbs', DAY.isoformat(), {'postings': fbs}, [])
    def no_request(request):
        raise AssertionError('Ruble prices require no exchange rate')
    report = await build_daily_report_with_currency(repo, shop.id, DAY,
        transport=httpx.MockTransport(no_request))
    assert report.sources[0].buyer_prices.rub_total == Decimal('322.58')
    assert 'оценка в рублях по ЦБ' not in format_daily(report)


@pytest.mark.parametrize('mutation', ['missing_price', 'missing_buyer_currency', 'quantity_mismatch', 'malformed_money'])
@pytest.mark.asyncio
async def test_partial_buyer_prices_never_infer_currency_or_publish_full_estimate(tmp_path, mutation):
    repo, shop, conn, _, fbs, _ = fixture(tmp_path)
    product = fbs[0]['financial_data']['products'][0]
    if mutation == 'missing_price':
        product['customer_price'] = None
    elif mutation == 'missing_buyer_currency':
        product['customer_price'] = '10.00'  # Seller currency RUB is irrelevant.
    elif mutation == 'quantity_mismatch':
        product['quantity'] = 1
    else:
        product['customer_price']['amount'] = 'NaN'
    repo.record_success(conn.id, 'postings/fbs', DAY.isoformat(), {'postings': fbs}, [])
    def no_request(request):
        raise AssertionError('Do not turn incomplete coverage into a full estimate')
    report = await build_daily_report_with_currency(repo, shop.id, DAY,
        transport=httpx.MockTransport(no_request))
    prices = report.sources[0].buyer_prices
    assert not prices.complete and prices.priced_units == 4 and prices.rub_total is None
    text = format_daily(report)
    assert 'неполно' in text and '292.58 ₽' in text
    assert '≈' not in text


def test_same_total_units_with_different_skus_is_not_full_coverage(tmp_path):
    repo, shop, conn, *_ = fixture(tmp_path)
    raw = {'result': {'data': [{'dimensions': [{'id': 'different-sku'}], 'metrics': [7, 7000]}]}}
    repo.record_success(conn.id, 'analytics/orders', DAY.isoformat(), raw, [
        MetricPoint(conn.id, DAY.isoformat(), 'ordered_units', 7, 'units'),
        MetricPoint(conn.id, DAY.isoformat(), 'ordered_revenue', 7000, 'RUB')])
    prices = build_daily_report(repo, shop.id, DAY).sources[0].buyer_prices
    assert prices.priced_units == 7 and not prices.complete and prices.rub_total is None


def test_stale_fbo_details_never_join_a_changed_list_snapshot(tmp_path):
    repo, shop, conn, fbo, _, _ = fixture(tmp_path)
    repo.record_success(conn.id, 'postings/fbo', DAY.isoformat(), {'postings': fbo, 'revision': 2}, [])
    prices = build_daily_report(repo, shop.id, DAY).sources[0].buyer_prices
    assert not prices.complete and prices.priced_units == 4 and prices.rub_total is None


def test_duplicate_posting_counts_once_and_detail_response_identity_is_checked(tmp_path):
    repo, shop, conn, fbo, _, details = fixture(tmp_path)
    run = repo.record_success(conn.id, 'postings/fbo', DAY.isoformat(), {'postings': fbo + [deepcopy(fbo[0])]}, [])
    details['list_source_run_id'] = run
    repo.record_success(conn.id, 'postings/fbo/details', DAY.isoformat(), details, [])
    assert build_daily_report(repo, shop.id, DAY).sources[0].buyer_prices.priced_units == 7
    details['responses'][0]['response']['result']['posting_number'] = 'another-order'
    repo.record_success(conn.id, 'postings/fbo/details', DAY.isoformat(), details, [])
    prices = build_daily_report(repo, shop.id, DAY).sources[0].buyer_prices
    assert not prices.complete and prices.priced_units == 5


@pytest.mark.asyncio
async def test_cbr_outage_preserves_original_currencies_and_existing_saved_rate(tmp_path):
    repo, shop, _, *_ = fixture(tmp_path)
    transport = httpx.MockTransport(lambda r: httpx.Response(503))
    report = await build_daily_report_with_currency(repo, shop.id, DAY, transport=transport)
    assert report.sources[0].buyer_prices.complete
    assert report.sources[0].buyer_prices.rub_total is None
    text = format_daily(report)
    assert '292.58 ₽ + 30.00 BYN' in text and 'нет курса ЦБ для BYN' in text
    repo.save_currency_rate_snapshot(DAY.isoformat(), DAY.isoformat(), XML)
    rates = await ensure_cbr_rates(repo, DAY, force=True, transport=transport)
    assert rates.rate('BYN') == Decimal('25.1250')
    assert repo.currency_rate_snapshot(DAY.isoformat())['payload'] == XML


@pytest.mark.asyncio
async def test_unknown_currency_stays_visible_without_invented_conversion(tmp_path):
    repo, shop, conn, _, fbs, _ = fixture(tmp_path)
    fbs[0]['financial_data']['products'][0]['customer_price']['currency'] = 'XTS'
    repo.record_success(conn.id, 'postings/fbs', DAY.isoformat(), {'postings': fbs}, [])
    report = await build_daily_report_with_currency(repo, shop.id, DAY,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=XML)))
    assert report.sources[0].buyer_prices.rub_total is None
    assert '30.00 XTS' in format_daily(report)


@pytest.mark.asyncio
async def test_daily_scheduler_fetches_report_date_rate_before_sending(tmp_path, monkeypatch):
    import app.services.scheduler as scheduler
    import app.reports.daily as daily
    from datetime import datetime
    from app.config import Settings
    repo, shop, _, *_ = fixture(tmp_path)
    events = []
    original = daily.ensure_cbr_rates
    async def ensure(repo, day, **kwargs):
        events.append(('rate', day.isoformat()))
        return await original(repo, day, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=XML)))
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 2, 10, tzinfo=tz)
    async def send(chat_id, text, **kwargs):
        events.append(('send', text))
        return SimpleNamespace(message_id=42)
    repo.grant_shop_access(101,shop.id,'owner')
    monkeypatch.setattr(daily, 'ensure_cbr_rates', ensure)
    monkeypatch.setattr(scheduler, 'datetime', FixedDatetime)
    for name in ('evaluate_forecast_quality', 'build_supply_plan', 'build_action_center'):
        monkeypatch.setattr(scheduler, name, lambda *a, **k: None)
    ctx = SimpleNamespace(repository=repo, shop_id=shop.id, settings=Settings.from_env(),
        preferences=lambda: SimpleNamespace(timezone='Europe/Moscow', finance_lookback_days=7),
        collect_day=AsyncMock(return_value=[SimpleNamespace(ok=True)]),
        collect_finance=AsyncMock(return_value=[]), collect_reconciliation=AsyncMock(return_value=[]),
        collect_advertising=AsyncMock(return_value=[]), collect_promotions=AsyncMock(return_value=[]),
        collect_inbound=AsyncMock(return_value=[]))
    await scheduler.collect_and_send_daily(SimpleNamespace(id=123,send_message=send), ctx)
    assert events[0] == ('rate', DAY.isoformat())
    assert events[1][0] == 'send' and '≈ 1 046,33 ₽' in events[1][1]
    assert repo.report_card(123,101,42)['report_day']==DAY.isoformat()
