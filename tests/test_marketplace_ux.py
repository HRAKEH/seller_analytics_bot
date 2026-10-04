"""Regressions for stock quantities, source failures and marketplace visibility."""
from datetime import date, timedelta
from html import escape
from html.parser import HTMLParser
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.integrations.base import FetchResult
from app.integrations.ozon import OzonClient
from app.reports.alerts import format_active_alerts, format_alert_digest, format_alert_detail
from app.reports.formatter import format_product_report, format_stock_report
from app.reports.operations import format_action_center, format_inbound
from app.reports.products import build_product_report, StockRisk
from app.reports.sku_finance import build_sku_economics, format_sku_economics
from app.reports.text import split_report_html, utf16_length
from app.reports.wb_accruals import build_wb_accrual_ledger, format_wb_accrual_ledger
from app.services.actions import ActionCenter, ActionItem
from app.services.alerts import AlertNotification, stock_alert_text
from app.services.collection import CollectionService
from app.services.product_analytics import (
    normalize_ozon_fbo_stocks, normalize_ozon_stocks, ProductNormalizationError,
)
from app.storage import Database, Repository, InventoryPoint, MetricPoint, ProductMetricPoint


DAY = date(2026, 10, 2)


@pytest.fixture
def data(tmp_path):
    db = Database(tmp_path / 'ux.sqlite3')
    db.initialize()
    repo = Repository(db)
    seller = repo.ensure_seller(101, 'Seller')
    shop = repo.ensure_shop(seller.id, 'Shop')
    repo.ensure_shop_preferences(shop.id)
    wb = repo.ensure_connection(shop.id, 'wildberries', 'WB')
    oz = repo.ensure_connection(shop.id, 'ozon', 'Ozon')
    return repo, shop, wb, oz


def listing(repo, shop, conn, sku, name='Товар'):
    product = repo.ensure_product(shop.id, f'{conn.marketplace}:{sku}', name)
    return repo.ensure_listing(product.id, conn.id, sku, offer_id=sku)


def stock(repo, conn, item, quantity, *, endpoint='product/info/stocks', scheme='FBS', payload=None):
    run = repo.record_success(conn.id, endpoint, DAY.isoformat(), payload or {'stock': quantity}, [])
    repo.save_inventory(run, [InventoryPoint(item.id, quantity, 0, scheme)])
    return run


def orders(repo, conn, item, units, revenue, *, days=1):
    for index in range(days):
        ds = (DAY - timedelta(days=index)).isoformat()
        endpoint = 'statistics/orders' if conn.marketplace == 'wildberries' else 'analytics/orders'
        run = repo.record_success(conn.id, endpoint, ds, {'sku': item.marketplace_sku, 'd': ds}, [
            MetricPoint(conn.id, ds, 'ordered_units', units, 'units', True, ds),
            MetricPoint(conn.id, ds, 'ordered_revenue', revenue, 'RUB', True, ds),
        ])
        repo.save_product_metrics(run, [
            ProductMetricPoint(item.id, ds, 'ordered_units', units, 'units', 'ALL', True, ds),
            ProductMetricPoint(item.id, ds, 'ordered_revenue', revenue, 'RUB', 'ALL', True, ds),
        ])


def test_alert_displays_three_units_and_fourteen_days(data):
    repo, shop, wb, _ = data
    item = listing(repo, shop, wb, '1001', 'Длинное название товара')
    orders(repo, wb, item, 3 / 14, 25, days=14)
    stock(repo, wb, item, 3, endpoint='analytics/stocks/seller-warehouses')
    repo.save_alert_state(shop.id, 'low_stock', 'wildberries:1001', active=True, value=14, fingerprint='f')
    report = build_product_report(repo, shop.id, DAY)
    row = next(r for r in report.stock_risks if r.sku == '1001')
    assert row.available_units == 3 and row.days_left == pytest.approx(14)
    text = format_active_alerts(repo, shop.id, report)
    assert 'WB · артикул <code>1001</code>' in text and '3 шт.' in text and '14.0 дн.' in text
    assert 'Снимок API:' not in text and 'Длинное название товара' in text
    detail=format_alert_detail(repo.active_alert_states(shop.id)[0],report)
    assert 'Снимок API:' in detail and 'Среднее:' in detail and '14.0 дн.' in detail


