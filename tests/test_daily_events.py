"""Event dates, source completeness and the unified daily Telegram card."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import SendMessage, EditMessageText

from app.integrations import OzonClient, FetchResult
from app.services.collection import CollectionService
from app.services.daily_events import (
    WB_SALES, WB_CANCELS, OZON_RETURNS, OZON_REALIZATION, DailyEventError,
    build_daily_events, normalize_wb_sales_day, normalize_wb_cancellations_day,
    normalize_ozon_returns_day, normalize_ozon_realization_day, split_event_rows,
)
from app.services.ozon_dates import MOSCOW
from app.reports.daily import build_daily_report
from app.reports.cards import format_daily_card
from app.reports.coverage import build_source_coverage
from app.services.report_refresh import RefreshStage, refresh_reports
from app.storage import Database, Repository, MetricPoint
from test_navigation import ui
from test_report_cards import orders, card, tap, stored, labels, DAY


def success(source, payload):
    return FetchResult.success(source,payload,200,1)


def setup(tmp_path, marketplace='wildberries'):
    db=Database(tmp_path/'events.sqlite3');db.initialize();repo=Repository(db)
    shop=repo.ensure_shop(repo.ensure_seller(101).id)
    repo.ensure_shop_preferences(shop.id)
    conn=repo.ensure_connection(shop.id,marketplace,marketplace)
    return repo,shop,conn


def sale(identifier='S1', day='2026-10-01T11:00:00', price=100, **fields):
    return {'saleID':identifier,'date':day,'finishedPrice':price,'priceWithDisc':750,
            'lastChangeDate':'2026-10-02T01:00:00','nmId':1,'srid':'order',**fields}


def customer_return(identifier=1, kind='ClientReturn', stamp='2026-09-30T21:00:00Z', quantity=2, **fields):
    return {'id':identifier,'type':kind,'schema':'Fbo','posting_number':'old-order-1',
            'logistic':{'return_date':stamp,'final_moment':'2026-10-04T21:00:00Z'},
            'product':{'sku':9001,'quantity':quantity,'price':{'price':'100.25','currency_code':'RUB'}},**fields}


def realization(currency=None, quantity=2):
    body={'rows':[{'item':{'sku':9001},
        'delivery_commission':{'quantity':quantity,'price_per_instance':100.25,'amount':100.25*quantity},
        'return_commission':{'quantity':1,'price_per_instance':50,'amount':50}}]}
    if currency:body['currency']=currency
    return body


def test_wb_sales_use_operation_day_buyer_price_and_positive_return_amount():
    buyouts,returns=normalize_wb_sales_day([
        sale(),sale(),sale('R1',price=-40),sale('S2',day='2026-10-02T00:00:00'),
        sale('S3',day='2026-09-30T22:15:00Z',price=20)],DAY.isoformat())
    assert (buyouts.units,buyouts.amount)==(2,Decimal('120.00'))
    assert (returns.units,returns.amount)==(1,Decimal('40.00'))


@pytest.mark.parametrize('price',[None,0,True,'NaN',-20])
def test_wb_missing_or_async_prices_do_not_use_seller_discount_or_zero(price):
    buyouts,_=normalize_wb_sales_day([sale(price=price)],DAY.isoformat())
    assert buyouts.units==1 and buyouts.amount is None


def test_wb_corrected_version_replaces_old_event_without_counting_twice():
    old=sale(price=100,lastChangeDate='2026-10-01T23:00:00')
    new=sale(price=90,lastChangeDate='2026-10-02T02:00:00')
    assert normalize_wb_sales_day([old,new,old],DAY.isoformat())[0].amount==Decimal('90.00')
    with pytest.raises(DailyEventError):normalize_wb_sales_day([old,{**old,'finishedPrice':91}],DAY.isoformat())


def test_wb_cancel_date_is_independent_of_order_and_last_change_dates():
    payload={'orders':[
        {'srid':'old','date':'2026-09-28','isCancel':True,'cancelDate':'2026-10-01','lastChangeDate':'2026-10-04'},
        {'srid':'today','date':'2026-10-01','isCancel':True,'cancelDate':'2026-10-03','lastChangeDate':'2026-10-04'},
        {'srid':'open','date':'2026-10-01','isCancel':False}]}
    assert normalize_wb_cancellations_day(payload,DAY.isoformat()).units==1
    payload['orders'].append({'srid':'missing','date':'2026-10-01','isCancel':True,'cancelDate':'0001-01-01','lastChangeDate':'2026-10-04'})
    reading=normalize_wb_cancellations_day(payload,DAY.isoformat())
    assert reading.units is None and reading.available_units==1


def test_ozon_customer_returns_use_event_day_quantity_and_exclude_refusals():
    body={'returns':[customer_return(),customer_return(),
        customer_return(2,stamp='2026-10-01T21:00:00Z'),
        customer_return(3,kind='FullReturn'),customer_return(4,kind='PartialReturn'),
        customer_return(5,kind='Cancellation'),customer_return(6,kind='Unknown')]}
    reading=normalize_ozon_returns_day(body,DAY.isoformat())
    assert (reading.units,reading.amount)==(2,Decimal('200.50'))
    assert 'после вручения' in reading.source


@pytest.mark.parametrize('change',[
    {'logistic':{'final_moment':'2026-10-01T10:00:00Z'}},
    {'product':{'sku':9001,'quantity':None}}, {'type':'new-undocumented-type'},
])
def test_ozon_missing_event_date_quantity_or_unknown_type_never_become_zero(change):
    reading=normalize_ozon_returns_day({'returns':[customer_return(**change)]},DAY.isoformat())
    assert reading.units is None and reading.amount is None


def test_foreign_return_price_preserves_units_without_false_rubles():
    reading=normalize_ozon_returns_day({'returns':[customer_return(product={
        'sku':9001,'quantity':3,'price':{'price':'14.98','currency_code':'BYN'}})]},DAY.isoformat())
    assert reading.units==3 and reading.amount is None


def test_daily_realization_does_not_infer_rubles_or_net_from_unknown_currency():
    buyouts,returns=normalize_ozon_realization_day(realization())
    assert buyouts.units==2 and buyouts.amount is None
    assert returns.units==1 and returns.amount is None
    buyouts,returns=normalize_ozon_realization_day(realization('RUB'))
    assert buyouts.amount==Decimal('200.50') and returns.amount==Decimal('50.00')
    assert normalize_ozon_realization_day({'rows':[]})[0].units==0
    body=realization('RUB');body['rows'][0]['delivery_commission']['amount']=750
    assert normalize_ozon_realization_day(body)[0].amount is None


def test_realization_different_numbered_rows_for_same_sku_are_counted_once_each():
    body=realization('RUB');body['rows'][0]['rowNumber']=1
    second={**body['rows'][0],'rowNumber':2}
    body['rows'].append(second)
    assert normalize_ozon_realization_day(body)[0].units==4
    body['rows'].append(second)
    with pytest.raises(DailyEventError):normalize_ozon_realization_day(body)


@pytest.mark.parametrize('identifier',[None,0,'0',{},True])
def test_invalid_ozon_return_identifiers_do_not_confirm_a_quantity(identifier):
    with pytest.raises(DailyEventError):
        normalize_ozon_returns_day({'returns':[customer_return(identifier)]},DAY.isoformat())


def test_daily_event_builder_latest_snapshot_and_empty_correction_not_history_sum(tmp_path):
    repo,shop,conn=setup(tmp_path)
    assert build_daily_events(repo,conn.id,'wildberries',DAY).buyouts.units is None
    repo.record_success(conn.id,WB_SALES,DAY.isoformat(),[sale()],[])
    repo.record_success(conn.id,WB_SALES,DAY.isoformat(),[sale(price=90)],[])
    assert build_daily_events(repo,conn.id,'wildberries',DAY).buyouts.amount==Decimal('90.00')
    repo.record_failure(conn.id,WB_SALES,DAY.isoformat(),'temporary API failure')
    reading=build_daily_events(repo,conn.id,'wildberries',DAY).buyouts
    assert reading.units==1 and 'сохранённые' in reading.warning
    repo.record_success(conn.id,WB_SALES,DAY.isoformat(),[],[])
    reading=build_daily_events(repo,conn.id,'wildberries',DAY).buyouts
    assert reading.units==0 and reading.amount==Decimal(0) and reading.warning is None
    other=repo.ensure_connection(shop.id,'ozon','Ozon')
    assert build_daily_events(repo,other.id,'ozon',DAY).returns.units is None


def test_partial_return_snapshot_hides_full_total_and_keeps_accessible_part(tmp_path):
    repo,shop,conn=setup(tmp_path,'ozon')
    repo.record_success(conn.id,OZON_RETURNS,DAY.isoformat(),{'returns':[customer_return()]},[],status='partial')
    reading=build_daily_events(repo,conn.id,'ozon',DAY).returns
    assert reading.units is None and reading.amount is None and reading.available_units==2


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_kind', ['http', 'missing_client', 'exception', 'pagination'])
async def test_wb_sales_failed_range_keeps_snapshots_and_warns_for_every_day(tmp_path, failure_kind):
    repo, shop, conn = setup(tmp_path)
    days = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY]
    previous = {}
    for day in days:
        rows = [] if day == DAY else [sale(day=day.isoformat()),
            sale('R1', day=day.isoformat(), price=50)]
        repo.record_success(conn.id, WB_SALES, day.isoformat(), rows, [])
        previous[day] = build_daily_events(repo, conn.id, 'wildberries', day)
    client = SimpleNamespace(sales_since=AsyncMock())
    status, attempts = None, 0
    if failure_kind == 'missing_client':
        client = None
    elif failure_kind == 'exception':
        client.sales_since.side_effect = RuntimeError('unexpected network failure')
        attempts = 1
    elif failure_kind == 'pagination':
        status, attempts = 200, 3
        client.sales_since.return_value = FetchResult.failure('wildberries',
            'WB sales pagination safety limit reached', status, attempts)
    else:
        status, attempts = 500, 3
        client.sales_since.return_value = FetchResult.failure('wildberries',
            'HTTP 500', status, attempts)
    service = CollectionService(repo, wildberries=client)
    outcomes = await service.collect_wb_sales_range(shop_id=shop.id, connection_id=conn.id,
        start=days[0], end=days[-1])
    assert [outcome.data_date for outcome in outcomes] == [day.isoformat() for day in days]
    assert all(not outcome.ok for outcome in outcomes)
    for day in days:
        attempt = repo.latest_run(conn.id, WB_SALES, day.isoformat())
        assert attempt.status == 'failed' and attempt.http_status == status
        assert attempt.attempts == attempts
        reading = build_daily_events(repo, conn.id, 'wildberries', day)
        for key in ('buyouts', 'returns'):
            old, new = getattr(previous[day], key), getattr(reading, key)
            assert (new.units, new.amount, new.freshness) == (old.units, old.amount, old.freshness)
            assert 'сохранённые данные' in new.warning
    if client is not None:
        client.sales_since.assert_awaited_once_with(days[0].isoformat())


def test_daily_summary_marks_saved_events_after_failure_and_clears_after_recovery(tmp_path):
    repo, shop, conn = setup(tmp_path)
    repo.record_success(conn.id, WB_SALES, DAY.isoformat(), [sale(), sale('R1', price=50)], [])
    repo.record_failure(conn.id, WB_SALES, DAY.isoformat(), 'HTTP 500', http_status=500)
    text = format_daily_card(repo, shop.id, build_daily_report(repo, shop.id, DAY))
    assert '✅ Выкуплено: <b>1 шт. · 100,00 ₽</b> · ⚠️ сохранённые данные' in text.summary_html
    assert '↩️ Возвращено: <b>1 шт. · 50,00 ₽</b> · ⚠️ сохранённые данные' in text.summary_html
    assert text.summary_html.count('Есть сбой загрузки') == 1
    repo.record_success(conn.id, WB_SALES, DAY.isoformat(), [], [])
    recovered = format_daily_card(repo, shop.id, build_daily_report(repo, shop.id, DAY))
    assert '✅ Выкуплено: <b>0 шт. · 0,00 ₽</b>' in recovered.summary_html
    assert 'сохранённые данные' not in recovered.summary_html
    assert 'Есть сбой загрузки' not in recovered.summary_html


@pytest.mark.asyncio
async def test_wb_sales_cancelled_request_is_propagated_without_recording_failures(tmp_path):
    import asyncio
    repo, shop, conn = setup(tmp_path)
    client = SimpleNamespace(sales_since=AsyncMock(side_effect=asyncio.CancelledError))
    service = CollectionService(repo, wildberries=client)
    with pytest.raises(asyncio.CancelledError):
        await service.collect_wb_sales_range(shop_id=shop.id, connection_id=conn.id,
            start=DAY - timedelta(days=1), end=DAY)
    assert repo.latest_run(conn.id, WB_SALES, DAY.isoformat()) is None
    assert repo.latest_run(conn.id, WB_SALES, (DAY - timedelta(days=1)).isoformat()) is None


@pytest.mark.asyncio
async def test_wb_sales_inverted_range_does_not_request_api(tmp_path):
    repo, shop, conn = setup(tmp_path)
    client = SimpleNamespace(sales_since=AsyncMock(return_value=success('wildberries', [])))
    service = CollectionService(repo, wildberries=client)
    with pytest.raises(ValueError):
        await service.collect_wb_sales_range(shop_id=shop.id, connection_id=conn.id,
            start=DAY, end=DAY - timedelta(days=1))
    client.sales_since.assert_not_awaited()


def test_order_cohort_cancellations_and_finance_net_do_not_fill_daily_events(tmp_path):
    repo,shop,conn=setup(tmp_path,'ozon')
    repo.record_success(conn.id,'analytics/orders',DAY.isoformat(),{'orders':10},[
        MetricPoint(conn.id,DAY.isoformat(),'ordered_units',10,'units'),
        MetricPoint(conn.id,DAY.isoformat(),'cancellations_units',7,'units')])
    repo.record_success(conn.id,'finance/accrual/by-day',DAY.isoformat(),{'accruals':[]},[
        MetricPoint(conn.id,DAY.isoformat(),'marketplace_net',17159,'RUB')])
    report=build_daily_report(repo,shop.id,DAY)
    assert report.sources[0].events.buyouts.units is None
    text=format_daily_card(repo,shop.id,report)
    assert 'Отменено: ⏳' in text.summary_html and 'Отменено: <b>7' not in text.summary_html
    assert '17 159' not in text.summary_html and '17 159' in text.accruals_html


@pytest.mark.asyncio
async def test_ozon_return_pagination_keeps_date_filter_and_every_page():
    client=OzonClient('local-client','LOCAL_TEST',min_interval=0)
    client.request=AsyncMock(side_effect=[
        success('ozon',{'returns':[customer_return()],'has_next':True}),
        success('ozon',{'returns':[customer_return(2)],'has_next':False})])
    result=await client.customer_returns_all('2026-09-30T21:00:00Z','2026-10-01T20:59:59Z')
    assert result.ok and len(result.data['returns'])==2
    requests=client.request.await_args_list
    assert requests[0].args==('POST','/v1/returns/list')
    assert requests[0].kwargs['json']['last_id']==0 and requests[1].kwargs['json']['last_id']==1
    assert requests[0].kwargs['json']['filter']==requests[1].kwargs['json']['filter']
    await client.close()


@pytest.mark.parametrize('body',[
    {'returns':[],'has_next':True},{'returns':[customer_return()],'has_next':None},
    {'returns':[customer_return()],'has_next':True},
])
@pytest.mark.asyncio
async def test_truncated_or_repeated_ozon_return_page_is_failure(body):
    client=OzonClient('local-client','LOCAL_TEST',min_interval=0)
    client.request=AsyncMock(return_value=success('ozon',body))
    result=await client.customer_returns_all('from','to',max_pages=2)
    assert not result.ok
    await client.close()


@pytest.mark.asyncio
async def test_ozon_realization_request_is_day_not_order_postings_filter():
    client=OzonClient('local-client','LOCAL_TEST',min_interval=0)
    client.request=AsyncMock(return_value=success('ozon',{'rows':[]}))
    await client.realization_day(DAY)
    assert client.request.await_args.args==('POST','/v1/finance/realization/by-day')
    assert client.request.await_args.kwargs['json']=={'day':1,'month':10,'year':2026}
    await client.close()


@pytest.mark.asyncio
async def test_collector_uses_cancel_date_and_customer_return_date_not_creation_day(tmp_path):
    repo,shop,wb=setup(tmp_path);oz=repo.ensure_connection(shop.id,'ozon','Ozon')
    wb_client=SimpleNamespace(orders_since=AsyncMock(return_value=success('wildberries',[
        {'srid':'old','isCancel':True,'date':'2026-09-25','cancelDate':DAY.isoformat()}])))
    oz_client=SimpleNamespace(customer_returns_all=AsyncMock(return_value=success('ozon',{
        'returns':[customer_return()],'has_next':False})))
    service=CollectionService(repo,wildberries=wb_client,ozon=oz_client)
    outcomes=await service.collect_daily_events_range(start=DAY,end=DAY,wb_connection_id=wb.id,ozon_connection_id=oz.id)
    assert all(x.ok for x in outcomes)
    assert build_daily_events(repo,wb.id,'wildberries',DAY).cancellations.units==1
    assert build_daily_events(repo,oz.id,'ozon',DAY).returns.units==2
    assert oz_client.customer_returns_all.await_args.args==('2026-09-30T21:00:00.000Z','2026-10-01T20:59:59.999Z')
    await service.collect_daily_events_range(start=DAY,end=DAY,wb_connection_id=wb.id,ozon_connection_id=oz.id)
    assert build_daily_events(repo,oz.id,'ozon',DAY).returns.units==2


@pytest.mark.asyncio
async def test_denied_premium_does_not_repeat_request_for_every_day_and_returns_still_load(tmp_path):
    repo,shop,conn=setup(tmp_path,'ozon')
    today=datetime.now(MOSCOW).date();start=today-timedelta(days=2);end=today-timedelta(days=1)
    client=SimpleNamespace(realization_day=AsyncMock(return_value=FetchResult.failure('ozon','denied',403,1)),
        customer_returns_all=AsyncMock(return_value=success('ozon',{'returns':[],'has_next':False})))
    service=CollectionService(repo,ozon=client)
    await service.collect_daily_events_range(start=start,end=end,ozon_connection_id=conn.id)
    client.realization_day.assert_awaited_once()
    for day in (start,end):
        events=build_daily_events(repo,conn.id,'ozon',day)
        assert events.returns.units==0 and events.buyouts.units is None
        assert 'Premium' in events.buyouts.warning


@pytest.mark.asyncio
async def test_daily_calendar_changes_one_message_and_does_not_load_marketplaces(ui):
    orders(ui);orders(ui,units=7,revenue=700,day=date(2026,9,30))
    message=await card(ui)
    ui.ctx.refresh_reports=AsyncMock()
    ui.telegram.methods.clear()
    assert '🗓 Выбрать дату' in labels(message) and '🏠 Главное меню' in labels(message)
    await tap(ui,message,'date')
    calendar=ui.telegram.messages[(101,message.message_id)]
    assert 'Выберите день' in calendar.text
    await tap(ui,message,'month:202609')
    await tap(ui,message,'day:20260930')
    result=ui.telegram.messages[(101,message.message_id)]
    assert 'Отчёт за 30.09.2026' in result.text and '7 шт.' in result.text
    assert stored(ui,message)['report_day']=='2026-09-30'
    assert not [m for m in ui.telegram.methods if isinstance(m,SendMessage)]
    assert any(isinstance(m,EditMessageText) for m in ui.telegram.methods)
    ui.ctx.refresh_reports.assert_not_awaited()


@pytest.mark.asyncio
async def test_calendar_is_available_to_manager_but_financial_block_is_hidden(ui):
    orders(ui);ui.repo.grant_shop_access(102,ui.shop.id,'manager')
    message=await card(ui,user=102)
    assert 'Начисления' not in labels(message)
    await tap(ui,message,'date',user=102)
    await tap(ui,message,'day:20261001',user=102)
    result=ui.telegram.messages[(102,message.message_id)]
    assert 'Выкуплено:' in result.text and 'Начисления' not in labels(result)
    ui.repo.revoke_shop_access(102,ui.shop.id)
    before=result.text
    await tap(ui,message,'day:20260930',user=102)
    assert ui.telegram.messages[(102,message.message_id)].text==before


def test_source_coverage_separates_premium_realization_from_finance_and_partial_events(tmp_path):
    repo,shop,conn=setup(tmp_path,'ozon')
    ds=DAY.isoformat()
    repo.record_success(conn.id,'finance/accrual/by-day',ds,{'accruals':[]},[
        MetricPoint(conn.id,ds,'marketplace_net',0,'RUB')])
    repo.record_failure(conn.id,OZON_REALIZATION,ds,'denied',http_status=403)
    repo.record_success(conn.id,OZON_RETURNS,ds,{'returns':[customer_return()],'undated_rows':1},[],status='partial')
    rows=build_source_coverage(repo,shop.id,ds,ds)
    finance=next(x for x in rows if x.component=='Финансы')
    returns=next(x for x in rows if x.component=='Клиентские возвраты')
    buyouts=next(x for x in rows if x.component=='Выкупы: реализация за день')
    assert finance.available_days==1 and not finance.failed_dates
    assert returns.available_days==0 and returns.partial_dates==(ds,)
    assert buyouts.available_days==0 and buyouts.failed_dates==(ds,) and 'Premium' in buyouts.note
    repo.record_success(conn.id,OZON_RETURNS,ds,{'returns':[],'undated_rows':0},[])
    rows=build_source_coverage(repo,shop.id,ds,ds)
    returns=next(x for x in rows if x.component=='Клиентские возвраты')
    assert returns.available_days==1 and not returns.partial_dates


@pytest.mark.asyncio
async def test_independent_event_sources_continue_if_another_raises(tmp_path):
    repo,shop,wb=setup(tmp_path);oz=repo.ensure_connection(shop.id,'ozon','Ozon')
    wb_client=SimpleNamespace(orders_since=AsyncMock(side_effect=RuntimeError('API failure')))
    oz_client=SimpleNamespace(customer_returns_all=AsyncMock(return_value=success('ozon',{'returns':[]})))
    service=CollectionService(repo,wildberries=wb_client,ozon=oz_client)
    outcomes=await service.collect_daily_events_range(start=DAY,end=DAY,wb_connection_id=wb.id,ozon_connection_id=oz.id)
    assert any(not x.ok and x.marketplace=='wildberries' for x in outcomes)
    assert build_daily_events(repo,oz.id,'ozon',DAY).returns.units==0


@pytest.mark.asyncio
async def test_scheduler_reconciliation_attempts_event_sources_without_retries_for_denied_optional_api(ui):
    collector=ui.ctx.collector
    collector.backfill_orders=AsyncMock(return_value=[])
    collector.collect_daily_events_range=AsyncMock(return_value=[SimpleNamespace(ok=False)])
    result=await ui.ctx.collect_reconciliation(DAY,DAY,include_finance=False)
    assert result==[]
    collector.collect_daily_events_range.assert_awaited_once_with(start=DAY,end=DAY,
        wb_connection_id=None,ozon_connection_id=None)


@pytest.mark.asyncio
async def test_manual_update_attempts_new_daily_sources(ui):
    ui.ctx.collector.collect_daily_events_range=AsyncMock(return_value=[SimpleNamespace(ok=True)])
    stages=await refresh_reports(ui.ctx,DAY,DAY,include_finance=False)
    assert [stage.key for stage in stages]==['events'] and stages[0].ok
    ui.ctx.collector.collect_daily_events_range.assert_awaited_once_with(start=DAY,end=DAY,
        wb_connection_id=None,ozon_connection_id=None)


@pytest.mark.asyncio
async def test_calendar_new_date_persists_and_refresh_uses_that_date(ui):
    orders(ui);message=await card(ui)
    await tap(ui,message,'day:20260930')
    ui.ctx.refresh_reports=AsyncMock(return_value=(RefreshStage('orders','WB',True),))
    await tap(ui,message,'refresh')
    ui.ctx.refresh_reports.assert_awaited_once_with(date(2026,9,30),date(2026,9,30))
    await ui.bot.session.close()


@pytest.mark.asyncio
async def test_calendar_cancel_restores_saved_report_and_main_menu_works(ui):
    orders(ui);message=await card(ui)
    await tap(ui,message,'date');await tap(ui,message,'collapse')
    assert ui.telegram.messages[(101,message.message_id)].text==message.text
    await tap(ui,message,'home')
    assert any(isinstance(method,SendMessage) and 'Выберите' in method.text for method in ui.telegram.methods)


def test_unknown_sales_format_does_not_prove_an_empty_daily_snapshot():
    for payload in ({},[{'date':DAY.isoformat(),'saleID':None}],[sale(day=None)]):
        with pytest.raises(DailyEventError):normalize_wb_sales_day(payload,DAY.isoformat())
