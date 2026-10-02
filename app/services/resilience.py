"""Persistent retry queue, process heartbeat and lightweight DB maintenance."""
from __future__ import annotations
import asyncio
import logging
import os
import socket
from datetime import date, datetime, timezone, timedelta
from html import escape
from zoneinfo import ZoneInfo

from app.reports import build_daily_report, format_daily
from app.reports.daily import build_daily_report_with_currency
from app.services.observability import set_log_context, reset_log_context

log=logging.getLogger(__name__)


class DatabaseIntegrityError(RuntimeError):
    """Non-recoverable SQLite integrity failure; the process must stop writing."""



def _all_ok(outcomes) -> bool:
    return bool(outcomes) and all(getattr(x,'ok',False) for x in outcomes)


def queue_retry(repo, settings, shop_id: int, job_type: str, unique_key: str, payload: dict, *, error: str | None=None, delay_seconds: int=60) -> int:
    return repo.enqueue_retry_job(shop_id,job_type,unique_key,payload,
        max_attempts=settings.retry_max_attempts,delay_seconds=delay_seconds,last_error=error)


async def _notify_shop(bot, ctx, text: str):
    shop=ctx.repository.get_shop(ctx.shop_id)
    prefix=f'🏪 <b>{escape(shop.name)}</b>\n' if shop else ''
    for row in ctx.repository.users_for_shop(ctx.shop_id):
        try: await bot.send_message(int(row['telegram_user_id']),prefix+text,parse_mode='HTML')
        except Exception: log.exception('Retry worker cannot notify user %s',row['telegram_user_id'])


async def execute_retry_job(bot, registry, job: dict) -> None:
    shop_id=int(job['shop_id']); job_type=str(job['job_type']); payload=job.get('payload') or {}
    if shop_id not in {c.shop_id for c in registry.contexts()}:
        await registry.refresh_shop(shop_id)
    ctx=registry.get(shop_id)
    tokens=set_log_context(shop_id=shop_id,job=f'retry:{job_type}')
    try:
        if job_type=='daily':
            day=date.fromisoformat(str(payload['day']))
            outcomes=await ctx.collect_day(day)
            if not _all_ok(outcomes):
                raise RuntimeError('daily retry remains partial')
            start=day-timedelta(days=ctx.preferences().finance_lookback_days-1)
            for kind,action in (('finance',ctx.collect_finance),('advertising',ctx.collect_advertising)):
                try:
                    result=await action(start,day)
                    if result and not _all_ok(result):raise RuntimeError(f'{kind} retry remains partial')
                except asyncio.CancelledError:raise
                except Exception as exc:
                    queue_retry(ctx.repository,ctx.settings,shop_id,kind,f'{start}:{day}',
                        {'start':start.isoformat(),'end':day.isoformat()},error=str(exc),delay_seconds=300)
            try:
                await ctx.collect_buyer_prices(day)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Buyer prices refresh failed; publishing saved prices')
            if payload.get('notify',True):
                await _notify_shop(bot,ctx,'🔄 <b>Данные восстановлены. Обновлённый отчёт:</b>\n\n'+
                    format_daily(await build_daily_report_with_currency(ctx.repository,ctx.shop_id,day)))
        elif job_type=='reconciliation':
            start=date.fromisoformat(str(payload['start'])); end=date.fromisoformat(str(payload['end']))
            outcomes=await ctx.collect_reconciliation(start,end)
        elif job_type=='advertising':
            start=date.fromisoformat(str(payload['start'])); end=date.fromisoformat(str(payload['end']))
            outcomes=await ctx.collect_advertising(start,end)
        elif job_type=='inventory':
            raw=payload.get('day'); day=date.fromisoformat(str(raw)) if raw else None
            outcomes=await ctx.collect_inventory(day)
        elif job_type=='inbound':
            raw=payload.get('day'); day=date.fromisoformat(str(raw)) if raw else None
            outcomes=await ctx.collect_inbound(day)
        elif job_type=='promotions':
            raw=payload.get('day'); day=date.fromisoformat(str(raw)) if raw else None
            outcomes=await ctx.collect_promotions(day)
        elif job_type=='finance':
            start=date.fromisoformat(str(payload['start'])); end=date.fromisoformat(str(payload['end']))
            outcomes=await ctx.collect_finance(start,end)
        else:
            raise ValueError(f'unknown retry job type: {job_type}')
        if job_type!='daily' and outcomes and not _all_ok(outcomes):
            raise RuntimeError(f'{job_type} retry remains partial')
    finally:
        reset_log_context(tokens)


