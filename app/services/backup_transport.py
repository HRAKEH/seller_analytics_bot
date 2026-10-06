"""Telegram delivery for persistent ZIP backups and legacy SQLite copies."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from html import escape
import logging
from pathlib import Path
import tempfile
import zipfile

from aiogram.exceptions import TelegramEntityTooLarge
from aiogram.types import FSInputFile

from app.services.backups import BackupResult, preserve_backup_file
from app.services.backup_files import unpack_backup


log = logging.getLogger(__name__)
# The cloud Bot API uploads documents up to 50 MB and downloads up to 20 MB.
# Keep the existing upload margin; start compression earlier to help /restore.
TELEGRAM_UPLOAD_LIMIT = 45 * 1024 * 1024
TELEGRAM_DOWNLOAD_LIMIT = 20_000_000
MAX_RESTORE_DATABASE_SIZE = 200 * 1024 * 1024


@dataclass(frozen=True)
class BackupUpload:
    path: Path | None
    size_bytes: int
    compressed: bool = False
    compression_failed: bool = False


async def run_backup_io(function, *args, **kwargs):
    """Keep the event loop responsive and finish disk work before temp cleanup."""
    task = asyncio.create_task(asyncio.to_thread(partial(function, *args, **kwargs)))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A thread cannot be cancelled. Do not remove its files while it writes.
        try:
            await task
        except Exception:
            pass
        raise


def _prepare_upload(source: Path, directory: Path) -> BackupUpload:
    original_size = source.stat().st_size
    if source.suffix.lower()=='.zip':
        return BackupUpload(source if original_size<=TELEGRAM_UPLOAD_LIMIT else None,
                            original_size,compressed=True)
    if original_size <= TELEGRAM_DOWNLOAD_LIMIT:
        return BackupUpload(source, original_size)
    archive = directory / (source.stem + '.zip')
    try:
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6) as zipped:
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
    checksum_kind='ZIP' if result.path.suffix.lower()=='.zip' else 'SQLite'
    lines.append(f'SHA-256 файла {checksum_kind}: {result.checksum}')
    if (upload.size_bytes > TELEGRAM_DOWNLOAD_LIMIT
            or result.original_size_bytes > MAX_RESTORE_DATABASE_SIZE):
        lines.append('Сохраните файл на компьютер. Для восстановления этой копии нужен файловый доступ к серверу; /restore её не примет.')
    elif upload.compressed:
        lines.append('Для восстановления через бота отправьте этот ZIP в разделе «Восстановить backup».')
    return '\n'.join(lines)


async def deliver_backup(bot, chat_id: int, result: BackupResult, upload: BackupUpload,
                         *, automatic_day: str | None = None):
    if upload.path is None:
        reason = ('Не удалось подготовить ZIP для отправки в Telegram.' if upload.compression_failed
                  else 'Файл слишком большой для отправки в Telegram, в том числе после попытки сжатия.')
        await bot.send_message(chat_id, backup_saved_message(result, reason), parse_mode='HTML')
        return
    try:
        await bot.send_document(chat_id, FSInputFile(upload.path),
                                caption=backup_caption(result, upload, automatic_day=automatic_day))
    except TelegramEntityTooLarge:
        # A permanent size rejection must not trigger endless scheduled retries.
        await bot.send_message(chat_id, backup_saved_message(
            result, 'Telegram отклонил отправку из-за размера файла.'), parse_mode='HTML')


def unpack_restore_source(source: Path, directory: Path) -> Path:
    """Validate and stream one SQLite member; never trust archive paths or sizes."""
    return unpack_backup(source,directory,max_size=MAX_RESTORE_DATABASE_SIZE)
