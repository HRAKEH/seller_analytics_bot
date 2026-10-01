from datetime import date
from uuid import uuid4
import pytest

from app.config import Settings
from app.integrations.base import FetchResult
from app.integrations.wildberries import WildberriesClient
from app.bot.context import AppContext
from app.reports import build_daily_report,format_daily
from app.services.collection import CollectionService
from app.services.normalization import NormalizationError
from app.services.wb_funnel import normalize_wb_funnel
from app.storage import Database,Repository,MetricPoint

DAY='2026-09-30'


def card(sku=1,units=5,amount=1000,day=DAY):
    return {'product':{'nmId':sku,'title':f'Product {sku}','vendorCode':f'A{sku}'},
            'statistic':{'selected':{'period':{'start':day,'end':day},
                                    'orderCount':units,'orderSum':amount,'cancelCount':0}}}


def payload(*cards):return {'data':{'products':list(cards)}}


def order(sku=1):
    return {'date':DAY+'T12:00:00','lastChangeDate':DAY+'T12:01:00',
            'nmId':sku,'srid':f'{sku}-order','priceWithDisc':100,
            'warehouseType':'Склад продавца','isCancel':False}


@pytest.fixture
def repo_shop(tmp_path):
    db=Database(tmp_path/'funnel.sqlite3');db.initialize();repo=Repository(db)
    seller=repo.ensure_seller(1);shop=repo.ensure_shop(seller.id)
    connection=repo.ensure_connection(shop.id,'wildberries','WB')
    return repo,shop,connection


class FakeWB:
    def __init__(self):
        self.rate_scope=str(uuid4())
        self.rows=[order(1),order(2)]
        self.funnel=FetchResult.success('wildberries',payload(card(1),card(2,2,400)),200,1)
        self.funnel_days=[]

    async def orders(self,*args,**kwargs):return FetchResult.success('wildberries',self.rows,200,1)
    async def orders_since(self,*args):return await self.orders()
    async def sales_funnel_all(self,day):
        self.funnel_days.append(day)
        return self.funnel


def test_funnel_uses_order_fields_and_sums_all_cards():
    points,products=normalize_wb_funnel(payload(card(1,100,80000),card(2,28,21350)),7,DAY)
    metrics={p.metric_key:p.value for p in points}
    assert metrics=={'ordered_units':128,'ordered_revenue':101350,'cancellations_units':0}
    assert sum(p.ordered_units for p in products)==128
    assert sum(p.ordered_revenue for p in products)==101350


@pytest.mark.parametrize('key,value',[('orderCount',None),('orderCount',-1),('orderCount',1.5),
                                     ('orderSum','NaN'),('cancelCount',True)])
def test_bad_funnel_fields_are_not_silent_zero(key,value):
    row=card();row['statistic']['selected'][key]=value
    with pytest.raises(NormalizationError):normalize_wb_funnel(payload(row),7,DAY)


def test_funnel_rejects_duplicate_cards_and_another_period():
    with pytest.raises(NormalizationError):normalize_wb_funnel(payload(card(),card()),7,DAY)
    with pytest.raises(NormalizationError):normalize_wb_funnel(payload(card(day='2026-09-29')),7,DAY)
    with pytest.raises(NormalizationError):normalize_wb_funnel({'data':{}},7,DAY)


@pytest.mark.asyncio
async def test_api_paginates_all_products_without_filters():
    client=WildberriesClient('test');requests=[]
    async def request(method,url,**kwargs):
        requests.append((method,url,kwargs))
        offset=kwargs['json']['offset']
        return FetchResult.success('wildberries',payload(*([card(1),card(2)] if offset==0 else [card(3)])),200,1)
    client.request=request
    result=await client.sales_funnel_all(DAY,limit=2)
    assert result.ok and result.attempts==2
    assert [r['product']['nmId'] for r in result.data['data']['products']]==[1,2,3]
    assert [r[2]['json']['offset'] for r in requests]==[0,2]
    assert requests[0][0]=='POST'
    assert requests[0][1].endswith('/api/analytics/v3/sales-funnel/products')
    assert requests[0][2]['json']['selectedPeriod']=={'start':DAY,'end':DAY}
    assert requests[0][2]['json']['nmIds']==[] and requests[0][2]['json']['skipDeletedNm'] is False
    assert requests[0][2]['min_interval']==20


