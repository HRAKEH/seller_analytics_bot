"""Regression coverage for real Ozon failures: empty campaigns and false zero spend."""
from datetime import date
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.integrations.base import FetchResult
from app.integrations.ozon_performance import OzonPerformanceClient
from app.reports.ads import build_advertising_report, format_advertising
from app.reports.coverage import build_source_coverage
from app.reports.management import build_management_report
from app.services.advertising import (
    AdvertisingNormalizationError, normalize_ozon_ad_campaign_detail,
    normalize_ozon_ad_product_detail,
)
from app.services.collection import CollectionService
from app.services.finance import normalize_ozon_ad_stats
from app.services.resilience import partial_error
from app.storage import Database, Repository, MetricPoint
from app.storage.models import AdCampaignPoint, AdProductPoint


DAY = date(2026, 10, 7)
DS = DAY.isoformat()


@pytest.fixture
def basis(tmp_path):
    db = Database(tmp_path/'ads.sqlite3'); db.initialize()
    repo = Repository(db)
    seller = repo.ensure_seller(101); shop = repo.ensure_shop(seller.id)
    conn = repo.ensure_connection(shop.id, 'ozon')
    product = repo.ensure_product(shop.id, 'P1', 'Товар', cost_price=10)
    listing = repo.ensure_listing(product.id, conn.id, '9001')
    return repo, shop, conn, listing


def success(body):
    return FetchResult.success('ozon_performance', body, 200, 1)


def campaign(cid='12', spend=10, sales=100, **extra):
    return {'campaignId': cid, 'expense': spend, 'ordersMoney': sales, **extra}


def sku(cid='12', spend=10, **extra):
    return {'date': DS, 'sku': '9001', 'campaignId': cid,
            'expense': spend, 'sales': 100, **extra}


def previous(basis, *, raw=True):
    repo, shop, conn, listing = basis
    run = repo.record_success(conn.id, 'performance/product-stats', DS,
        {'rows': [campaign(spend=80)]}, [MetricPoint(conn.id, DS, 'ad_spend', 80, 'RUB')], store_raw=raw)
    repo.save_ad_details(run, campaigns=[AdCampaignPoint(conn.id, DS, '12', 'Кампания', 80, 100)],
        products=[AdProductPoint(conn.id, DS, '9001', '12', 'Кампания', listing.id, 80, 100)])
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize('ids', [None, [], [''], ['0'], ['-1'], ['nan'], ['12', str(2**64)]])
async def test_client_rejects_missing_or_invalid_campaign_ids_without_network(ids):
    seen = []
    client = OzonPerformanceClient('test', 'secret', min_interval=0,
        transport=httpx.MockTransport(lambda req: seen.append(req)))
    try:
        result = await client.product_sku_stats(DS, DS, ids)
        assert not result.ok and 'ID кампаний' in result.error
        assert seen == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_deduplicates_ids_and_reuses_token_without_empty_request():
    requests = []
    async def handler(request):
        requests.append(request)
        if request.url.path == '/api/client/token':
            return httpx.Response(200, json={'access_token': 'test-token', 'expires_in': 1800})
        assert json.loads(request.content) == {'dateFrom': DS, 'dateTo': DS, 'campaignIds': ['12', '13']}
        return httpx.Response(200, json={'rows': []})
    client = OzonPerformanceClient('test', 'secret', min_interval=0, transport=httpx.MockTransport(handler))
    try:
        assert (await client.product_sku_stats(DS, DS, ['12', '12', '13'])).ok
        assert (await client.product_sku_stats(DS, DS, ['12', '13'])).ok
        assert len(requests) == 3
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_collector_uses_all_period_ids_and_retains_original_responses(basis):
    repo, shop, conn, _ = basis
    body = {'rows': [campaign('12', state='CAMPAIGN_STATE_INACTIVE'),
                     campaign('13', state='CAMPAIGN_STATE_ARCHIVED')]}
    sku_body = {'rows': [sku('12'), sku('13', spend=5)]}
    perf = SimpleNamespace(product_campaign_stats=AsyncMock(return_value=success(body)),
                           product_sku_stats=AsyncMock(return_value=success(sku_body)))
    result = await CollectionService(repo, ozon_performance=perf).collect_advertising(
        start=DAY, end=DAY, ozon_connection_id=conn.id)
    assert all(r.ok for r in result)
    perf.product_sku_stats.assert_awaited_once_with(DS, DS, campaign_ids=['12', '13'])
    assert json.loads(repo.raw_payload_for_run(result[0].run_id))['response'] == body
    raw = json.loads(repo.raw_payload_for_run(result[1].run_id))
    assert raw['responses'][0]['response'] == sku_body
    assert raw['responses'][0]['campaignIds'] == ['12', '13']
    assert 'secret' not in json.dumps(raw) and 'Authorization' not in json.dumps(raw)
    assert repo.ad_product_totals(shop.id, DS, DS)[0]['spend'] == 15
    assert repo.latest_metric(conn.id, DS, 'ad_spend')['value'] == 20


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, '', ' ', True, 'NaN', '-1', {}])
async def test_unknown_spend_retains_previous_data_and_saves_failed_response(basis, value):
    repo, shop, conn, _ = basis
    previous(basis)
    body = {'rows': [campaign(spend=value)]}
    perf = SimpleNamespace(product_campaign_stats=AsyncMock(return_value=success(body)))
    result = await CollectionService(repo, ozon_performance=perf).collect_advertising(
        start=DAY, end=DAY, ozon_connection_id=conn.id)
    assert len(result) == 1 and not result[0].ok
    assert repo.latest_metric(conn.id, DS, 'ad_spend')['value'] == 80
    assert repo.ad_campaign_totals(shop.id, DS, DS)[0]['spend'] == 80
    assert json.loads(repo.raw_payload_for_run(result[0].run_id))['response'] == body
    text = format_advertising(build_advertising_report(repo, shop.id, DAY, 1))
    assert 'данные неполные' in text


