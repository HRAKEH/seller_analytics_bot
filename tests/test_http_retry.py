from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.integrations.base import MarketplaceClient, _server_retry_delay


def response_with_headers(**headers):
    return httpx.Response(429,headers={k.replace('_','-'):str(v) for k,v in headers.items()})


def test_wb_retry_header_is_honored_without_ten_minute_cap():
    response=response_with_headers(X_Ratelimit_Retry='1473')
    assert _server_retry_delay(response)==1473.0


def test_wb_retry_header_takes_priority_over_generic_retry_after():
    response=httpx.Response(429,headers={
        'X-Ratelimit-Retry':'37',
        'Retry-After':'5',
        'X-Ratelimit-Reset':'120',
    })
    assert _server_retry_delay(response)==37.0


def test_retry_after_http_date_is_supported():
    target=datetime.now(timezone.utc)+timedelta(seconds=30)
    response=httpx.Response(429,headers={'Retry-After':format_datetime(target,usegmt=True)})
    delay=_server_retry_delay(response)
    assert delay is not None
    assert 20 <= delay <= 31


def test_rate_limit_reset_is_used_as_fallback():
    response=httpx.Response(429,headers={'X-Ratelimit-Reset':'29'})
    assert _server_retry_delay(response)==29.0


@pytest.mark.asyncio
async def test_diagnostic_request_can_fail_fast_on_429():
    async def handler(request):
        return httpx.Response(429,headers={'X-Ratelimit-Retry':'40548'},request=request)
    client=MarketplaceClient('test','https://example.test',transport=httpx.MockTransport(handler),max_retries=3)
    try:
        result=await client.request('GET','/ping',retry_on_429=False)
    finally:
        await client.close()
    assert not result.ok
    assert result.status_code==429
    assert result.attempts==1
    assert '40548' in (result.error or '')


@pytest.mark.asyncio
async def test_final_429_preserves_cooldown_and_shared_scope_can_fail_fast():
    calls=0
    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429,headers={'X-Ratelimit-Retry':'120'},text='limited',request=request)

    transport=httpx.MockTransport(handler)
    first=MarketplaceClient('test','https://example.com',min_interval=0,max_retries=0,
                            transport=transport,rate_scope='same-credential')
    second=MarketplaceClient('test','https://example.com',min_interval=0,max_retries=0,
                             transport=transport,rate_scope='same-credential')
    try:
        result=await first.request('GET','/',retry_on_429=False)
        assert not result.ok and result.status_code==429 and calls==1
        assert first.cooldown_remaining()>100
        result2=await second.request('GET','/',fail_fast_rate_limit=True)
        assert not result2.ok and result2.status_code==429 and calls==1
        assert 'повтор' in (result2.error or '')
    finally:
        await first.close(); await second.close()
