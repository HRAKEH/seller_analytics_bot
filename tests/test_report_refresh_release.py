from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import csv
import pytest

from app.config import Settings
from app.storage import Database, Repository, MetricPoint, ProductMetricPoint
from app.storage.models import AdProductPoint
from app.services.collection import CollectionService
from app.services.finance import normalize_ozon_accruals, normalize_ozon_product_finance
from app.services.money import money_sum
from app.services.report_period import parse_report_period
from app.services.report_refresh import refresh_reports
from app.reports.coverage import build_source_coverage, format_source_coverage
from app.reports.accruals import build_accrual_ledger, export_accrual_ledger
from app.reports.sku_finance import build_sku_economics
from app.services.scheduler import automatic_backup_once, collect_and_send_daily


def database(tmp_path):
    db=Database(tmp_path/'release.sqlite3');db.initialize();repo=Repository(db)
    shop=repo.ensure_shop(repo.ensure_seller(1).id)
    conn=repo.ensure_connection(shop.id,'ozon','Ozon')
    return repo,shop,conn


def sku_basis(repo,shop,conn):
    product=repo.ensure_product(shop.id,'P1','Product',cost_price=100)
    listing=repo.ensure_listing(product.id,conn.id,'9001')
    run=repo.record_success(conn.id,'analytics/orders','2026-09-28',{'orders':5},[
        MetricPoint(conn.id,'2026-09-28','ordered_units',5,'units'),
        MetricPoint(conn.id,'2026-09-28','ordered_revenue',1000,'RUB')])
    repo.save_product_metrics(run,[ProductMetricPoint(listing.id,'2026-09-28','ordered_units',5,'units'),
        ProductMetricPoint(listing.id,'2026-09-28','ordered_revenue',1000,'RUB')])
    return listing


def finance(repo,shop,conn,amount=200,legacy=False):
    payload={'accruals':[{'total_amount':800,'accrued_category':'POSTING',
        'posting':{'products':[{'sku':9001,'commission':{'seller_price':1000}}]},
        'item_fees':{'fees':[{'sku':9001,'fees':[{'type_id':54,'accrued':-amount}]}]}}]}
    points=normalize_ozon_accruals(payload,conn.id,'2026-09-28')
    if legacy:points=[p for p in points if p.metric_key!='finance_ad_spend']
    run=repo.record_success(conn.id,'finance/accrual/by-day','2026-09-28',payload,points)
    CollectionService(repo)._save_product_finance_observations(shop_id=shop.id,connection_id=conn.id,
        marketplace='ozon',source_run_id=run,observations=normalize_ozon_product_finance(payload,'2026-09-28'),replace_finance_snapshot=True)
    return run


def advertising(repo,conn,listing,day,spend,revision=1):
    run=repo.record_success(conn.id,'performance/products-sku',day,{'spend':spend,'revision':revision},[])
    repo.save_ad_details(run,products=[AdProductPoint(conn.id,day,'9001','c1','Campaign',listing.id,spend,1000,1,5,10)])


@pytest.mark.parametrize('legacy',[False,True])
def test_sku_performance_is_not_deducted_twice_even_for_legacy_finance(tmp_path,legacy):
    repo,shop,conn=database(tmp_path);listing=sku_basis(repo,shop,conn)
    finance(repo,shop,conn,legacy=legacy);advertising(repo,conn,listing,'2026-09-28',150)
    row=build_sku_economics(repo,shop.id,date(2026,9,28),1).rows[0]
    assert row.contribution_after_known_expenses==300
    assert row.performance_expense==0 and row.performance_reference==150
    assert row.financial_metrics['finance_ad_spend']==200


def test_sku_mixed_days_use_only_unbilled_latest_performance_and_isolate_shops(tmp_path):
    repo,shop,conn=database(tmp_path);listing=sku_basis(repo,shop,conn)
    finance(repo,shop,conn);advertising(repo,conn,listing,'2026-09-28',150)
    advertising(repo,conn,listing,'2026-09-29',100)
    advertising(repo,conn,listing,'2026-09-29',70,2)
    other=repo.ensure_shop(shop.seller_id,'Other');other_conn=repo.ensure_connection(other.id,'ozon','Other')
    other_listing=sku_basis(repo,other,other_conn)
    advertising(repo,other_conn,other_listing,'2026-09-29',999)
    row=build_sku_economics(repo,shop.id,date(2026,9,29),2).rows[0]
    assert row.performance_expense==70 and row.performance_reference==150
    assert row.contribution_after_known_expenses==230
    assert any('предварительная' in w for w in row.warnings)


