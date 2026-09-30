from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from app.config import Settings
from app.services.collection import CollectionService, CollectionOutcome
from app.services.preferences import defaults_from_settings
from app.storage import Repository
from app.storage.models import ShopPreferences


@dataclass
class AppContext:
    settings: Settings
    repository: Repository
    shop_id: int
    collector: CollectionService
    wb_connection_id: int | None = None
    ozon_connection_id: int | None = None
    job_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @asynccontextmanager
    async def operation_lock(self, operation: str):
        """Local + cross-process lock with lease renewal for long jobs."""
        lease_key=f'shop:{self.shop_id}:operation:{operation}'
        ttl=self.settings.distributed_lock_ttl_seconds
        owner=self.settings.instance_id
        async with self.job_lock:
            acquired=self.repository.acquire_lease(
                lease_key, owner, ttl,
                metadata={'shop_id':self.shop_id,'operation':operation})
            if not acquired:
                info=self.repository.lease_info(lease_key) or {}
                raise RuntimeError(
                    f'Операция {operation} уже выполняется другим экземпляром '
                    f'({info.get("owner_id","unknown")}).')

            owner_task=asyncio.current_task()
            lease_lost=asyncio.Event()

            async def renew_loop():
                interval=max(1,min(max(1,ttl//3),300))
                while True:
                    await asyncio.sleep(interval)
                    if not self.repository.renew_lease(lease_key,owner,ttl):
                        lease_lost.set()
                        # A lost distributed lease means another process may legally
                        # acquire the same operation. Cancel the in-flight owner task
                        # immediately instead of allowing two collectors to overlap.
                        if owner_task is not None:
                            owner_task.cancel()
                        return

            renew=asyncio.create_task(renew_loop(),name=f'lease-renew:{lease_key}')
            try:
                yield
            except asyncio.CancelledError:
                if lease_lost.is_set():
                    raise RuntimeError(f'Потеряна межпроцессная блокировка {lease_key}; операция остановлена.')
                raise
            finally:
                renew.cancel()
                try: await renew
                except asyncio.CancelledError: pass
                self.repository.release_lease(lease_key,owner)


    def demo_mode(self) -> bool:
        return bool(self.preferences().demo_mode)

    def _demo_skip(self):
        return [] if self.demo_mode() else None

    async def collect_day(self, day: date) -> list[CollectionOutcome]:
        if self.demo_mode(): return []
        async with self.operation_lock('day'):
            tasks=[]
            if self.wb_connection_id is not None:
                tasks.append(self.collector.collect_wb_orders_day(self.wb_connection_id,day,shop_id=self.shop_id))
            if self.ozon_connection_id is not None:
                tasks.append(self.collector.collect_ozon_orders_day(self.ozon_connection_id,day,shop_id=self.shop_id))
            if not tasks: return []
            results=await asyncio.gather(*tasks, return_exceptions=True)
            out=[]
            for item in results:
                if isinstance(item, Exception):
                    raise item
                out.append(item)
            return out

    async def backfill_orders(self, start: date, end: date, marketplace: str = 'all'):
        if self.demo_mode(): return []
        source=(marketplace or 'all').strip().lower()
        if source not in {'all','ozon','wildberries','wb'}:
            raise ValueError('marketplace must be all, ozon or wildberries')
        wb_id=self.wb_connection_id if source in {'all','wildberries','wb'} else None
        ozon_id=self.ozon_connection_id if source in {'all','ozon'} else None
        if source in {'wildberries','wb'} and wb_id is None:
            raise ValueError('Wildberries не подключён к текущему магазину.')
        if source=='ozon' and ozon_id is None:
            raise ValueError('Ozon не подключён к текущему магазину.')
        if source=='all' and wb_id is None and ozon_id is None:
            raise ValueError('К текущему магазину не подключены маркетплейсы.')
        async with self.operation_lock('backfill'):
            return await self.collector.backfill_orders(start=start,end=end,shop_id=self.shop_id,
                wb_connection_id=wb_id,ozon_connection_id=ozon_id)

    async def collect_inventory(self, day: date | None = None) -> list[CollectionOutcome]:
        if self.demo_mode(): return []
        async with self.operation_lock('inventory'):
            return await self.collector.collect_inventory(
                shop_id=self.shop_id, wb_connection_id=self.wb_connection_id,
                ozon_connection_id=self.ozon_connection_id, data_date=day)

    async def collect_finance(self, start: date, end: date):
        if self.demo_mode(): return []
        async with self.operation_lock('finance'):
            return await self.collector.collect_finance(start=start,end=end,wb_connection_id=self.wb_connection_id,
                ozon_connection_id=self.ozon_connection_id,shop_id=self.shop_id)

    async def collect_reconciliation(self, start: date, end: date):
        if self.demo_mode(): return []
        """Refresh sources required for lifecycle reconciliation."""
        async with self.operation_lock('reconciliation'):
            outcomes=[]
            outcomes += await self.collector.backfill_orders(start=start,end=end,shop_id=self.shop_id,
                wb_connection_id=self.wb_connection_id,ozon_connection_id=self.ozon_connection_id)
            if self.wb_connection_id is not None:
                outcomes += await self.collector.collect_wb_sales_range(shop_id=self.shop_id,
                    connection_id=self.wb_connection_id,start=start,end=end)
            outcomes += await self.collector.collect_finance(start=start,end=end,shop_id=self.shop_id,
                wb_connection_id=self.wb_connection_id,ozon_connection_id=self.ozon_connection_id)
            return outcomes

    async def collect_advertising(self, start: date, end: date):
        if self.demo_mode(): return []
        async with self.operation_lock('advertising'):
            return await self.collector.collect_advertising(start=start,end=end,
                wb_connection_id=self.wb_connection_id,ozon_connection_id=self.ozon_connection_id)

    async def collect_inbound(self, day: date | None = None):
        if self.demo_mode(): return []
        async with self.operation_lock('inbound'):
            return await self.collector.collect_inbound(shop_id=self.shop_id,wb_connection_id=self.wb_connection_id,
                ozon_connection_id=self.ozon_connection_id,data_date=day)

    async def collect_promotions(self, day: date | None = None):
        if self.demo_mode(): return []
        async with self.operation_lock('promotions'):
            return await self.collector.collect_promotions(shop_id=self.shop_id,as_of=day or date.today(),
                wb_connection_id=self.wb_connection_id,ozon_connection_id=self.ozon_connection_id)

    def configured_sources(self) -> int:
        if self.demo_mode():
            return 2
        return int(self.wb_connection_id is not None)+int(self.ozon_connection_id is not None)

    def preferences(self) -> ShopPreferences:
        return self.repository.ensure_shop_preferences(
            self.shop_id, defaults_from_settings(self.settings))
