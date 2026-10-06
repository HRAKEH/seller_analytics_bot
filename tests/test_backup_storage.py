"""Retention must save disk space without losing the final usable recovery point."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import zipfile

import pytest

from app.config import Settings
from app.services import backup_transport as transport
from app.services.backup_files import unpack_backup
from app.services.backups import BackupService, _sha256, preserve_backup_file
from app.services.scheduler import backup_storage_maintenance_once
from test_navigation import ui, press


def configured(ui, **changes):
    return BackupService.from_settings(ui.repo.db,ui.repo,replace(ui.ctx.settings,**changes))


def snapshot(ui, *, age_days=0, kind='manual'):
    stamp=datetime.now(timezone.utc).timestamp()-age_days*86400
    name=f"sellerbot_{datetime.fromtimestamp(stamp,timezone.utc):%Y%m%dT%H%M%S%fZ}_{kind}.sqlite3"
    service=BackupService(ui.repo.db,ui.repo,ui.repo.db.path.parent/'backups')
    result=service.create(kind=kind,destination=service.directory/name)
    os.utime(result.path,(stamp,stamp))
    return result


def test_default_and_custom_storage_settings(monkeypatch):
    for name in ('BACKUP_COMPRESS','BACKUP_RETENTION_DAYS','BACKUP_MAX_COUNT','BACKUP_MAX_TOTAL_MB'):
        monkeypatch.delenv(name,raising=False)
    settings=Settings.from_env()
    assert (settings.backup_compress,settings.backup_retention_days,
            settings.backup_max_count,settings.backup_max_total_mb)==(True,7,10,512)
    monkeypatch.setenv('BACKUP_COMPRESS','false')
    monkeypatch.setenv('BACKUP_RETENTION_DAYS','3')
    monkeypatch.setenv('BACKUP_MAX_COUNT','2')
    monkeypatch.setenv('BACKUP_MAX_TOTAL_MB','64')
    settings=Settings.from_env()
    assert (settings.backup_compress,settings.backup_retention_days,
            settings.backup_max_count,settings.backup_max_total_mb)==(False,3,2,64)


def test_stored_zip_preserves_database_and_records_archive_checksum(ui,tmp_path):
    with ui.repo.db.connect() as live:
        live.execute('CREATE TABLE backup_probe (payload BLOB)')
        live.execute('INSERT INTO backup_probe VALUES (zeroblob(?))',(4*1024*1024,))
    result=configured(ui).create()
    assert result.path.suffix=='.zip' and result.size_bytes<result.original_size_bytes
    assert not result.path.with_suffix('.sqlite3').exists()
    assert result.path.stat().st_mode&0o777==0o600
    assert _sha256(result.path)==result.checksum
    row=ui.repo.recent_backups()[0]
    assert (row['filename'],row['checksum'],row['size_bytes'])==(result.path.name,result.checksum,result.size_bytes)
    raw=unpack_backup(result.path,tmp_path/'unpacked')
    with sqlite3.connect(raw) as saved:
        assert saved.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert saved.execute('SELECT length(payload) FROM backup_probe').fetchone()[0]==4*1024*1024
        assert saved.execute('SELECT name FROM shops WHERE id=?',(ui.shop.id,)).fetchone()[0]=='Shop'
    assert raw.stat().st_size==result.original_size_bytes


@pytest.mark.asyncio
async def test_stored_zip_is_sent_directly_and_uses_correct_size_and_checksum(ui,monkeypatch):
    result=configured(ui).create()
    def no_second_zip(*args,**kwargs):
        raise AssertionError('Already compressed backups must not be compressed again')
    monkeypatch.setattr(zipfile.ZipFile,'write',no_second_zip)
    async with transport.backup_upload(result.path) as upload:
        assert upload.path==result.path and upload.compressed
        caption=transport.backup_caption(result,upload)
        assert f'SHA-256 файла ZIP: {result.checksum}' in caption
        assert f'{result.original_size_bytes/1_000_000:.1f} МБ до сжатия' in caption
        monkeypatch.setattr(transport,'MAX_RESTORE_DATABASE_SIZE',result.original_size_bytes-1)
        assert '/restore её не примет' in transport.backup_caption(result,upload)
    assert result.path.exists()


def test_legacy_conversion_updates_history_and_preserves_age(ui,tmp_path):
    old=snapshot(ui,age_days=4,kind='automatic')
    row_before=ui.repo.recent_backups()[0]
    mtime=old.path.stat().st_mtime_ns
    outcome=configured(ui).maintain()
    archive=old.path.with_suffix('.zip')
    row_after=ui.repo.recent_backups()[0]
    assert outcome.compressed==1 and outcome.files==1 and outcome.removed==0
    assert not old.path.exists() and archive.stat().st_mtime_ns==mtime
    assert (row_after['id'],row_after['created_at'])==(row_before['id'],row_before['created_at'])
    assert row_after['filename']==archive.name and row_after['checksum']==_sha256(archive)
    assert row_after['size_bytes']==archive.stat().st_size
    assert _sha256(unpack_backup(archive,tmp_path/'unpacked'))==old.checksum
    restored_result=configured(ui).result_from_history(row_after)
    assert restored_result.original_size_bytes==old.size_bytes


def test_pre_migration_copy_is_managed_without_touching_other_files(ui):
    old=snapshot(ui)
    migration=old.path.with_name('pre_migration_v20_20261005T120000123456Z.sqlite3')
    migration.write_bytes(old.path.read_bytes())
    other=old.path.parent/'keep-my-copy.sqlite3'
    other.write_bytes(b'personal file')
    linked=old.path.parent/'sellerbot_20260901T120000123456Z_manual.sqlite3'
    linked.symlink_to(ui.repo.db.path)
    configured(ui).maintain()
    assert not migration.exists() and migration.with_suffix('.zip').exists()
    assert other.read_bytes()==b'personal file' and linked.is_symlink()
    assert ui.repo.get_shop(ui.shop.id).name=='Shop' and ui.repo.db.integrity_check()


@pytest.mark.parametrize('failure',['write','history','conflicting_zip','changed_checksum'])
def test_failed_conversion_preserves_legacy_file(ui,monkeypatch,failure):
    old=snapshot(ui,age_days=10)
    original_bytes=old.path.read_bytes()
    archive=old.path.with_suffix('.zip')
    if failure=='write':
        def fail(*args,**kwargs):
            raise OSError('disk full')
        monkeypatch.setattr(zipfile.ZipFile,'write',fail)
    elif failure=='history':
        def fail(*args,**kwargs):
            raise OSError('history update failed')
        monkeypatch.setattr(ui.repo,'replace_backup_file',fail)
    elif failure=='conflicting_zip':
        with zipfile.ZipFile(archive,'w') as zipped:
            zipped.writestr('other.db',b'other database')
        conflicting_bytes=archive.read_bytes()
    else:
        with ui.repo.db.connect() as live:
            live.execute('UPDATE backup_history SET checksum=?',('0'*64,))
    configured(ui,backup_max_count=1).maintain()
    assert old.path.read_bytes()==original_bytes
    assert ui.repo.recent_backups()[0]['filename']==old.path.name
    if failure=='conflicting_zip' and archive.exists():
        assert archive.read_bytes()==conflicting_bytes
    assert not list(old.path.parent.glob('*.tmp'))


def test_age_retention_keeps_recent_copy_and_final_old_recovery_point(ui):
    old=snapshot(ui,age_days=10)
    recent=snapshot(ui,age_days=3)
    service=configured(ui,backup_compress=False)
    assert service.prune(7)==1
    assert not old.path.exists() and recent.path.exists()
    os.utime(recent.path,(1,1))
    assert service.prune(7)==0 and recent.path.exists()


def test_failed_compression_still_allows_retention_to_free_disk_space(ui,monkeypatch):
    copies=[snapshot(ui,age_days=age) for age in (3,2,1,0)]
    def disk_full(*args,**kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(zipfile.ZipFile,'write',disk_full)
    result=configured(ui,backup_max_count=2).maintain()
    assert result.compressed==0 and result.removed==2 and result.files==2
    assert copies[-1].path.exists() and _sha256(copies[-1].path)==copies[-1].checksum
    assert ui.repo.db.integrity_check()


def test_count_and_size_limits_preserve_at_least_one_copy(ui):
    copies=[snapshot(ui,age_days=age) for age in (3,2,1,0)]
    service=configured(ui,backup_compress=False,backup_max_count=2)
    assert service.prune(7)==2
    assert service.storage_usage()[0]==2 and copies[-1].path.exists()
    service.max_total_bytes=copies[-1].size_bytes
    assert service.prune(7)==1 and service.storage_usage()[0]==1
    service.max_total_bytes=1
    assert service.prune(7)==0 and copies[-1].path.exists()


@pytest.mark.parametrize('damage',['invalid_database','valid_database_wrong_checksum'])
def test_newer_damaged_copy_does_not_replace_last_verified_backup(ui,damage):
    good=snapshot(ui,age_days=10)
    bad=snapshot(ui)
    if damage=='invalid_database':
        bad.path.write_bytes(b'damaged database')
    else:
        with sqlite3.connect(bad.path) as saved:
            saved.execute('UPDATE shops SET name=?',('Tampered',))
            saved.commit()
            saved.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        saved.close()
    service=configured(ui,backup_compress=False,backup_max_count=1)
    service.prune(7)
    assert good.path.exists() and _sha256(good.path)==good.checksum
    assert not bad.path.exists()


def test_no_usable_copy_means_cleanup_does_not_remove_files(ui):
    copies=[snapshot(ui,age_days=age) for age in (10,11)]
    for result in copies:
        result.path.write_bytes(b'damaged')
    service=configured(ui,backup_compress=False,backup_max_count=1)
    assert service.prune(7)==0 and all(result.path.exists() for result in copies)


def test_one_daily_automatic_copy_is_preferred_over_duplicate_manual_copy(ui):
    automatic_old=snapshot(ui,age_days=1,kind='automatic')
    manual=snapshot(ui,age_days=0.1)
    automatic_new=snapshot(ui,kind='automatic')
    configured(ui,backup_compress=False,backup_max_count=2).prune(7)
    assert automatic_old.path.exists() and automatic_new.path.exists()
    assert not manual.path.exists()


def test_in_use_file_is_not_converted_or_pruned(ui):
    old=snapshot(ui,age_days=10)
    newest=snapshot(ui)
    service=configured(ui,backup_max_count=1)
    with preserve_backup_file(old.path):
        service.maintain()
        assert old.path.exists() and _sha256(old.path)==old.checksum
    service.maintain()
    assert not old.path.exists() and newest.path.with_suffix('.zip').exists()
    assert service.storage_usage()[0]==1


def test_compressed_restore_works_even_with_count_limit_one(ui):
    service=configured(ui,backup_max_count=1)
    source=service.create()
    ui.repo.rename_shop(ui.shop.id,'Changed')
    service.restore(source.path)
    assert ui.repo.get_shop(ui.shop.id).name=='Shop' and ui.repo.db.integrity_check()
    assert source.path.exists()
    safety=list(service.directory.glob('*_pre_restore.zip'))
    assert len(safety)==1
    assert not list(service.directory.glob('.seller-bot-restore-*'))


def test_failed_restore_rolls_back_from_zip_safety(ui,monkeypatch):
    service=configured(ui)
    source=service.create()
    ui.repo.rename_shop(ui.shop.id,'Keep this current data')
    monkeypatch.setattr(ui.repo.db,'quick_check',lambda:False)
    with pytest.raises(RuntimeError,match='quick_check'):
        service.restore(source.path)
    assert ui.repo.get_shop(ui.shop.id).name=='Keep this current data'
    assert ui.repo.db.integrity_check()
    assert len(list(service.directory.glob('*_pre_restore.zip')))==1
    assert ui.repo.recent_backups()[0]['status']=='failed'
    assert not list(service.directory.glob('.seller-bot-restore-*'))


@pytest.mark.asyncio
async def test_storage_maintenance_runs_with_automatic_backups_disabled(ui):
    old=snapshot(ui,age_days=10)
    newest=snapshot(ui)
    settings=replace(ui.ctx.settings,auto_backup_enabled=False,instance_id='test-storage')
    registry=SimpleNamespace(repository=ui.repo,settings=settings,maintenance_lock=asyncio.Lock())
    result=await backup_storage_maintenance_once(registry)
    assert result.compressed==2 and result.removed==1 and result.files==1
    assert not old.path.exists() and newest.path.with_suffix('.zip').exists()
    assert ui.repo.lease_info('maintenance:backup-storage') is None


@pytest.mark.asyncio
async def test_storage_maintenance_respects_other_instance_lease(ui):
    old=snapshot(ui)
    settings=replace(ui.ctx.settings,instance_id='test-storage')
    registry=SimpleNamespace(repository=ui.repo,settings=settings,maintenance_lock=asyncio.Lock())
    assert ui.repo.acquire_lease('maintenance:backup-storage','other-instance',3600)
    assert await backup_storage_maintenance_once(registry) is None
    assert old.path.exists() and ui.repo.lease_info('maintenance:backup-storage')['owner_id']=='other-instance'


@pytest.mark.asyncio
async def test_history_shows_actual_storage_and_marks_pruned_files(ui):
    old=snapshot(ui,age_days=10)
    configured(ui).create()
    await press(ui,'/backups')
    text=next(method.text for method in reversed(ui.telegram.methods) if getattr(method,'text',None))
    assert 'Копии бота на сервере: 1' in text and '7 дней' in text and '512 МиБ' in text
    assert old.path.name in text and 'файл уже удалён' in text