def test_changed_finance_snapshot_replaces_old_billed_ads(tmp_path):
    repo,shop,conn=database(tmp_path);sku_basis(repo,shop,conn)
    finance(repo,shop,conn);finance(repo,shop,conn,amount=0)
    metrics=repo.sku_financial_totals(shop.id,'2026-09-28','2026-09-28')[('ozon','9001')]
    assert metrics['services']==0 and metrics['finance_ad_spend']==0


def test_money_rounds_at_aggregate_boundary_and_preserves_signed_refunds():
    assert money_sum(['0.1','0.2'])==0.3
    assert money_sum(['0.004','0.004'])==0.01
    payload={'accruals':[{'total_amount':'0.01','item_fees':[{'accrued':'-0.004'},{'accrued':'-0.004'}]},
        {'total_amount':'0.1','non_item_fee':{'type_id':41,'accrued':{'amount':'0.10','currency':'RUB'}}}]}
    metrics={p.metric_key:p.value for p in normalize_ozon_accruals(payload,1,'2026-09-28')}
    assert metrics['services']==-0.09 and metrics['finance_ad_spend']==-0.1
    assert metrics['marketplace_net']==0.11


def test_ledger_matches_finance_keeps_latest_revision_and_does_not_drop_unknown_fees(tmp_path):
    repo,shop,conn=database(tmp_path)
    old={'accruals':[{'total_amount':1000}]}
    repo.record_success(conn.id,'finance/accrual/by-day','2026-09-28',old,normalize_ozon_accruals(old,conn.id,'2026-09-28'))
    payload={'accruals':[{'id':'=BAD','total_amount':-20.25,'non_item_fee':{'type_id':999,'accrued':-20.25}},
        {'id':'return','total_amount':5.25,'non_item_fee':{'type_id':41,'accrued':5.25}}]}
    repo.record_success(conn.id,'finance/accrual/by-day','2026-09-28',payload,normalize_ozon_accruals(payload,conn.id,'2026-09-28'))
    repo.record_failure(conn.id,'finance/accrual/by-day','2026-09-28','Late retry failed')
    ledger=build_accrual_ledger(repo,shop.id,'2026-09-28','2026-09-28')
    assert len(ledger.rows)==2 and money_sum(r['marketplace_net'] for r in ledger.rows)==-15
    assert ledger.rows[0]['fee_type_ids']=='999'
    assert money_sum(r['services'] for r in ledger.rows)==15
    path=export_accrual_ledger(ledger,tmp_path/'ledger.csv')
    with path.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f,delimiter=';'))
    assert rows[0]['operation_id']=="'=BAD"


def test_source_coverage_distinguishes_absent_zero_and_failed_retry(tmp_path):
    repo,shop,conn=database(tmp_path)
    payload={'accruals':[]}
    repo.record_success(conn.id,'finance/accrual/by-day','2026-09-28',payload,normalize_ozon_accruals(payload,conn.id,'2026-09-28'))
    repo.record_failure(conn.id,'finance/accrual/by-day','2026-09-28','Retry failed')
    rows=build_source_coverage(repo,shop.id,'2026-09-28','2026-09-29')
    fin=next(r for r in rows if r.component=='Финансы')
    assert fin.available_days==1 and fin.missing_dates==('2026-09-29',)
    assert fin.failed_dates==('2026-09-28',)
    assert 'сохранён предыдущий успешный ответ' in format_source_coverage(rows)


@pytest.mark.parametrize('text',['/refresh 0','/finance 32','/ads 1 2026-10-02','/reconcile 1 bad','/management 1 2026-09-28 extra'])
def test_period_rejects_invalid_and_future_ranges(text):
    with pytest.raises(ValueError):parse_report_period(text,14,date(2026,10,1))


