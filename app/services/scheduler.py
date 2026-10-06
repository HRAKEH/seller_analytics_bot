"""Daily operational scheduler plus proactive alert loop."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from aiogram import Bot
from app.bot.context import AppContext
from app.bot.paged_reports import send_report_pages
from app.bot.report_cards import send_daily_cards
from app.reports.alerts import format_alert_digest_pages
from app.services.alerts import AlertEngine
from app.services.resilience import queue_retry, partial_error
from app.services.supply import evaluate_forecast_quality, build_supply_plan
from app.services.actions import build_action_center
from app.reports.dates import readable_text

log=logging.getLogger(__name__)

def _next_target(now: datetime, hhmm: str) -> datetime:
    hour,minute=(int(x) for x in hhmm.split(':',1))
    target=now.replace(hour=hour,minute=minute,second=0,microsecond=0)
    if target <= now: target += timedelta(days=1)
    return target


def _scheduled_report_day(now: datetime) -> str:
    """Daily scheduler runs today but reports the last completed local day."""
    return (now.date()-timedelta(days=1)).isoformat()

async def send_to_owners(bot: Bot, ctx: AppContext, text: str):
    # Kept under the old name for compatibility; recipients are now shop-scoped users.
    recipients=ctx.repository.users_for_shop(ctx.shop_id)
    text=readable_text(text, tz=ctx.preferences().timezone)
    for row in recipients:
        uid=int(row['telegram_user_id'])
        try: await bot.send_message(uid,text,parse_mode='HTML')
        except Exception: log.exception('Cannot send shop report to %s',uid)


async def send_alert_digest(bot: Bot, ctx: AppContext, notes):
    shop = ctx.repository.get_shop(ctx.shop_id)
    pages = format_alert_digest_pages(notes, shop_name=shop.name if shop else None,
                                     timezone=ctx.preferences().timezone)
    if not pages:
        return
    for row in ctx.repository.users_for_shop(ctx.shop_id):
        uid = int(row['telegram_user_id'])
        try:
            await send_report_pages(bot, ctx, uid, pages, user_id=uid)
        except Exception:
            log.exception('Cannot send shop alert digest to %s', uid)

async def collect_and_send_daily(bot: Bot, ctx: AppContext):
    pref=ctx.preferences()
    tz=ZoneInfo(pref.timezone)
    day=datetime.now(tz).date()-timedelta(days=1)
    try:outcomes=await ctx.collect_day(day)
    except Exception:
        log.exception('Daily orders failed; continuing independent sources')
        outcomes=[]
    if not (outcomes and all(x.ok for x in outcomes)):
        queue_retry(ctx.repository,ctx.settings,ctx.shop_id,'daily',day.isoformat(),
            {'day':day.isoformat(),'notify':True},error='daily report remained partial',
            delay_seconds=ctx.settings.daily_retry_minutes*60)
        log.warning('Daily report is partial; persistent retry queued')
    # Delayed sources are independent: failure in advertising must not re-run finance unnecessarily.
    start=day-timedelta(days=pref.finance_lookback_days-1)
    try:
        finance=await ctx.collect_finance(start,day)
        if finance and not all(getattr(x,'ok',False) for x in finance):
            raise RuntimeError('finance refresh remained partial')
    except Exception as exc:
        queue_retry(ctx.repository,ctx.settings,ctx.shop_id,'finance',f'{start}:{day}',
            {'start':start.isoformat(),'end':day.isoformat()},error=str(exc),delay_seconds=300)
        log.exception('Delayed finance refresh failed; retry queued')
    try:
        rec=await ctx.collect_reconciliation(start,day,include_finance=False)
        if rec and not all(getattr(x,'ok',False) for x in rec):
            raise RuntimeError('reconciliation refresh remained partial')
    except Exception as exc:
        queue_retry(ctx.repository,ctx.settings,ctx.shop_id,'reconciliation',f'{start}:{day}',
            {'start':start.isoformat(),'end':day.isoformat()},error=str(exc),delay_seconds=300)
        log.exception('Delayed reconciliation refresh failed; retry queued')
    try:
        ads=await ctx.collect_advertising(start,day)
        if ads and not all(getattr(x,'ok',False) for x in ads):
            raise RuntimeError('advertising refresh remained partial')
    except Exception as exc:
        queue_retry(ctx.repository,ctx.settings,ctx.shop_id,'advertising',f'{start}:{day}',
            {'start':start.isoformat(),'end':day.isoformat()},error=str(exc),delay_seconds=300)
        log.exception('Delayed advertising refresh failed; retry queued')
    # Publish only after delayed finance/reconciliation and ads have been
    # attempted. Failed sources retain their last successful snapshots.
    await send_daily_cards(bot,ctx,day)
    try:
        promos=await ctx.collect_promotions(day)
        if promos and not all(getattr(x,'ok',False) for x in promos):
            raise RuntimeError(partial_error('promotions', promos))
    except Exception as exc:
        queue_retry(ctx.repository,ctx.settings,ctx.shop_id,'promotions',day.isoformat(),
            {'day':day.isoformat()},error=str(exc),delay_seconds=900)
        log.exception('Promotion calendar refresh failed; retry queued')
    try:
        inbound=await ctx.collect_inbound(day)
        if inbound and not all(getattr(x,'ok',False) for x in inbound):
            raise RuntimeError('inbound refresh remained partial')
    except Exception as exc:
        queue_retry(ctx.repository,ctx.settings,ctx.shop_id,'inbound',day.isoformat(),
            {'day':day.isoformat()},error=str(exc),delay_seconds=600)
        log.exception('Inbound supply refresh failed; retry queued')
    # Local learning is intentionally decoupled from remote APIs. Persist a
    # completed backtest and today's plan so calibration has durable evidence.
    try:
        evaluate_forecast_quality(ctx.repository,ctx.shop_id,day,persist=True)
        build_supply_plan(ctx.repository,ctx.shop_id,day,persist=True)
        build_action_center(ctx.repository,ctx.shop_id,day,persist=True)
    except Exception:
        log.exception('Forecast quality / supply calibration refresh failed')

async def scheduler_loop(bot: Bot, ctx: AppContext):
    while True:
        pref=ctx.preferences(); tz=ZoneInfo(pref.timezone)
        now=datetime.now(tz); target=_next_target(now,pref.report_time)
        await asyncio.sleep(max(1,(target-now).total_seconds()))
        try: await collect_and_send_daily(bot,ctx)
        except asyncio.CancelledError: raise
        except Exception: log.exception('Daily scheduler failed')

async def alerts_loop(bot: Bot, ctx: AppContext):
    while True:
        pref=ctx.preferences()
        if not pref.alerts_enabled:
            await asyncio.sleep(60)
            continue
        await asyncio.sleep(pref.alerts_interval_minutes*60)
        try:
            pref=ctx.preferences(); tz=ZoneInfo(pref.timezone)
            engine=AlertEngine(ctx.repository,cooldown_minutes=pref.alert_cooldown_minutes)
            if not ctx.job_lock.locked():
                await ctx.collect_inventory(datetime.now(tz).date(),automatic=True)
            notes=engine.evaluate(ctx.shop_id,today=datetime.now(tz).date(),
                order_drop_pct=pref.alert_order_drop_pct,
                order_lookback_days=pref.alert_order_lookback_days,
                api_stale_hours=pref.alert_api_stale_hours,
                drr_pct=pref.alert_drr_pct,
                stock_risk_days=pref.stock_risk_days,
                stock_velocity_days=pref.stock_velocity_days)
            await send_alert_digest(bot, ctx, notes)
        except asyncio.CancelledError: raise
        except Exception: log.exception('Alerts loop failed')


async def multi_scheduler_loop(bot: Bot, registry):
    """Persistent scheduler for every active shop, including shops added at runtime."""
    while True:
        try:
            if registry.maintenance_lock.locked():
                await asyncio.sleep(5); continue
            for ctx in registry.contexts():
                pref=ctx.preferences(); tz=ZoneInfo(pref.timezone); now=datetime.now(tz)
                run_key=now.date().isoformat()
                report_day=_scheduled_report_day(now)
                hh,mm=(int(x) for x in pref.report_time.split(':',1))
                due=now.time() >= now.replace(hour=hh,minute=mm,second=0,microsecond=0).time()
                if due and ctx.repository.get_job_state(ctx.shop_id,'daily_report') != run_key:
                    lease=f'scheduler:daily:{ctx.shop_id}:{run_key}'
                    if not ctx.repository.acquire_lease(lease,registry.settings.instance_id,7200):
                        continue
                    try:
                        # collect_and_send_daily queues persistent retries for partial sources.
                        await collect_and_send_daily(bot,ctx)
                    except Exception as exc:
                        # The scheduled run happens today, but the report itself is for
                        # yesterday.  Retrying ``run_key`` would silently fetch today's
                        # incomplete data after an outer scheduler failure.
                        queue_retry(ctx.repository,ctx.settings,ctx.shop_id,'daily',report_day,
                            {'day':report_day,'notify':True},error=str(exc),delay_seconds=ctx.settings.daily_retry_minutes*60)
                        log.exception('Daily scheduler failed for report day %s; persistent retry queued',report_day)
                    finally:
                        # The scheduled slot is consumed; subsequent attempts belong to the persistent queue.
                        ctx.repository.set_job_state(ctx.shop_id,'daily_report',run_key)
                        ctx.repository.release_lease(lease,registry.settings.instance_id)
        except asyncio.CancelledError: raise
        except Exception: log.exception('Multi-shop daily scheduler iteration failed')
        await asyncio.sleep(30)


async def multi_alerts_loop(bot: Bot, registry):
    """Evaluate alerts at each shop's own cadence."""
    while True:
        try:
            if registry.maintenance_lock.locked():
                await asyncio.sleep(5); continue
            now_utc=datetime.now(ZoneInfo('UTC'))
            for ctx in registry.contexts():
                pref=ctx.preferences()
                if not pref.alerts_enabled: continue
                state=ctx.repository.get_job_state(ctx.shop_id,'alerts')
                due=True
                if state:
                    try:
                        last=datetime.fromisoformat(state)
                        if last.tzinfo is None: last=last.replace(tzinfo=ZoneInfo('UTC'))
                        due=(now_utc-last.astimezone(ZoneInfo('UTC'))).total_seconds() >= pref.alerts_interval_minutes*60
                    except ValueError: pass
                if not due: continue
                lease=f'scheduler:alerts:{ctx.shop_id}'
                if not ctx.repository.acquire_lease(lease,registry.settings.instance_id,max(300,pref.alerts_interval_minutes*60)):
                    continue
                try:
                    tz=ZoneInfo(pref.timezone); today=datetime.now(tz).date()
                    engine=AlertEngine(ctx.repository,cooldown_minutes=pref.alert_cooldown_minutes)
                    if not ctx.job_lock.locked():
                        try: await ctx.collect_inventory(today,automatic=True)
                        except Exception as exc:
                            queue_retry(ctx.repository,ctx.settings,ctx.shop_id,'inventory',today.isoformat(),
                                {'day':today.isoformat()},error=str(exc),delay_seconds=300)
                            log.exception('Inventory refresh failed; retry queued')
                    notes=engine.evaluate(ctx.shop_id,today=today,order_drop_pct=pref.alert_order_drop_pct,
                        order_lookback_days=pref.alert_order_lookback_days,api_stale_hours=pref.alert_api_stale_hours,
                        drr_pct=pref.alert_drr_pct,stock_risk_days=pref.stock_risk_days,
                        stock_velocity_days=pref.stock_velocity_days)
                    await send_alert_digest(bot, ctx, notes)
                finally:
                    ctx.repository.set_job_state(ctx.shop_id,'alerts',now_utc.isoformat(timespec='seconds'))
                    ctx.repository.release_lease(lease,registry.settings.instance_id)
        except asyncio.CancelledError: raise
        except Exception: log.exception('Multi-shop alerts iteration failed')
        await asyncio.sleep(60)


