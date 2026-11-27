"""Telegram delivery for persistent ZIP backups and legacy SQLite copies."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from functools import partial
from html import escape
import logging
import os
from threading import Lock
import hashlib
import json
import shutil
from pathlib import Path
import tempfile
import zipfile

from aiogram.exceptions import TelegramEntityTooLarge
from aiogram.types import FSInputFile

from app.services.backups import BackupResult, preserve_backup_file, _sha256
from app.services.backup_files import unpack_backup, database_member
from app.services.performance import run_heavy_io


log = logging.getLogger(__name__)
# The cloud Bot API uploads documents up to 50 MB and downloads up to 20 MB.
# Keep the existing upload margin; start compression earlier to help /restore.
TELEGRAM_UPLOAD_LIMIT = 45 * 1024 * 1024
TELEGRAM_DOWNLOAD_LIMIT = 20_000_000
MAX_RESTORE_DATABASE_SIZE = 200 * 1024 * 1024

_backup_operation_lock = Lock()

@contextmanager
def try_backup_operation(db_path):
    """Reject duplicate backup jobs before the heavy-I/O queue.

    Linux processes sharing a database also share an advisory file lock.
    """
    if not _backup_operation_lock.acquire(blocking=False):
        yield False
        return
    fd = None
    try:
        try:
            import fcntl
        except ImportError:  # Windows development: process-local guard.
            fcntl = None
        if fcntl is not None:
            lock_path = Path(str(Path(db_path).resolve()) + '.backup-operation.lock')
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        yield True
    finally:
        try:
            if fd is not None:
                os.close(fd)
        finally:
            _backup_operation_lock.release()


@dataclass(frozen=True)
class BackupUpload:
    path: Path | None
    size_bytes: int
    compressed: bool = False
    compression_failed: bool = False
    parts: tuple[Path, ...] = ()
    instructions: Path | None = None
    checksum: str | None = None


async def run_backup_io(function, *args, **kwargs):
    """Keep the event loop responsive and finish disk work before temp cleanup."""
    return await run_heavy_io(function, *args, **kwargs)


def _prepare_single(source: Path, directory: Path) -> BackupUpload:
    original_size = source.stat().st_size
    if source.suffix.lower()=='.zip':
        return BackupUpload(source if original_size<=TELEGRAM_UPLOAD_LIMIT else None,
                            original_size,compressed=True)
    if original_size <= TELEGRAM_DOWNLOAD_LIMIT:
        return BackupUpload(source, original_size)
    archive = directory / (source.stem + '.zip')
    try:
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9) as zipped:
            zipped.write(source, arcname=source.name)
        archive.chmod(0o600)
        archive_size = archive.stat().st_size
    except (OSError, zipfile.LargeZipFile):
        log.exception('Cannot compress backup; the original SQLite copy is preserved')
        return BackupUpload(source if original_size <= TELEGRAM_UPLOAD_LIMIT else None,
                            original_size, compression_failed=True)
    if archive_size < original_size:
        return BackupUpload(archive if archive_size <= TELEGRAM_UPLOAD_LIMIT else None,
                            archive_size, compressed=True)
    return BackupUpload(source if original_size <= TELEGRAM_UPLOAD_LIMIT else None,
                        original_size)


def _strong_archive(source: Path, target: Path):
    """Repack one verified ZIP member without loading a database into RAM."""
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_LZMA) as outgoing:
        if source.suffix.lower() == '.zip':
            with zipfile.ZipFile(source) as incoming:
                entry = database_member(incoming)
                with incoming.open(entry) as reader, outgoing.open(entry.filename, 'w', force_zip64=True) as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
        else:
            outgoing.write(source, arcname=source.name)
    target.chmod(0o600)
    with zipfile.ZipFile(target) as check:
        if check.testzip() is not None:
            raise ValueError('Ошибка проверки сжатого архива')


def _split_upload(source: Path, directory: Path, *, compressed: bool) -> BackupUpload:
    parts = []; checksums = []; digest = hashlib.sha256()
    with source.open('rb') as incoming:
        while True:
            block = incoming.read(min(1024 * 1024, TELEGRAM_UPLOAD_LIMIT))
            if not block:
                break
            part = directory / (source.name + f'.{len(parts) + 1:03d}')
            part_hash = hashlib.sha256(); size = 0
            with part.open('xb') as outgoing:
                part.chmod(0o600)
                while block:
                    outgoing.write(block); digest.update(block); part_hash.update(block); size += len(block)
                    if size == TELEGRAM_UPLOAD_LIMIT:
                        break
                    block = incoming.read(min(1024 * 1024, TELEGRAM_UPLOAD_LIMIT - size))
            parts.append(part)
            checksums.append({'name': part.name, 'size': size, 'sha256': part_hash.hexdigest()})
    checksum = digest.hexdigest()
    instructions = directory / (source.stem + '_RESTORE.txt')
    commands = ' + '.join('"' + part.name + '"' for part in parts)
    instructions.write_text(
        'Резервная копия разбита на части. Сохраните ВСЕ части в одну папку.\n'
        'Не отправляйте отдельные части в /restore: это не самостоятельные архивы.\n'
        'Windows: откройте командную строку cmd в этой папке и выполните:\n'
        f'copy /b {commands} "{source.name}"\n'
        f'certutil -hashfile "{source.name}" SHA256\n'
        f'Ожидаемый SHA256: {checksum}\n'
        'Альтернатива: откройте файл .001 через 7-Zip и распакуйте все части.\n'
        'ZIP с усиленным сжатием LZMA открывайте через 7-Zip.\n'
        'Для большой базы восстановление выполняется через файловый доступ к серверу.\n'
        'Перед заменой рабочей базы остановите бота и сохраните текущую базу отдельно.\n'
        'Манифест частей (размер в байтах, SHA256):\n' +
        json.dumps(checksums, ensure_ascii=False, indent=2), encoding='utf-8')
    instructions.chmod(0o600)
    return BackupUpload(None, source.stat().st_size, compressed=compressed,
                        parts=tuple(parts), instructions=instructions, checksum=checksum)


def _prepare_upload(source: Path, directory: Path) -> BackupUpload:
    upload = _prepare_single(source, directory)
    if upload.path is not None:
        return upload
    candidate = source
    strong = directory / (source.stem + '_compact.zip')
    try:
        _strong_archive(source, strong)
        if strong.stat().st_size < source.stat().st_size:
            candidate = strong
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
        log.exception('Stronger compression failed; original recovery file is preserved')
        strong.unlink(missing_ok=True)
    if candidate.stat().st_size <= TELEGRAM_UPLOAD_LIMIT:
        return BackupUpload(candidate, candidate.stat().st_size, compressed=True, checksum=_sha256(candidate))
    try:
        return _split_upload(candidate, directory, compressed=candidate.suffix.lower() == '.zip')
    except OSError:
        log.exception('Cannot prepare backup parts; original recovery file is preserved')
        return BackupUpload(None, source.stat().st_size, compression_failed=True)


@asynccontextmanager
async def backup_upload(source: Path):
    """Send stored ZIPs directly; compress legacy SQLite only for transport."""
    source = Path(source)
    with preserve_backup_file(source), tempfile.TemporaryDirectory(
            prefix='.seller-bot-upload-', dir=source.parent) as tmp:
        yield await run_backup_io(_prepare_upload, source, Path(tmp))


def backup_saved_message(result: BackupResult, reason: str) -> str:
    return (
        '💾 <b>Резервная копия создана и сохранена на сервере.</b>\n'
        f'{escape(reason)}\n\n'
        f"Файл {'ZIP' if result.path.suffix.lower()=='.zip' else 'SQLite'}:\n"
        f'<code>{escape(str(result.path.resolve()))}</code>\n'
        f'Размер: {result.size_bytes / 1_000_000:.1f} МБ · схема v{result.schema_version}\n'
        'Скачайте этот файл через панель или файловый доступ вашего хостинга '
        'и сохраните отдельно от сервера.'
    )


def backup_caption(result: BackupResult, upload: BackupUpload, *, automatic_day: str | None = None) -> str:
    title = ('💾 Автоматическая резервная копия всей БД · ' + automatic_day
             if automatic_day else '💾 Резервная копия всей БД')
    lines = [title, f'Схема v{result.schema_version} · {upload.size_bytes / 1_000_000:.1f} МБ']
    if upload.compressed:
        lines.append(f'ZIP с одним файлом SQLite ({result.original_size_bytes / 1_000_000:.1f} МБ до сжатия).')
    checksum_kind='ZIP' if result.path.suffix.lower()=='.zip' or upload.checksum and upload.compressed else 'SQLite'
    lines.append(f'SHA-256 файла {checksum_kind}: {upload.checksum or result.checksum}')
    if upload.checksum and upload.compressed:
        lines.append('Усиленное сжатие ZIP/LZMA: открыть можно через 7-Zip.')
    if (upload.size_bytes > TELEGRAM_DOWNLOAD_LIMIT
            or result.original_size_bytes > MAX_RESTORE_DATABASE_SIZE):
        lines.append('Сохраните файл на компьютер. Для восстановления этой копии нужен файловый доступ к серверу; /restore её не примет.')
    elif upload.compressed:
        lines.append('Для восстановления через бота отправьте этот ZIP в разделе «Восстановить backup».')
    return '\n'.join(lines)


async def deliver_backup(bot, chat_id: int, result: BackupResult, upload: BackupUpload,
                         *, automatic_day: str | None = None):
    if upload.parts:
        try:
            for index, part in enumerate(upload.parts, 1):
                await bot.send_document(chat_id, FSInputFile(part),
                    caption=f'💾 Резервная копия · часть {index} из {len(upload.parts)}. Сохраните все части и инструкцию.',
                    request_timeout=300)
                await asyncio.sleep(1)
            await bot.send_document(chat_id, FSInputFile(upload.instructions),
                caption='Инструкция по сборке и проверке резервной копии. Отдельные части /restore не принимает.')
        except TelegramEntityTooLarge:
            await bot.send_message(chat_id, backup_saved_message(result,
                'Telegram отклонил часть архива. Набор частей отправлен не полностью.'), parse_mode='HTML')
        return
    if upload.path is None:
        reason = ('Не удалось подготовить ZIP для отправки в Telegram.' if upload.compression_failed
                  else 'Файл слишком большой для отправки в Telegram, в том числе после попытки сжатия.')
        await bot.send_message(chat_id, backup_saved_message(result, reason), parse_mode='HTML')
        return
    try:
        await bot.send_document(chat_id, FSInputFile(upload.path),
                                caption=backup_caption(result, upload, automatic_day=automatic_day), request_timeout=300)
    except TelegramEntityTooLarge:
        # A permanent size rejection must not trigger endless scheduled retries.
        await bot.send_message(chat_id, backup_saved_message(
            result, 'Telegram отклонил отправку из-за размера файла.'), parse_mode='HTML')


def unpack_restore_source(source: Path, directory: Path) -> Path:
    """Validate and stream one SQLite member; never trust archive paths or sizes."""
    return unpack_backup(source,directory,max_size=MAX_RESTORE_DATABASE_SIZE)