def test_all_report_commands_anchor_the_same_period():
    for name in ('refresh','finance','ads','management','sku_finance','reconcile','sources','accruals'):
        assert parse_report_period(f'/{name} 7 2026-09-28',14,date(2026,10,1))==(7,date(2026,9,22),date(2026,9,28))


@pytest.mark.asyncio
async def test_refresh_continues_independent_sources_and_progress_failure():
    calls=[]
    async def call(name,**kwargs):
        calls.append(name)
        if name=='wb':raise RuntimeError('WB unavailable')
        return SimpleNamespace(ok=True) if name=='ozon' else [SimpleNamespace(ok=True)]
    @asynccontextmanager
    async def lock(name):yield
    collector=SimpleNamespace(
        collect_wb_orders_day=lambda *a,**k:call('wb'),collect_ozon_orders_day=lambda *a,**k:call('ozon'),
        collect_finance=lambda **k:call('finance'),collect_advertising=lambda **k:call('ads'),
        collect_ozon_fulfillment_range=lambda **k:call('fulfillment'),collect_wb_sales_range=lambda **k:call('sales'))
    ctx=SimpleNamespace(demo_mode=lambda:False,operation_lock=lock,shop_id=1,collector=collector,wb_connection_id=2,ozon_connection_id=3)
    stages=await refresh_reports(ctx,date(2026,9,28),date(2026,9,28),progress=AsyncMock(side_effect=RuntimeError('Telegram unavailable')))
    assert calls==['wb','ozon','finance','finance','ads','ads','fulfillment','sales']
    assert stages[0].ok is False and all(s.ok for s in stages[1:])


@pytest.mark.asyncio
async def test_daily_publishes_after_delayed_data_attempts_even_if_orders_raise(tmp_path,monkeypatch):
    import app.services.scheduler as scheduler
    repo,shop,_=database(tmp_path);repo.grant_shop_access(1,shop.id,'owner');events=[]
    async def order(day):events.append('orders');raise RuntimeError('API failure')
    async def finance(*args):events.append('finance');return [SimpleNamespace(ok=True)]
    async def rec(*args,**kwargs):events.append('reconcile');return [SimpleNamespace(ok=True)]
    async def ads(*args):events.append('ads');return [SimpleNamespace(ok=True)]
    async def send(*args,**kwargs):
        events.append('send')
        return SimpleNamespace(message_id=42)
    ctx=SimpleNamespace(preferences=lambda:SimpleNamespace(timezone='Europe/Moscow',finance_lookback_days=7),
        collect_day=order,collect_finance=finance,collect_reconciliation=rec,collect_advertising=ads,repository=repo,shop_id=shop.id,
        settings=Settings.from_env(),collect_promotions=AsyncMock(return_value=[]),collect_inbound=AsyncMock(return_value=[]))
    for name in ('evaluate_forecast_quality','build_supply_plan','build_action_center'):monkeypatch.setattr(scheduler,name,lambda *a,**k:None)
    await collect_and_send_daily(SimpleNamespace(id=123,send_message=send),ctx)
    assert events==['orders','finance','reconcile','ads','send']
    assert repo.report_card(123,1,42) is not None


@pytest.mark.asyncio
async def test_backup_delivery_retries_without_creating_duplicate_database_copy(tmp_path):
    repo,shop,_=database(tmp_path)
    settings=replace(Settings.from_env(),auto_backup_enabled=True,auto_backup_hour_utc=0,
        auto_backup_send_telegram=True,owner_ids=(101,),instance_id='test-backup')
    registry=SimpleNamespace(repository=repo,settings=settings,default_shop_id=shop.id)
    bot=SimpleNamespace(send_document=AsyncMock(side_effect=[RuntimeError('Network'),None]),send_message=AsyncMock())
    now=datetime.now(timezone.utc)
    await automatic_backup_once(registry,bot,now)
    assert len(repo.recent_backups())==1 and bot.send_document.await_count==1
    await automatic_backup_once(registry,bot,now)
    await automatic_backup_once(registry,bot,now)
    assert len(repo.recent_backups())==1 and bot.send_document.await_count==2
    assert bot.send_document.await_args.args[0]==101