@pytest.mark.asyncio
async def test_missing_money_does_not_block_sku_with_valid_campaign_ids(basis):
    repo, _, conn, _ = basis
    perf = SimpleNamespace(product_campaign_stats=AsyncMock(return_value=success(
        {'rows': [{'id': '12', 'ordersMoney': 100}]})),
        product_sku_stats=AsyncMock(return_value=success({'rows': [sku()]})))
    result = await CollectionService(repo, ozon_performance=perf).collect_advertising(
        start=DAY, end=DAY, ozon_connection_id=conn.id)
    assert [r.ok for r in result] == [False, True]
    perf.product_sku_stats.assert_awaited_once_with(DS, DS, campaign_ids=['12'])
    assert repo.latest_metric(conn.id, DS, 'ad_spend') is None


@pytest.mark.asyncio
async def test_explicit_empty_campaigns_clear_old_snapshot_without_sku_request(basis):
    repo, shop, conn, _ = basis
    previous(basis)
    perf = SimpleNamespace(product_campaign_stats=AsyncMock(return_value=success({'rows': []})),
                           product_sku_stats=AsyncMock())
    result = await CollectionService(repo, ozon_performance=perf).collect_advertising(
        start=DAY, end=DAY, ozon_connection_id=conn.id)
    assert all(r.ok for r in result)
    perf.product_sku_stats.assert_not_awaited()
    assert all(r['spend']==0 for r in repo.ad_campaign_totals(shop.id, DS, DS))
    assert all(r['spend']==0 for r in repo.ad_product_totals(shop.id, DS, DS))
    assert repo.latest_metric(conn.id, DS, 'ad_spend')['value'] == 0
    assert json.loads(repo.raw_payload_for_run(result[1].run_id))['empty_campaign_response'] == {'rows': []}


@pytest.mark.asyncio
async def test_failed_campaign_lookup_never_sends_empty_ids_or_clears_sku(basis):
    repo, shop, conn, _ = basis
    previous(basis)
    perf = SimpleNamespace(product_campaign_stats=AsyncMock(return_value=FetchResult.failure(
        'ozon', 'HTTP 403: forbidden', 403, 1)), product_sku_stats=AsyncMock())
    result = await CollectionService(repo, ozon_performance=perf).collect_advertising(
        start=DAY, end=DAY, ozon_connection_id=conn.id)
    assert len(result) == 2 and all(not r.ok for r in result)
    perf.product_sku_stats.assert_not_awaited()
    assert repo.ad_product_totals(shop.id, DS, DS)[0]['spend'] == 80


