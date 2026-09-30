from pathlib import Path
import pytest
from app.storage import Database, Repository, MetricPoint, LATEST_SCHEMA_VERSION

@pytest.fixture
def repo(tmp_path: Path):
    database = Database(tmp_path / 'db.sqlite3')
    assert database.initialize() == LATEST_SCHEMA_VERSION
    assert database.integrity_check()
    return Repository(database)

@pytest.fixture
def connection(repo):
    seller = repo.ensure_seller(1001, 'Test Seller')
    shop = repo.ensure_shop(seller.id, 'Shop A')
    return repo.ensure_connection(shop.id, 'ozon', 'Ozon main')

def point(connection_id, day='2026-09-28', value=100.0):
    return MetricPoint(connection_id, day, 'revenue', value, 'RUB', True, '2026-09-29T08:00:00+00:00')

def test_multi_shop_model(repo):
    seller = repo.ensure_seller(777, 'Seller')
    a = repo.ensure_shop(seller.id, 'A')
    b = repo.ensure_shop(seller.id, 'B')
    ca = repo.ensure_connection(a.id, 'wildberries', 'WB')
    cb = repo.ensure_connection(b.id, 'wildberries', 'WB')
    assert a.id != b.id and ca.id != cb.id
    assert repo.count('shops') == 2

def test_same_success_payload_is_idempotent(repo, connection):
    raw = {'rows': [1,2,3]}
    rid1 = repo.record_success(connection.id, 'analytics', '2026-09-28', raw, [point(connection.id)])
    rid2 = repo.record_success(connection.id, 'analytics', '2026-09-28', raw, [point(connection.id)])
    assert rid1 == rid2
    assert repo.count('source_runs') == 1
    assert repo.count('metric_values') == 1
    assert repo.count('raw_payloads') == 1

def test_failure_does_not_shadow_last_success(repo, connection):
    day = '2026-09-28'
    repo.record_success(connection.id, 'analytics', day, {'v': 1}, [point(connection.id, day, 100)])
    repo.record_failure(connection.id, 'analytics', day, 'HTTP 500', http_status=500, attempts=4)
    latest = repo.latest_metric(connection.id, day, 'revenue')
    assert latest['value'] == 100
    assert repo.count('source_runs') == 2
    assert repo.count('metric_values') == 1

def test_new_success_becomes_latest(repo, connection):
    day = '2026-09-28'
    repo.record_success(connection.id, 'analytics', day, {'v': 1}, [point(connection.id, day, 100)])
    repo.record_success(connection.id, 'analytics', day, {'v': 2}, [point(connection.id, day, 125)])
    latest = repo.latest_metric(connection.id, day, 'revenue')
    assert latest['value'] == 125
    assert repo.metrics_for_day(connection.id, day)['revenue'] == 125

def test_wrong_metric_unit_rejected(repo, connection):
    bad = MetricPoint(connection.id, '2026-09-28', 'revenue', 10, 'units')
    with pytest.raises(ValueError, match='Unexpected unit'):
        repo.record_success(connection.id, 'analytics', '2026-09-28', {'x': 1}, [bad])
    assert repo.count('source_runs') == 0

def test_metric_cannot_leak_to_another_connection(repo, connection):
    seller = repo.ensure_seller(2002, 'Other')
    shop = repo.ensure_shop(seller.id, 'Other shop')
    other = repo.ensure_connection(shop.id, 'ozon', 'Other Ozon')
    leaked = MetricPoint(other.id, '2026-09-28', 'revenue', 10, 'RUB')
    with pytest.raises(ValueError, match='another connection'):
        repo.record_success(connection.id, 'analytics', '2026-09-28', {'x': 1}, [leaked])

def test_last_success_ignores_failure(repo, connection):
    repo.record_success(connection.id, 'analytics', '2026-09-27', {'x': 1}, [point(connection.id, '2026-09-27')])
    repo.record_failure(connection.id, 'analytics', '2026-09-28', 'timeout')
    run = repo.last_successful_run(connection.id, 'analytics')
    assert run is not None and run.data_date == '2026-09-27'

def test_initialize_is_repeatable(tmp_path):
    database = Database(tmp_path / 'repeat.sqlite3')
    assert database.initialize() == LATEST_SCHEMA_VERSION
    assert database.initialize() == LATEST_SCHEMA_VERSION
    assert database.integrity_check()
