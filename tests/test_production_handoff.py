from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, timedelta
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.integrations.base import FetchResult, MarketplaceClient, _server_retry_delay
from app.integrations.ozon_performance import OzonPerformanceClient
from app.reports.management import build_management_report, format_management, ManagementReport, ManagementSource
from app.services.collection import CollectionService
from app.services.finance import (FinanceNormalizationError, normalize_wb_product_finance,
    normalize_ozon_accruals, normalize_ozon_product_finance, normalize_ozon_ad_stats,
    normalize_wb_finance_report)
from app.services.live_audit import inspect_database, run_live_audit
from app.services.reconciliation import normalize_wb_finance_events, normalize_ozon_finance_events
from app.services.supply import QUALITY_METHOD_VERSION, _xyz, _trend
from app.storage import Database, Repository, MetricPoint, CommerceEventPoint


DAY=date(2026,9,28)


def make_repo(tmp_path,marketplace='ozon'):
    db=Database(tmp_path/'handoff.sqlite3'); db.initialize(); repo=Repository(db)
    seller=repo.ensure_seller(1,'Seller'); shop=repo.ensure_shop(seller.id,'ИП Карпенко В.В')
    conn=repo.ensure_connection(shop.id,marketplace,marketplace)
    return db,repo,seller,shop,conn


def amounts(points):
    return {p.metric_key:p.value for p in points}


@pytest.mark.parametrize('raw', [150,-150])
def test_wb_return_is_subtracted_once_and_current_fields_are_loaded(raw):
    payload=[{'rrDate':DAY.isoformat(),'nmId':123,'vendorCode':'ITEM','title':'Item',
              'docTypeName':'Возврат','retailAmount':raw,'forPay':raw,
              'deliveryService':10,'paidStorage':2,'paidAcceptance':3,'additionalPayment':4}]
    metrics=normalize_wb_product_finance(payload)[0].metrics
    assert metrics=={'financial_sales':-150,'goods_payable':-150,'logistics':10,'storage':2,
                     'acceptance':3,'compensation':4}
    event=normalize_wb_finance_events(payload)[0]
    assert event.gross_amount==-150 and event.net_amount==-150


def ozon_rows(category='POSTING'):
    return {'accruals':[
        {'accrued_category':category,'total_amount':-160,'posting':{'products':[
            {'sku':123,'commission':{'seller_price':1000,'sale_commission':-150},
             'delivery':{'total_accrued':-10}}]},
         'item_fees':{'fees':[{'sku':123,'fees':[{'accrued':-20}]}]}},
        {'accrued_category':category,'total_amount':37,'posting':{'products':[
            {'sku':123,'commission':{'seller_price':0,'sale_commission':30},
             'delivery':{'total_accrued':2}}]},
         'item_fees':{'fees':[{'sku':123,'fees':[{'accrued':{'amount':5,'currency':'RUB'}}]}]}}
    ]}


def test_ozon_corrections_reduce_aggregate_and_sku_expenses():
    payload=ozon_rows()
    aggregate=amounts(normalize_ozon_accruals(payload,1,DAY.isoformat()))
    sku=normalize_ozon_product_finance(payload,DAY.isoformat())[0].metrics
    for key,value in {'financial_sales':1000,'commission':120,'logistics':8,'services':15}.items():
        assert aggregate[key]==sku[key]==value
    assert aggregate['marketplace_net']==-123


def test_ozon_nonposting_products_do_not_create_financial_sales():
    payload=ozon_rows('OTHER')
    assert amounts(normalize_ozon_accruals(payload,1,DAY.isoformat()))['financial_sales']==0
    assert 'financial_sales' not in normalize_ozon_product_finance(payload,DAY.isoformat())[0].metrics
    assert all(x.gross_amount==0 for x in normalize_ozon_finance_events(payload,data_date=DAY.isoformat()))


@pytest.mark.parametrize('payload',[{}, {'accruals':None},{'accruals':{}},{'accruals':[None]}])
def test_malformed_ozon_finance_is_not_a_zero_sales_day(payload):
    with pytest.raises(FinanceNormalizationError): normalize_ozon_accruals(payload,1,DAY.isoformat())
    with pytest.raises(FinanceNormalizationError): normalize_ozon_product_finance(payload,DAY.isoformat())