def test_full_names_and_missing_ozon_stock_are_explicit(data):
    repo, shop, wb, _ = data
    name = 'Очень длинное название <нового> товара & полная модель упаковки 500 миллилитров'
    item = listing(repo, shop, wb, '1002', name)
    orders(repo, wb, item, 2, 300)
    stock(repo, wb, item, 3, endpoint='analytics/stocks/seller-warehouses')
    report = build_product_report(repo, shop.id, DAY)
    assert escape(name) in format_product_report(report)
    text = format_stock_report(report)
    assert 'Ozon' in text and 'Это не означает нулевой остаток' in text
    assert escape(name) in text and 'FBS 3 шт.' in text


def test_inventory_failure_preserves_snapshot_and_empty_success_replaces_it(data):
    repo, shop, _, oz = data
    item = listing(repo, shop, oz, '501')
    stock(repo, oz, item, 9)
    repo.record_failure(oz.id, 'product/info/stocks', DAY.isoformat(), 'HTTP 500')
    assert repo.latest_inventory_by_listing(shop.id)[0]['available_units'] == 9
    repo.record_success(oz.id, 'product/info/stocks', DAY.isoformat(), {'items': []}, [])
    assert repo.latest_inventory_by_listing(shop.id) == []
    assert 'нет товарного снимка' in format_stock_report(build_product_report(repo, shop.id, DAY))


def test_fbo_source_replaces_legacy_fbo_without_losing_fbs(data):
    repo, shop, _, oz = data
    item = listing(repo, shop, oz, '501')
    legacy = stock(repo, oz, item, 90, scheme='FBO')
    repo.save_inventory(legacy, [InventoryPoint(item.id, 4, 0, 'FBS')])
    stock(repo, oz, item, 6, endpoint='analytics/stocks/fbo', scheme='FBO')
    rows = repo.latest_inventory_by_scheme(shop.id)
    assert {(r['fulfillment_scheme'], r['available_units']) for r in rows} == {('FBO', 6), ('FBS', 4)}
    assert repo.latest_inventory_by_listing(shop.id)[0]['available_units'] == 10


def test_identical_stock_payload_updates_verified_time_and_captures_raw(data):
    repo, shop, _, oz = data
    item = listing(repo, shop, oz, '501')
    payload = {'items': [{'sku': '501', 'q': 8}]}
    run = repo.record_success(oz.id, 'product/info/stocks', DAY.isoformat(), payload, [], store_raw=False)
    repo.save_inventory(run, [InventoryPoint(item.id, 8, 0, 'FBS')])
    with repo.db.connect() as c:
        c.execute("UPDATE source_runs SET finished_at='2026-09-01T10:00:00+00:00' WHERE id=?", (run,))
    again = repo.record_success(oz.id, 'product/info/stocks', DAY.isoformat(), payload, [], store_raw=True)
    assert again == run and repo.count('inventory_snapshots') == 1
    row = repo.latest_inventory_by_scheme(shop.id)[0]
    assert row['captured_at'] != '2026-09-01T10:00:00+00:00'
    with repo.db.connect() as c:
        assert json.loads(c.execute('SELECT payload_json FROM raw_payloads WHERE source_run_id=?', (run,)).fetchone()[0]) == payload


def test_fbo_uses_only_free_to_sell_and_deduplicates_locations():
    rows = [{'sku': 501, 'warehouse_id': 1, 'free_to_sell_amount': 6, 'promised_amount': 10, 'transit_amount': 40},
            {'sku': 501, 'warehouse_id': 1, 'free_to_sell_amount': 6},
            {'sku': 501, 'warehouse_id': 2, 'free_to_sell_amount': 3}]
    normalized = normalize_ozon_fbo_stocks({'items': rows})
    assert sum(r.available_units for r in normalized) == 9 and len(normalized) == 2
    assert {r.fulfillment_scheme for r in normalized} == {'FBO'}


@pytest.mark.parametrize('payload', [{}, {'items': [{'sku': 501, 'transit_amount': 5}]},
    {'items': [{'sku': 501, 'free_to_sell_amount': 1}, {'sku': 501, 'free_to_sell_amount': 2}]},
    {'items': [{'sku': 501, 'free_to_sell_amount': None}]}])
def test_unknown_or_conflicting_fbo_quantity_is_not_zero(payload):
    with pytest.raises(ProductNormalizationError):
        normalize_ozon_fbo_stocks(payload)


def test_product_id_is_never_used_as_ozon_sku():
    with pytest.raises(ProductNormalizationError, match='product_id is not a SKU'):
        normalize_ozon_stocks({'items': [{'product_id': 123, 'stocks': [{'present': 3, 'reserved': 0, 'type': 'fbs'}]}]})


