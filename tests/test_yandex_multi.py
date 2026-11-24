from unittest.mock import AsyncMock
import pytest
from app.config import MarketplaceCredentials
from app.integrations.base import FetchResult
from app.integrations.yandex_multi import YandexMarketMultiClient
from app.services.yandex_market import normalize_orders, normalize_stocks, normalize_cards, normalize_finance


def combined(*data):
    return {'campaignSnapshots': [{'campaignId': i, 'data': d} for i, d in enumerate(data, 20)]}


def test_comma_ids_trim_deduplicate_and_preserve_order():
    creds = MarketplaceCredentials('DEFAULT', yandex_market_api_key='secret',
        yandex_market_business_id='10', yandex_market_campaign_id='20, 21,20,22')
    assert creds.yandex_market_campaign_ids == (20,21,22)
    assert 'secret' not in repr(creds)


@pytest.mark.parametrize('raw', ['20,', ',20', '20,,21', '20,-1', '20,0', '20,abc', '20;21'])
def test_invalid_list_rejected(raw):
    with pytest.raises(ValueError):
        MarketplaceCredentials('DEFAULT', yandex_market_api_key='secret',
            yandex_market_business_id='10', yandex_market_campaign_id=raw)


def test_orders_same_sku_sum_across_campaigns():
    def order(cid):
        return {'orderId': 1, 'campaignId': cid, 'creationDate': '2026-11-22T12:00:00+03:00',
            'programType': 'FBS', 'status': 'PROCESSING', 'items': [{'id':1, 'offerId':'SKU', 'count':2,
                'prices': {'payment': {'value':100, 'currencyId':'RUR'},
                           'subsidy': {'value':50, 'currencyId':'RUR'}}}]}
    points, observations = normalize_orders({'orders': [order(20), order(21)]}, 1, '2026-11-22', (20,21))
    assert len(observations) == 1 and observations[0].ordered_units == 4
    assert observations[0].ordered_revenue == 300
    assert next(p.value for p in points if p.metric_key=='ordered_revenue') == 300
    with pytest.raises(ValueError):
        normalize_orders({'orders': [order(22)]}, 1, '2026-11-22', (20,21))


def stocks(wid, count):
    return {'scheme':'FBS','warehouses':[{'warehouseId':wid,'offers':[{'offerId':'SKU',
        'stocks':[{'type':'AVAILABLE','count':count}]}]}]}


def test_shared_warehouse_not_doubled_and_conflicts_rejected():
    obs = normalize_stocks(combined(stocks(50,7), stocks(50,7), stocks(51,3)), 'MULTI')
    assert sum(x.available_units for x in obs) == 10
    with pytest.raises(ValueError):
        normalize_stocks(combined(stocks(50,7),stocks(50,6)), 'MULTI')


def test_cards_healthy_shop_cannot_resolve_other_shop_block():
    def cards(status):
        return {'offers':[{'offerId':'SKU','status':status}],
            'offerMappings':[{'offer':{'offerId':'SKU','cardStatus':'HAS_CARD_CAN_UPDATE'}}]}
    issues, healthy = normalize_cards(combined(cards('REJECTED_BY_MARKET'),cards('PUBLISHED')))
    assert not healthy and issues['yandex_market:SKU'][0] == 'card_blocked'
    assert '20' in issues['yandex_market:SKU'][1]
    assert normalize_cards(combined(cards('PUBLISHED'),cards('PUBLISHED')))[1] == {'yandex_market:SKU'}


def test_finance_same_order_ids_in_distinct_shops_are_summed():
    data = {'orders':[{'id':1,'creationDate':'2026-11-22','currency':'RUR','commissions':[{'type':'FEE','actual':10}]}]}
    points = normalize_finance(combined(data,data), 1, '2026-11-22')
    assert next(p.value for p in points if p.metric_key == 'ym_order_services') == 20


