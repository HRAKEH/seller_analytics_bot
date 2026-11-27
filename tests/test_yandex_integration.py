from copy import deepcopy
from datetime import date
from dataclasses import replace
import json
import httpx
import pytest
from app.config import Settings, MarketplaceCredentials
from app.storage import Database, Repository, MetricPoint
from app.integrations.yandex_market import YandexMarketClient
from app.services.collection import CollectionService
from app.services.yandex_market import normalize_orders, normalize_stocks, normalize_cards, normalize_finance
from app.reports import build_daily_report, format_daily, build_product_report, format_product_report
from app.reports.cards import format_daily_card
from app.services.product_status import check_product_status
from app.bot.context import AppContext

DAY = date(2026,11,14)


def order():
    return {'orderId':1,'campaignId':20,'fake':False,'creationDate':'2026-11-14T01:00:00+03:00',
        'status':'PROCESSING','programType':'FBS','items':[{'id':1,'offerId':'SKU','offerName':'Товар','count':3,
        'prices':{'payment':{'value':240,'currencyId':'RUR'},'cashback':{'value':60,'currencyId':'RUR'}},
        'itemStatuses':[{'status':'CREATED','count':3}]}]}


def setup(tmp_path):
    db=Database(tmp_path/'db.sqlite');db.initialize();repo=Repository(db)
    seller=repo.ensure_seller(1,'Seller');shop=repo.ensure_shop(seller.id,'Shop')
    conn=repo.ensure_connection(shop.id,'yandex_market','Яндекс Маркет')
    return repo,shop,conn


def test_orders_amount_is_line_total_with_cashback_not_quantity_multiplied():
    points,obs=normalize_orders({'orders':[order()]},1,DAY.isoformat(),20)
    assert {p.metric_key:p.value for p in points} == {'ordered_units':3,'ordered_revenue':300,'cancellations_units':0}
    assert obs[0].fulfillment_units=={'FBS':3}


def test_dashboard_order_amount_includes_market_compensation_once():
    payload=order()
    payload['items'][0]['prices']['subsidy']={'value':700,'currencyId':'RUR'}
    payload['prices']={'payment':{'value':240,'currencyId':'RUR'},
                       'subsidy':{'value':700,'currencyId':'RUR'},
                       'delivery':{'payment':{'value':90,'currencyId':'RUR'}}}
    points,obs=normalize_orders({'orders':[payload]},1,DAY.isoformat(),20)
    assert {p.metric_key:p.value for p in points} == {
        'ordered_units':3,'ordered_revenue':1000,'cancellations_units':0}
    assert obs[0].ordered_revenue==1000


@pytest.mark.parametrize('subsidy',[None, {'value':0,'currencyId':'RUB'}])
def test_zero_or_null_market_compensation_does_not_change_total(subsidy):
    payload=order()
    payload['items'][0]['prices']['subsidy']=subsidy
    points,obs=normalize_orders({'orders':[payload]},1,DAY.isoformat(),20)
    assert obs[0].ordered_revenue==300
    assert next(p.value for p in points if p.metric_key=='ordered_revenue')==300


@pytest.mark.parametrize('subsidy',[{}, {'value':2,'currencyId':'USD'},
    {'value':'NaN','currencyId':'RUR'}, {'value':-1,'currencyId':'RUR'}])
def test_invalid_market_compensation_is_not_silently_ignored(subsidy):
    payload=order()
    payload['items'][0]['prices']['subsidy']=subsidy
    with pytest.raises(ValueError):
        normalize_orders({'orders':[payload]},1,DAY.isoformat(),20)


@pytest.mark.parametrize('mutation',[
    lambda o:o.update(campaignId=21),lambda o:o.update(fake=True),lambda o:o.update(creationDate='2026-11-13T01:00:00+03:00'),
    lambda o:o['items'][0].update(count=-1),lambda o:o['items'][0]['prices']['payment'].update(currencyId='USD'),
    lambda o:o['items'][0].update(itemStatuses=[{'status':'CREATED','count':4}]),
    lambda o:o['items'][0]['prices']['payment'].update(value='NaN'),
])
def test_orders_reject_corrupt_or_cross_account_data(mutation):
    item=order();mutation(item)
    with pytest.raises(ValueError):normalize_orders({'orders':[item]},1,DAY.isoformat(),20)


