"""Per-user shop selection and per-shop runtime contexts."""
from __future__ import annotations
import asyncio
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, types

from app.config import Settings
from app.integrations import OzonClient, WildberriesClient, OzonPerformanceClient
from app.services.collection import CollectionService
from app.services.preferences import defaults_from_settings
from app.services.observability import set_log_context, reset_log_context
from app.storage import Repository
from .context import AppContext

_current_context: ContextVar[AppContext | None]=ContextVar('seller_bot_current_context',default=None)


class RuntimeRegistry:
    def __init__(self, settings: Settings, repository: Repository, seller_id: int, default_shop_id: int):
        self.settings=settings; self.repository=repository; self.seller_id=seller_id; self.default_shop_id=default_shop_id
        self._contexts: dict[int,AppContext]={}
        self._lock=asyncio.Lock()
        self.maintenance_lock=asyncio.Lock()

    async def initialize(self) -> None:
        for shop in self.repository.list_shops():
            for uid in self.settings.owner_ids:
                self.repository.grant_shop_access(uid,shop.id,'owner',display_name='Owner')
            await self.refresh_shop(shop.id)

    async def _close_context(self, ctx: AppContext) -> None:
        for client in (ctx.collector.ozon,ctx.collector.wb,ctx.collector.ozon_performance):
            if client is not None:
                try: await client.close()
                except Exception: pass

    async def refresh_shop(self, shop_id: int) -> AppContext:
        async with self._lock:
            old=self._contexts.pop(shop_id,None)
            if old: await self._close_context(old)
            shop=self.repository.get_shop(shop_id)
            if not shop or not shop.active: raise ValueError('shop not found or inactive')
            pref=self.repository.ensure_shop_preferences(shop.id,defaults_from_settings(self.settings))
            creds=self.settings.credentials_for_profile(shop.credential_profile)
            ozon=wb=ozon_perf=None; oz_id=wb_id=None
            if creds.has_ozon:
                conn=self.repository.ensure_connection(shop.id,'ozon','Ozon')
                oz_id=conn.id
                ozon=OzonClient(creds.ozon_client_id,creds.ozon_api_key,
                    timeout=self.settings.http_timeout,min_interval=self.settings.ozon_min_interval)
            if creds.has_wb:
                conn=self.repository.ensure_connection(shop.id,'wildberries','Wildberries')
                wb_id=conn.id
                wb=WildberriesClient(creds.wb_api_token,timeout=self.settings.http_timeout,
                                      min_interval=self.settings.wb_min_interval)
            if creds.has_ozon_performance:
                ozon_perf=OzonPerformanceClient(creds.ozon_perf_client_id,creds.ozon_perf_client_secret,
                                                timeout=self.settings.http_timeout)
            collector=CollectionService(self.repository,ozon=ozon,wildberries=wb,ozon_performance=ozon_perf)
            ctx=AppContext(self.settings,self.repository,shop.id,collector,wb_id,oz_id)
            self._contexts[shop.id]=ctx
            return ctx

    async def reload(self) -> None:
        seller=self.repository.ensure_seller(self.settings.owner_ids[0],'Основной селлер',self.settings.timezone)
        self.seller_id=seller.id
        async with self._lock:
            olds=list(self._contexts.values()); self._contexts={}
            for ctx in olds: await self._close_context(ctx)
        shops=self.repository.shops_for_startup(self.seller_id)
        if not any(s.id==self.default_shop_id for s in shops):
            self.default_shop_id=shops[0].id
        for shop in shops:
            for uid in self.settings.owner_ids:
                self.repository.grant_shop_access(uid,shop.id,'owner',display_name='Owner')
            await self.refresh_shop(shop.id)

    def contexts(self) -> list[AppContext]:
        return list(self._contexts.values())

    def get(self, shop_id: int) -> AppContext:
        ctx=self._contexts.get(shop_id)
        if ctx is None: raise ValueError('shop runtime is not initialized')
        return ctx

    async def context_for_user(self, telegram_user_id: int) -> AppContext:
        shop_id=self.repository.selected_authorized_shop_for_user(telegram_user_id,self.default_shop_id)
        if shop_id is None and self.repository.can_user(telegram_user_id,self.default_shop_id,'shop_lifecycle'):
            # The handlers still enforce per-shop access for ordinary reports.
            shop_id=self.default_shop_id
        if shop_id is None:
            raise PermissionError('Для этого Telegram-пользователя не выдан доступ ни к одному магазину.')
        if shop_id not in self._contexts:
            await self.refresh_shop(int(shop_id))
        return self._contexts[int(shop_id)]

    def select_shop(self, telegram_user_id: int, shop_id: int) -> None:
        if shop_id not in self._contexts: raise ValueError('shop runtime is not initialized')
        self.repository.select_authorized_shop_for_user(telegram_user_id,shop_id)

    def can_user(self, telegram_user_id: int, shop_id: int, permission: str='view') -> bool:
        return self.repository.can_user(telegram_user_id,shop_id,permission)

    def role_for_user(self, telegram_user_id: int, shop_id: int) -> str | None:
        return self.repository.role_for_user(telegram_user_id,shop_id)

    async def create_shop(self, name: str, profile: str='DEFAULT') -> AppContext:
        # Validate the profile before persisting it.  A typo must not leave a
        # newly-created shop in an unusable state.
        clean_profile=self.settings.credentials_for_profile(profile).profile
        shop=self.repository.ensure_shop(self.seller_id,name,credential_profile=clean_profile)
        self.repository.set_shop_credential_profile(shop.id,clean_profile)
        self.repository.ensure_shop_preferences(shop.id,defaults_from_settings(self.settings))
        for uid in self.settings.owner_ids:
            self.repository.grant_shop_access(uid,shop.id,'owner',display_name='Owner')
        return await self.refresh_shop(shop.id)

    async def archive_shop(self, shop_id: int, *, actor_id: int) -> None:
        async with self._lock:
            current=self._contexts.get(shop_id)
            if current is not None and current.job_lock.locked():
                raise RuntimeError('Нельзя архивировать магазин во время загрузки данных.')
            self.repository.archive_shop(actor_id,shop_id)
            current=self._contexts.pop(shop_id,None)
            if current is not None:
                await self._close_context(current)
            active=self.repository.list_shops()
            if self.default_shop_id==shop_id or not any(x.id==self.default_shop_id for x in active):
                self.default_shop_id=active[0].id

    async def restore_shop(self, shop_id: int, *, actor_id: int) -> AppContext:
        self.repository.restore_shop(actor_id,shop_id)
        for uid in self.settings.owner_ids:
            self.repository.grant_shop_access(uid,shop_id,'owner',display_name='Owner')
        return await self.refresh_shop(shop_id)

    async def delete_archived_shop(self, shop_id: int, *, actor_id: int) -> None:
        if shop_id in self._contexts:
            raise RuntimeError('Активный runtime магазина нельзя удалить. Сначала архивируйте магазин.')
        self.repository.delete_archived_shop(actor_id,shop_id)

    async def set_profile(self, shop_id: int, profile: str) -> AppContext:
        current=self._contexts.get(shop_id)
        if current is not None and current.job_lock.locked():
            raise RuntimeError('Нельзя менять профиль во время загрузки данных.')
        # Validate first so an invalid profile can never be stored in SQLite.
        clean_profile=self.settings.credentials_for_profile(profile).profile
        self.repository.set_shop_credential_profile(shop_id,clean_profile)
        return await self.refresh_shop(shop_id)

    async def close(self) -> None:
        async with self._lock:
            contexts=list(self._contexts.values()); self._contexts={}
        for ctx in contexts: await self._close_context(ctx)