@pytest.mark.asyncio
async def test_failed_second_batch_retains_complete_previous_sku_snapshot(basis):
    repo, shop, conn, _ = basis
    previous(basis)
    ids = [str(i) for i in range(1, 12)]
    perf = SimpleNamespace(product_campaign_stats=AsyncMock(return_value=success(
        {'rows': [campaign(cid) for cid in ids]})),
        product_sku_stats=AsyncMock(side_effect=[success({'rows': [sku('1', spend=3)]}),
            FetchResult.failure('ozon', 'HTTP 429: лимит API', 429, 1)]))
    result = await CollectionService(repo, ozon_performance=perf).collect_advertising(
        start=DAY, end=DAY, ozon_connection_id=conn.id)
    assert [r.ok for r in result] == [True, False]
    assert perf.product_sku_stats.await_args_list[0].kwargs['campaign_ids'] == ids[:10]
    assert perf.product_sku_stats.await_args_list[1].kwargs['campaign_ids'] == ids[10:]
    assert repo.ad_product_totals(shop.id, DS, DS)[0]['spend'] == 80
    raw = json.loads(repo.raw_payload_for_run(result[1].run_id))
    assert len(raw['responses']) == 2 and raw['responses'][1]['http_status'] == 429
    assert '429' in partial_error('advertising', result)


@pytest.mark.asyncio
@pytest.mark.parametrize('broken', [
    {'date': '2026-10-06'}, {'campaignId': '999'}, {'sku': ''}, {'expense': None},
])
async def test_wrong_sku_date_campaign_or_missing_fields_preserve_previous(basis, broken):
    repo, shop, conn, _ = basis
    previous(basis)
    perf = SimpleNamespace(product_campaign_stats=AsyncMock(return_value=success({'rows': [campaign()]})),
        product_sku_stats=AsyncMock(return_value=success({'rows': [sku(**broken)]})))
    result = await CollectionService(repo, ozon_performance=perf).collect_advertising(
        start=DAY, end=DAY, ozon_connection_id=conn.id)
    assert [r.ok for r in result] == [True, False]
    assert repo.ad_product_totals(shop.id, DS, DS)[0]['spend'] == 80


def test_money_spent_field_is_consistent_in_campaign_and_aggregate_parsers():
    row = {'date': DS, 'id': '12', 'title': 'Кампания', 'moneySpent': '1\u00a0234,56', 'ordersMoney': '2000'}
    point = normalize_ozon_ad_campaign_detail({'rows': [row]}, 1)[0]
    aggregate = {p.metric_key: p.value for p in normalize_ozon_ad_stats({'rows': [row]}, 1)[DS]}
    assert point.spend == aggregate['ad_spend'] == 1234.56
    assert point.campaign_name == 'Кампания'
    assert point.attributed_sales == aggregate['ad_attributed_sales'] == 2000


def test_duplicate_identical_rows_are_not_double_counted_but_conflicts_fail():
    row = sku()
    assert len(normalize_ozon_ad_product_detail({'rows': [row, row]}, 1)) == 1
    with pytest.raises(AdvertisingNormalizationError):
        normalize_ozon_ad_product_detail({'rows': [row, sku(spend=20)]}, 1)
    crow = campaign(date=DS)
    assert len(normalize_ozon_ad_campaign_detail({'rows': [crow, crow]}, 1)) == 1


@pytest.mark.asyncio
async def test_repeated_refresh_is_idempotent_and_spend_changes_replace_values(basis):
    repo, shop, conn, _ = basis
    perf = SimpleNamespace(product_campaign_stats=AsyncMock(return_value=success({'rows': [campaign()]})),
        product_sku_stats=AsyncMock(return_value=success({'rows': [sku()]})))
    collector = CollectionService(repo, ozon_performance=perf)
    first = await collector.collect_advertising(start=DAY, end=DAY, ozon_connection_id=conn.id)
    second = await collector.collect_advertising(start=DAY, end=DAY, ozon_connection_id=conn.id)
    assert [r.run_id for r in first] == [r.run_id for r in second]
    assert repo.count('source_runs') == repo.count('raw_payloads') == 2
    assert repo.count('ad_campaign_daily') == repo.count('ad_product_daily') == 1
    perf.product_campaign_stats.return_value = success({'rows': [campaign(spend=15)]})
    perf.product_sku_stats.return_value = success({'rows': [sku(spend=15)]})
    await collector.collect_advertising(start=DAY, end=DAY, ozon_connection_id=conn.id)
    assert repo.ad_campaign_totals(shop.id, DS, DS)[0]['spend'] == 15
    assert repo.ad_product_totals(shop.id, DS, DS)[0]['spend'] == 15


