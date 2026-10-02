"""Capture financial posting fields before assigning them a money metric."""
import json
import sqlite3
from datetime import date, timedelta

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
        if request.url.path == '/v2/posting/fbo/get':
            assert body['with'] == {'financial_data': True}
            number = body['posting_number']
            row = next(p for p in expected['FBO'] if p['posting_number'] == number)
            seen.append(('FBO-details', int(number.rsplit('-', 1)[1])))
            return httpx.Response(200, json={'result': row})
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

    assert len(outcomes) == 3 and all(x.ok for x in outcomes)
    assert seen == [('FBO', 0), ('FBO', 1), ('FBO-details', 0), ('FBO-details', 1),
                    ('FBS', 0), ('FBS', 1)]
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

    expected_count = 2 if failed_scheme == 'FBO' else 3
    assert len(outcomes) == expected_count
    assert sum(x.ok for x in outcomes) == expected_count - 1
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


def detail_response(row, amount=550.12, currency='RUB'):
    return {'result': {
        'posting_number': row['posting_number'], 'status': row['status'],
        'created_at': '2026-09-29T23:00:00Z',
        'financial_data': {'products': [{
            'product_id': row['products'][0]['sku'], 'quantity': 1,
            'customer_price': amount, 'customer_currency_code': currency,
            'price': 1250, 'currency_code': 'RUB',
            'extension': {'future_field': ['retain', {'nanos': 250000000}]},
        }]},
    }, 'future_response_field': {'keep': True}}


def saved_payloads(repo, endpoint):
    with repo.db.connect() as c:
        rows = c.execute('''SELECT sr.id,sr.data_date,sr.status,rp.payload_json
            FROM source_runs sr JOIN raw_payloads rp ON rp.source_run_id=sr.id
            WHERE sr.endpoint=? ORDER BY sr.id''', (endpoint,)).fetchall()
    return [(r['id'], r['data_date'], r['status'], json.loads(r['payload_json'])) for r in rows]


