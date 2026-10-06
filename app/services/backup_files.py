"""Validate and unpack SQLite backups without trusting ZIP member paths."""
from __future__ import annotations

from pathlib import Path
import stat
import zipfile


SQLITE_SUFFIXES = {'.sqlite3', '.sqlite', '.db'}


def database_member(zipped: zipfile.ZipFile) -> zipfile.ZipInfo:
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
    return entry


def unpack_backup(source: Path, directory: Path, *, max_size: int | None = None) -> Path:
    """Stream one member; the Telegram caller supplies its download/restore limit."""
    source = Path(source)
    if source.suffix.lower() != '.zip':
        return source
    target = None
    created = False
    try:
        with zipfile.ZipFile(source) as zipped:
            entry = database_member(zipped)
            if entry.file_size <= 0 or (max_size is not None and entry.file_size > max_size):
                raise ValueError('Распакованная база должна быть непустой и не больше 200 МиБ; большую копию восстановите через сервер.')
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = directory / entry.filename
            size = 0
            with zipped.open(entry) as incoming, target.open('xb') as outgoing:
                created = True
                target.chmod(0o600)
                while block := incoming.read(1024 * 1024):
                    size += len(block)
                    if size > entry.file_size or (max_size is not None and size > max_size):
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