@pytest.mark.asyncio
@pytest.mark.parametrize('broken_page',['failure','repeated','malformed'])
async def test_api_never_returns_partial_success(broken_page):
    client=WildberriesClient('test');calls=0
    async def request(*args,**kwargs):
        nonlocal calls
        calls+=1
        if calls==1:return FetchResult.success('wildberries',payload(card()),200,1)
        if broken_page=='failure':return FetchResult.failure('wildberries','HTTP 500',500,1)
        return FetchResult.success('wildberries',payload(card()) if broken_page=='repeated' else {},200,1)
    client.request=request
    result=await client.sales_funnel_all(DAY,limit=1)
    assert not result.ok and result.data is None


@pytest.mark.asyncio
async def test_statistics_refresh_cannot_overwrite_funnel_or_product_totals(repo_shop):
    repo,shop,connection=repo_shop;wb=FakeWB();service=CollectionService(repo,wildberries=wb)
    assert (await service.collect_wb_orders_day(connection.id,date.fromisoformat(DAY),shop_id=shop.id)).ok
    wb.rows.append(order(3))
    wb.funnel=FetchResult.failure('wildberries','HTTP 500',500,1)
    assert not (await service.collect_wb_orders_day(connection.id,date.fromisoformat(DAY),shop_id=shop.id)).ok
    metrics=repo.metrics_for_day(connection.id,DAY)
    assert metrics['ordered_units']==7 and metrics['ordered_revenue']==1400
    rows=repo.product_period_totals(shop.id,DAY,DAY)
    assert sum(r['value'] for r in rows)==7
    assert '3' not in {r['marketplace_sku'] for r in rows}
    schemes=repo.product_period_totals(shop.id,DAY,DAY,'fulfillment_units','FBS')
    assert sum(r['value'] for r in schemes)==3
    text=format_daily(build_daily_report(repo,shop.id,date.fromisoformat(DAY)))
    assert 'Воронка продаж' in text and 'HTTP 500' in text
    assert 'неподтверждённой оплатой' not in text


@pytest.mark.asyncio
async def test_first_funnel_failure_leaves_explicit_statistics_fallback(repo_shop):
    repo,shop,connection=repo_shop;wb=FakeWB()
    wb.funnel=FetchResult.failure('wildberries','HTTP 403',403,1)
    service=CollectionService(repo,wildberries=wb)
    await service.collect_wb_orders_day(connection.id,date.fromisoformat(DAY),shop_id=shop.id)
    text=format_daily(build_daily_report(repo,shop.id,date.fromisoformat(DAY)))
    assert '2 шт.' in text and 'резервный источник' in text and 'HTTP 403' in text
    await service.collect_wb_orders_day(connection.id,date.fromisoformat(DAY),shop_id=shop.id)
    assert len(wb.funnel_days)==1


@pytest.mark.asyncio
async def test_empty_complete_funnel_zeros_orders_but_preserves_operational_schemes(repo_shop):
    repo,shop,connection=repo_shop;wb=FakeWB();service=CollectionService(repo,wildberries=wb)
    await service.collect_wb_orders_day(connection.id,date.fromisoformat(DAY),shop_id=shop.id)
    wb.funnel=FetchResult.success('wildberries',payload(),200,1)
    await service.collect_wb_orders_day(connection.id,date.fromisoformat(DAY),shop_id=shop.id)
    assert repo.metrics_for_day(connection.id,DAY)['ordered_units']==0
    assert sum(r['value'] for r in repo.product_period_totals(shop.id,DAY,DAY))==0
    assert sum(r['value'] for r in repo.product_period_totals(shop.id,DAY,DAY,'fulfillment_units','FBS'))==2