@pytest.mark.asyncio
async def test_backup_telegram_delivery_is_opt_in(tmp_path):
    repo,shop,_=database(tmp_path)
    settings=replace(Settings.from_env(),auto_backup_enabled=True,auto_backup_hour_utc=0,auto_backup_send_telegram=False)
    registry=SimpleNamespace(repository=repo,settings=settings,default_shop_id=shop.id)
    bot=SimpleNamespace(send_document=AsyncMock(),send_message=AsyncMock())
    await automatic_backup_once(registry,bot,datetime.now(timezone.utc))
    assert len(repo.recent_backups())==1
    bot.send_document.assert_not_awaited()


def test_year_export_still_works_with_source_coverage(tmp_path):
    from app.services.exporting import export_csv_zip
    import zipfile
    repo,shop,_=database(tmp_path)
    result=export_csv_zip(repo,shop.id,date(2026,9,28),365,tmp_path/'year.zip')
    with zipfile.ZipFile(result.path) as archive:
        assert 'finance.csv' in archive.namelist()


@pytest.mark.asyncio
async def test_partial_finance_retry_is_not_marked_as_completed(tmp_path):
    from app.services.resilience import execute_retry_job
    repo,shop,_=database(tmp_path)
    ctx=SimpleNamespace(shop_id=shop.id,collect_finance=AsyncMock(return_value=[SimpleNamespace(ok=False)]))
    registry=SimpleNamespace(contexts=lambda:[ctx],get=lambda shop_id:ctx)
    with pytest.raises(RuntimeError,match='finance retry remains partial'):
        await execute_retry_job(None,registry,{'shop_id':shop.id,'job_type':'finance','payload':{'start':'2026-09-28','end':'2026-09-28'}})


@pytest.mark.asyncio
async def test_actual_telegram_refresh_menu_routes_date_and_rejects_viewer(tmp_path):
    from aiogram import Bot, Dispatcher, types
    from aiogram.methods import SendMessage, DeleteMessage
    from app.bot import AppContext, register_handlers
    from app.bot.keyboards import COMMAND_BUTTONS
    from app.services.report_refresh import RefreshStage
    repo,shop,_=database(tmp_path);repo.grant_shop_access(101,shop.id,'owner');repo.grant_shop_access(102,shop.id,'viewer')
    repo.ensure_shop_preferences(shop.id)
    settings=replace(Settings.from_env(),telegram_token='123456:LOCAL_TEST',owner_ids=(101,))
    ctx=AppContext(settings,repo,shop.id,CollectionService(repo))
    ctx.refresh_reports=AsyncMock(return_value=(RefreshStage('finance:Ozon','Начисления Ozon',True),))
    bot=Bot(settings.telegram_token);calls=[]
    async def record(bot,method,**kwargs):
        if isinstance(method,DeleteMessage): return True
        assert isinstance(method,SendMessage)
        calls.append(method)
        return types.Message(message_id=len(calls)+100,date=datetime.now(timezone.utc),chat=types.Chat(id=method.chat_id,type='private'),text=method.text)
    bot.session.make_request=AsyncMock(side_effect=record)
    dp=Dispatcher();register_handlers(dp,ctx)
    async def send(uid,text,number):
        msg=types.Message(message_id=number,date=datetime.now(timezone.utc),chat=types.Chat(id=uid,type='private'),
            from_user=types.User(id=uid,is_bot=False,first_name='Test'),text=text)
        await dp.feed_update(bot,types.Update(update_id=number,message=msg))
    try:
        await send(102,COMMAND_BUTTONS['refresh'],1)
        assert 'Недостаточно прав' in calls[-1].text
        ctx.refresh_reports.assert_not_awaited()
        await send(101,COMMAND_BUTTONS['refresh'],2)
        await send(101,'1 2026-09-28',3)
        assert ctx.refresh_reports.await_args.args==(date(2026,9,28),date(2026,9,28))
        assert any('2026-09-28 — 2026-09-28' in getattr(call,'text','') for call in calls)
    finally:await bot.session.close()