async def automatic_backup_once(registry,bot=None,now=None):
    """Create once; retry delivery separately without making new DB copies."""
    from app.services.backups import BackupService
    from app.services.backup_transport import backup_upload, deliver_backup, run_backup_io
    settings=registry.settings
    if not settings.auto_backup_enabled:return
    now=now or datetime.now(ZoneInfo('UTC'));run_key=now.date().isoformat()
    if now.hour<settings.auto_backup_hour_utc:return
    repo=registry.repository;lease=f'scheduler:backup:{run_key}'
    if not repo.acquire_lease(lease,settings.instance_id,3600):return
    try:
        service=BackupService.from_settings(repo.db,repo,settings)
        if repo.get_job_state(registry.default_shop_id,'database_backup')!=run_key:
            await run_backup_io(service.create,kind='automatic')
            repo.set_job_state(registry.default_shop_id,'database_backup',run_key)
        if not settings.auto_backup_send_telegram or bot is None:return
        with repo.db.connect() as c:
            row=c.execute("SELECT * FROM backup_history WHERE kind='automatic' AND status='success' AND created_at LIKE ? ORDER BY id DESC LIMIT 1",(run_key+'%',)).fetchone()
        if row is None:return
        path=service.directory/row['filename']
        recipients=[uid for uid in settings.owner_ids
                    if repo.get_job_state(registry.default_shop_id,f'database_backup_delivery:{uid}')!=run_key]
        if not recipients:return
        async with backup_upload(path) as upload:
            result=await run_backup_io(service.result_from_history,row)
            for uid in recipients:
                key=f'database_backup_delivery:{uid}'
                try:
                    await deliver_backup(bot,uid,result,upload,automatic_day=run_key)
                    repo.set_job_state(registry.default_shop_id,key,run_key)
                except asyncio.CancelledError:raise
                except Exception:log.exception('Automatic backup delivery failed for system owner %s; will retry',uid)
    finally:repo.release_lease(lease,settings.instance_id)