@pytest.mark.asyncio
async def test_fbo_details_keep_full_responses_currency_zero_and_list_day_in_backup(tmp_path):
    repo, shop, conn = repository(tmp_path)
    rows = [posting('FBO', n, False) for n in range(3)]
    rows[0]['order_number'] = rows[1]['order_number'] = 'same-order'
    rows[1]['status'] = 'cancelled'
    rows[2]['created_at'] = rows[2]['in_process_at'] = '2026-10-01T21:05:00Z'
    replies = {rows[0]['posting_number']: detail_response(rows[0]),
               rows[1]['posting_number']: detail_response(rows[1], 0),
               rows[2]['posting_number']: detail_response(rows[2], 11.25, 'BYN')}
    get_calls = []

    async def handler(request):
        body = json.loads(request.content)
        if request.url.path == '/v3/posting/fbo/list':
            return httpx.Response(200, json={'postings': rows + [rows[0]], 'has_next': False})
        if request.url.path == '/v4/posting/fbs/list':
            return httpx.Response(200, json={'postings': [], 'has_next': False})
        assert request.url.path == '/v2/posting/fbo/get'
        assert body == {'posting_number': body['posting_number'], 'with': {'financial_data': True}}
        get_calls.append(body['posting_number'])
        return httpx.Response(200, json=replies[body['posting_number']])

    client = OzonClient('capture-fbo-details', 'test-key', min_interval=0,
                        transport=httpx.MockTransport(handler))
    try:
        outcomes = await CollectionService(repo, ozon=client).collect_ozon_fulfillment_range(
            shop_id=shop.id, connection_id=conn.id, start=DAY, end=DAY + timedelta(days=1))
    finally:
        await client.close()
    assert len(outcomes) == 6 and all(x.ok for x in outcomes)
    # Duplicate list entries share a lookup; sibling postings in one order do not.
    assert get_calls == [row['posting_number'] for row in rows]
    assert repo.metrics_for_day(conn.id, DAY.isoformat())['ordered_revenue'] == 10000
    assert repo.metrics_for_day(conn.id, DAY.isoformat())['marketplace_net'] == 4000.25
    backup = BackupService(repo.db, repo, tmp_path / 'backups').create()
    with sqlite3.connect(backup.path.as_uri() + '?mode=ro', uri=True) as c:
        saved = c.execute('''SELECT sr.data_date,sr.status,rp.payload_json
            FROM source_runs sr JOIN raw_payloads rp ON rp.source_run_id=sr.id
            WHERE sr.endpoint='postings/fbo/details' ORDER BY sr.data_date''').fetchall()
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert len(saved) == 2
    for (ds, status, raw), numbers in zip(saved, [get_calls[:2], get_calls[2:]]):
        payload = json.loads(raw)
        assert ds == (DAY if numbers == get_calls[:2] else DAY + timedelta(days=1)).isoformat()
        assert status == 'success' and payload['complete'] is True
        assert payload['request_endpoint'] == '/v2/posting/fbo/get'
        assert payload['request_with'] == {'financial_data': True}
        assert payload['expected_postings'] == payload['requested_postings'] == numbers
        assert payload['errors'] == []
        assert payload['responses'] == [
            {'posting_number': n, 'http_status': 200, 'response': replies[n]} for n in numbers]
    # Detail capture retains customer zero/BYN prices, rather than generating RUB totals.
    assert all('financial_data' in p and p['financial_data'] is None
               for _, _, _, raw in saved_payloads(repo, 'postings/fbo') for p in raw['postings'])


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['http_error', 'missing_result', 'wrong_number'])
async def test_partial_fbo_details_keep_good_responses_old_snapshot_and_fbs(tmp_path, failure):
    repo, shop, conn = repository(tmp_path)
    old = {'previous': 'complete FBO detail snapshot'}
    old_run = repo.record_success(conn.id, 'postings/fbo/details', DAY.isoformat(), old, [])
    rows = [posting('FBO', n, False) for n in range(3)]
    # Every row belongs to the same Moscow day.
    rows[2]['created_at'] = rows[2]['in_process_at'] = '2026-10-01T12:00:00Z'
    calls = []
    replies = {r['posting_number']: detail_response(r) for r in rows}

    async def handler(request):
        if request.url.path.endswith('/list'):
            return httpx.Response(200, json={'postings': rows if '/fbo/' in request.url.path else [],
                                            'has_next': False})
        assert request.url.path == '/v2/posting/fbo/get'
        number = json.loads(request.content)['posting_number']
        calls.append(number)
        if number == rows[1]['posting_number']:
            if failure == 'http_error':
                return httpx.Response(400, json={'message': 'Unavailable for test-key'})
            return httpx.Response(200, json={'result': None} if failure == 'missing_result'
                                  else {'result': {'posting_number': 'different-posting'}})
        return httpx.Response(200, json=replies[number])

    client = OzonClient(f'capture-fbo-partial-{failure}', 'test-key', min_interval=0,
                        transport=httpx.MockTransport(handler))
    try:
        outcomes = await CollectionService(repo, ozon=client).collect_ozon_fulfillment_range(
            shop_id=shop.id, connection_id=conn.id, start=DAY, end=DAY)
    finally:
        await client.close()
    assert len(outcomes) == 3 and [o.ok for o in outcomes] == [True, False, True]
    assert calls == [r['posting_number'] for r in rows]
    saved = saved_payloads(repo, 'postings/fbo/details')
    assert saved[0] == (old_run, DAY.isoformat(), 'success', old)
    _, ds, status, payload = saved[-1]
    assert ds == DAY.isoformat() and status == 'partial' and payload['complete'] is False
    assert payload['responses'] == [
        {'posting_number': r['posting_number'], 'http_status': 200,
         'response': replies[r['posting_number']]} for r in (rows[0], rows[2])]
    assert len(payload['errors']) == 1
    error = payload['errors'][0]
    assert error['posting_number'] == rows[1]['posting_number'] and error['skipped'] is False
    assert error['http_status'] == (400 if failure == 'http_error' else 200)
    assert 'test-key' not in json.dumps(payload)
    with repo.db.connect() as c:
        failed = c.execute("SELECT status,http_status FROM source_runs WHERE endpoint='postings/fbo/get'").fetchall()
        latest_success = c.execute("SELECT id FROM source_runs WHERE endpoint='postings/fbo/details' AND status='success' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert [tuple(r) for r in failed] == [('failed', error['http_status'])]
    assert latest_success == old_run
    assert saved_payloads(repo, 'postings/fbo')[-1][2] == 'success'
    assert saved_payloads(repo, 'postings/fbs')[-1][2] == 'success'
    assert repo.metrics_for_day(conn.id, DAY.isoformat())['marketplace_net'] == 4000.25


@pytest.mark.asyncio
async def test_fbo_access_error_stops_remaining_detail_requests_across_days(tmp_path):
    repo, shop, conn = repository(tmp_path)
    rows = [posting('FBO', n, False) for n in range(3)]
    rows[2]['created_at'] = rows[2]['in_process_at'] = '2026-10-01T21:05:00Z'
    calls = []

    async def handler(request):
        if request.url.path.endswith('/list'):
            return httpx.Response(200, json={'postings': rows if '/fbo/' in request.url.path else [],
                                            'has_next': False})
        number = json.loads(request.content)['posting_number']; calls.append(number)
        return (httpx.Response(200, json=detail_response(rows[0])) if number == rows[0]['posting_number']
                else httpx.Response(403, json={'message': 'Forbidden'}))

    client = OzonClient('capture-fbo-forbidden', 'test-key', min_interval=0,
                        transport=httpx.MockTransport(handler))
    try:
        outcomes = await CollectionService(repo, ozon=client).collect_ozon_fulfillment_range(
            shop_id=shop.id, connection_id=conn.id, start=DAY, end=DAY + timedelta(days=1))
    finally:
        await client.close()
    assert calls == [r['posting_number'] for r in rows[:2]]
    assert sum(not o.ok for o in outcomes) == 2
    saved = saved_payloads(repo, 'postings/fbo/details')
    assert [r[2] for r in saved] == ['partial', 'partial']
    skipped = saved[-1][3]['errors'][0]
    assert skipped['posting_number'] == rows[2]['posting_number']
    assert skipped['skipped'] is True and skipped['http_status'] is None
    assert skipped['blocked_http_status'] == 403
    assert saved[-1][3]['responses'] == []
    assert saved[-1][3]['expected_postings'] == [rows[2]['posting_number']]
    assert saved[-1][3]['requested_postings'] == []


@pytest.mark.asyncio
async def test_empty_fbo_day_saves_complete_empty_details_without_get_requests(tmp_path):
    repo, shop, conn = repository(tmp_path)
    repo.record_success(conn.id, 'postings/fbo/details', DAY.isoformat(), {'previous': True}, [])

    async def handler(request):
        assert request.url.path in {'/v3/posting/fbo/list', '/v4/posting/fbs/list'}
        return httpx.Response(200, json={'postings': [], 'has_next': False})

    client = OzonClient('capture-fbo-empty', 'test-key', min_interval=0,
                        transport=httpx.MockTransport(handler))
    try:
        outcomes = await CollectionService(repo, ozon=client).collect_ozon_fulfillment_range(
            shop_id=shop.id, connection_id=conn.id, start=DAY, end=DAY)
    finally:
        await client.close()
    assert len(outcomes) == 3 and all(o.ok for o in outcomes)
    payload = saved_payloads(repo, 'postings/fbo/details')[-1][3]
    assert payload['complete'] is True
    assert payload['expected_postings'] == payload['requested_postings'] == payload['responses'] == payload['errors'] == []


@pytest.mark.asyncio
async def test_missing_fbo_number_is_reported_without_inventing_a_lookup(tmp_path):
    repo, shop, conn = repository(tmp_path)
    row = posting('FBO', 0, False); row.pop('posting_number')

    async def handler(request):
        assert request.url.path.endswith('/list')
        return httpx.Response(200, json={'postings': [row] if '/fbo/' in request.url.path else [],
                                        'has_next': False})

    client = OzonClient('capture-fbo-missing-number', 'test-key', min_interval=0,
                        transport=httpx.MockTransport(handler))
    try:
        outcomes = await CollectionService(repo, ozon=client).collect_ozon_fulfillment_range(
            shop_id=shop.id, connection_id=conn.id, start=DAY, end=DAY)
    finally:
        await client.close()
    assert [o.ok for o in outcomes] == [True, False, True]
    payload = saved_payloads(repo, 'postings/fbo/details')[-1][3]
    assert payload['complete'] is False and payload['responses'] == []
    assert payload['errors'][0]['posting_number'] is None
    assert payload['errors'][0]['skipped'] is True
