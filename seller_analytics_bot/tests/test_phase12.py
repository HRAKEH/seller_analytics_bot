from __future__ import annotations
import json
import logging
import sqlite3
from pathlib import Path

from app.storage import Database, Repository
from app.storage.database import (
    LATEST_SCHEMA_VERSION,_migration_1,_migration_2,_migration_3,_migration_4,
    _migration_5,_migration_6,_migration_7,
)
from app.services.health import build_health
from app.services.observability import SecretRedactionFilter


def _repo(tmp_path):
    db=Database(tmp_path/'bot.sqlite3'); assert db.initialize()==LATEST_SCHEMA_VERSION
    repo=Repository(db); seller=repo.ensure_seller(1); shop=repo.ensure_shop(seller.id)
    return db,repo,shop


def test_schema_v7_migrates_to_v8(tmp_path):
    path=tmp_path/'v7.sqlite3'
    conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
    for version,fn in ((1,_migration_1),(2,_migration_2),(3,_migration_3),(4,_migration_4),(5,_migration_5),(6,_migration_6),(7,_migration_7)):
        fn(conn); conn.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    conn.commit(); conn.close()
    db=Database(path)
    assert db.initialize()==LATEST_SCHEMA_VERSION
    with db.connect() as c:
        names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'runtime_leases','retry_jobs','process_heartbeats'} <= names


def test_runtime_lease_is_exclusive_and_owner_can_renew(tmp_path):
    db,repo,shop=_repo(tmp_path)
    assert repo.acquire_lease('x','instance-a',60)
    assert not repo.acquire_lease('x','instance-b',60)
    assert repo.renew_lease('x','instance-a',120)
    assert not repo.release_lease('x','instance-b')
    assert repo.release_lease('x','instance-a')
    assert repo.acquire_lease('x','instance-b',60)


def test_expired_lease_can_be_taken_over(tmp_path):
    db,repo,shop=_repo(tmp_path)
    assert repo.acquire_lease('x','old',60)
    with db.connect() as c:
        c.execute("UPDATE runtime_leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE lease_key='x'")
    assert repo.acquire_lease('x','new',60)
    assert repo.lease_info('x')['owner_id']=='new'


def test_retry_queue_claim_fail_requeue_complete(tmp_path):
    db,repo,shop=_repo(tmp_path)
    job_id=repo.enqueue_retry_job(shop.id,'daily','2026-01-01',{'day':'2026-01-01'},max_attempts=2)
    job=repo.claim_retry_job('worker-a')
    assert job and job['id']==job_id and job['payload']['day']=='2026-01-01'
    assert repo.fail_retry_job(job_id,'temporary',base_delay_seconds=0)=='pending'
    with db.connect() as c:
        c.execute("UPDATE retry_jobs SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE id=?",(job_id,))
    job=repo.claim_retry_job('worker-b'); assert job
    assert repo.fail_retry_job(job_id,'again',base_delay_seconds=0)=='dead'
    assert repo.requeue_retry_job(job_id)
    job=repo.claim_retry_job('worker-c'); assert job
    repo.complete_retry_job(job_id)
    assert repo.retry_job_counts()['success']==1


def test_heartbeat_is_upserted(tmp_path):
    db,repo,shop=_repo(tmp_path)
    repo.heartbeat('i-1',hostname='host',pid=10,metadata={'shops':1})
    repo.heartbeat('i-1',hostname='host',pid=10,metadata={'shops':2})
    rows=repo.recent_heartbeats()
    assert len(rows)==1
    assert json.loads(rows[0]['metadata_json'])['shops']==2


def test_safe_initialize_creates_pre_migration_backup(tmp_path):
    path=tmp_path/'v7.sqlite3'; backups=tmp_path/'backups'
    conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
    for version,fn in ((1,_migration_1),(2,_migration_2),(3,_migration_3),(4,_migration_4),(5,_migration_5),(6,_migration_6),(7,_migration_7)):
        fn(conn); conn.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    conn.commit(); conn.close()
    db=Database(path)
    assert db.initialize_safely(backups)==LATEST_SCHEMA_VERSION
    files=list(backups.glob('pre_migration_v7_*.sqlite3'))
    assert len(files)==1
    assert Database(files[0]).schema_version()==7


def test_secret_redaction_filter_rewrites_message():
    f=SecretRedactionFilter(['super-secret'])
    rec=logging.LogRecord('x',logging.INFO,__file__,1,'token=super-secret',(),None)
    assert f.filter(rec)
    assert 'super-secret' not in rec.getMessage()
    assert 'REDACTED' in rec.getMessage()


def test_health_report_ready_for_initialized_runtime(tmp_path):
    db,repo,shop=_repo(tmp_path)
    class Settings: instance_id='i';
    class Registry:
        repository=repo; settings=Settings(); maintenance_lock=type('L',(),{'locked':lambda self:False})()
        def contexts(self): return [object()]
    report=build_health(Registry(),deep=True)
    assert report['ready'] is True
    assert report['checks']['schema_version']==LATEST_SCHEMA_VERSION
    assert report['checks']['sqlite_quick_check']=='ok'

def test_safe_initialize_rolls_back_file_on_migration_failure(tmp_path, monkeypatch):
    import pytest
    import app.storage.database as dbmod
    path=tmp_path/'v8.sqlite3'; backups=tmp_path/'backups'
    conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
    from app.storage.database import _migration_8
    for version,fn in ((1,_migration_1),(2,_migration_2),(3,_migration_3),(4,_migration_4),(5,_migration_5),(6,_migration_6),(7,_migration_7),(8,_migration_8)):
        fn(conn); conn.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    conn.commit(); conn.close()
    def broken(conn):
        conn.execute('CREATE TABLE should_be_rolled_back(x INTEGER)')
        raise RuntimeError('boom')
    monkeypatch.setitem(dbmod.MIGRATIONS,999,broken)
    db=Database(path)
    with pytest.raises(RuntimeError,match='boom'):
        db.initialize_safely(backups)
    assert Database(path).schema_version()==8
    with Database(path).connect() as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='should_be_rolled_back'").fetchone() is None


def test_restore_does_not_resurrect_runtime_leases(tmp_path):
    from app.services.backups import BackupService
    db,repo,shop=_repo(tmp_path)
    repo.acquire_lease('singleton:telegram-poller','old-instance',90)
    repo.heartbeat('old-instance',hostname='old')
    service=BackupService(db,repo,tmp_path/'backups')
    backup=service.create(kind='manual')
    # Change current runtime state, then restore historical backup that contained old-instance.
    repo.release_lease('singleton:telegram-poller','old-instance')
    repo.acquire_lease('singleton:telegram-poller','current-instance',90)
    service.restore(backup.path)
    assert repo.lease_info('singleton:telegram-poller') is None
    assert repo.recent_heartbeats()==[]