@pytest.mark.asyncio
async def test_history_upgrades_cached_statistics_to_funnel(repo_shop):
    repo,shop,connection=repo_shop;wb=FakeWB();service=CollectionService(repo,wildberries=wb)
    repo.record_success(connection.id,'statistics/orders',DAY,[order()],
        [MetricPoint(connection.id,DAY,'ordered_units',1,'units')])
    settings=Settings.from_env()
    ctx=AppContext(settings,repo,shop.id,service,wb_connection_id=connection.id)
    await ctx.backfill_orders(date.fromisoformat(DAY),date.fromisoformat(DAY),'wb')
    assert wb.funnel_days==[DAY]
    assert repo.metrics_for_day(connection.id,DAY)['ordered_units']==7
    assert repo.successful_order_dates(connection.id,DAY,DAY,prefer_wb_funnel=True)==[DAY]
    await ctx.backfill_orders(date.fromisoformat(DAY),date.fromisoformat(DAY),'wb')
    assert wb.funnel_days==[DAY]


@pytest.mark.asyncio
async def test_daily_comparison_does_not_mix_order_sources(repo_shop):
    repo,shop,connection=repo_shop
    repo.record_success(connection.id,'statistics/orders','2026-09-29',{},
        [MetricPoint(connection.id,'2026-09-29','ordered_units',4,'units')])
    await CollectionService(repo,wildberries=FakeWB()).collect_wb_orders_day(connection.id,date.fromisoformat(DAY),shop_id=shop.id)
    report=build_daily_report(repo,shop.id,date.fromisoformat(DAY))
    assert report.sources[0].previous_units is None


@pytest.mark.asyncio
async def test_period_and_product_comparisons_do_not_mix_order_sources(repo_shop):
    from app.reports import build_period_report
    from app.reports.products import build_product_report
    repo,shop,connection=repo_shop
    repo.record_success(connection.id,'statistics/orders','2026-09-29',{},
        [MetricPoint(connection.id,'2026-09-29','ordered_units',4,'units')])
    await CollectionService(repo,wildberries=FakeWB()).collect_wb_orders_day(
        connection.id,date.fromisoformat(DAY),shop_id=shop.id)
    period=build_period_report(repo,shop.id,date.fromisoformat(DAY),1,'День')
    assert period.total==7 and period.previous_total is None
    products=build_product_report(repo,shop.id,date.fromisoformat(DAY),days=1)
    assert not products.comparison_complete and not products.growth
    repo.record_success(connection.id,'analytics/orders','2026-09-29',{},
        [MetricPoint(connection.id,'2026-09-29','ordered_units',4,'units')])
    period=build_period_report(repo,shop.id,date.fromisoformat(DAY),1,'День')
    assert period.previous_total==4


@pytest.mark.asyncio
async def test_backfill_skips_cached_funnel_after_an_earlier_gap(repo_shop):
    repo,shop,connection=repo_shop;wb=FakeWB();service=CollectionService(repo,wildberries=wb)
    await service.collect_wb_orders_day(connection.id,date.fromisoformat(DAY),shop_id=shop.id)
    wb.funnel_days.clear()
    async def funnel(day):
        wb.funnel_days.append(day)
        return FetchResult.success('wildberries',payload(card(day=day)),200,1)
    wb.sales_funnel_all=funnel
    outcomes=await service.backfill_orders(start=date(2026,9,29),end=date(2026,9,30),
        wb_connection_id=connection.id,shop_id=shop.id)
    assert all(r.ok for r in outcomes)
    assert wb.funnel_days==['2026-09-29']
    assert repo.metrics_for_day(connection.id,DAY)['ordered_units']==7


@pytest.mark.asyncio
async def test_unchanged_funnel_success_clears_prior_failure_and_statistics_warning(repo_shop):
    repo,shop,connection=repo_shop;wb=FakeWB();service=CollectionService(repo,wildberries=wb)
    await service.collect_wb_orders_day(connection.id,date.fromisoformat(DAY),shop_id=shop.id)
    good=wb.funnel
    wb.funnel=FetchResult.failure('wildberries','HTTP 500',500,1)
    await service.collect_wb_orders_day(connection.id,date.fromisoformat(DAY),shop_id=shop.id)
    wb.funnel=good
    async def orders(*args,**kwargs):return FetchResult.failure('wildberries','Statistics HTTP 500',500,1)
    wb.orders=orders
    await service.collect_wb_orders_day(connection.id,date.fromisoformat(DAY),shop_id=shop.id)
    report=build_daily_report(repo,shop.id,date.fromisoformat(DAY))
    assert report.sources[0].units==7 and report.sources[0].warning is None
