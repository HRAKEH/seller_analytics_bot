"""SQLite online backups and guarded restores."""
from __future__ import annotations
import hashlib
import logging
import os
import re
import sqlite3
import tempfile
import threading
import uuid
import zipfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.storage import Database, Repository
from app.storage.database import LATEST_SCHEMA_VERSION
from app.services.backup_files import database_member, unpack_backup


log = logging.getLogger(__name__)
_storage_lock = threading.RLock()
_protection_lock = threading.RLock()
_protected_files: dict[Path, int] = {}
_managed_name = re.compile(
    r'^(?:sellerbot_\d{8}T\d{12}Z_(?:manual|automatic|pre_restore)'
    r'|pre_migration_v\d+_\d{8}T\d{12}Z)\.(?:sqlite3|zip)$')


@contextmanager
def preserve_backup_file(path: Path):
    """Keep an upload/restore source out of concurrent retention cleanup."""
    key = Path(path).resolve()
    with _protection_lock:
        if not key.is_file():
            raise FileNotFoundError(key)
        _protected_files[key] = _protected_files.get(key, 0) + 1
    try:
        yield
    finally:
        with _protection_lock:
            remaining = _protected_files[key] - 1
            if remaining:
                _protected_files[key] = remaining
            else:
                _protected_files.pop(key, None)


@dataclass(frozen=True)
class BackupResult:
    path: Path
    checksum: str
    size_bytes: int
    schema_version: int
    kind: str
    database_size_bytes: int | None = None

    @property
    def original_size_bytes(self) -> int:
        return self.database_size_bytes if self.database_size_bytes is not None else self.size_bytes


@dataclass(frozen=True)
class BackupMaintenanceResult:
    compressed: int
    removed: int
    size_bytes: int
    files: int


def _sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def inspect_database(path: Path) -> tuple[int,bool]:
    if not path.exists() or path.stat().st_size == 0:
        raise ValueError('Файл базы пуст или не существует.')
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as c:
            ok=c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
            if not exists:
                raise ValueError('Это не база Seller Analytics Bot: нет schema_migrations.')
            version=int(c.execute('SELECT COALESCE(MAX(version),0) FROM schema_migrations').fetchone()[0])
    except sqlite3.DatabaseError as exc:
        raise ValueError(f'Некорректный SQLite-файл: {exc}') from exc
    return version,ok