def test_all_created_orders_and_partial_cancellations_are_separate():
    full=order()
    full['status']='CANCELLED'
    full['items'][0]['count']=2
    full['items'][0]['itemStatuses']=[{'status':'CANCELLED','count':2}]
    full['items'][0]['prices']={'payment':{'value':2700,'currencyId':'RUR'}}
    active=order()
    active['orderId']=2
    active['items'][0]['count']=12
    active['items'][0]['itemStatuses']=[{'status':'CREATED','count':12}]
    active['items'][0]['prices']={'payment':{'value':9123,'currencyId':'RUR'}}
    points,obs=normalize_orders({'orders':[full,active]},1,DAY.isoformat(),20)
    assert {p.metric_key:p.value for p in points}=={
        'ordered_units':14,'ordered_revenue':11823,'cancellations_units':2}
    assert obs[0].fulfillment_units=={'FBS':14}
    gross,_=normalize_orders({'orders':[full,active]},1,DAY.isoformat(),20,include_created=True)
    assert {p.metric_key:p.value for p in gross}=={'ordered_units':14,'ordered_revenue':11823,'cancellations_units':2,'ym_created_units':14,'ym_created_revenue':11823,'ym_noncancelled_units':12,'ym_noncancelled_revenue':9123}
    partial=order()
    partial['items'][0]['itemStatuses']=[{'status':'CREATED','count':2},{'status':'CANCELLED','count':1}]
    points,obs=normalize_orders({'orders':[partial]},1,DAY.isoformat(),20)
    assert {p.metric_key:p.value for p in points}=={
        'ordered_units':3,'ordered_revenue':300,'cancellations_units':1}


def test_stock_explicit_zero_and_overreserve():
    offer={'offerId':'SKU','stocks':[{'type':'FIT','count':0}]}
    payload={'warehouses':[{'warehouseId':50,'offers':[offer]}]}
    assert normalize_stocks(payload,'FBS')[0].available_units==0
    offer['stocks']=[{'type':'FIT','count':2},{'type':'FREEZE','count':3}]
    assert normalize_stocks(payload,'FBS')[0].available_units==0


def test_supply_before_receipt_without_fact_count():
    from app.services.yandex_market import normalize_inbound
    payload={'requests':[{'id':{'id':1},'type':'SUPPLY','status':'CREATED'}],
             'items':{'1':[{'offerId':'SKU','counters':{'planCount':10}}]}}
    assert normalize_inbound(payload)[0]['items'][0]['remaining_units']==10
    payload['requests'][0]['status']='WAREHOUSE_HANDLING'
    with pytest.raises(ValueError,match='factCount'):
        normalize_inbound(payload)


def test_stocks_do_not_double_count_fit_and_available():
    payload={'warehouses':[{'warehouseId':50,'offers':[{'offerId':'SKU','stocks':[
        {'type':'FIT','count':10},{'type':'FREEZE','count':3},{'type':'AVAILABLE','count':7}]}]}]}
    obs=normalize_stocks(payload,'FBS')[0]
    assert (obs.available_units,obs.reserved_units)==(7,3)
    payload['warehouses'][0]['offers'][0]['stocks']=[{'type':'FIT','count':10}]
    with pytest.raises(ValueError):normalize_stocks(payload,'FBS')


def cards(status='REJECTED_BY_MARKET',card='NO_CARD_ERRORS'):
    return {'offers':[{'offerId':'SKU','status':status,'errors':[]}],
        'offerMappings':[{'offer':{'offerId':'SKU','name':'Товар','cardStatus':card}}]}


def test_card_recovery_requires_explicit_success():
    issues,healthy=normalize_cards(cards());assert issues['yandex_market:SKU'][0]=='card_blocked';assert not healthy
    issues,healthy=normalize_cards(cards('NO_STOCKS','HAS_CARD_CAN_UPDATE'));assert not issues and not healthy
    issues,healthy=normalize_cards(cards('PUBLISHED','HAS_CARD_CAN_UPDATE'));assert healthy=={'yandex_market:SKU'}


def test_finance_is_separate_order_cohort_not_payout():
    payload={'orders':[{'id':1,'creationDate':DAY.isoformat(),'currency':'RUR',
        'commissions':[{'type':'FEE','actual':20},{'type':'AUCTION_PROMOTION','actual':10}]}]}
    values={p.metric_key:p.value for p in normalize_finance(payload,1,DAY.isoformat())}
    assert values=={'ym_order_services':30,'ym_order_boost':10}


