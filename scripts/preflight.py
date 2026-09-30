"""Deployment preflight that does not contact marketplace APIs."""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
except ImportError:
    pass

from app.config import Settings
from app.storage import Database, LATEST_SCHEMA_VERSION


def main() -> int:
    s=Settings.from_env(); checks={}; warnings=[]
    checks['python']=sys.version.split()[0]
    checks['python_ok']=sys.version_info >= (3,11)
    checks['telegram_token']=bool(s.telegram_token)
    checks['owner_ids']=len(s.owner_ids)
    checks['credential_profiles']=list(s.available_credential_profiles())
    checks['db_file']=str(s.db_file)
    try:
        s.db_file.parent.mkdir(parents=True,exist_ok=True)
        probe=s.db_file.parent/'.sellerbot-write-test'
        probe.write_text('ok'); probe.unlink()
        checks['db_directory_writable']=True
    except Exception as exc:
        checks['db_directory_writable']=False; checks['db_directory_error']=str(exc)
    try:
        db=Database(s.db_file); version=db.initialize_safely(s.db_file.parent/'backups')
        checks['schema_version']=version
        checks['schema_current']=version==LATEST_SCHEMA_VERSION
        checks['sqlite_quick_check']=db.quick_check()
    except Exception as exc:
        checks['schema_version']=None; checks['schema_current']=False; checks['sqlite_quick_check']=False
        checks['database_error']=str(exc)
    if not s.db_file.is_absolute():
        warnings.append('DB_FILE is relative. On Bothost use /app/data/... for persistent storage across deploys.')
    if s.health_server_enabled:
        checks['health_endpoint']=f'{s.health_host}:{s.health_port}'
    ok=all([
        checks['python_ok'],checks['telegram_token'],checks['owner_ids']>0,
        checks.get('db_directory_writable',False),checks.get('schema_current',False),
        checks.get('sqlite_quick_check',False),
    ])
    print(json.dumps({'ok':ok,'checks':checks,'warnings':warnings},ensure_ascii=False,indent=2))
    return 0 if ok else 1


if __name__=='__main__':
    raise SystemExit(main())
