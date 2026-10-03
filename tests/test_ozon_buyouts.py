"""Buyout contract probes and conservative joins; fixtures are not live proof."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.bot.context import AppContext
from app.config import Settings
from app.integrations import FetchResult, OzonClient
from app.reports.cards import format_daily_card
from app.reports.coverage import build_source_coverage
from app.reports.daily import build_daily_report
from app.services.backups import BackupService
from app.services.collection import CollectionService
from app.services.ozon_buyouts import build_buyout_check, format_buyout_check
from app.services.report_refresh import refresh_reports
from app.storage import Database, MetricPoint, Repository

DAY=date(2026,10,1)
NUMBER='0225557127-0029-1'
SKU=4449497713


def fixture(tmp_path, qty=1):
    db=Database(tmp_path/'buyouts.sqlite3');db.initialize();repo=Repository(db)
    shop=repo.ensure_shop(repo.ensure_seller(1).id)
    conn=repo.ensure_connection(shop.id,'ozon','Ozon')
    analytics={'result':{'data':[{'dimensions':[{'id':str(SKU)}],'metrics':[qty,750*qty]}]}}
    repo.record_success(conn.id,'analytics/orders',DAY.isoformat(),analytics,[
        MetricPoint(conn.id,DAY.isoformat(),'ordered_units',qty,'units'),
        MetricPoint(conn.id,DAY.isoformat(),'ordered_revenue',750*qty,'RUB')])
    repo.record_success(conn.id,'finance/accrual/by-day',DAY.isoformat(),{'accruals':[]},[
        MetricPoint(conn.id,DAY.isoformat(),'marketplace_net',0,'RUB')])
    posting={'posting_number':NUMBER,'in_process_at':'2026-10-01T15:07:03Z','status':'delivering',
        'products':[{'sku':SKU,'offer_id':'tube','quantity':qty,'is_marketplace_buyout':True,
                     'price':{'amount':'750','currency':'RUB'}}],
        'financial_data':{'products':[{'product_id':SKU,'quantity':qty,
            'customer_price':{'amount':'14.98','currency':'BYN'},'price':750,'payout':0}]}}
    repo.record_success(conn.id,'postings/fbs',DAY.isoformat(),{'postings':[posting]},[])
    repo.record_success(conn.id,'postings/fbo',DAY.isoformat(),{'postings':[]},[])
    return repo,shop,conn,posting


def row(qty=1, **changes):
    result={'posting_number':NUMBER,'sku':str(SKU),'offer_id':'tube','quantity':qty,
            'seller_price_per_instance':750,'buyout_price':'345.00','amount':str(345*qty),
            'extension':{'arbitrary':'preserve'}}
    result.update(changes)
    return result


def capture(repo,conn,rows,*,start=DAY,end=DAY,status='success'):
    raw={'order_date_from':start.isoformat(),'order_date_to':end.isoformat(),
        'report_date_from':start.isoformat(),'report_date_to':(end+timedelta(days=2)).isoformat(),
        'reports':[{'date_from':start.isoformat(),'date_to':(end+timedelta(days=2)).isoformat(),
                    'ok':True,'http_status':200,'response':{'products':rows}}]}
    return repo.record_success(conn.id,'finance/products/buyout',end.isoformat(),raw,[],status=status)


@pytest.mark.asyncio
async def test_transport_and_backup_keep_original_prices_and_request_period(tmp_path):
    repo,_,conn,_=fixture(tmp_path);response={'products':[row()], 'new_field':['untouched']}
    async def handler(request):
        assert request.method=='POST' and request.url.path=='/v1/finance/products/buyout'
        assert request.headers['Client-Id']=='buyout-test' and request.headers['Api-Key']=='test-key'
        assert json.loads(request.content)=={'date_from':'2026-10-01','date_to':'2026-10-03'}
        return httpx.Response(200,json=response)
    client=OzonClient('buyout-test','test-key',min_interval=0,max_retries=0,transport=httpx.MockTransport(handler))
    try:
        outcomes=await CollectionService(repo,ozon=client).collect_ozon_buyout_prices_range(
            connection_id=conn.id,start=DAY,end=DAY,as_of=DAY+timedelta(days=2))
    finally:await client.close()
    assert len(outcomes)==1 and outcomes[0].ok
    source=repo.latest_buyout_capture(conn.id,DAY.isoformat())
    assert json.loads(source['payload_json'])['reports'][0]['response']==response
    backup=BackupService(repo.db,repo,tmp_path/'backups').create()
    with sqlite3.connect(backup.path.as_uri()+'?mode=ro',uri=True) as c:
        payload=c.execute('SELECT payload_json FROM raw_payloads WHERE source_run_id=?',(source['id'],)).fetchone()[0]
        assert json.loads(payload)['reports'][0]['response']==response
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert repo.metrics_for_day(conn.id,DAY.isoformat())=={'ordered_units':1,'ordered_revenue':750,'marketplace_net':0}


@pytest.mark.asyncio
async def test_client_rejects_oversized_or_reversed_period_before_network():
    client=OzonClient('buyout-invalid','test',transport=httpx.MockTransport(lambda r:pytest.fail('unexpected request')))
    try:
        for first,last in [('2026-10-01','2026-11-01'),('2026-10-02','2026-10-01')]:
            with pytest.raises(ValueError):await client.finance_products_buyout(first,last)
    finally:await client.close()


@pytest.mark.asyncio
async def test_delayed_window_splits_without_overlap_and_has_one_snapshot(tmp_path):
    repo,_,conn,_=fixture(tmp_path);calls=[]
    async def fetch(first,last):
        calls.append((first,last));return FetchResult.success('ozon',{'products':[]},200,1)
    service=CollectionService(repo,ozon=SimpleNamespace(finance_products_buyout=fetch))
    await service.collect_ozon_buyout_prices_range(connection_id=conn.id,start=DAY,end=DAY,
        as_of=DAY+timedelta(days=100))
    assert calls==[('2026-10-01','2026-10-31'),('2026-11-01','2026-11-30')]
    with repo.db.connect() as c:
        assert c.execute("SELECT count(*) FROM source_runs WHERE endpoint='finance/products/buyout'").fetchone()[0]==1
    check=build_buyout_check(repo,conn.id,DAY)
    assert check.report_complete and check.matched_units==0 and check.expected_units==1


def test_range_capture_is_available_for_each_order_day_and_latest_empty_replaces_it(tmp_path):
    repo,_,conn,_=fixture(tmp_path)
    capture(repo,conn,[row()],start=DAY-timedelta(days=2),end=DAY+timedelta(days=3))
    assert build_buyout_check(repo,conn.id,DAY).matches[0].price==Decimal('345')
    assert repo.latest_buyout_capture(conn.id,(DAY-timedelta(days=3)).isoformat()) is None
    capture(repo,conn,[])
    check=build_buyout_check(repo,conn.id,DAY)
    assert not check.matches and check.matched_units==0 and check.expected_units==1


@pytest.mark.parametrize('change',[
    {'posting_number':'other-posting'}, {'sku':999}, {'sku':None}, {'quantity':2},
    {'quantity':True}, {'quantity':'1.5'}, {'buyout_price':None}, {'buyout_price':'NaN'},
    {'buyout_price':-1}, {'buyout_price':'1E+9999'}, {'amount':344}, {'amount':None},
    {'currency':'invalid'}, {'posting_number':{}}, {'sku':{}},
])
def test_invalid_or_unrelated_rows_never_become_prices(tmp_path,change):
    repo,_,conn,_=fixture(tmp_path);capture(repo,conn,[row(**change)])
    assert not build_buyout_check(repo,conn.id,DAY).matches


@pytest.mark.parametrize('duplicate',[row(),row(buyout_price=340,amount=340)])
def test_duplicates_do_not_double_count_or_select_arbitrary_price(tmp_path,duplicate):
    repo,_,conn,_=fixture(tmp_path);capture(repo,conn,[row(),duplicate])
    check=build_buyout_check(repo,conn.id,DAY)
    assert not check.matches and any('Неоднозначные' in w for w in check.warnings)


@pytest.mark.parametrize('qty',[1,2])
def test_exact_join_keeps_currency_unknown_and_does_not_change_daily_totals(tmp_path,qty):
    repo,shop,conn,_=fixture(tmp_path,qty);capture(repo,conn,[row(qty)])
    check=build_buyout_check(repo,conn.id,DAY)
    assert check.expected_units==check.matched_units==qty
    assert check.matches[0].amount==Decimal(345*qty) and check.matches[0].currency is None
    report=build_daily_report(repo,shop.id,DAY)
    assert report.sources[0].ordered_revenue==750*qty and report.sources[0].marketplace_net==0
    assert report.sources[0].buyer_prices.totals[0].amount==Decimal('14.98')*qty
    text=format_daily_card(repo,shop.id,report)
    assert '345' not in text.summary_html
    assert NUMBER in text.details_html and '345,00 (валюта в ответе не указана)' in text.details_html
    assert 'ещё требует сверки' in text.details_html


def test_explicit_currency_zero_price_and_other_shop_are_preserved(tmp_path):
    repo,shop,conn,_=fixture(tmp_path);capture(repo,conn,[row(buyout_price=0,amount=0,currency='RUB')])
    other=repo.ensure_shop(shop.seller_id,'Other');other_conn=repo.ensure_connection(other.id,'ozon','Other')
    capture(repo,other_conn,[row(buyout_price=999,amount=999,currency='BYN')])
    check=build_buyout_check(repo,conn.id,DAY)
    assert check.matches[0].price==0 and check.matches[0].currency=='RUB'
    assert '0,00 ₽' in '\n'.join(format_buyout_check(check))


@pytest.mark.asyncio
@pytest.mark.parametrize('code',[403,429])
async def test_unavailable_report_keeps_saved_price_and_general_finance_healthy(tmp_path,code):
    repo,_,conn,_=fixture(tmp_path);capture(repo,conn,[row()])
    client=SimpleNamespace(finance_products_buyout=AsyncMock(return_value=FetchResult.failure('ozon','Access denied',code,1)))
    outcomes=await CollectionService(repo,ozon=client).collect_ozon_buyout_prices_range(
        connection_id=conn.id,start=DAY,end=DAY,as_of=DAY)
    assert not outcomes[0].ok and client.finance_products_buyout.await_count==1
    check=build_buyout_check(repo,conn.id,DAY)
    assert check.matches[0].price==345 and any('Последний запрос' in w for w in check.warnings)
    fin=next(r for r in build_source_coverage(repo,conn.shop_id,DAY.isoformat(),DAY.isoformat()) if r.component=='Финансы')
    assert fin.available_days==1 and not fin.failed_dates


@pytest.mark.asyncio
async def test_malformed_success_is_saved_for_review_and_not_treated_as_empty(tmp_path):
    repo,_,conn,_=fixture(tmp_path)
    body={'unexpected_format':{'items':[row()]}}
    client=SimpleNamespace(finance_products_buyout=AsyncMock(return_value=FetchResult.success('ozon',body,200,1)))
    outcomes=await CollectionService(repo,ozon=client).collect_ozon_buyout_prices_range(
        connection_id=conn.id,start=DAY,end=DAY,as_of=DAY)
    saved=repo.latest_buyout_capture(conn.id,DAY.isoformat())
    assert not outcomes[0].ok and saved['status']=='partial'
    assert json.loads(saved['payload_json'])['reports'][0]['response']==body
    check=build_buyout_check(repo,conn.id,DAY)
    assert not check.report_complete and not check.matches


def test_missing_flag_partial_postings_and_conflicts_prevent_full_coverage(tmp_path):
    repo,_,conn,posting=fixture(tmp_path);capture(repo,conn,[row()])
    changed=deepcopy(posting);changed['products'][0].pop('is_marketplace_buyout')
    repo.record_success(conn.id,'postings/fbs',DAY.isoformat(),{'postings':[changed]},[])
    check=build_buyout_check(repo,conn.id,DAY)
    assert not check.postings_complete and not check.matches
    repo.record_success(conn.id,'postings/fbs',DAY.isoformat(),{'postings':[posting,changed]},[])
    check=build_buyout_check(repo,conn.id,DAY)
    assert not check.postings_complete and not check.matches


@pytest.mark.asyncio
async def test_partial_financial_window_does_not_claim_complete_coverage(tmp_path):
    repo,_,conn,_=fixture(tmp_path);responses=iter([
        FetchResult.success('ozon',{'products':[row()]},200,1),
        FetchResult.failure('ozon','Unavailable',500,1)])
    async def fetch(*a):return next(responses)
    outcomes=await CollectionService(repo,ozon=SimpleNamespace(finance_products_buyout=fetch)).collect_ozon_buyout_prices_range(
        connection_id=conn.id,start=DAY,end=DAY,as_of=DAY+timedelta(days=60))
    check=build_buyout_check(repo,conn.id,DAY)
    assert not outcomes[0].ok and not check.report_complete and check.matched_units==1


@pytest.mark.asyncio
async def test_manual_refresh_and_scheduled_reconciliation_capture_buyouts_independently(tmp_path):
    repo,shop,conn,_=fixture(tmp_path)
    client=SimpleNamespace(finance_products_buyout=AsyncMock(return_value=FetchResult.success('ozon',{'products':[row()]},200,1)))
    collector=CollectionService(repo,ozon=client)
    for method in ('backfill_orders','collect_finance','collect_advertising','collect_ozon_fulfillment_range'):
        setattr(collector,method,AsyncMock(return_value=[]))
    collector.collect_ozon_orders_day=AsyncMock(return_value=SimpleNamespace(ok=True))
    ctx=AppContext(Settings.from_env(),repo,shop.id,collector,ozon_connection_id=conn.id)
    stages=await refresh_reports(ctx,DAY,DAY)
    assert next(s for s in stages if s.key=='buyouts').ok
    await ctx.collect_reconciliation(DAY,DAY,include_finance=False)
    await ctx.collect_buyer_prices(DAY)
    assert client.finance_products_buyout.await_count==3
    assert repo.latest_buyout_capture(conn.id,DAY.isoformat()) is not None


@pytest.mark.asyncio
async def test_denied_optional_buyout_does_not_retry_lifecycle_or_finance(tmp_path):
    repo,shop,conn,_=fixture(tmp_path)
    core=SimpleNamespace(ok=True)
    client=SimpleNamespace(finance_products_buyout=AsyncMock(return_value=FetchResult.failure('ozon','Denied',403,1)))
    collector=CollectionService(repo,ozon=client)
    collector.backfill_orders=AsyncMock(return_value=[core])
    collector.collect_finance=AsyncMock(return_value=[core])
    ctx=AppContext(Settings.from_env(),repo,shop.id,collector,ozon_connection_id=conn.id)
    assert await ctx.collect_reconciliation(DAY,DAY)==[core,core]
    assert repo.latest_run(conn.id,'finance/products/buyout',DAY.isoformat()).status=='failed'