def test_credentials_validate_and_never_display_key(monkeypatch):
    with pytest.raises(ValueError):MarketplaceCredentials('DEFAULT',yandex_market_api_key='secret')
    monkeypatch.setenv('SELLERBOT_TEST_YANDEX_MARKET_API_KEY','secret')
    monkeypatch.setenv('SELLERBOT_TEST_YANDEX_MARKET_BUSINESS_ID','10')
    monkeypatch.setenv('SELLERBOT_TEST_YANDEX_MARKET_CAMPAIGN_ID','20')
    settings=Settings.from_env();creds=settings.credentials_for_profile('TEST')
    assert creds.has_yandex_market and 'secret' not in repr(creds)
    assert 'TEST' in settings.available_credential_profiles()


@pytest.mark.asyncio
async def test_runtime_collection_reports_and_failed_refresh_preserve_data(tmp_path):
    repo,shop,conn=setup(tmp_path);failed=False;subsidy=None
    def handle(req):
        if req.url.path=='/v2/campaigns':
            return httpx.Response(200,json={'campaigns':[{'id':20,'business':{'id':10},'placementType':'FBS'}]})
        assert req.url.path=='/v1/businesses/10/orders'
        payload=order()
        payload['items'][0]['prices']['subsidy']=subsidy
        return httpx.Response(200,json={} if failed else {'orders':[payload]})
    async with YandexMarketClient('key',min_interval=0,transport=httpx.MockTransport(handle)) as client:
        client.business_id=10;client.campaign_id=20
        ctx=AppContext(Settings.from_env(),repo,shop.id,CollectionService(repo,yandex_market=client),yandex_market_connection_id=conn.id)
        assert ctx.configured_sources()==1
        assert (await ctx.collect_day(DAY))[0].ok
        report=build_daily_report(repo,shop.id,DAY)
        assert report.total_units==3 and 'Яндекс Маркет' in format_daily(report)
        card=format_daily_card(repo,shop.id,report)
        assert 'Яндекс Маркет' in card.summary_html and 'Источник заказов: Воронка продаж WB' not in card.details_html
        assert 'Яндекс Маркет' in format_product_report(build_product_report(repo,shop.id,DAY,days=1))
        assert repo.raw_payload_for_run(repo.last_successful_order_run(conn.id,DAY.isoformat()).id) is None
        assert repo.latest_metric(conn.id,DAY.isoformat(),'ordered_revenue')['value']==300
        subsidy={'value':700,'currencyId':'RUR'}
        assert (await ctx.collect_day(DAY))[0].ok
        assert repo.latest_metric(conn.id,DAY.isoformat(),'ordered_revenue')['value']==1000
        failed=True
        assert not (await ctx.collect_day(DAY))[0].ok
        assert build_daily_report(repo,shop.id,DAY).total_units==3
        assert repo.latest_metric(conn.id,DAY.isoformat(),'ordered_revenue')['value']==1000


@pytest.mark.asyncio
async def test_wrong_business_stops_all_collection(tmp_path):
    calls=[]
    def handle(req):
        calls.append(req.url.path)
        return httpx.Response(200,json={'campaigns':[{'id':20,'business':{'id':99}}]})
    async with YandexMarketClient('key',min_interval=0,transport=httpx.MockTransport(handle)) as client:
        client.business_id=10;client.campaign_id=20
        assert not (await client.inventory_snapshot()).ok
        assert calls==['/v2/campaigns']


def test_migration_preserves_existing_connection_and_children(tmp_path):
    from app.storage import database as module
    db=Database(tmp_path/'old.sqlite')
    with db.connect() as c:
        c.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
        for version,migration in sorted(module.MIGRATIONS.items()):
            if version>=23:break
            migration(c);c.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    repo=Repository(db);seller=repo.ensure_seller(1,'S');shop=repo.ensure_shop(seller.id,'Old')
    conn=repo.ensure_connection(shop.id,'wildberries','WB')
    repo.record_success(conn.id,'statistics/orders',DAY.isoformat(),{},[MetricPoint(conn.id,DAY.isoformat(),'ordered_units',7,'units',True)])
    assert db.initialize_safely()==module.LATEST_SCHEMA_VERSION
    assert repo.latest_metric(conn.id,DAY.isoformat(),'ordered_units')['value']==7
    repo.ensure_connection(shop.id,'yandex_market','Яндекс Маркет')
    with db.connect() as c:
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1


