"""Telegram transport for backups; the verified SQLite copy stays on disk."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from html import escape
import logging
from pathlib import Path
import stat
import tempfile
import zipfile

from aiogram.exceptions import TelegramEntityTooLarge
from aiogram.types import FSInputFile

from app.services.backups import BackupResult


log = logging.getLogger(__name__)
# The cloud Bot API uploads documents up to 50 MB and downloads up to 20 MB.
# Keep the existing upload margin; start compression earlier to help /restore.
TELEGRAM_UPLOAD_LIMIT = 45 * 1024 * 1024
TELEGRAM_DOWNLOAD_LIMIT = 20_000_000
MAX_RESTORE_DATABASE_SIZE = 200 * 1024 * 1024
SQLITE_SUFFIXES = {'.sqlite3', '.sqlite', '.db'}


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
    """One temporary ZIP for delivery; never replace or delete the raw backup."""
    source = Path(source)
    with tempfile.TemporaryDirectory(prefix='.seller-bot-upload-', dir=source.parent) as tmp:
        yield await run_backup_io(_prepare_upload, source, Path(tmp))


def backup_saved_message(result: BackupResult, reason: str) -> str:
    return (
        '💾 <b>Резервная копия создана и сохранена на сервере.</b>\n'
        f'{escape(reason)}\n\n'
        'Файл SQLite:\n'
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
        lines.append(f'ZIP с одним файлом SQLite ({result.size_bytes / 1_000_000:.1f} МБ до сжатия).')
    lines.append(f'SHA-256 файла SQLite: {result.checksum}')
    if (upload.size_bytes > TELEGRAM_DOWNLOAD_LIMIT
            or result.size_bytes > MAX_RESTORE_DATABASE_SIZE):
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
    source = Path(source)
    if source.suffix.lower() != '.zip':
        return source
    target = None
    created = False
    try:
        with zipfile.ZipFile(source) as zipped:
            entries = zipped.infolist()
            if len(entries) != 1:
                raise ValueError('В ZIP должен быть ровно один файл базы SQLite.')
            entry = entries[0]
            name = entry.filename
            mode = entry.external_attr >> 16
            if (entry.is_dir() or '/' in name or '\\' in name
                    or Path(name).name != name or Path(name).suffix.lower() not in SQLITE_SUFFIXES
                    or (stat.S_IFMT(mode) and not stat.S_ISREG(mode))):
                raise ValueError('В ZIP нужен один обычный файл .sqlite3/.db/.sqlite без вложенных папок.')
            if entry.flag_bits & 1:
                raise ValueError('ZIP с паролем не поддерживается.')
            if entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError('Нужен ZIP со стандартным сжатием Deflate или без сжатия.')
            if entry.file_size <= 0 or entry.file_size > MAX_RESTORE_DATABASE_SIZE:
                raise ValueError('Распакованная база должна быть непустой и не больше 200 МиБ; большую копию восстановите через сервер.')
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = directory / name
            size = 0
            with zipped.open(entry) as incoming, target.open('xb') as outgoing:
                created = True
                target.chmod(0o600)
                while block := incoming.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_RESTORE_DATABASE_SIZE or size > entry.file_size:
                        raise ValueError('Распакованная база превышает разрешённый размер.')
                    outgoing.write(block)
            if size != entry.file_size:
                raise ValueError('ZIP повреждён: размер базы не совпадает.')
            return target
    except Exception as exc:
        if created:
            target.unlink(missing_ok=True)
        if isinstance(exc, (zipfile.BadZipFile, EOFError)):
            raise ValueError('ZIP повреждён или не является ZIP-архивом.') from exc
        raise