class ContextProxy:
    """Keeps existing handlers simple while resolving a shop per Telegram update."""
    def __getattr__(self, name: str) -> Any:
        ctx=_current_context.get()
        if ctx is None: raise RuntimeError('No shop context bound to current update')
        return getattr(ctx,name)


class ShopContextMiddleware(BaseMiddleware):
    def __init__(self, registry: RuntimeRegistry): self.registry=registry

    async def __call__(self, handler: Callable[[types.TelegramObject,dict[str,Any]],Awaitable[Any]],
                       event: types.TelegramObject, data: dict[str,Any]) -> Any:
        user=getattr(event,'from_user',None)
        if user is None: return await handler(event,data)
        if self.registry.maintenance_lock.locked():
            if isinstance(event,types.Message):
                return await event.answer('🛠 Выполняется обслуживание базы. Повторите команду после завершения.')
            return None
        try:
            ctx=await self.registry.context_for_user(user.id)
        except PermissionError:
            if isinstance(event,types.Message):
                return await event.answer(f'🔐 Ваш Telegram ID: <code>{user.id}</code>\n'
                    'Доступ к магазинам пока не выдан. Передайте этот ID владельцу бота.',parse_mode='HTML')
            return None
        token=_current_context.set(ctx)
        log_tokens=set_log_context(shop_id=ctx.shop_id,user_id=user.id)
        try: return await handler(event,data)
        finally:
            reset_log_context(log_tokens)
            _current_context.reset(token)