@pytest.mark.asyncio
async def test_complete_empty_ad_refresh_clears_old_spend_and_marks_day_loaded(tmp_path):
    from app.integrations.base import FetchResult
    repo,shop,conn=database(tmp_path);listing=sku_basis(repo,shop,conn)
    advertising(repo,conn,listing,'2026-09-28',150)
    perf=SimpleNamespace(product_campaign_stats=AsyncMock(return_value=FetchResult.success('ozon',{'rows':[]},200,1)),
        product_sku_stats=AsyncMock(return_value=FetchResult.success('ozon',{'rows':[]},200,1)))
    collector=CollectionService(repo,ozon_performance=perf)
    outcomes=await collector.collect_advertising(start=date(2026,9,28),end=date(2026,9,28),ozon_connection_id=conn.id)
    assert all(x.ok for x in outcomes)
    assert repo.latest_metric(conn.id,'2026-09-28','ad_spend')['value']==0
    assert repo.ad_product_totals_map(shop.id,'2026-09-28','2026-09-28')[('ozon','9001')]['ad_spend']==0
    coverage=build_source_coverage(repo,shop.id,'2026-09-28','2026-09-28')
    assert next(x for x in coverage if x.component=='Рекламная статистика').available_days==1


@pytest.mark.asyncio
@pytest.mark.parametrize('payload',[{'unexpected':[]},{'rows':[None]}])
async def test_unknown_ad_payload_is_failed_and_preserves_existing_spend(tmp_path,payload):
    from app.integrations.base import FetchResult
    repo,shop,conn=database(tmp_path);listing=sku_basis(repo,shop,conn)
    advertising(repo,conn,listing,'2026-09-28',150)
    perf=SimpleNamespace(product_campaign_stats=AsyncMock(return_value=FetchResult.success('ozon',payload,200,1)),
        product_sku_stats=AsyncMock(return_value=FetchResult.success('ozon',payload,200,1)))
    outcomes=await CollectionService(repo,ozon_performance=perf).collect_advertising(
        start=date(2026,9,28),end=date(2026,9,28),ozon_connection_id=conn.id)
    assert all(not x.ok for x in outcomes)
    assert repo.ad_product_totals_map(shop.id,'2026-09-28','2026-09-28')[('ozon','9001')]['ad_spend']==150


@pytest.mark.asyncio
async def test_wb_empty_full_ad_response_replaces_old_rows_for_all_requested_days(tmp_path):
    from app.integrations.base import FetchResult
    repo,shop,_=database(tmp_path);conn=repo.ensure_connection(shop.id,'wildberries','WB')
    run=repo.record_success(conn.id,'promotion/fullstats','2026-09-28',{'spend':150},[])
    repo.save_ad_details(run,products=[AdProductPoint(conn.id,'2026-09-28','123','c1','Campaign',None,150)])
    wb=SimpleNamespace(promotion_fullstats_all=AsyncMock(return_value=FetchResult.success('wildberries',[],200,1)))
    result=await CollectionService(repo,wildberries=wb).collect_advertising(start=date(2026,9,28),end=date(2026,9,29),wb_connection_id=conn.id)
    assert len(result)==2 and all(x.ok for x in result)
    assert repo.ad_product_totals_map(shop.id,'2026-09-28','2026-09-29')[('wildberries','123')]['ad_spend']==0
    assert repo.latest_metric(conn.id,'2026-09-29','ad_spend')['value']==0


def test_wb_auxiliary_statistics_does_not_hide_failed_funnel_refresh(tmp_path):
    repo,shop,_=database(tmp_path);conn=repo.ensure_connection(shop.id,'wildberries','WB');day='2026-09-28'
    repo.record_success(conn.id,'analytics/orders',day,{'funnel':1},[MetricPoint(conn.id,day,'ordered_units',10,'units')])
    repo.record_failure(conn.id,'analytics/orders',day,'Analytics permission missing')
    repo.record_success(conn.id,'statistics/orders',day,{'statistics':1},[])
    rows=build_source_coverage(repo,shop.id,day,day)
    wb=next(r for r in rows if r.marketplace=='wildberries' and r.component=='Заказы')
    assert wb.available_days==1 and wb.failed_dates==(day,)