@pytest.mark.parametrize('currency',['USD','EUR'])
def test_foreign_currency_is_not_labelled_as_rubles(currency):
    with pytest.raises(FinanceNormalizationError):
        normalize_ozon_accruals({'accruals':[{'total_amount':{'amount':1,'currency':currency}}]},1,DAY.isoformat())
    with pytest.raises(FinanceNormalizationError):
        normalize_wb_finance_report({'dateTo':DAY.isoformat(),'retailAmountSum':1,'currency':currency},1)


@pytest.mark.asyncio
async def test_daily_wb_report_types_are_combined_without_page_duplicates(tmp_path):
    _,repo,_,shop,conn=make_repo(tmp_path,'wildberries')
    class WB:
        async def finance_sales_reports_list_all(self,*args,**kwargs):
            a={'reportId':1,'dateTo':DAY.isoformat(),'retailAmountSum':100,'forPaySum':80}
            b={'reportId':2,'dateTo':DAY.isoformat(),'retailAmountSum':200,'forPaySum':150}
            return FetchResult.success('wildberries',[a,b,a],200,1)
    service=CollectionService(repo,wildberries=WB())
    outcomes=await service.collect_finance(start=DAY,end=DAY,wb_connection_id=conn.id)
    totals=repo.financial_metric_totals(shop.id,DAY.isoformat(),DAY.isoformat())['wildberries']
    assert totals['financial_sales']==300 and totals['goods_payable']==230
    assert len([x for x in outcomes if x.ok])==1


def test_management_warns_about_unknown_expenses_and_missing_marketplace(tmp_path):
    _,repo,_,shop,conn=make_repo(tmp_path)
    repo.ensure_connection(shop.id,'wildberries','WB')
    repo.record_success(conn.id,'analytics/orders',DAY.isoformat(),{'units':0},[
        MetricPoint(conn.id,DAY.isoformat(),'ordered_units',0,'units'),
        MetricPoint(conn.id,DAY.isoformat(),'ordered_revenue',0,'RUB')])
    report=build_management_report(repo,shop.id,DAY,1); text=format_management(report)
    assert report.missing_sources==('wildberries',)
    assert report.sources[0].warnings
    assert 'неизвестен' in text and 'Нет данных: Wildberries' in text
    assert 'Суммарная оценка: —' in text


def test_management_does_not_add_incompatible_money_bases():
    row=ManagementSource('ozon',100,10,100,10,5,0,75,None,None,None,None)
    text=format_management(ManagementReport(DAY.isoformat(),DAY.isoformat(),1,(row,replace(row,marketplace='wildberries'))))
    assert 'денежные базы WB/Ozon не унифицированы' in text
    assert '150' not in text


def test_forecast_calibration_filters_before_limit_and_preserves_zero_actual_error(tmp_path):
    _,repo,_,shop,_=make_repo(tmp_path); product=repo.ensure_product(shop.id,'P','P')
    def save(offset,predicted,actual,method=QUALITY_METHOD_VERSION,horizon=7):
        repo.save_forecast_quality(shop.id,[{'product_id':product.id,
            'as_of_date':(DAY-timedelta(days=offset)).isoformat(),'horizon_days':horizon,
            'predicted_units':predicted,'actual_units':actual}],method_version=method)
    for offset,pred,actual in [(40,10,0),(30,10,10),(20,10,10)]: save(offset,pred,actual)
    for offset in range(12): save(offset,0,100,'old-model')
    save(1,0,100); save(15,0,100,horizon=14)
    samples=repo.forecast_quality_samples_by_product(shop.id,limit_per_product=3,
        as_of_date=DAY.isoformat(),method_version=QUALITY_METHOD_VERSION,horizon_days=7)
    assert len(samples[product.id])==3
    correction=repo.forecast_bias_corrections(shop.id,limit_per_product=3,
        as_of_date=DAY.isoformat(),method_version=QUALITY_METHOD_VERSION,horizon_days=7)
    assert correction[product.id]==0.85


