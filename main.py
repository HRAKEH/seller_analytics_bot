"""BotHost/VPS entry point: python main.py"""
from __future__ import annotations
import asyncio
import logging

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from app.config import Settings
from app.storage import Database, Repository
from app.services.preferences import defaults_from_settings
from app.services.scheduler import (
    multi_scheduler_loop, multi_alerts_loop, automatic_backup_loop,
    backup_storage_maintenance_loop,
)
from app.services.observability import configure_logging
from app.services.resilience import (
    retry_worker_loop, heartbeat_loop, database_maintenance_loop, polling_lease_heartbeat,
)
from app.services.health import health_http_server
from app.bot import RuntimeRegistry, ContextProxy, ShopContextMiddleware, register_handlers
from app.bot.updates import TelegramUpdateMiddleware


async def main():
    settings=Settings.from_env()
    configure_logging(settings)
    log=logging.getLogger('seller_bot')
    if not settings.telegram_token: raise RuntimeError('TELEGRAM_BOT_TOKEN is required')
    if not settings.owner_ids: raise RuntimeError('TELEGRAM_OWNER_ID is required')

    database=Database(settings.db_file)
    version=database.initialize_safely(settings.db_file.parent/'backups')
    repo=Repository(database)

    # Telegram long polling must be singleton for one bot token. The DB lease also
    # protects accidental double-start on the same persistent volume.
    poller_lease='singleton:telegram-poller'
    if not repo.acquire_lease(poller_lease,settings.instance_id,90,metadata={'role':'poller'}):
        info=repo.lease_info(poller_lease) or {}
        raise RuntimeError(f'Another bot instance owns Telegram polling lease: {info.get("owner_id","unknown")}')

    registry=None
    bot=None
    tasks: list[asyncio.Task]=[]
    fatal_errors: list[BaseException]=[]
    try:
        seller=repo.ensure_seller(settings.owner_ids[0],'Основной селлер',settings.timezone)
        active_shops=repo.shops_for_startup(seller.id)
        shop=active_shops[0]
        defaults=defaults_from_settings(settings)
        for existing in active_shops:
            preferences=repo.ensure_shop_preferences(existing.id,defaults)
            for uid in settings.owner_ids:
                repo.grant_shop_access(uid,existing.id,'owner',display_name='Owner')
            if not preferences.setup_completed:
                repo.update_shop_preferences(existing.id,**defaults)

        registry=RuntimeRegistry(settings,repo,seller.id,shop.id)
        await registry.initialize()
        proxy=ContextProxy()
        bot=Bot(settings.telegram_token)
        dp=Dispatcher(storage=MemoryStorage())
        dp.update.outer_middleware(TelegramUpdateMiddleware(settings.instance_id))
        context_middleware=ShopContextMiddleware(registry)
        dp.message.middleware(context_middleware)
        dp.callback_query.middleware(context_middleware)
        register_handlers(dp,proxy,registry)

        async def poller_guard():
            try:
                await polling_lease_heartbeat(repo,settings,registry)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.critical('Polling lease lost; stopping Telegram polling',exc_info=True)
                raise

        tasks=[
            asyncio.create_task(poller_guard(),name='polling-lease-heartbeat'),
            asyncio.create_task(heartbeat_loop(registry),name='process-heartbeat'),
            asyncio.create_task(retry_worker_loop(bot,registry),name='persistent-retry-worker'),
            asyncio.create_task(database_maintenance_loop(registry),name='database-maintenance'),
            asyncio.create_task(multi_scheduler_loop(bot,registry),name='multi-daily-scheduler'),
            asyncio.create_task(multi_alerts_loop(bot,registry),name='multi-alerts-scheduler'),
            asyncio.create_task(automatic_backup_loop(registry,bot),name='automatic-backup'),
            asyncio.create_task(backup_storage_maintenance_loop(registry),name='backup-storage-maintenance'),
        ]
        if settings.health_server_enabled:
            tasks.append(asyncio.create_task(health_http_server(registry),name='health-http-server'))

        # A background service is part of the bot's correctness. If one crashes,
        # stop polling and exit non-zero instead of looking healthy while scheduled
        # reports/retries/leases are silently dead. The hosting supervisor can then
        # restart the whole process into a known state.
        def _task_done(task: asyncio.Task):
            if task.cancelled(): return
            try: exc=task.exception()
            except Exception as callback_exc:
                exc=callback_exc
            if exc is None: return
            fatal_errors.append(exc)
            log.critical('Critical background task crashed: %s: %s',task.get_name(),exc,
                         exc_info=(type(exc),exc,exc.__traceback__))
            try:
                asyncio.get_running_loop().create_task(dp.stop_polling())
            except RuntimeError:
                pass

        for task in tasks: task.add_done_callback(_task_done)

        log.info('Started schema=%s db=%s shops=%s instance=%s',version,settings.db_file,len(registry.contexts()),settings.instance_id)
        await dp.start_polling(bot)
        if fatal_errors:
            raise RuntimeError(f'Critical background task failed: {fatal_errors[0]}') from fatal_errors[0]
    finally:
        for task in tasks: task.cancel()
        for task in tasks:
            try: await task
            except asyncio.CancelledError: pass
            except Exception:
                # The primary failure is preserved in fatal_errors / outer exception.
                log.exception('Background task stopped with error: %s',task.get_name())
        if registry is not None:
            try: await registry.close()
            except Exception: log.exception('Runtime registry close failed')
        if bot is not None:
            try: await bot.session.close()
            except Exception: log.exception('Telegram session close failed')
        repo.release_lease(poller_lease,settings.instance_id)


if __name__ == '__main__':
    asyncio.run(main())