@pytest.mark.asyncio
async def test_fbo_requests_all_known_skus_in_batches():
    calls = []
    async def handle(request):
        body = json.loads(request.content)
        calls.append(body['skus'])
        return httpx.Response(200, json={'items': [{'sku': sku, 'free_to_sell_amount': 1} for sku in body['skus']]})
    client = OzonClient('test', 'test', min_interval=0, transport=httpx.MockTransport(handle))
    try:
        result = await client.fbo_stocks([str(x) for x in range(1, 102)] + ['1', 'not-a-sku'])
        assert result.ok and len(result.data['items']) == 101
        assert list(map(len, calls)) == [100, 1]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_v3_supply_states_are_short_names():
    calls = []
    async def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        if any(s.startswith('ORDER_STATE_') for s in body['filter']['states']):
            return httpx.Response(400, json={'message': 'States: value must contain at least 1 item(s)'})
        return httpx.Response(200, json={'order_ids': ['123'], 'last_id': ''})
    client = OzonClient('test', 'test', min_interval=0, transport=httpx.MockTransport(handle))
    try:
        result = await client.supply_orders_all(states=['ORDER_STATE_IN_TRANSIT'])
        assert result.ok and result.data['order_ids'] == ['123']
        assert calls[0]['filter']['states'] == ['IN_TRANSIT']
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_failed_supply_bundle_keeps_saved_item_quantities(data):
    repo, shop, _, oz = data
    listing(repo, shop, oz, '501')
    run = repo.record_success(oz.id, 'supply-order/inbound', DAY.isoformat(), {'old': 1}, [])
    repo.upsert_inbound_shipments(oz.id, run, 'ozon', [{
        'external_supply_id': 'supply:77', 'status': 'IN_TRANSIT', 'planned_at': '2026-10-05',
        'items': [{'marketplace_sku': '501', 'planned_units': 20, 'remaining_units': 20}],
    }])
    client = SimpleNamespace(
        supply_orders_all=AsyncMock(return_value=FetchResult.success('ozon', {'order_ids': ['7']}, 200, 1)),
        supply_orders_get=AsyncMock(return_value=FetchResult.success('ozon', {'orders': [
            {'order_id': '7', 'state': 'IN_TRANSIT', 'supplies': [{'supply_id': '77', 'bundle_id': 'b'}]},
        ]}, 200, 1)),
        supply_bundle_all=AsyncMock(return_value=FetchResult.failure('ozon', 'HTTP 500', 500, 1)),
    )
    result = await CollectionService(repo, ozon=client).collect_inbound(shop_id=shop.id, ozon_connection_id=oz.id, data_date=DAY)
    assert not result[0].ok
    assert repo.latest_run(oz.id, 'supply-order/inbound').status == 'partial'
    assert repo.active_inbound_items(shop.id)[0]['remaining_units'] == 20
    assert 'Ozon' in format_inbound(repo, shop.id) and 'не завершена' in format_inbound(repo, shop.id)


@pytest.mark.asyncio
async def test_missing_performance_credentials_are_an_explicit_failure(data):
    repo, shop, _, oz = data
    result = await CollectionService(repo).collect_advertising(start=DAY, end=DAY, ozon_connection_id=oz.id)
    assert result and not result[0].ok
    assert 'OZON_PERF_CLIENT_ID' in result[0].message


@pytest.mark.asyncio
async def test_first_fbo_failure_keeps_product_stock_fallback(data):
    repo, shop, _, oz = data
    listing(repo, shop, oz, '501')
    body={'items':[{'offer_id':'501','stocks':[
        {'sku':501,'type':'fbo','present':9,'reserved':0},
        {'sku':501,'type':'fbs','present':3,'reserved':0},
    ]}]}
    client=SimpleNamespace(
        product_stocks_all=AsyncMock(return_value=FetchResult.success('ozon',body,200,1)),
        fbo_stocks=AsyncMock(return_value=FetchResult.failure('ozon','HTTP 403',403,1)),
    )
    result=await CollectionService(repo,ozon=client).collect_inventory(
        shop_id=shop.id,ozon_connection_id=oz.id,data_date=DAY)
    assert [r.ok for r in result]==[True,False]
    assert repo.latest_inventory_by_listing(shop.id)[0]['available_units']==12
    with repo.db.connect() as c:
        assert json.loads(c.execute('SELECT payload_json FROM raw_payloads WHERE source_run_id=?',
            (result[0].run_id,)).fetchone()[0])==body