def test_promotions_confirm_exact_shop_participation():
    from app.services.yandex_market import normalize_promotions
    payload={'campaignId':20,'promos':[{'id':'p','name':'Sale','period':{'dateTimeFrom':'2026-11-01T00:00:00Z','dateTimeTo':'2026-12-01T00:00:00Z'}}],
        'products':{'p':[{'offerId':'SKU','status':'PARTIALLY_AUTO','autoParticipatingDetails':{'campaignIds':[99]}}]}}
    assert not normalize_promotions(payload)[0]['products'][0]['in_action']
    payload['products']['p'][0]['autoParticipatingDetails']['campaignIds']=[20]
    assert normalize_promotions(payload)[0]['products'][0]['in_action']


def test_inbound_parent_and_closed_shipments_not_double_counted():
    from app.services.yandex_market import normalize_inbound
    item={'offerId':'SKU','counters':{'planCount':10,'factCount':3}}
    payload={'requests':[{'id':{'id':1},'type':'SUPPLY','status':'CREATED','childrenLinks':[{'id':2}]}],'items':{'1':[item]}}
    assert normalize_inbound(payload)[0]['items'][0]['remaining_units']==0
    payload['requests'][0]['childrenLinks']=[]
    assert normalize_inbound(payload)[0]['items'][0]['remaining_units']==7
    payload['requests'][0]['status']='FINISHED'
    assert normalize_inbound(payload)[0]['items'][0]['remaining_units']==0


@pytest.mark.asyncio
async def test_card_notifications_change_once_and_recover(tmp_path):
    from app.integrations import FetchResult
    from unittest.mock import AsyncMock
    repo,shop,conn=setup(tmp_path)
    client=type('Client',(),{})()
    client.card_status_snapshot=AsyncMock(return_value=FetchResult.success('yandex_market',cards(),200,1))
    ctx=AppContext(Settings.from_env(),repo,shop.id,CollectionService(repo,yandex_market=client),yandex_market_connection_id=conn.id)
    notes=await check_product_status(ctx)
    assert len(notes)==1 and notes[0].rule_key=='card_blocked'
    repo.set_job_state(shop.id,'card_check:yandex_market:cards','2000-01-01T00:00:00+00:00')
    assert not await check_product_status(ctx)
    repo.set_job_state(shop.id,'card_check:yandex_market:cards','2000-01-01T00:00:00+00:00')
    client.card_status_snapshot.return_value=FetchResult.success('yandex_market',cards('PUBLISHED','HAS_CARD_CAN_UPDATE'),200,1)
    assert (await check_product_status(ctx))[0].severity=='resolved'


@pytest.mark.asyncio
async def test_partner_stock_routes_only_configured_campaign():
    calls=[]
    def handle(req):
        calls.append(req.url.path)
        if req.url.path=='/v2/campaigns':return httpx.Response(200,json={'campaigns':[{'id':20,'business':{'id':10},'placementType':'FBS'}]})
        if req.url.path.endswith('/warehouses'):return httpx.Response(200,json={'result':{'warehouses':[{'id':50,'models':[{'campaignId':20}]},{'id':51,'models':[{'campaignId':99}]}]}})
        assert json.loads(req.content)=={'partnerWarehouseId':50}
        return httpx.Response(200,json={'result':{'offers':[{'offerId':'SKU','stocks':[{'type':'AVAILABLE','count':7},{'type':'FREEZE','count':3}]}]}})
    async with YandexMarketClient('key',min_interval=0,transport=httpx.MockTransport(handle)) as client:
        client.business_id=10;client.campaign_id=20
        result=await client.inventory_snapshot()
        assert result.ok and normalize_stocks(result.data,result.data['scheme'])[0].available_units==7
        assert len(calls)==3


def test_partial_cancellation_keeps_original_order_amount_and_separate_active_total():
    payload=order()
    payload['items'][0]['itemStatuses']=[{'status':'CREATED','count':2},{'status':'CANCELLED','count':1}]
    points,obs=normalize_orders({'orders':[payload]},1,DAY.isoformat(),20,include_created=True)
    values={p.metric_key:p.value for p in points}
    assert values['ordered_units']==3 and values['ordered_revenue']==300
    assert values['ym_noncancelled_units']==2 and values['ym_noncancelled_revenue']==200
    assert obs[0].ordered_units==3 and obs[0].ordered_revenue==300
    assert obs[0].fulfillment_units=={'FBS':3}
