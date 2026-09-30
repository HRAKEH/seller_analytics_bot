import httpx
import pytest
from app.integrations.base import MarketplaceClient
from app.integrations.ozon import OzonClient
from app.integrations.wildberries import WildberriesClient

@pytest.mark.asyncio
async def test_wb_rejects_non_list_response():
    async def handler(request):
        return httpx.Response(200, json={'unexpected': True})
    client = WildberriesClient('token', min_interval=0, transport=httpx.MockTransport(handler))
    result = await client.sales('2026-01-01')
    assert not result.ok and 'списка' in result.error
    await client.close()

@pytest.mark.asyncio
async def test_ozon_uses_credentials_headers():
    seen = {}
    async def handler(request):
        seen['headers'] = request.headers
        return httpx.Response(200, json={'result': {}})
    client = OzonClient('cid', 'secret', min_interval=0, transport=httpx.MockTransport(handler))
    result = await client.analytics({'metrics': []})
    assert result.ok
    assert seen['headers']['Client-Id'] == 'cid'
    assert seen['headers']['Api-Key'] == 'secret'
    await client.close()

@pytest.mark.asyncio
async def test_http_401_is_not_retried():
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(401, text='unauthorized')
    client = MarketplaceClient('test', 'https://example.com', min_interval=0,
                               max_retries=3, transport=httpx.MockTransport(handler))
    result = await client.request('GET', '/')
    assert not result.ok and result.status_code == 401 and calls == 1
    await client.close()

@pytest.mark.asyncio
async def test_wb_pagination_advances_by_last_change_date():
    calls=[]
    async def handler(request):
        calls.append(str(request.url))
        if len(calls)==1:
            return httpx.Response(200,json=[
                {'srid':'a','lastChangeDate':'2026-09-28T10:00:00'},
                {'srid':'b','lastChangeDate':'2026-09-28T11:00:00'}])
        return httpx.Response(200,json=[{'srid':'c','lastChangeDate':'2026-09-28T12:00:00'}])
    client=WildberriesClient('token',min_interval=0,transport=httpx.MockTransport(handler))
    result=await client.orders_since('2026-09-28',row_threshold=2)
    assert result.ok and len(result.data)==3 and len(calls)==2
    assert 'dateFrom=2026-09-28T11%3A00%3A00' in calls[1]
    await client.close()

@pytest.mark.asyncio
async def test_ozon_performance_sku_statistics_request_matches_current_schema():
    import json as jsonlib
    from app.integrations.ozon_performance import OzonPerformanceClient
    seen=[]
    async def handler(request):
        seen.append(request)
        if request.url.path == '/api/client/token':
            return httpx.Response(200,json={'access_token':'token','expires_in':1800})
        assert request.url.path == '/api/client/statistics/products/sku'
        body=jsonlib.loads(request.content.decode())
        assert body == {'dateFrom':'2026-09-28','dateTo':'2026-09-28','campaignIds':['12','13']}
        return httpx.Response(200,json={'rows':[{'date':'2026-09-28','sku':'9001','expense':'10','sales':'100'}]})
    client=OzonPerformanceClient('cid','secret',min_interval=0,transport=httpx.MockTransport(handler))
    result=await client.product_sku_stats('2026-09-28','2026-09-28',['12','13'])
    assert result.ok and result.data['rows'][0]['sku']=='9001'
    assert len(seen)==2
    await client.close()
