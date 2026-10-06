"""Optional Ozon reports have distinct entitlement and throttling failures."""
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.integrations import FetchResult, OzonClient
from app.reports.cards import format_daily_card
from app.reports.daily import build_daily_report
from app.services.collection import CollectionService
from app.services.daily_events import build_daily_events, OZON_REALIZATION
from app.services.ozon_buyouts import build_buyout_check, format_buyout_check
from app.services.ozon_dates import MOSCOW
from test_ozon_buyouts import fixture, capture, row, DAY
from test_daily_events import setup, realization


def no_buyouts(repo, conn, posting):
    posting=deepcopy(posting)
    posting['products'][0]['is_marketplace_buyout']=False
    repo.record_success(conn.id,'postings/fbs',DAY.isoformat(),{'postings':[posting]},[])


@pytest.mark.asyncio
async def test_proven_absence_skips_api_and_ignores_obsolete_buyout_failure(tmp_path):
    repo,shop,conn,posting=fixture(tmp_path)
    no_buyouts(repo,conn,posting)
    repo.record_failure(conn.id,'finance/products/buyout',DAY.isoformat(),'Rate limited',http_status=429)
    client=SimpleNamespace(finance_products_buyout=AsyncMock())
    service=CollectionService(repo,ozon=client)
    assert await service.collect_ozon_buyout_prices_range(connection_id=conn.id,start=DAY,end=DAY,as_of=DAY)==[]
    client.finance_products_buyout.assert_not_awaited()
    check=build_buyout_check(repo,conn.id,DAY)
    assert not check.report_required and not check.warnings
    assert check.expected_units==0 and not check.matches
    text=format_daily_card(repo,shop.id,build_daily_report(repo,shop.id,DAY))
    assert 'Отдельный отчёт не требуется' in text.details_html
    assert 'Последний запрос выкупов' not in text.details_html
    assert 'Отчёт о выкупах пока' not in text.details_html
    assert 'Цена выкупа —' not in text.details_html
    assert repo.metrics_for_day(conn.id,DAY.isoformat())['ordered_units']==1


@pytest.mark.asyncio
@pytest.mark.parametrize('problem',['missing_flag','partial_postings','newer_posting_failure'])
async def test_incomplete_postings_never_prove_buyout_absence(tmp_path,problem):
    repo,_,conn,posting=fixture(tmp_path)
    posting=deepcopy(posting)
    posting['products'][0]['is_marketplace_buyout']=False
    if problem=='missing_flag':posting['products'][0].pop('is_marketplace_buyout')
    repo.record_success(conn.id,'postings/fbs',DAY.isoformat(),{'postings':[posting]},[],
                        status='partial' if problem=='partial_postings' else 'success')
    if problem=='newer_posting_failure':
        repo.record_failure(conn.id,'postings/fbs',DAY.isoformat(),'offline')
    client=SimpleNamespace(finance_products_buyout=AsyncMock(return_value=FetchResult.success('ozon',{'products':[]},200,1)))
    await CollectionService(repo,ozon=client).collect_ozon_buyout_prices_range(
        connection_id=conn.id,start=DAY,end=DAY,as_of=DAY)
    client.finance_products_buyout.assert_awaited_once()
    assert not build_buyout_check(repo,conn.id,DAY).postings_complete


@pytest.mark.asyncio
async def test_buyout_429_stops_remaining_periods_and_keeps_saved_price(tmp_path):
    repo,_,conn,_=fixture(tmp_path)
    capture(repo,conn,[row()])
    client=SimpleNamespace(finance_products_buyout=AsyncMock(return_value=FetchResult.failure('ozon','Rate limited',429,1)))
    await CollectionService(repo,ozon=client).collect_ozon_buyout_prices_range(
        connection_id=conn.id,start=DAY,end=DAY,as_of=DAY+timedelta(days=60))
    client.finance_products_buyout.assert_awaited_once()
    check=build_buyout_check(repo,conn.id,DAY)
    assert check.matches[0].price==345
    warnings=' '.join(check.warnings)
    assert '429' in warnings and 'проверьте доступ' not in warnings
    assert 'сохранённые цены' in warnings