@pytest.mark.parametrize('status',[403,500])
@pytest.mark.asyncio
async def test_http_errors_redact_credentials_before_truncation(status):
    secret='SECRET_TOKEN_LONG_123456789'
    def handler(request): return httpx.Response(status,text='x'*285+secret+' '+secret)
    client=MarketplaceClient('test','https://example.test',transport=httpx.MockTransport(handler),min_interval=0,max_retries=0)
    try:
        result=await client.request('GET','/',headers={'Authorization':'Bearer '+secret})
        assert secret not in result.error and secret[:15] not in result.error
        assert '[REDACTED]' in result.error
    finally: await client.close()


@pytest.mark.asyncio
async def test_http_errors_redact_oauth_body_secret():
    secret='OAUTH_PRIVATE_SECRET'
    client=MarketplaceClient('test','https://example.test',min_interval=0,max_retries=0,
        transport=httpx.MockTransport(lambda request:httpx.Response(403,text=secret)))
    try:
        result=await client.request('POST','/',json={'client_secret':secret})
        assert secret not in result.error
    finally: await client.close()


@pytest.mark.parametrize('value',['nan','inf','-inf'])
def test_nonfinite_retry_header_is_ignored(value):
    response=httpx.Response(429,headers={'X-Ratelimit-Retry':value,'Retry-After':'7'})
    assert _server_retry_delay(response)==7


def test_configuration_repr_hides_credentials(monkeypatch):
    for key in ('TELEGRAM_BOT_TOKEN','WB_API_TOKEN','OZON_API_KEY','OZON_PERF_CLIENT_SECRET'):
        monkeypatch.setenv(key,'PRIVATE_'+key)
    settings=Settings.from_env()
    assert 'PRIVATE_' not in repr(settings)
    assert 'PRIVATE_' not in repr(settings.credentials_for_profile())


def test_live_audit_missing_db_does_not_create_one(tmp_path):
    path=tmp_path/'missing.sqlite3'; report,_=inspect_database(path,1)
    assert not report['ok'] and not path.exists()


def mock_host(request):
    path=request.url.path
    if path.endswith('/getMe'): return httpx.Response(200,json={'ok':True,'result':{'is_bot':True,'username':'PRIVATE'}})
    if path.endswith('/seller-info'): return httpx.Response(200,json={'name':'PRIVATE SELLER'})
    if path=='/v1/seller/info': return httpx.Response(200,json={'company':{'name':'PRIVATE SELLER'}})
    if path=='/v1/roles': return httpx.Response(200,json={'roles':[]})
    if path=='/v1/analytics/data': return httpx.Response(200,json={'result':{'data':[],'totals':[0,0]}})
    if path=='/v4/product/info/stocks': return httpx.Response(200,json={'items':[],'cursor':''})
    if path=='/v1/finance/accrual/by-day': return httpx.Response(200,json={'accruals':[],'last_id':''})
    if path.endswith('/orders') or 'sales-reports' in path: return httpx.Response(200,json=[])
    raise AssertionError('Unexpected probe')


@pytest.mark.asyncio
async def test_live_audit_no_db_writes_secrets_or_unrequested_stocks(tmp_path):
    db,repo,_,shop,_=make_repo(tmp_path)
    repo.ensure_connection(shop.id,'wildberries','WB')
    # Compare a settled database file, not a deferred WAL checkpoint triggered
    # when Python collects an earlier writable connection during the audit.
    checkpoint=db.connect()
    try:checkpoint.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    finally:checkpoint.close()
    before=db.path.read_bytes()
    settings=replace(Settings.from_env(),db_file=db.path,telegram_token='PRIVATE_TOKEN',owner_ids=(1,),
        wb_api_token='PRIVATE_WB',ozon_client_id='PRIVATE_CLIENT',ozon_api_key='PRIVATE_OZON',
        wb_min_interval=0,ozon_min_interval=0)
    calls=[]
    def handler(request): calls.append(request.url.path); return mock_host(request)
    report=await run_live_audit(settings,shop_id=shop.id,day=DAY,transport=httpx.MockTransport(handler))
    assert report['ok'] and report['optional_status']=='checked',report
    assert 'PRIVATE' not in json.dumps(report)
    assert db.path.read_bytes()==before
    assert not any('stocks-report' in x for x in calls)
    assert all(not x.endswith('/sendMessage') and not x.endswith('/getUpdates') for x in calls)