@pytest.mark.asyncio
async def test_one_failed_campaign_returns_no_partial_snapshot():
    client = YandexMarketMultiClient('secret',10,(20,21), min_interval=0)
    client.configured_campaign = AsyncMock(return_value=FetchResult.success('yandex_market',{},200,1))
    client.clients[0].inventory_snapshot = AsyncMock(return_value=FetchResult.success('yandex_market',stocks(50,7),200,1))
    client.clients[1].inventory_snapshot = AsyncMock(return_value=FetchResult.failure('yandex_market','offline',503,1))
    try:
        result = await client.inventory_snapshot()
        assert not result.ok and result.data is None and '21' in result.error
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_all_campaigns_must_belong_to_business():
    client = YandexMarketMultiClient('secret',10,(20,21), min_interval=0)
    client.clients[0].campaigns = AsyncMock(return_value=FetchResult.success('yandex_market',{'campaigns':[
        {'id':20,'business':{'id':10}}, {'id':21,'business':{'id':11}}]},200,1))
    try:
        assert not (await client.configured_campaign()).ok
        assert not hasattr(client.clients[0], '_campaign')
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_multi_orders_collection_persists_sum_and_failure_keeps_previous(tmp_path):
    from datetime import date
    from app.storage import Database, Repository
    from app.services.collection import CollectionService
    db=Database(tmp_path/'db.sqlite'); db.initialize(); repo=Repository(db)
    seller=repo.ensure_seller(1,'Seller'); shop=repo.ensure_shop(seller.id,'Shop')
    connection=repo.ensure_connection(shop.id,'yandex_market','Яндекс Маркет')
    client=YandexMarketMultiClient('secret',10,(20,21),min_interval=0)
    campaigns={'campaigns':[{'id':cid,'business':{'id':10}} for cid in (20,21)]}
    client.clients[0].campaigns=AsyncMock(return_value=FetchResult.success('yandex_market',campaigns,200,1))
    for child in client.clients:
        data={'orders':[{'orderId':child.campaign_id,'campaignId':child.campaign_id,
            'creationDate':'2026-11-22T12:00:00+03:00','programType':'FBS','status':'PROCESSING',
            'items':[{'id':1,'offerId':'SKU','count':2,'prices':{'payment':{'value':100,'currencyId':'RUR'}}}]}]}
        child.orders_day=AsyncMock(return_value=FetchResult.success('yandex_market',data,200,1))
    collector=CollectionService(repo,yandex_market=client)
    day=date(2026,11,22)
    try:
        assert (await collector.collect_yandex_orders_day(connection.id,day,shop_id=shop.id)).ok
        with db.connect() as conn:
            before=[tuple(row) for row in conn.execute('SELECT * FROM metric_values')]
        client.clients[1].orders_day=AsyncMock(return_value=FetchResult.failure('yandex_market','offline',503,1))
        assert not (await collector.collect_yandex_orders_day(connection.id,day,shop_id=shop.id)).ok
        with db.connect() as conn:
            assert [tuple(row) for row in conn.execute('SELECT * FROM metric_values')] == before
    finally:
        await client.close()


def test_missing_offer_in_one_campaign_never_confirms_recovery():
    healthy={'offers':[{'offerId':'SKU','status':'PUBLISHED'}],
        'offerMappings':[{'offer':{'offerId':'SKU','cardStatus':'HAS_CARD_CAN_UPDATE'}}]}
    missing={'offers':[], 'offerMappings':healthy['offerMappings']}
    assert not normalize_cards(combined(healthy, missing))[1]


def test_promotions_union_participation_without_duplicate_campaign_promo():
    from app.services.yandex_market import normalize_promotions
    def promo(cid):
        return {'campaignId':cid, 'promos':[{'id':'P','period':{'dateTimeFrom':'2026-11-22T00:00:00Z',
            'dateTimeTo':'2026-11-23T00:00:00Z'}}], 'products':{'P':[{'offerId':'SKU',
            'status':'PARTIALLY_AUTO', 'autoParticipatingDetails':{'campaignIds':[21]}}]}}
    out=normalize_promotions(combined(promo(20),promo(21)))
    assert len(out)==1 and len(out[0]['products'])==1 and out[0]['products'][0]['in_action']


def test_inbound_ids_are_scoped_to_campaign():
    from app.services.yandex_market import normalize_inbound
    data={'requests':[{'id':{'id':1},'status':'FINISHED','type':'SUPPLY'}], 'items':{'1':[]}}
    out=normalize_inbound(combined(data,data))
    assert {item['external_supply_id'] for item in out}=={'20:1','21:1'}