@pytest.mark.asyncio
async def test_buyout_transport_stops_retries_and_does_not_block_ordinary_finance():
    paths=[]
    def respond(request):
        paths.append(request.url.path)
        if request.url.path=='/v1/finance/products/buyout':
            return httpx.Response(429,headers={'Retry-After':'600'},json={'message':'limit'})
        assert request.url.path=='/v1/finance/accrual/by-day'
        return httpx.Response(200,json={'accruals':[]})
    client=OzonClient('optional-buyout-rate-test','test-key',min_interval=0,max_retries=3,
                      transport=httpx.MockTransport(respond))
    try:
        first=await client.finance_products_buyout('2026-10-01','2026-10-03')
        second=await client.finance_products_buyout('2026-10-01','2026-10-03')
        finance=await client.finance_accrual_by_day('2026-10-01')
    finally:
        await client.close()
    assert first.status_code==429 and first.attempts==1
    assert second.status_code==429 and second.attempts==0
    assert finance.ok
    assert paths==['/v1/finance/products/buyout','/v1/finance/accrual/by-day']


@pytest.mark.asyncio
async def test_manual_access_recheck_recovers_without_restart_and_remembers_real_denial(tmp_path):
    repo,_,conn=setup(tmp_path,'ozon')
    start=datetime.now(MOSCOW).date()-timedelta(days=2);end=start+timedelta(days=1)
    denied=FetchResult.failure('ozon','Data is available only with a Premium plus subscription',403,1)
    client=SimpleNamespace(realization_day=AsyncMock(return_value=denied),
        customer_returns_all=AsyncMock(return_value=FetchResult.success('ozon',{'returns':[]},200,1)))
    service=CollectionService(repo,ozon=client)
    await service.collect_daily_events_range(start=start,end=end,ozon_connection_id=conn.id)
    await service.collect_daily_events_range(start=start,end=end,ozon_connection_id=conn.id)
    client.realization_day.assert_awaited_once()
    cached=repo.latest_run(conn.id,OZON_REALIZATION,end.isoformat())
    assert 'Premium plus subscription' in cached.error and cached.attempts==0
    blocked=build_daily_events(repo,conn.id,'ozon',end).buyouts
    assert blocked.units is None and 'Premium Plus' in blocked.unavailable_note
    client.realization_day.return_value=FetchResult.success('ozon',realization('RUB'),200,1)
    await service.collect_daily_events_range(start=start,end=end,ozon_connection_id=conn.id,recheck_access=True)
    assert client.realization_day.await_count==3
    assert OZON_REALIZATION not in service._event_disabled
    assert OZON_REALIZATION not in service._event_denials
    restored=build_daily_events(repo,conn.id,'ozon',end).buyouts
    assert restored.units==2 and not restored.load_failed and restored.unavailable_note is None


@pytest.mark.asyncio
async def test_manual_recheck_probes_denied_source_once_per_period(tmp_path):
    repo,_,conn=setup(tmp_path,'ozon')
    end=datetime.now(MOSCOW).date()-timedelta(days=1);start=end-timedelta(days=3)
    client=SimpleNamespace(realization_day=AsyncMock(return_value=FetchResult.failure('ozon','Premium subscription required',403,1)),
        customer_returns_all=AsyncMock(return_value=FetchResult.success('ozon',{'returns':[]},200,1)))
    service=CollectionService(repo,ozon=client)
    for _ in range(2):
        await service.collect_daily_events_range(start=start,end=end,ozon_connection_id=conn.id,recheck_access=True)
    assert client.realization_day.await_count==2
    events=build_daily_events(repo,conn.id,'ozon',end)
    assert events.returns.units==0 and events.cancellations.units is None
    assert events.cancellations.unavailable_note=='⏳ дата отмены не получена'


def test_known_entitlement_denial_does_not_claim_saved_buyout_values(tmp_path):
    repo,shop,conn,posting=fixture(tmp_path)
    no_buyouts(repo,conn,posting)
    repo.record_failure(conn.id,OZON_REALIZATION,DAY.isoformat(),
                        'Data is available only with a Premium plus subscription',http_status=403)
    card=format_daily_card(repo,shop.id,build_daily_report(repo,shop.id,DAY))
    assert '✅ Выкуплено: ⏳ нужна Premium Plus' in card.summary_html
    assert '❌ Отменено: ⏳ дата отмены не получена' in card.summary_html
    assert 'Есть сбой загрузки' not in card.summary_html
    assert 'показаны сохранённые данные' not in card.details_html.lower()