async def automatic_backup_loop(registry,bot=None):
    """One verified online backup per UTC day, with optional Telegram delivery."""
    while True:
        try:
            await automatic_backup_once(registry,bot)
        except asyncio.CancelledError: raise
        except Exception: log.exception('Automatic backup failed')
        await asyncio.sleep(300)


async def backup_storage_maintenance_once(registry):
    """Compress/prune managed copies independently of automatic snapshot creation."""
    from app.services.backups import BackupService
    from app.services.backup_transport import run_backup_io
    repo=registry.repository; settings=registry.settings
    lease='maintenance:backup-storage'
    if not repo.acquire_lease(lease,settings.instance_id,3600):
        return None
    try:
        async with registry.maintenance_lock:
            service=BackupService.from_settings(repo.db,repo,settings)
            result=await run_backup_io(service.maintain)
        log.info('Backup storage maintained: compressed=%s removed=%s files=%s bytes=%s',
                 result.compressed,result.removed,result.files,result.size_bytes)
        return result
    finally:
        repo.release_lease(lease,settings.instance_id)


async def backup_storage_maintenance_loop(registry):
    """Maintain storage on startup and hourly, also with AUTO_BACKUP_ENABLED=false."""
    while True:
        try:
            await backup_storage_maintenance_once(registry)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Backup storage maintenance failed; will retry')
        await asyncio.sleep(3600)