class BackupService:
    def __init__(self, database: Database, repository: Repository, directory: Path | str = './data/backups',
                 *, compress: bool = False, retention_days: int | None = None,
                 max_count: int = 10, max_total_bytes: int = 512 * 1024 * 1024):
        self.database=database; self.repo=repository; self.directory=Path(directory)
        self.compress=compress; self.retention_days=retention_days
        self.max_count=max(1,int(max_count)); self.max_total_bytes=max(1,int(max_total_bytes))

    @classmethod
    def from_settings(cls, database: Database, repository: Repository, settings):
        return cls(database, repository, database.path.parent/'backups',
            compress=getattr(settings,'backup_compress',True),
            retention_days=getattr(settings,'backup_retention_days',7),
            max_count=getattr(settings,'backup_max_count',10),
            max_total_bytes=getattr(settings,'backup_max_total_mb',512)*1024*1024)

    def create(self, *, kind: str='manual', destination: Path | None=None) -> BackupResult:
        with _storage_lock:
            return self._create(kind=kind,destination=destination)

    def _create(self, *, kind: str, destination: Path | None) -> BackupResult:
        self.directory.mkdir(parents=True,exist_ok=True)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        path=destination or (self.directory/f'sellerbot_{stamp}_{kind}.sqlite3')
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with closing(self.database.connect()) as src, closing(sqlite3.connect(path)) as dst:
                src.execute('PRAGMA wal_checkpoint(PASSIVE)')
                src.backup(dst)
            version,ok=inspect_database(path)
            if not ok: raise RuntimeError('integrity_check резервной копии не пройден')
            try: path.chmod(0o600)
            except OSError: pass
            original_size=path.stat().st_size
            if self.compress:
                try:
                    archived=self._compress_file(path)
                except Exception:
                    log.exception('Backup compression failed; the verified SQLite copy is preserved')
                else:
                    path=archived
            checksum=_sha256(path); size=path.stat().st_size
            self.repo.record_backup(kind,path.name,checksum,size,version,'success')
            result=BackupResult(path,checksum,size,version,kind,original_size)
            if self.retention_days is not None:
                try:
                    self.prune(self.retention_days,preserve=(path,))
                except Exception:
                    log.exception('Backup saved; retention cleanup failed')
            return result
        except Exception as exc:
            try: self.repo.record_backup(kind,path.name,None,path.stat().st_size if path.exists() else 0,
                                         self.database.schema_version(),'failed',str(exc)[:1000])
            except Exception: pass
            raise

    def _files(self) -> list[Path]:
        if not self.directory.exists():
            return []
        return [path for path in self.directory.iterdir()
                if _managed_name.fullmatch(path.name) and not path.is_symlink() and path.is_file()]

    @staticmethod
    def _archive_database_checksum(path: Path) -> tuple[str,int]:
        with zipfile.ZipFile(path) as zipped:
            entry=database_member(zipped)
            checksum=hashlib.sha256(); size=0
            with zipped.open(entry) as incoming:
                while block := incoming.read(1024*1024):
                    checksum.update(block); size+=len(block)
            if size != entry.file_size:
                raise ValueError('ZIP backup database size changed')
            return checksum.hexdigest(),size

    def _compress_file(self, source: Path) -> Path:
        """Replace a legacy copy only after verifying the ZIP and updating history."""
        wal=Path(str(source)+'-wal')
        if wal.exists() and wal.stat().st_size:
            raise ValueError('Backup has an active WAL; compression skipped')
        _,ok=inspect_database(source)
        if not ok:
            raise ValueError('integrity_check резервной копии не пройден.')
        original_checksum=_sha256(source)
        with closing(self.repo.db.connect()) as conn:
            rows=conn.execute("SELECT checksum FROM backup_history WHERE filename=? AND status='success' AND kind!='restore'",(source.name,)).fetchall()
        if any(row['checksum'] != original_checksum for row in rows):
            raise ValueError('Stored backup checksum changed; compression skipped')
        source_stat=source.stat(); target=source.with_suffix('.zip')
        staged=target.with_name('.'+target.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            if target.exists():
                if target.is_symlink():
                    raise ValueError('Backup ZIP destination is a symlink')
                checksum,size=self._archive_database_checksum(target)
            else:
                with zipfile.ZipFile(staged,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zipped:
                    zipped.write(source,arcname=source.name)
                staged.chmod(0o600)
                with staged.open('rb') as saved:
                    os.fsync(saved.fileno())
                checksum,size=self._archive_database_checksum(staged)
                if checksum != original_checksum or size != source_stat.st_size:
                    raise ValueError('Compressed backup checksum does not match SQLite')
                os.utime(staged,ns=(source_stat.st_atime_ns,source_stat.st_mtime_ns))
                os.replace(staged,target)
            if checksum != original_checksum or size != source_stat.st_size:
                raise ValueError('Existing ZIP does not match the SQLite backup')
            with _protection_lock:
                if source.resolve() in _protected_files:
                    return source
                self.repo.replace_backup_file(source.name,target.name,_sha256(target),target.stat().st_size,
                                              previous_checksum=original_checksum)
                source.unlink()
            return target
        finally:
            staged.unlink(missing_ok=True)

    def _usable(self, path: Path) -> bool:
        try:
            wal=Path(str(path)+'-wal')
            if path.suffix!='.zip' and wal.exists() and wal.stat().st_size:
                return False
            with closing(self.repo.db.connect()) as conn:
                rows=conn.execute("SELECT checksum FROM backup_history WHERE filename=? AND status='success' AND kind!='restore'",(path.name,)).fetchall()
            if rows and any(row['checksum'] != _sha256(path) for row in rows):
                return False
            with tempfile.TemporaryDirectory(prefix='.seller-bot-check-',dir=self.directory) as tmp:
                raw=unpack_backup(path,Path(tmp))
                version,ok=inspect_database(raw)
                return ok and version<=LATEST_SCHEMA_VERSION
        except Exception:
            log.warning('Backup cannot be verified: %s',path.name,exc_info=True)
            return False

    def prune(self, retention_days: int, *, preserve: tuple[Path,...] = ()) -> int:
        with _storage_lock:
            paths=sorted(self._files(),key=lambda path:(path.stat().st_mtime_ns,path.name),reverse=True)
            if not paths:
                return 0
            protected={path.resolve() for path in preserve}
            # Never remove the final usable recovery point, even when it exceeds a limit.
            latest=next((path for path in paths if self._usable(path)),None)
            if latest is None:
                log.warning('Backup retention skipped: no usable recovery point')
                return 0
            protected.add(latest.resolve())
            cutoff=datetime.now(timezone.utc).timestamp()-max(1,int(retention_days))*86400
            removed=0
            def remove(path):
                nonlocal removed
                with _protection_lock:
                    if path.resolve() in protected or path.resolve() in _protected_files:
                        return
                    try:
                        path.unlink(); paths.remove(path); removed+=1
                    except OSError:
                        log.warning('Cannot remove expired backup: %s',path.name,exc_info=True)
            for path in list(paths):
                if path.resolve() not in protected and path.stat().st_mtime < cutoff:
                    remove(path)
            # Prefer one automatic recovery point per day over duplicate/manual copies.
            daily={}
            for path in paths:
                if '_automatic.' in path.name:
                    daily.setdefault(path.name.split('T',1)[0],path)
            preferred=set(daily.values())
            total=sum(path.stat().st_size for path in paths)
            candidates=sorted(paths,key=lambda path:(path in preferred,path.stat().st_mtime_ns,path.name))
            for path in candidates:
                if len(paths)<=self.max_count and total<=self.max_total_bytes:
                    break
                if path.resolve() in protected:
                    continue
                size=path.stat().st_size
                remove(path)
                if path not in paths:
                    total-=size
            if len(paths)>self.max_count or total>self.max_total_bytes:
                log.warning('Backup limits exceeded by protected recovery files: count=%s bytes=%s',len(paths),total)
            return removed

    def maintain(self) -> BackupMaintenanceResult:
        """Upgrade old backups and apply the same policy with auto-backups disabled."""
        with _storage_lock:
            compressed=0
            preserved=[]
            if self.compress:
                for path in self._files():
                    with _protection_lock:
                        in_use=path.resolve() in _protected_files
                    if path.suffix=='.sqlite3' and not in_use:
                        try:
                            archived=self._compress_file(path)
                            compressed+=int(archived != path)
                        except Exception:
                            # An archive may have been committed before its history
                            # update failed. Keep the original until that completes.
                            # If no archive was created (e.g. disk full), normal
                            # retention can still free space while keeping one good copy.
                            if path.with_suffix('.zip').exists():
                                preserved.append(path)
                            log.exception('Legacy backup compression skipped: %s',path.name)
            removed=self.prune(self.retention_days or 7,preserve=tuple(preserved))
            paths=self._files()
            return BackupMaintenanceResult(compressed,removed,sum(path.stat().st_size for path in paths),len(paths))

    def storage_usage(self) -> tuple[int,int]:
        with _storage_lock:
            paths=self._files()
            return len(paths),sum(path.stat().st_size for path in paths)

    def result_from_history(self, row) -> BackupResult:
        path=self.directory/row['filename']
        if _sha256(path) != row['checksum']:
            raise ValueError('Backup checksum changed; delivery stopped')
        original_size=path.stat().st_size
        if path.suffix=='.zip':
            with zipfile.ZipFile(path) as zipped:
                original_size=database_member(zipped).file_size
        return BackupResult(path,row['checksum'],path.stat().st_size,row['schema_version'],
                            row['kind'],original_size)

    def _install_atomic(self, source: Path) -> int:
        """Build and validate a restored DB beside the live file, then atomically replace it.

        This avoids writing a large backup directly into a WAL-enabled live
        database while background heartbeat/poller tasks may still exist.
        Callers are still expected to hold the application's maintenance lock.
        """
        target=self.database.path
        target.parent.mkdir(parents=True,exist_ok=True)
        staging=target.with_name(f'.{target.name}.restore-{uuid.uuid4().hex}.tmp')
        candidates=(staging,Path(str(staging)+'-wal'),Path(str(staging)+'-shm'))
        for candidate in candidates:
            try: candidate.unlink()
            except FileNotFoundError: pass
        try:
            with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(staging)) as dst:
                src.backup(dst)
            staged=Database(staging)
            version=staged.initialize()
            if not staged.integrity_check():
                raise RuntimeError('integrity_check восстановленной базы не пройден')
            # Runtime state belongs to the current process, never to a backup.
            with staged.connect() as c:
                c.execute('DELETE FROM runtime_leases')
                c.execute('DELETE FROM process_heartbeats')
            staged.checkpoint('TRUNCATE')
            # TRUNCATE checkpoint above guarantees all WAL pages are merged into
            # the main staging file.  Keep WAL mode in the database header; the
            # next normal Database.connect() will recreate fresh sidecars.
            with closing(sqlite3.connect(staging)) as c:
                if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('quick_check staging-базы не пройден')
            for suffix in ('-wal','-shm'):
                try: Path(str(target)+suffix).unlink()
                except FileNotFoundError: pass
            os.replace(staging,target)
            try: target.chmod(0o600)
            except OSError: pass
            return version
        finally:
            for candidate in candidates:
                try: candidate.unlink()
                except FileNotFoundError: pass

    def restore(self, source: Path) -> BackupResult:
        source=Path(source)
        self.directory.mkdir(parents=True,exist_ok=True)
        with _storage_lock, preserve_backup_file(source), tempfile.TemporaryDirectory(
                prefix='.seller-bot-restore-',dir=self.directory) as tmp:
            raw=unpack_backup(source,Path(tmp)/'incoming')
            source_version,ok=inspect_database(raw)
            if not ok: raise ValueError('integrity_check загруженной базы не пройден.')
            if source_version > LATEST_SCHEMA_VERSION:
                raise ValueError(f'Backup schema v{source_version} новее поддерживаемой v{LATEST_SCHEMA_VERSION}.')
            checksum=_sha256(source); size=source.stat().st_size; original_size=raw.stat().st_size
            safety=self.create(kind='pre_restore')
            with preserve_backup_file(safety.path):
                try:
                    version=self._install_atomic(raw)
                    if not self.database.quick_check():
                        raise RuntimeError('quick_check после атомарного восстановления не пройден')
                    self.repo.record_backup('restore',source.name,checksum,size,version,'success',
                                            f'pre_restore={safety.path.name}')
                    return BackupResult(source,checksum,size,version,'restore',original_size)
                except Exception as exc:
                    # Rollback also works when the safety copy is stored as ZIP.
                    try:
                        safety_raw=unpack_backup(safety.path,Path(tmp)/'safety')
                        self._install_atomic(safety_raw)
                    finally:
                        try: self.repo.record_backup('restore',source.name,None,size,source_version,'failed',str(exc)[:1000])
                        except Exception: pass
                    raise
