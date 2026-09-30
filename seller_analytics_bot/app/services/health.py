"""Health/readiness diagnostics for Telegram and optional HTTP probes."""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any
from app.storage.database import LATEST_SCHEMA_VERSION

log=logging.getLogger(__name__)


def build_health(registry, *, deep: bool=False, shop_id: int | None=None, include_runtime_details: bool=True) -> dict[str,Any]:
    repo=registry.repository; db=repo.db; settings=registry.settings
    checks: dict[str,Any]={}
    ok=True
    try:
        with db.connect() as c:
            c.execute('SELECT 1').fetchone()
        checks['database']='ok'
    except Exception as exc:
        checks['database']=f'error: {exc}'[:300]; ok=False
    try:
        version=db.schema_version(); checks['schema_version']=version
        checks['schema_current']=version==LATEST_SCHEMA_VERSION
        if version!=LATEST_SCHEMA_VERSION: ok=False
    except Exception as exc:
        checks['schema_version']=None; checks['schema_current']=False; ok=False
        checks['schema_error']=str(exc)[:300]
    if deep:
        try:
            checks['sqlite_quick_check']='ok' if db.quick_check() else 'failed'
            if checks['sqlite_quick_check']!='ok': ok=False
        except Exception as exc:
            checks['sqlite_quick_check']=f'error: {exc}'[:300]; ok=False
    checks['shops_runtime']=len(registry.contexts()) if shop_id is None else int(any(c.shop_id==shop_id for c in registry.contexts()))
    if checks['shops_runtime']<=0: ok=False
    try: checks['retry_jobs']=repo.retry_job_counts(shop_id)
    except Exception: checks['retry_jobs']={}
    if include_runtime_details:
        try:
            lease=repo.lease_info('singleton:telegram-poller')
            checks['poller_lease']={'owner_id':lease.get('owner_id'),'expires_at':lease.get('expires_at')} if lease else None
        except Exception: checks['poller_lease']=None
        try:
            heartbeats=repo.recent_heartbeats(5)
            checks['heartbeats']=[{'instance_id':x['instance_id'],'role':x['role'],'heartbeat_at':x['heartbeat_at']} for x in heartbeats]
        except Exception: checks['heartbeats']=[]
        checks['instance_id']=settings.instance_id
    checks['maintenance_mode']=registry.maintenance_lock.locked()
    return {'status':'ok' if ok else 'degraded','ready':ok,'timestamp':datetime.now(timezone.utc).isoformat(timespec='seconds'),'checks':checks}


def public_health_report(report: dict[str,Any]) -> dict[str,Any]:
    """Minimal health payload safe to expose on a public hosting probe."""
    c=report.get('checks') or {}
    return {
        'status':report.get('status'),'ready':bool(report.get('ready')),'timestamp':report.get('timestamp'),
        'checks':{
            'database':c.get('database'),'schema_version':c.get('schema_version'),
            'schema_current':c.get('schema_current'),'shops_runtime':c.get('shops_runtime'),
            'maintenance_mode':c.get('maintenance_mode'),
        },
    }


def format_health(report: dict[str,Any]) -> str:
    c=report['checks']; icon='✅' if report['ready'] else '⚠️'
    retry=c.get('retry_jobs') or {}
    lines=[f'{icon} <b>Health: {report["status"]}</b>','━━━━━━━━━━━━━━━━',
           f'БД: {c.get("database","—")}',
           f'Схема: v{c.get("schema_version","?")} · {"✅ актуальна" if c.get("schema_current") else "❌ требует миграции"}',
           f'Runtime-магазинов: {c.get("shops_runtime",0)}',
           f'Retry queue: pending {retry.get("pending",0)} · running {retry.get("running",0)} · dead {retry.get("dead",0)}',
           f'Maintenance: {"🛠 да" if c.get("maintenance_mode") else "нет"}']
    if c.get('instance_id') is not None: lines.append(f'Instance: <code>{c.get("instance_id")}</code>')
    if 'sqlite_quick_check' in c: lines.append(f'SQLite quick_check: {c["sqlite_quick_check"]}')
    lease=c.get('poller_lease')
    if lease: lines.append(f'Poller lease: <code>{lease.get("owner_id")}</code> до {lease.get("expires_at")}')
    return '\n'.join(lines)


async def health_http_server(registry):
    """Dependency-free /health and /ready HTTP server; disabled by default."""
    settings=registry.settings
    if not settings.health_server_enabled:
        return

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data=await asyncio.wait_for(reader.read(4096),timeout=5)
            first=(data.split(b'\r\n',1)[0] if data else b'').decode('ascii','ignore')
            parts=first.split()
            path=parts[1] if len(parts)>=2 else '/'
            if path not in {'/health','/ready'}:
                status='404 Not Found'; body={'status':'not_found'}
            else:
                report=build_health(registry,deep=False,include_runtime_details=False)
                body=public_health_report(report)
                status='200 OK' if path=='/health' or report['ready'] else '503 Service Unavailable'
            raw=json.dumps(body,ensure_ascii=False,default=str).encode('utf-8')
            writer.write((f'HTTP/1.1 {status}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n').encode('ascii')+raw)
            await writer.drain()
        except Exception:
            log.exception('Health HTTP request failed')
        finally:
            writer.close()
            try: await writer.wait_closed()
            except Exception: pass

    server=await asyncio.start_server(handle,settings.health_host,settings.health_port)
    log.info('Health HTTP server listening on %s:%s',settings.health_host,settings.health_port)
    async with server:
        await server.serve_forever()
