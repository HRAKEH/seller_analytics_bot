"""SQLite online backups and guarded restores."""
from __future__ import annotations
import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.storage import Database, Repository
from app.storage.database import LATEST_SCHEMA_VERSION


@dataclass(frozen=True)
class BackupResult:
    path: Path
    checksum: str
    size_bytes: int
    schema_version: int
    kind: str


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
        with sqlite3.connect(path) as c:
            ok=c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
            if not exists:
                raise ValueError('Это не база Seller Analytics Bot: нет schema_migrations.')
            version=int(c.execute('SELECT COALESCE(MAX(version),0) FROM schema_migrations').fetchone()[0])
    except sqlite3.DatabaseError as exc:
        raise ValueError(f'Некорректный SQLite-файл: {exc}') from exc
    return version,ok


class BackupService:
    def __init__(self, database: Database, repository: Repository, directory: Path | str = './data/backups'):
        self.database=database; self.repo=repository; self.directory=Path(directory)

    def create(self, *, kind: str='manual', destination: Path | None=None) -> BackupResult:
        self.directory.mkdir(parents=True,exist_ok=True)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        path=destination or (self.directory/f'sellerbot_{stamp}_{kind}.sqlite3')
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with self.database.connect() as src, sqlite3.connect(path) as dst:
                src.execute('PRAGMA wal_checkpoint(PASSIVE)')
                src.backup(dst)
            version,ok=inspect_database(path)
            if not ok: raise RuntimeError('integrity_check резервной копии не пройден')
            try: path.chmod(0o600)
            except OSError: pass
            checksum=_sha256(path); size=path.stat().st_size
            self.repo.record_backup(kind,path.name,checksum,size,version,'success')
            return BackupResult(path,checksum,size,version,kind)
        except Exception as exc:
            try: self.repo.record_backup(kind,path.name,None,path.stat().st_size if path.exists() else 0,
                                         self.database.schema_version(),'failed',str(exc)[:1000])
            except Exception: pass
            raise

    def prune(self, retention_days: int) -> int:
        cutoff=datetime.now(timezone.utc).timestamp()-max(1,int(retention_days))*86400
        removed=0
        if not self.directory.exists(): return 0
        for path in self.directory.glob('sellerbot_*.sqlite3'):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(); removed += 1
            except OSError:
                continue
        return removed

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
            with sqlite3.connect(f'file:{source}?mode=ro',uri=True) as src, sqlite3.connect(staging) as dst:
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
            with sqlite3.connect(staging) as c:
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
        source_version,ok=inspect_database(source)
        if not ok: raise ValueError('integrity_check загруженной базы не пройден.')
        if source_version > LATEST_SCHEMA_VERSION:
            raise ValueError(f'Backup schema v{source_version} новее поддерживаемой v{LATEST_SCHEMA_VERSION}.')
        safety=self.create(kind='pre_restore')
        try:
            version=self._install_atomic(source)
            if not self.database.quick_check():
                raise RuntimeError('quick_check после атомарного восстановления не пройден')
            checksum=_sha256(source)
            self.repo.record_backup('restore',source.name,checksum,source.stat().st_size,version,'success',
                                    f'pre_restore={safety.path.name}')
            return BackupResult(source,checksum,source.stat().st_size,version,'restore')
        except Exception as exc:
            # Restore the exact DB captured immediately before the attempted swap.
            try:
                self._install_atomic(safety.path)
            finally:
                try: self.repo.record_backup('restore',source.name,None,source.stat().st_size,source_version,'failed',str(exc)[:1000])
                except Exception: pass
            raise