@pytest.mark.asyncio
async def test_live_audit_429_fails_fast_and_stops_remaining_marketplace(tmp_path):
    db,_,_,shop,_=make_repo(tmp_path,'wildberries')
    settings=replace(Settings.from_env(),db_file=db.path,telegram_token='TOKEN',owner_ids=(1,),
        wb_api_token='WB',ozon_client_id='',ozon_api_key='',wb_min_interval=0)
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/seller-info'): return httpx.Response(429,headers={'X-Ratelimit-Retry':'40000'})
        return mock_host(request)
    report=await asyncio.wait_for(run_live_audit(settings,shop_id=shop.id,day=DAY,
        transport=httpx.MockTransport(handler)),timeout=1)
    assert not report['ok'] and len(calls)==2
    probe=next(x for x in report['probes'] if x['key']=='wb_auth')
    assert probe['retry_after_seconds']>=39999
    assert all(x['status']=='skipped' for x in report['probes'][2:])


@pytest.mark.asyncio
async def test_live_audit_archived_shop_never_falls_back_to_duplicate(tmp_path):
    db,repo,seller,shop,_=make_repo(tmp_path)
    repo.ensure_shop(seller.id,'Основной магазин')
    repo.grant_shop_access(seller.telegram_user_id,shop.id,'owner')
    repo.archive_shop(seller.telegram_user_id,shop.id)
    calls=[]
    settings=replace(Settings.from_env(),db_file=db.path)
    report=await run_live_audit(settings,shop_id=shop.id,day=DAY,
        transport=httpx.MockTransport(lambda request:calls.append(request)))
    assert not report['ok'] and not calls


def test_ozon_orders_money_is_used_for_aggregate_advertising():
    metrics=amounts(normalize_ozon_ad_stats({'rows':[{'date':DAY.isoformat(),'expense':5,
        'ordersMoney':25,'sales':999}]},1)[DAY.isoformat()])
    assert metrics=={'ad_spend':5,'ad_attributed_sales':25}


@pytest.mark.asyncio
async def test_performance_token_readiness_does_not_sleep_on_429():
    client=OzonPerformanceClient('id','secret',transport=httpx.MockTransport(
        lambda request:httpx.Response(429,headers={'X-Ratelimit-Retry':'40000'})))
    try:
        result=await asyncio.wait_for(client._token(retry_on_429=False,fail_fast_rate_limit=True),timeout=1)
        assert not result.ok and result.status_code==429
    finally: await client.close()


def test_wb_corrected_finance_line_updates_existing_event(tmp_path):
    db,repo,_,shop,conn=make_repo(tmp_path,'wildberries')
    p=repo.ensure_product(shop.id,'P','P'); listing=repo.ensure_listing(p.id,conn.id,'123')
    for i,value in enumerate([100,120]):
        run=repo.record_success(conn.id,'finance/sales-reports/detailed',DAY.isoformat(),{'v':value},[])
        repo.save_commerce_events(run,[CommerceEventPoint(listing.id,DAY.isoformat(),'finance','wb_finance',
            'stable-rrd-id',quantity=1,gross_amount=value,net_amount=value-10)])
    with db.connect() as c:
        rows=c.execute('SELECT gross_amount,net_amount FROM commerce_events').fetchall()
    assert len(rows)==1 and tuple(rows[0])==(120,110)


def test_xyz_uses_real_full_calendar_weeks():
    monday=date(2026,8,3)
    days=[(monday+timedelta(days=i)).isoformat() for i in range(35) if i%7!=2]
    assert len(days)==30
    assert _xyz([1]*len(days),8,days)==('?',None)
    full=[(monday+timedelta(days=i)).isoformat() for i in range(28)]
    assert _xyz([1]*28,8,full)==('X',0.0)


def test_growth_from_zero_is_not_a_fake_percentage():
    assert _trend([0]*14+[1]*14) is None
    assert _trend([0]*28)==0