@pytest.mark.asyncio
async def test_fbo_can_refresh_even_when_fbs_request_fails(data):
    repo, shop, _, oz = data
    item=listing(repo,shop,oz,'501')
    stock(repo,oz,item,4)
    client=SimpleNamespace(
        product_stocks_all=AsyncMock(return_value=FetchResult.failure('ozon','HTTP 500',500,1)),
        fbo_stocks=AsyncMock(return_value=FetchResult.success('ozon',{'items':[
            {'sku':501,'warehouse_id':1,'free_to_sell_amount':6}]},200,1)),
    )
    result=await CollectionService(repo,ozon=client).collect_inventory(
        shop_id=shop.id,ozon_connection_id=oz.id,data_date=DAY)
    assert [r.ok for r in result]==[False,True]
    assert repo.latest_inventory_by_listing(shop.id)[0]['available_units']==10
    assert 'Ozon FBS' in format_stock_report(build_product_report(repo,shop.id,DAY))


def test_linked_product_with_missing_market_stock_has_no_purchase_recommendation(data):
    from app.services.supply import build_supply_plan
    repo,shop,wb,oz=data
    product=repo.ensure_product(shop.id,'BOTH','Одинаковый товар')
    item=repo.ensure_listing(product.id,wb.id,'1001')
    repo.ensure_listing(product.id,oz.id,'501')
    stock(repo,wb,item,0,endpoint='analytics/stocks/seller-warehouses')
    plan=build_supply_plan(repo,shop.id,DAY,persist=False)
    row=next(r for r in plan.rows if r.product_id==product.id)
    assert row.available_units is None and row.recommended_order_units==0
    assert row.missing_inventory_marketplaces==('ozon',)


def test_economics_keeps_wb_visible_when_ozon_has_larger_orders(data):
    repo, shop, wb, oz = data
    item = listing(repo, shop, wb, '1003', 'Товар WB для проверки')
    orders(repo, wb, item, 1, 100)
    for index in range(12):
        item = listing(repo, shop, oz, str(500 + index), f'Товар Ozon {index}')
        orders(repo, oz, item, 2, 1000 + index)
    text = format_sku_economics(build_sku_economics(repo, shop.id, DAY, 1))
    assert 'Товар WB для проверки' in text and 'Ozon' in text and 'WB' in text
    assert 'не задана' in text and 'себестоимость заполнена не полностью' in text


def test_wb_accruals_deduplicate_report_and_keep_latest_correction(data):
    repo, shop, wb, _ = data
    old = {'reportId': 99, 'dateFrom': '2026-09-28', 'dateTo': '2026-09-28', 'retailAmountSum': 100, 'forPaySum': 70}
    repo.record_success(wb.id, 'finance/sales-reports/list', '2026-10-01', {'reports': [old]}, [])
    corrected = dict(old, retailAmountSum=120, forPaySum=85, bankPaymentSum=80)
    repo.record_success(wb.id, 'finance/sales-reports/list', '2026-10-02', {'reports': [corrected]}, [])
    ledger = build_wb_accrual_ledger(repo, shop.id, '2026-10-01', '2026-10-02')
    assert len(ledger.rows) == 1 and ledger.rows[0]['financial_sales'] == 120
    assert ledger.rows[0]['bank_payment'] == 80 and ledger.rows[0]['storage'] is None
    assert ledger.rows[0]['report_from'] == '2026-09-28'
    text = format_wb_accrual_ledger(ledger)
    assert 'нет значения в API' in text and '120.00' in text and 'не подтверждает поступление' in text


def test_alert_digest_keeps_marketplace_sku_quantity_before_long_name():
    row = StockRisk('ozon', '😀<&' * 500, '501', 3, 0, .2, 15, 14, None)
    text = format_alert_digest([AlertNotification('low_stock', 'ozon:501', 'warning', stock_alert_text(row), 15)])
    assert 'Ozon · артикул 501' in text and '3 шт.' in text and '15.0 дн.' in text
    assert utf16_length(text) <= 3900


def test_action_center_long_products_still_fit_one_message():
    items = tuple(ActionItem(str(i), 1, 'supply', '😀<&' * 500, '🟠<&' * 500, 'Подсказка ' * 50) for i in range(20))
    text = format_action_center(ActionCenter(1, DAY, items))
    assert utf16_length(text) <= 3900
    assert 'Всего: 20 · страница 1/4' in text
    assert text.count('Срочно ·') == 5


def test_report_splitting_preserves_long_names_emoji_and_entities():
    name = '😀<&' * 2000
    chunks = split_report_html('<b>' + escape(name) + '</b>\nОстаток: 3 шт.')
    assert all(utf16_length(chunk) <= 3900 for chunk in chunks)
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(); self.parts = []
        def handle_data(self, value):
            self.parts.append(value)
    parsers = []
    for chunk in chunks:
        parser = Text(); parser.feed(chunk); parsers.append(''.join(parser.parts))
    assert ''.join(parsers).replace('\n', '') == name + 'Остаток: 3 шт.'
