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


def test_rename_shop_rejects_duplicate_name(repo):
    seller = repo.ensure_seller(3003, 'Duplicate Seller')
    first = repo.ensure_shop(seller.id, 'Shop A')
    second = repo.ensure_shop(seller.id, 'Shop B')

    with pytest.raises(ValueError, match='уже существует'):
        repo.rename_shop(second.id, 'Shop A')

    assert repo.get_shop(first.id).name == 'Shop A'
    assert repo.get_shop(second.id).name == 'Shop B'


def test_shop_archive_restore_and_delete_lifecycle(repo):
    seller = repo.ensure_seller(4004, 'Lifecycle Seller')
    keep = repo.ensure_shop(seller.id, 'Keep')
    old = repo.ensure_shop(seller.id, 'Old')
    repo.ensure_shop_preferences(old.id)
    repo.grant_shop_access(4004, old.id, 'owner')
    repo.select_shop_for_user(4004, old.id)
    conn = repo.ensure_connection(old.id, 'ozon', 'Ozon')
    repo.record_success(conn.id, 'analytics/orders', '2026-09-29', {'x': 1}, [])

    archived = repo.archive_shop(seller.id, old.id)
    assert archived.active is False
    assert repo.selected_shop_for_user(4004, None) is None
    assert [x.id for x in repo.list_shops(seller.id)] == [keep.id]
    assert [x.id for x in repo.archived_shops(seller.id)] == [old.id]

    restored = repo.restore_shop(seller.id, old.id)
    assert restored.active is True
    assert {x.id for x in repo.list_shops(seller.id)} == {keep.id, old.id}

    repo.archive_shop(seller.id, old.id)
    deleted = repo.delete_archived_shop(seller.id, old.id)
    assert deleted.id == old.id
    assert repo.get_shop(old.id) is None
    assert repo.count('marketplace_connections') == 0


def test_shop_lifecycle_guards_last_active_and_active_delete(repo):
    seller = repo.ensure_seller(5005, 'Guard Seller')
    only = repo.ensure_shop(seller.id, 'Only')

    with pytest.raises(ValueError, match='последний активный'):
        repo.archive_shop(seller.id, only.id)
    with pytest.raises(ValueError, match='Сначала архивируйте'):
        repo.delete_archived_shop(seller.id, only.id)
