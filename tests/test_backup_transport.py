"""Backup delivery failures must never be confused with losing a database copy."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

from aiogram import Dispatcher, types
from aiogram.exceptions import TelegramEntityTooLarge, TelegramNetworkError
from aiogram.methods import SendDocument, SendMessage
import pytest

from app.bot.handlers import register_handlers
from app.services import backup_transport as transport
from app.services.backups import BackupResult, BackupService, _sha256
from app.services.scheduler import automatic_backup_once
from test_navigation import ui, press


def service(ui):
    return BackupService(ui.repo.db, ui.repo, ui.repo.db.path.parent / 'backups')


def last_text(ui):
    return next(method.text for method in reversed(ui.telegram.methods) if isinstance(method, SendMessage))


def restore_runtime(ui):
    registry = SimpleNamespace(contexts=lambda: [ui.ctx], maintenance_lock=asyncio.Lock(),
                               reload=AsyncMock())
    ui.dp = Dispatcher()
    register_handlers(ui.dp, ui.ctx, registry)
    return registry


async def upload_restore(ui, source, *, file_size=None):
    ui.update_id += 1
    message = types.Message(message_id=ui.update_id, date=datetime.now(timezone.utc),
        chat=types.Chat(id=101, type='private'), from_user=types.User(id=101, is_bot=False, first_name='Owner'),
        document=types.Document(file_id='restore-file', file_unique_id='restore-unique',
                                file_name=source.name, file_size=source.stat().st_size if file_size is None else file_size))
    await ui.dp.feed_update(ui.bot, types.Update(update_id=ui.update_id, message=message))


def mock_download(ui, source):
    ui.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path='remote-backup'))
    def copy(remote, *, destination):
        assert remote == 'remote-backup'
        shutil.copyfile(source, destination)
    ui.bot.download_file = AsyncMock(side_effect=copy)


@pytest.mark.asyncio
async def test_small_backup_keeps_sqlite_format_checksum_and_original_file(ui):
    result = service(ui).create()
    async with transport.backup_upload(result.path) as upload:
        assert upload.path == result.path and not upload.compressed
        assert _sha256(upload.path) == result.checksum
    assert result.path.exists() and _sha256(result.path) == result.checksum
    assert not list(result.path.parent.glob('.seller-bot-upload-*'))


@pytest.mark.asyncio
async def test_compressed_backup_restores_exact_database_and_cleans_temporary_zip(ui, tmp_path):
    payload_size = 52 * 1024 * 1024
    with ui.repo.db.connect() as live:
        live.execute('CREATE TABLE backup_probe (payload BLOB)')
        live.execute('INSERT INTO backup_probe VALUES (zeroblob(?))', (payload_size,))
    result = service(ui).create()
    assert result.size_bytes > 50_000_000
    ui.repo.rename_shop(ui.shop.id, 'Changed after backup')
    async with transport.backup_upload(result.path) as upload:
        assert upload.compressed and upload.path.suffix == '.zip'
        assert upload.size_bytes < result.size_bytes
        archive_path = upload.path
        with zipfile.ZipFile(upload.path) as zipped:
            assert zipped.namelist() == [result.path.name]
        restored = transport.unpack_restore_source(upload.path, tmp_path / 'unpacked')
        assert _sha256(restored) == result.checksum
        service(ui).restore(restored)
    assert ui.repo.get_shop(ui.shop.id).name == 'Shop'
    assert ui.repo.db.integrity_check()
    with ui.repo.db.connect() as live:
        assert live.execute('SELECT length(payload) FROM backup_probe').fetchone()[0] == payload_size
    assert not archive_path.exists()
    assert result.path.exists() and _sha256(result.path) == result.checksum
    safety_files = list(result.path.parent.glob('sellerbot_*_pre_restore.sqlite3'))
    assert len(safety_files) == 1
    with sqlite3.connect(safety_files[0]) as saved:
        assert saved.execute('SELECT name FROM shops WHERE id=?', (ui.shop.id,)).fetchone()[0] == 'Changed after backup'


@pytest.mark.asyncio
async def test_uncompressible_oversize_backup_sends_saved_path_instead_of_document(tmp_path, monkeypatch):
    source = tmp_path / 'random.sqlite3'
    source.write_bytes(os.urandom(4096))
    result = BackupResult(source, _sha256(source), source.stat().st_size, 21, 'manual')
    monkeypatch.setattr(transport, 'TELEGRAM_DOWNLOAD_LIMIT', 100)
    monkeypatch.setattr(transport, 'TELEGRAM_UPLOAD_LIMIT', 2000)
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    async with transport.backup_upload(source) as upload:
        assert upload.path is None
        await transport.deliver_backup(bot, 101, result, upload)
    bot.send_document.assert_not_awaited()
    assert str(source.resolve()) in bot.send_message.await_args.args[1]
    assert _sha256(source) == result.checksum


@pytest.mark.parametrize('name', ['../outside.sqlite3', 'nested/file.db', '..\\outside.db', 'wrong.txt', 'nested/'])
def test_restore_rejects_archive_paths_and_non_database_members(tmp_path, name):
    source = tmp_path / 'backup.zip'
    with zipfile.ZipFile(source, 'w') as zipped:
        zipped.writestr(name, b'not a database')
    with pytest.raises(ValueError):
        transport.unpack_restore_source(source, tmp_path / 'unpacked')
    assert not (tmp_path / 'unpacked').exists()
    assert not (tmp_path / 'outside.sqlite3').exists()


def test_restore_rejects_zip_symlink(tmp_path):
    source = tmp_path / 'backup.zip'
    entry = zipfile.ZipInfo('linked.db')
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(source, 'w') as zipped:
        zipped.writestr(entry, '../other.db')
    with pytest.raises(ValueError, match='обычный файл'):
        transport.unpack_restore_source(source, tmp_path / 'unpacked')


def test_restore_rejects_zip_with_multiple_files(tmp_path):
    source = tmp_path / 'backup.zip'
    with zipfile.ZipFile(source, 'w') as zipped:
        zipped.writestr('one.db', b'data')
        zipped.writestr('two.db', b'data')
    with pytest.raises(ValueError, match='ровно один'):
        transport.unpack_restore_source(source, tmp_path / 'unpacked')


def test_restore_rejects_expanded_size_before_writing(tmp_path, monkeypatch):
    source = tmp_path / 'backup.zip'
    with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr('large.db', b'0' * 2048)
    monkeypatch.setattr(transport, 'MAX_RESTORE_DATABASE_SIZE', 1024)
    with pytest.raises(ValueError, match='200 МиБ'):
        transport.unpack_restore_source(source, tmp_path / 'unpacked')
    assert not (tmp_path / 'unpacked').exists()


def test_restore_detects_bad_crc_and_removes_partial_extracted_file(tmp_path):
    source = tmp_path / 'backup.zip'
    payload = b'sqlite-payload-to-corrupt'
    with zipfile.ZipFile(source, 'w') as zipped:
        zipped.writestr('backup.db', payload)
    data = source.read_bytes().replace(payload, b'X' + payload[1:], 1)
    source.write_bytes(data)
    with pytest.raises(ValueError, match='ZIP повреждён'):
        transport.unpack_restore_source(source, tmp_path / 'unpacked')
    assert not (tmp_path / 'unpacked' / 'backup.db').exists()


def test_restore_does_not_remove_preexisting_file_on_unpack_failure(tmp_path):
    source = tmp_path / 'backup.zip'
    target = tmp_path / 'unpacked' / 'backup.db'
    target.parent.mkdir()
    target.write_bytes(b'keep this file')
    with zipfile.ZipFile(source, 'w') as zipped:
        zipped.writestr('backup.db', b'data')
    with pytest.raises(FileExistsError):
        transport.unpack_restore_source(source, target.parent)
    assert target.read_bytes() == b'keep this file'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [TelegramEntityTooLarge, TelegramNetworkError])
async def test_manual_delivery_failure_reports_created_backup_and_saved_path(ui, failure):
    original = ui.telegram.request
    async def fail_document(bot, method, **kwargs):
        if isinstance(method, SendDocument):
            ui.telegram.methods.append(method)
            raise failure(method, 'Request Entity Too Large' if failure is TelegramEntityTooLarge else 'offline')
        return await original(bot, method, **kwargs)
    ui.bot.session.make_request = AsyncMock(side_effect=fail_document)
    await press(ui, '/backup')
    row = ui.repo.recent_backups()[0]
    path = ui.repo.db.path.parent / 'backups' / row['filename']
    assert row['status'] == 'success'
    assert path.exists() and _sha256(path) == row['checksum']
    assert 'создана и сохранена' in last_text(ui)
    assert str(path.resolve()) in last_text(ui)
    assert 'не создан' not in last_text(ui)


@pytest.mark.asyncio
async def test_manual_creation_failure_does_not_report_success_or_send_document(ui, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(BackupService, 'create', fail)
    await press(ui, '/backup')
    assert 'не удалось создать' in last_text(ui)
    assert not any(isinstance(method, SendDocument) for method in ui.telegram.methods)
    assert ui.repo.get_shop(ui.shop.id).name == 'Shop'


@pytest.mark.asyncio
async def test_manual_backup_sends_valid_zip_then_removes_only_transport_file(ui, monkeypatch):
    monkeypatch.setattr(transport, 'TELEGRAM_DOWNLOAD_LIMIT', 1)
    original = ui.telegram.request
    sent = []
    async def inspect_document(bot, method, **kwargs):
        if isinstance(method, SendDocument):
            path = Path(method.document.path)
            with zipfile.ZipFile(path) as zipped:
                payload = zipped.read(zipped.namelist()[0])
            assert payload.startswith(b'SQLite format 3')
            sent.append(path)
        return await original(bot, method, **kwargs)
    ui.bot.session.make_request = AsyncMock(side_effect=inspect_document)
    await press(ui, '/backup')
    assert len(sent) == 1 and not sent[0].exists()
    row = ui.repo.recent_backups()[0]
    assert (ui.repo.db.path.parent / 'backups' / row['filename']).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('zipped', [False, True])
async def test_restore_handler_accepts_sqlite_and_zip_and_reloads_runtime(ui, tmp_path, zipped):
    registry = restore_runtime(ui)
    result = service(ui).create()
    source = result.path
    if zipped:
        source = tmp_path / 'uploaded.zip'
        with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(result.path, arcname=result.path.name)
    ui.repo.rename_shop(ui.shop.id, 'Changed')
    mock_download(ui, source)
    await press(ui, '/restore')
    await upload_restore(ui, source)
    assert ui.repo.get_shop(ui.shop.id).name == 'Shop'
    assert ui.repo.db.integrity_check()
    registry.reload.assert_awaited_once()
    assert 'База восстановлена' in last_text(ui)


@pytest.mark.asyncio
async def test_restore_handler_rejects_oversize_before_requesting_telegram_file(ui, tmp_path):
    registry = restore_runtime(ui)
    source = tmp_path / 'backup.zip'
    source.write_bytes(b'unused')
    mock_download(ui, source)
    await press(ui, '/restore')
    await upload_restore(ui, source, file_size=transport.TELEGRAM_DOWNLOAD_LIMIT + 1)
    ui.bot.get_file.assert_not_awaited()
    registry.reload.assert_not_awaited()
    assert 'Telegram не даст боту его скачать' in last_text(ui)
    assert ui.repo.get_shop(ui.shop.id).name == 'Shop'


@pytest.mark.asyncio
@pytest.mark.parametrize('corrupt_zip', [False, True])
async def test_restore_invalid_upload_preserves_live_data_and_skips_reload(ui, tmp_path, corrupt_zip):
    registry = restore_runtime(ui)
    source = tmp_path / 'invalid.zip'
    if corrupt_zip:
        source.write_bytes(b'not a zip')
    else:
        with zipfile.ZipFile(source, 'w') as zipped:
            zipped.writestr('not_sqlite.db', b'not SQLite')
    mock_download(ui, source)
    await press(ui, '/restore')
    await upload_restore(ui, source)
    assert ui.repo.get_shop(ui.shop.id).name == 'Shop' and ui.repo.db.integrity_check()
    assert not ui.repo.recent_backups()
    registry.reload.assert_not_awaited()
    assert 'исходная база сохранена' in last_text(ui)


@pytest.mark.asyncio
async def test_scheduled_size_rejection_notifies_once_and_continues_other_owners(ui):
    settings = replace(ui.ctx.settings, auto_backup_enabled=True, auto_backup_hour_utc=0,
                       auto_backup_send_telegram=True, owner_ids=(101, 103), instance_id='test-backup')
    registry = SimpleNamespace(repository=ui.repo, settings=settings, default_shop_id=ui.shop.id)
    def size_rejection(uid, document, **kwargs):
        if uid == 101:
            raise TelegramEntityTooLarge(SendDocument(chat_id=uid, document=document), 'Request Entity Too Large')
    bot = SimpleNamespace(send_document=AsyncMock(side_effect=size_rejection), send_message=AsyncMock())
    now = datetime.now(timezone.utc)
    await automatic_backup_once(registry, bot, now)
    await automatic_backup_once(registry, bot, now)
    assert len(ui.repo.recent_backups()) == 1
    assert bot.send_document.await_count == 2
    assert [call.args[0] for call in bot.send_document.await_args_list] == [101, 103]
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 101
    assert str(ui.repo.db.path.parent / 'backups') in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('compression_failure', [False, True])
async def test_scheduled_oversize_keeps_one_backup_and_sends_saved_path_once(ui, monkeypatch, compression_failure):
    settings = replace(ui.ctx.settings, auto_backup_enabled=True, auto_backup_hour_utc=0,
                       auto_backup_send_telegram=True, owner_ids=(101,), instance_id='test-backup')
    registry = SimpleNamespace(repository=ui.repo, settings=settings, default_shop_id=ui.shop.id)
    monkeypatch.setattr(transport, 'TELEGRAM_DOWNLOAD_LIMIT', 1)
    monkeypatch.setattr(transport, 'TELEGRAM_UPLOAD_LIMIT', 1)
    if compression_failure:
        def fail(*args, **kwargs):
            raise OSError('not enough disk space')
        monkeypatch.setattr(zipfile.ZipFile, 'write', fail)
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    now = datetime.now(timezone.utc)
    await automatic_backup_once(registry, bot, now)
    await automatic_backup_once(registry, bot, now)
    bot.send_document.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    rows = ui.repo.recent_backups()
    assert len(rows) == 1 and rows[0]['status'] == 'success'
    path = ui.repo.db.path.parent / 'backups' / rows[0]['filename']
    assert _sha256(path) == rows[0]['checksum']
    assert str(path.resolve()) in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_cancelled_compression_finishes_worker_before_deleting_temporary_files(tmp_path, monkeypatch):
    source = tmp_path / 'backup.db'
    source.write_bytes(b'keep source')
    started, release = threading.Event(), threading.Event()
    directories = []
    def slow_prepare(source, directory):
        directories.append(directory)
        started.set()
        assert release.wait(timeout=5)
        assert directory.exists()
        (directory / 'worker-finished').write_bytes(b'done')
        return transport.BackupUpload(source, source.stat().st_size)
    monkeypatch.setattr(transport, '_prepare_upload', slow_prepare)
    async def run():
        async with transport.backup_upload(source):
            raise AssertionError('Cancelled before entering context')
    task = asyncio.create_task(run())
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not directories[0].exists()
    assert source.read_bytes() == b'keep source'