def test_legacy_unsaved_ozon_spend_is_unknown_in_report_and_coverage(basis):
    repo, shop, _, _ = basis
    previous(basis, raw=False)
    report = build_advertising_report(repo, shop.id, DAY, 1)
    assert report.campaigns[0].spend is None and report.products[0].spend is None
    assert report.campaigns[0].drr is None and report.products[0].roas is None
    assert 'расход не подтверждён' in format_advertising(report)
    coverage = next(r for r in build_source_coverage(repo, shop.id, DS, DS)
                    if r.component == 'Рекламная статистика')
    assert coverage.available_days == 0 and coverage.note


@pytest.mark.asyncio
async def test_explicit_zero_is_confirmed_and_has_raw_source(basis):
    repo, shop, conn, _ = basis
    perf = SimpleNamespace(product_campaign_stats=AsyncMock(return_value=success(
        {'rows': [campaign(spend=0, sales=0)]})))
    result = await CollectionService(repo, ozon_performance=perf).collect_advertising(
        start=DAY, end=DAY, ozon_connection_id=conn.id)
    assert all(r.ok for r in result)
    report = build_advertising_report(repo, shop.id, DAY, 1)
    assert report.campaigns[0].spend == 0
    coverage = next(r for r in report.source_coverage if r.marketplace == 'ozon')
    assert coverage.available_days == 1 and not coverage.failed_dates


@pytest.mark.asyncio
async def test_historical_sku_limit_keeps_archive_and_is_not_retried_as_an_error(basis):
    repo, shop, conn, _ = basis
    previous(basis)
    perf = SimpleNamespace(product_campaign_stats=AsyncMock(return_value=success({'rows': [campaign()]})),
        product_sku_stats=AsyncMock(), sku_history_start=lambda: date(2026,10,8))
    collector = CollectionService(repo, ozon_performance=perf)
    first = await collector.collect_advertising(start=DAY, end=DAY, ozon_connection_id=conn.id)
    second = await collector.collect_advertising(start=DAY, end=DAY, ozon_connection_id=conn.id)
    assert all(r.ok for r in first+second)
    perf.product_sku_stats.assert_not_awaited()
    assert first[1].run_id == second[1].run_id
    assert repo.latest_run(conn.id, 'performance/products-sku', DS).status == 'partial'
    assert repo.ad_product_totals(shop.id, DS, DS)[0]['spend'] == 80
    assert 'архив' in format_advertising(build_advertising_report(repo, shop.id, DAY, 1))
    coverage = next(r for r in build_source_coverage(repo, shop.id, DS, DS)
                    if r.component == 'Рекламная статистика')
    assert coverage.partial_dates == (DS,) and not coverage.failed_dates


@pytest.mark.asyncio
async def test_yesterday_sku_remains_queryable(basis):
    repo, _, conn, _ = basis
    perf = SimpleNamespace(product_campaign_stats=AsyncMock(return_value=success({'rows': [campaign()]})),
        product_sku_stats=AsyncMock(return_value=success({'rows': [sku()]})), sku_history_start=lambda: DAY)
    result = await CollectionService(repo, ozon_performance=perf).collect_advertising(
        start=DAY, end=DAY, ozon_connection_id=conn.id)
    assert all(r.ok for r in result)
    perf.product_sku_stats.assert_awaited_once_with(DS, DS, campaign_ids=['12'])


def test_legacy_spend_cannot_be_a_confirmed_management_expense(basis):
    repo, shop, _, _ = basis
    previous(basis, raw=False)
    report=build_management_report(repo,shop.id,DAY,1)
    source=next(r for r in report.sources if r.marketplace=='ozon')
    assert source.ad_spend is None and source.estimated_result is None
    assert source.performance_ad_spend is None
    assert any('не подтверждены' in warning for warning in source.warnings)


def test_billed_finance_remains_known_when_legacy_performance_is_unknown(basis):
    repo, shop, conn, _ = basis
    previous(basis, raw=False)
    repo.record_success(conn.id,'finance/accrual/by-day',DS,{'finance':1},[
        MetricPoint(conn.id,DS,'marketplace_net',100,'RUB'),
        MetricPoint(conn.id,DS,'services',20,'RUB'),
        MetricPoint(conn.id,DS,'finance_ad_spend',20,'RUB')])
    report=build_management_report(repo,shop.id,DAY,1)
    source=next(r for r in report.sources if r.marketplace=='ozon')
    assert source.ad_spend==20 and source.ads_from_finance
    assert source.performance_ad_spend is None