async def retry_worker_loop(bot, registry):
    """Claim retry jobs transactionally so only one process executes a job.

    Running jobs renew their DB lease.  If ownership is lost, the in-flight task
    is cancelled and the stale worker must not complete/fail a job that another
    process has already reclaimed.
    """
    repo=registry.repository; settings=registry.settings; owner=settings.instance_id
    lease_seconds=max(300,settings.distributed_lock_ttl_seconds)
    while True:
        try:
            if registry.maintenance_lock.locked():
                await asyncio.sleep(5); continue
            job=repo.claim_retry_job(owner,lease_seconds=lease_seconds)
            if not job:
                await asyncio.sleep(settings.retry_worker_interval_seconds); continue

            job_id=int(job['id']); lease_lost=asyncio.Event()
            run_task=asyncio.create_task(execute_retry_job(bot,registry,job),name=f'retry-job:{job_id}')

            async def renew_job_lease():
                interval=max(15,min(lease_seconds//3,300))
                while True:
                    await asyncio.sleep(interval)
                    if run_task.done(): return
                    if not repo.renew_retry_job(job_id,owner,lease_seconds):
                        lease_lost.set()
                        run_task.cancel()
                        return

            renew_task=asyncio.create_task(renew_job_lease(),name=f'retry-lease:{job_id}')
            try:
                await run_task
            except asyncio.CancelledError:
                if lease_lost.is_set():
                    log.error('Retry job %s lost its lease; stale execution cancelled',job_id)
                    continue
                raise
            except Exception as exc:
                status=repo.fail_retry_job(job_id,str(exc),base_delay_seconds=60,owner_id=owner)
                if status=='lost':
                    log.error('Retry job %s ownership was lost before failure could be recorded',job_id)
                    continue
                log.exception('Retry job %s failed; status=%s',job_id,status)
                if status=='dead':
                    try:
                        ctx=registry.get(int(job['shop_id']))
                        await _notify_shop(bot,ctx,
                            f'❌ <b>Фоновая задача остановлена после повторных попыток</b>\n'
                            f'Тип: <code>{escape(str(job["job_type"]))}</code>\n'
                            f'Ошибка: {escape(str(exc)[:500])}')
                    except Exception: log.exception('Cannot notify about dead retry job')
            else:
                if not repo.complete_retry_job(job_id,owner_id=owner):
                    log.error('Retry job %s completed locally after its lease was lost; result not committed',job_id)
            finally:
                renew_task.cancel()
                try: await renew_task
                except asyncio.CancelledError: pass
        except asyncio.CancelledError: raise
        except Exception:
            log.exception('Retry worker iteration failed')
            # A broken/locked DB must not turn the worker into a CPU/log spin loop.
            await asyncio.sleep(max(1,min(settings.retry_worker_interval_seconds,30)))


async def heartbeat_loop(registry):
    settings=registry.settings; repo=registry.repository
    while True:
        try:
            if registry.maintenance_lock.locked():
                await asyncio.sleep(5); continue
            repo.heartbeat(settings.instance_id,role='telegram-bot',hostname=socket.gethostname(),pid=os.getpid(),
                metadata={'shops':len(registry.contexts())})
        except asyncio.CancelledError: raise
        except Exception: log.exception('Heartbeat write failed')
        await asyncio.sleep(30)


async def database_maintenance_loop(registry):
    """Checkpoint WAL frequently and run a cheap integrity check daily."""
    repo=registry.repository
    last_quick_check: date | None=None
    while True:
        try:
            if not registry.maintenance_lock.locked():
                repo.db.checkpoint('PASSIVE')
                today=datetime.now(timezone.utc).date()
                if last_quick_check != today:
                    if not repo.db.quick_check():
                        raise DatabaseIntegrityError('SQLite quick_check FAILED; stopping process to avoid writes to a damaged database')
                    log.info('SQLite quick_check OK')
                    last_quick_check=today
        except asyncio.CancelledError: raise
        except DatabaseIntegrityError:
            log.critical('SQLite integrity failure detected',exc_info=True)
            raise
        except Exception: log.exception('Database maintenance failed')
        await asyncio.sleep(3600)


async def polling_lease_heartbeat(repo, settings, registry=None):
    key='singleton:telegram-poller'; owner=settings.instance_id
    ttl=90; interval=30
    while True:
        await asyncio.sleep(interval)
        if registry is not None and registry.maintenance_lock.locked():
            continue
        renewed=repo.renew_lease(key,owner,ttl)
        if not renewed:
            # During guarded restore the database file can temporarily contain historical
            # runtime rows. The restore handler re-acquires the lease before maintenance ends.
            if registry is not None and registry.maintenance_lock.locked():
                continue
            raise RuntimeError('Lost telegram polling lease')
