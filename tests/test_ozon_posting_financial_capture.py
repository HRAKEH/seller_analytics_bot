"""Capture financial posting fields before assigning them a money metric."""
import json
import sqlite3
from datetime import date

import httpx
import pytest

from app.integrations.ozon import OzonClient
from app.services.backups import BackupService
from app.services.collection import CollectionService
from app.storage import Database, MetricPoint, Repository


DAY = date(2026, 10, 1)


def repository(tmp_path):
    db = Database(tmp_path / 'capture.sqlite3')
    db.initialize()
    repo = Repository(db)
    shop = repo.ensure_shop(repo.ensure_seller(1).id)
    conn = repo.ensure_connection(shop.id, 'ozon', 'Ozon')
    repo.record_success(conn.id, 'analytics/orders', DAY.isoformat(), {'orders': 7}, [
        MetricPoint(conn.id, DAY.isoformat(), 'ordered_units', 7, 'units'),
        MetricPoint(conn.id, DAY.isoformat(), 'ordered_revenue', 10000, 'RUB'),
    ])
    repo.record_success(conn.id, 'finance/accrual/by-day', DAY.isoformat(), {'net': '4000.25'}, [
        MetricPoint(conn.id, DAY.isoformat(), 'marketplace_net', 4000.25, 'RUB'),
    ])
    return repo, shop, conn


def posting(scheme, page, financial):
    # Money types and unknown financial fields must survive without a new
    # normalizer. This fixture is not evidence of any live API price meaning.
    sku = (9000 if scheme == 'FBO' else 9100) + page
    stamp = '2026-09-30T22:30:00Z' if page == 0 else '2026-10-01T20:55:00Z'
    return {
        'posting_number': f'{scheme}-{page}',
        'created_at': stamp,
        'in_process_at': stamp,
        'status': 'delivering',
        'products': [{'sku': sku, 'quantity': 1, 'name': 'Test product',
                      'price': {'amount': '1250.00', 'currency': 'RUB'}}],
        'financial_data': ({
            'products': [{'product_id': sku, 'quantity': 1,
                          'customer_price': '550.1200', 'payout': '412.3400',
                          'extension': {'units': '10', 'nanos': 123456789}}],
            'cluster_from': 'Source', 'cluster_to': 'Destination',
        } if financial else None),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('financial', [True, False])
async def test_financial_postings_survive_all_pages_day_partition_and_backup(tmp_path, financial):
    repo, shop, conn = repository(tmp_path)
    seen = []
    expected = {s: [posting(s, page, financial) for page in range(2)] for s in ('FBO', 'FBS')}

    async def handler(request):
        body = json.loads(request.content)
        scheme = 'FBO' if request.url.path == '/v3/posting/fbo/list' else 'FBS'
        assert request.url.path in {'/v3/posting/fbo/list', '/v4/posting/fbs/list'}
        assert body['filter'] == {'since': '2026-09-30T21:00:00.000Z',
                                  'to': '2026-10-01T20:59:59.999Z'}
        page = 0 if body['cursor'] == '' else 1
        assert body['cursor'] in {'', 'next'}
        seen.append((scheme, page))
        row = posting(scheme, page, financial and body.get('with', {}).get('financial_data') is True)
        response = {'postings': [row], 'has_next': page == 0, 'cursor': 'next' if page == 0 else ''}
        return httpx.Response(200, json=response if scheme == 'FBO' else {'result': response})

    client = OzonClient(f'capture-{financial}', 'test-key', min_interval=0,
                        transport=httpx.MockTransport(handler))
    try:
        outcomes = await CollectionService(repo, ozon=client).collect_ozon_fulfillment_range(
            shop_id=shop.id, connection_id=conn.id, start=DAY, end=DAY)
    finally:
        await client.close()

    assert len(outcomes) == 2 and all(x.ok for x in outcomes)
    assert seen == [('FBO', 0), ('FBO', 1), ('FBS', 0), ('FBS', 1)]
    metrics = repo.metrics_for_day(conn.id, DAY.isoformat())
    assert metrics['ordered_units'] == 7 and metrics['ordered_revenue'] == 10000
    assert metrics['marketplace_net'] == 4000.25
    backup = BackupService(repo.db, repo, tmp_path / 'backups').create()
    with sqlite3.connect(backup.path.as_uri() + '?mode=ro', uri=True) as c:
        rows = c.execute('''SELECT sr.endpoint, sr.data_date, rp.payload_json
            FROM source_runs sr JOIN raw_payloads rp ON rp.source_run_id=sr.id
            WHERE sr.connection_id=? AND sr.endpoint IN ('postings/fbo','postings/fbs')
              AND sr.status='success' ORDER BY sr.id''', (conn.id,)).fetchall()
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert len(rows) == 2
    for endpoint, data_date, raw in rows:
        scheme = endpoint.rsplit('/', 1)[1].upper()
        payload = json.loads(raw)
        assert data_date == DAY.isoformat()
        assert payload['postings'] == expected[scheme]
        assert payload['scheme'] == scheme
        assert payload['request_with'] == {'financial_data': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_scheme', ['FBO', 'FBS'])
async def test_failed_financial_page_keeps_last_successful_snapshot(tmp_path, failed_scheme):
    repo, shop, conn = repository(tmp_path)
    endpoint = f'postings/{failed_scheme.lower()}'
    old = {'postings': [posting(failed_scheme, 0, True)], 'scheme': failed_scheme}
    old_run = repo.record_success(conn.id, endpoint, DAY.isoformat(), old, [])

    async def handler(request):
        body = json.loads(request.content)
        scheme = 'FBO' if request.url.path == '/v3/posting/fbo/list' else 'FBS'
        if scheme != failed_scheme:
            return httpx.Response(200, json={'postings': [], 'has_next': False, 'cursor': ''})
        if body['cursor']:
            return httpx.Response(400, json={'message': 'Financial page unavailable'})
        return httpx.Response(200, json={'postings': [posting(scheme, 1, True)],
                                        'has_next': True, 'cursor': 'next'})

    client = OzonClient(f'capture-failure-{failed_scheme}', 'test-key', min_interval=0,
                        transport=httpx.MockTransport(handler))
    try:
        outcomes = await CollectionService(repo, ozon=client).collect_ozon_fulfillment_range(
            shop_id=shop.id, connection_id=conn.id, start=DAY, end=DAY)
    finally:
        await client.close()

    assert len(outcomes) == 2
    assert sum(x.ok for x in outcomes) == 1
    with repo.db.connect() as c:
        runs = c.execute('SELECT id,status,http_status FROM source_runs WHERE endpoint=? ORDER BY id',
                         (endpoint,)).fetchall()
        raw = c.execute('SELECT payload_json FROM raw_payloads WHERE source_run_id=?',
                        (old_run,)).fetchone()[0]
    failed = next(x for x in outcomes if not x.ok)
    assert [(r['id'], r['status']) for r in runs] == [(old_run, 'success'), (failed.run_id, 'failed')]
    assert runs[-1]['http_status'] == 400
    assert json.loads(raw) == old
    assert repo.metrics_for_day(conn.id, DAY.isoformat())['ordered_revenue'] == 10000
