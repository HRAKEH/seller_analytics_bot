from datetime import date
import httpx
import pytest
from app.integrations.yandex_market import YandexMarketClient

@pytest.mark.asyncio
async def test_orders_use_current_api_filter_and_all_pages():
    calls=[]
    def handle(req):
        import json
        calls.append(req)
        assert req.headers['Api-Key']=='test-secret'
        assert req.url.path=='/v1/businesses/10/orders'
        assert json.loads(req.content)=={'campaignIds':[20],'fake':False,
            'dates':{'creationDateFrom':'2026-11-15','creationDateTo':'2026-11-15'}}
        if len(calls)==1:
            return httpx.Response(200,json={'orders':[{'orderId':1}], 'paging':{'nextPageToken':'next'}})
        assert req.url.params['pageToken']=='next'
        return httpx.Response(200,json={'orders':[{'orderId':2}]})
    async with YandexMarketClient('test-secret',min_interval=0,transport=httpx.MockTransport(handle)) as client:
        result=await client.orders_day(10,20,date(2026,11,15))
    assert result.ok and len(result.data['orders'])==2

@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{}, {'orders':None}, {'status':'ERROR','orders':[]}, {'orders':[1]}, {'orders':[], 'paging':[]}])
async def test_bad_orders_never_become_empty_success(payload):
    async with YandexMarketClient('key',min_interval=0,transport=httpx.MockTransport(
        lambda req:httpx.Response(200,json=payload))) as client:
        result=await client.orders_day(1,2,date(2026,11,15))
    assert not result.ok

@pytest.mark.asyncio
async def test_campaign_discovery_follows_root_paging():
    def handle(req):
        if not req.url.params.get('pageToken'):
            return httpx.Response(200,json={'campaigns':[{'id':1}], 'paging':{'nextPageToken':'next'}})
        return httpx.Response(200,json={'campaigns':[{'id':2}]})
    async with YandexMarketClient('key',min_interval=0,transport=httpx.MockTransport(handle)) as client:
        result=await client.campaigns()
    assert result.ok and len(result.data['campaigns'])==2

@pytest.mark.asyncio
async def test_repeated_cursor_is_failure():
    async with YandexMarketClient('key',min_interval=0,transport=httpx.MockTransport(
        lambda req:httpx.Response(200,json={'orders':[{'orderId':1}], 'paging':{'nextPageToken':'same'}}))) as client:
        result=await client.orders_day(1,2,date(2026,11,15))
    assert not result.ok

@pytest.mark.asyncio
async def test_credentials_are_redacted_on_error():
    async with YandexMarketClient('test-secret',min_interval=0,transport=httpx.MockTransport(
        lambda req:httpx.Response(403,text='denied test-secret'))) as client:
        result=await client.campaigns()
    assert not result.ok and 'test-secret' not in result.error
