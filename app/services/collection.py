"""Orchestrates API -> normalization -> durable storage per marketplace."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable
import json
import logging

from app.integrations import OzonClient, WildberriesClient, OzonPerformanceClient, FetchResult
from app.integrations.wildberries import decode_wb_token
from app.storage import Repository, MetricPoint, ProductMetricPoint, InventoryPoint, CommerceEventPoint
from .normalization import normalize_ozon_order_analytics, normalize_wb_orders, NormalizationError
from .finance import (
    normalize_wb_finance_report, normalize_ozon_accruals, normalize_wb_ad_stats, normalize_ozon_ad_stats,
    normalize_wb_product_finance, normalize_ozon_product_finance, ProductFinanceObservation,
    FinanceNormalizationError,
)
from .product_analytics import (
    ProductDayObservation, StockObservation, ProductNormalizationError,
    normalize_wb_product_orders, normalize_ozon_product_analytics,
    normalize_wb_stocks, normalize_ozon_stocks, normalize_ozon_postings,
)
from .reconciliation import (
    CommerceObservation, ReconciliationNormalizationError,
    normalize_wb_order_events, normalize_wb_sales_events, normalize_wb_finance_events,
    normalize_ozon_posting_events, normalize_ozon_finance_events,
)
from .advertising import (
    normalize_wb_ad_detail, normalize_ozon_ad_campaign_detail, normalize_ozon_ad_product_detail,
    AdvertisingNormalizationError,
)
from .inbound import normalize_wb_supply, normalize_ozon_order
from .promotions import normalize_wb_promotions, normalize_ozon_promotions, PromotionNormalizationError
from .ozon_dates import posting_day, posting_range
from .wb_funnel import normalize_wb_funnel

log=logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectionOutcome:
    marketplace: str
    data_date: str
    ok: bool
    run_id: int
    message: str


class CollectionService:
    _shared_wb_auto_disabled: dict[str,set[str]] = {}

    def __init__(self, repository: Repository, *, ozon: OzonClient | None = None,
                 wildberries: WildberriesClient | None = None, ozon_performance: OzonPerformanceClient | None = None):
        self.repo, self.ozon, self.wb, self.ozon_performance = repository, ozon, wildberries, ozon_performance
        # Authorization failures on optional WB stock endpoints are usually not
        # transient. Share suppression across shop runtimes that use the same WB
        # credential so a duplicate shop/profile cannot probe the same forbidden
        # endpoint again in the same process.
        if wildberries is not None:
            self._auto_disabled_endpoints=self._shared_wb_auto_disabled.setdefault(
                getattr(wildberries,'rate_scope',f'instance:{id(wildberries)}'),set())
        else:
            self._auto_disabled_endpoints=set()

    async def _ozon_analytics_all(self, payload: dict) -> FetchResult:
        if self.ozon is None:
            return FetchResult.failure('ozon','Ozon client is not configured',None,1)
        method=getattr(self.ozon,'analytics_all',None)
        if method is not None:
            return await method(payload)
        # Compatibility with injected test/custom clients that expose only analytics().
        return await self.ozon.analytics(payload)

    def _failure(self, connection_id: int, endpoint: str, data_date: str,
                 marketplace: str, result: FetchResult) -> CollectionOutcome:
        rid = self.repo.record_failure(connection_id, endpoint, data_date,
                                       result.error or 'unknown API error',
                                       http_status=result.status_code, attempts=result.attempts)
        return CollectionOutcome(marketplace, data_date, False, rid, result.error or 'API error')

    def _listing(self, shop_id: int, connection_id: int, marketplace: str,
                 sku: str, name: str, offer_id: str | None = None):
        existing = self.repo.listing_by_marketplace_sku(connection_id, sku)
        if existing:
            return existing
        product = self.repo.ensure_product(shop_id, f'{marketplace}:{offer_id or sku}', name)
        return self.repo.ensure_listing(product.id, connection_id, sku, offer_id)

    def _save_product_observations(self, *, shop_id: int, connection_id: int,
                                   marketplace: str, source_run_id: int,
                                   observations: Iterable[ProductDayObservation]) -> int:
        points: list[ProductMetricPoint] = []
        for obs in observations:
            listing = self._listing(shop_id, connection_id, marketplace, obs.marketplace_sku,
                                    obs.name, obs.offer_id)
            points.extend([
                ProductMetricPoint(listing.id, obs.data_date, 'ordered_units', obs.ordered_units,
                                   'units', 'ALL', True, obs.as_of),
                ProductMetricPoint(listing.id, obs.data_date, 'ordered_revenue', obs.ordered_revenue,
                                   'RUB', 'ALL', True, obs.as_of),
                ProductMetricPoint(listing.id, obs.data_date, 'cancellations_units', obs.cancellations_units,
                                   'units', 'ALL', True, obs.as_of),
            ])
            for scheme, units in obs.fulfillment_units.items():
                points.append(ProductMetricPoint(listing.id, obs.data_date, 'fulfillment_units', units,
                                                 'units', scheme, True, obs.as_of))
        return self.repo.save_product_metrics(source_run_id, points, replace_order_snapshot=True)

    def _save_fulfillment_observations(self, *, shop_id: int, connection_id: int,
                                       marketplace: str, source_run_id: int,
                                       observations: Iterable[ProductDayObservation]) -> int:
        points: list[ProductMetricPoint] = []
        for obs in observations:
            listing=self._listing(shop_id,connection_id,marketplace,obs.marketplace_sku,obs.name,obs.offer_id)
            for scheme,units in obs.fulfillment_units.items():
                points.append(ProductMetricPoint(listing.id,obs.data_date,'fulfillment_units',units,
                                                 'units',scheme,True,obs.as_of))
        return self.repo.save_product_metrics(source_run_id,points,replace_fulfillment_snapshot=True)

    def _save_inventory_observations(self, *, shop_id: int, connection_id: int,
                                     marketplace: str, source_run_id: int,
                                     observations: Iterable[StockObservation]) -> int:
        points: list[InventoryPoint] = []
        for obs in observations:
            listing = self._listing(shop_id, connection_id, marketplace, obs.marketplace_sku,
                                    obs.name, obs.offer_id)
            points.append(InventoryPoint(
                listing.id, obs.available_units, obs.reserved_units,
                obs.fulfillment_scheme, obs.warehouse_name, obs.as_of,
            ))
        return self.repo.save_inventory(source_run_id, points) if points else 0

    def _save_product_finance_observations(self, *, shop_id: int, connection_id: int,
                                           marketplace: str, source_run_id: int,
                                           observations: Iterable[ProductFinanceObservation],
                                           replace_finance_snapshot: bool = False) -> int:
        points: list[ProductMetricPoint] = []
        for obs in observations:
            listing=self._listing(shop_id,connection_id,marketplace,obs.marketplace_sku,obs.name,obs.offer_id)
            for metric,value in obs.metrics.items():
                points.append(ProductMetricPoint(listing.id,obs.data_date,metric,float(value),'RUB','ALL',False,obs.as_of))
        return self.repo.save_product_metrics(source_run_id,points,replace_finance_snapshot=replace_finance_snapshot)

    def _save_commerce_observations(self, *, shop_id: int, connection_id: int,
                                    marketplace: str, source_run_id: int,
                                    observations: Iterable[CommerceObservation],
                                    replace_finance_snapshot: bool = False) -> int:
        points: list[CommerceEventPoint]=[]
        for obs in observations:
            listing=self._listing(shop_id,connection_id,marketplace,obs.marketplace_sku,obs.name,obs.offer_id)
            points.append(CommerceEventPoint(
                listing.id,obs.data_date,obs.event_kind,obs.source_name,obs.fingerprint,obs.event_time,
                obs.external_order_id,obs.external_event_id,obs.quantity,obs.gross_amount,obs.net_amount,
                obs.fulfillment_scheme,obs.is_preliminary,json.dumps(obs.metadata,ensure_ascii=False,sort_keys=True,default=str)))
        return self.repo.save_commerce_events(source_run_id,points,replace_finance_snapshot=replace_finance_snapshot)

    def _resolve_ad_product_listings(self, connection_id: int, products):
        resolved=[]
        for p in products:
            listing=self.repo.listing_by_marketplace_sku(connection_id,p.marketplace_sku)
            resolved.append(type(p)(p.connection_id,p.data_date,p.marketplace_sku,p.campaign_id,
                p.campaign_name,listing.id if listing else None,p.spend,p.attributed_sales,p.orders,p.clicks,p.impressions))
        return resolved

    def _save_wb_statistics(self, connection_id: int, ds: str, endpoint: str,
                            result: FetchResult, shop_id: int | None) -> CollectionOutcome:
        if not result.ok:
            return self._failure(connection_id, endpoint, ds, 'wildberries', result)
        try:
            points = normalize_wb_orders(result.data, connection_id, ds)
            product_obs = normalize_wb_product_orders(result.data, data_date=ds)
            primary=self.repo.has_wb_funnel_snapshot(connection_id,ds)
            rid = self.repo.record_success(connection_id, endpoint, ds, result.data, [] if primary else points,
                                           attempts=result.attempts)
            if shop_id is not None:
                saver=self._save_fulfillment_observations if primary else self._save_product_observations
                saver(shop_id=shop_id,connection_id=connection_id,marketplace='wildberries',
                      source_run_id=rid,observations=product_obs)
                self._save_commerce_observations(shop_id=shop_id, connection_id=connection_id,
                    marketplace='wildberries', source_run_id=rid,
                    observations=normalize_wb_order_events(result.data,data_date=ds))
        except (NormalizationError, ProductNormalizationError, ValueError) as exc:
            rid = self.repo.record_failure(connection_id, endpoint, ds, f'Normalization: {exc}',
                                           http_status=result.status_code, attempts=result.attempts)
            return CollectionOutcome('wildberries', ds, False, rid, str(exc))
        return CollectionOutcome('wildberries', ds, True, rid, 'orders loaded')

    async def collect_wb_funnel_day(self, connection_id: int, day: date, *,
                                   shop_id: int | None = None, backfill: bool = False) -> CollectionOutcome:
        ds=day.isoformat(); endpoint='analytics/orders/backfill' if backfill else 'analytics/orders'
        if self.wb is None or getattr(self.wb,'sales_funnel_all',None) is None:
            return self._failure(connection_id,endpoint,ds,'wildberries',
                FetchResult.failure('wildberries','WB sales funnel client is not configured',None,0))
        meta=decode_wb_token(getattr(self.wb,'token',''))
        if meta['ok'] and 'Аналитика' not in meta['categories']:
            result=FetchResult.failure('wildberries','В WB-токене нет категории «Аналитика»',403,0)
        elif 'analytics/orders' in self._auto_disabled_endpoints:
            result=FetchResult.failure('wildberries','WB «Воронка продаж» недоступна по текущему токену; проверьте подключения',403,0)
        else:
            result=await self.wb.sales_funnel_all(ds)
        if not result.ok:
            if result.status_code in (401,402,403):
                self._auto_disabled_endpoints.add('analytics/orders')
            return self._failure(connection_id,endpoint,ds,'wildberries',result)
        try:
            points,observations=normalize_wb_funnel(result.data,connection_id,ds)
            rid=self.repo.record_success(connection_id,endpoint,ds,result.data,points,attempts=result.attempts)
            if shop_id is not None:
                self._save_product_observations(shop_id=shop_id,connection_id=connection_id,
                    marketplace='wildberries',source_run_id=rid,observations=observations)
            return CollectionOutcome('wildberries',ds,True,rid,'sales funnel loaded')
        except (NormalizationError,ProductNormalizationError,ValueError) as exc:
            rid=self.repo.record_failure(connection_id,endpoint,ds,f'Normalization: {exc}',attempts=result.attempts)
            return CollectionOutcome('wildberries',ds,False,rid,str(exc))

    async def collect_wb_orders_day(self, connection_id: int, day: date, *, shop_id: int | None = None) -> CollectionOutcome:
        ds=day.isoformat()
        if self.wb is None:
            return self._failure(connection_id,'statistics/orders',ds,'wildberries',
                FetchResult.failure('wildberries','WB client is not configured',None,0))
        result=await self.wb.orders(ds,flag=1)
        operational=self._save_wb_statistics(connection_id,ds,'statistics/orders',result,shop_id)
        if getattr(self.wb,'sales_funnel_all',None) is not None:
            return await self.collect_wb_funnel_day(connection_id,day,shop_id=shop_id)
        return operational

    async def collect_ozon_orders_day(self, connection_id: int, day: date, *, shop_id: int | None = None) -> CollectionOutcome:
        ds = day.isoformat(); endpoint = 'analytics/orders'
        if self.ozon is None:
            rid = self.repo.record_failure(connection_id, endpoint, ds, 'Ozon client is not configured')
            return CollectionOutcome('ozon', ds, False, rid, 'Ozon client is not configured')
        payload = {'date_from': ds, 'date_to': ds, 'metrics': ['ordered_units', 'revenue'],
                   'dimension': ['sku'], 'limit': 1000, 'offset': 0}
        result = await self._ozon_analytics_all(payload)
        if not result.ok:
            return self._failure(connection_id, endpoint, ds, 'ozon', result)
        try:
            points = normalize_ozon_order_analytics(result.data, connection_id, ds)
            product_obs = normalize_ozon_product_analytics(result.data, default_date=ds)
            rid = self.repo.record_success(connection_id, endpoint, ds, result.data, points,
                                           attempts=result.attempts)
            if shop_id is not None:
                self._save_product_observations(shop_id=shop_id, connection_id=connection_id,
                                                marketplace='ozon', source_run_id=rid,
                                                observations=product_obs)
        except (NormalizationError, ProductNormalizationError, ValueError) as exc:
            rid = self.repo.record_failure(connection_id, endpoint, ds, f'Normalization: {exc}',
                                           http_status=result.status_code, attempts=result.attempts)
            return CollectionOutcome('ozon', ds, False, rid, str(exc))
        return CollectionOutcome('ozon', ds, True, rid, 'orders loaded')

    @staticmethod
    def _ozon_row_day(row: dict) -> str | None:
        for dim in row.get('dimensions') or []:
            if not isinstance(dim, dict):
                continue
            for key in ('id', 'value', 'name'):
                value = str(dim.get(key) or '')[:10]
                try:
                    date.fromisoformat(value)
                except ValueError:
                    continue
                return value
        return None

    async def backfill_orders(self, *, start: date, end: date, wb_connection_id: int | None = None,
                              ozon_connection_id: int | None = None, shop_id: int | None = None,
                              include_ozon_fulfillment: bool = True) -> list[CollectionOutcome]:
        if end < start:
            raise ValueError('end must not be before start')
        outcomes: list[CollectionOutcome] = []

        if wb_connection_id is not None:
            endpoint = 'statistics/orders/backfill'
            if self.wb is None:
                rid=self.repo.record_failure(wb_connection_id, endpoint, start.isoformat(), 'WB client is not configured')
                outcomes.append(CollectionOutcome('wildberries', start.isoformat(), False, rid, 'WB client is not configured'))
            else:
                result = await self.wb.orders_since(start.isoformat())
                if not result.ok:
                    rid=self.repo.record_failure(wb_connection_id, endpoint, start.isoformat(), result.error or 'WB error', http_status=result.status_code, attempts=result.attempts)
                    outcomes.append(CollectionOutcome('wildberries', start.isoformat(), False, rid, result.error or 'WB error'))
                else:
                    grouped: dict[str,list] = {}
                    for row in result.data or []:
                        ds=str(row.get('date',''))[:10]
                        if start.isoformat() <= ds <= end.isoformat():
                            grouped.setdefault(ds,[]).append(row)
                    current=start
                    while current <= end:
                        ds=current.isoformat(); rows=grouped.get(ds,[])
                        outcomes.append(self._save_wb_statistics(wb_connection_id,ds,endpoint,
                            FetchResult.success('wildberries',rows,200,result.attempts),shop_id))
                        current += timedelta(days=1)
            if self.wb is not None and getattr(self.wb,'sales_funnel_all',None) is not None:
                current=start
                while current<=end:
                    if self.repo.has_wb_funnel_snapshot(wb_connection_id,current.isoformat()):
                        run=self.repo.last_successful_order_run(wb_connection_id,current.isoformat())
                        outcomes.append(CollectionOutcome('wildberries',current.isoformat(),True,
                            run.id if run else 0,'sales funnel already loaded'))
                    else:
                        outcomes.append(await self.collect_wb_funnel_day(wb_connection_id,current,
                            shop_id=shop_id,backfill=True))
                    current += timedelta(days=1)

        if ozon_connection_id is not None:
            endpoint='analytics/orders/backfill'
            if self.ozon is None:
                rid=self.repo.record_failure(ozon_connection_id,endpoint,start.isoformat(),'Ozon client is not configured')
                outcomes.append(CollectionOutcome('ozon',start.isoformat(),False,rid,'Ozon client is not configured'))
            else:
                payload={'date_from':start.isoformat(),'date_to':end.isoformat(),'metrics':['ordered_units','revenue'],
                         'dimension':['sku','day'],'limit':1000,'offset':0}
                result=await self._ozon_analytics_all(payload)
                if not result.ok:
                    rid=self.repo.record_failure(ozon_connection_id,endpoint,start.isoformat(),result.error or 'Ozon error',http_status=result.status_code,attempts=result.attempts)
                    outcomes.append(CollectionOutcome('ozon',start.isoformat(),False,rid,result.error or 'Ozon error'))
                else:
                    try:
                        product_obs=normalize_ozon_product_analytics(result.data) if shop_id is not None else []
                        obs_by_day: dict[str,list[ProductDayObservation]] = {}
                        for obs in product_obs:
                            if start.isoformat() <= obs.data_date <= end.isoformat():
                                obs_by_day.setdefault(obs.data_date,[]).append(obs)
                        result_rows=((result.data or {}).get('result') or {}).get('data') or []
                        raw_by_day: dict[str,list[dict]] = {}
                        for row in result_rows:
                            ds=self._ozon_row_day(row) if isinstance(row,dict) else None
                            if ds and start.isoformat() <= ds <= end.isoformat():
                                raw_by_day.setdefault(ds,[]).append(row)
                    except ProductNormalizationError as exc:
                        rid=self.repo.record_failure(ozon_connection_id,endpoint,start.isoformat(),f'Normalization: {exc}',http_status=200,attempts=result.attempts)
                        outcomes.append(CollectionOutcome('ozon',start.isoformat(),False,rid,str(exc)))
                    else:
                        current=start
                        while current <= end:
                            ds=current.isoformat(); raw_rows=raw_by_day.get(ds,[])
                            synthetic={'result':{'data':raw_rows}}
                            # A successful empty day is represented by explicit zero totals.
                            if not raw_rows:
                                synthetic['result']['totals']=[0,0]
                            try:
                                points=normalize_ozon_order_analytics(synthetic,ozon_connection_id,ds)
                                rid=self.repo.record_success(ozon_connection_id,endpoint,ds,synthetic,points,attempts=result.attempts)
                                if shop_id is not None:
                                    self._save_product_observations(shop_id=shop_id,connection_id=ozon_connection_id,
                                        marketplace='ozon',source_run_id=rid,observations=obs_by_day.get(ds,[]))
                                outcomes.append(CollectionOutcome('ozon',ds,True,rid,'orders loaded'))
                            except (NormalizationError, ValueError) as exc:
                                rid=self.repo.record_failure(ozon_connection_id,endpoint,ds,f'Normalization: {exc}',attempts=result.attempts)
                                outcomes.append(CollectionOutcome('ozon',ds,False,rid,str(exc)))
                            current += timedelta(days=1)
        if include_ozon_fulfillment and ozon_connection_id is not None and shop_id is not None and self.ozon is not None:
            outcomes.extend(await self.collect_ozon_fulfillment_range(
                shop_id=shop_id,connection_id=ozon_connection_id,start=start,end=end))
        return outcomes

    async def collect_ozon_fulfillment_range(self, *, shop_id: int, connection_id: int,
                                             start: date, end: date) -> list[CollectionOutcome]:
        """Collect Ozon FBO/FBS posting split without redefining core ordered_units."""
        if self.ozon is None:
            rid=self.repo.record_failure(connection_id,'postings/fulfillment',start.isoformat(),'Ozon client is not configured')
            return [CollectionOutcome('ozon',start.isoformat(),False,rid,'Ozon client is not configured')]
        since,to=posting_range(start,end)
        outcomes:list[CollectionOutcome]=[]
        for scheme in ('FBO','FBS'):
            endpoint=f'postings/{scheme.lower()}'
            result=await self.ozon.postings_all(scheme,since,to)
            if not result.ok:
                outcomes.append(self._failure(connection_id,endpoint,start.isoformat(),'ozon',result))
                continue
            try:
                observations=normalize_ozon_postings(result.data,fulfillment_scheme=scheme)
            except ProductNormalizationError as exc:
                rid=self.repo.record_failure(connection_id,endpoint,start.isoformat(),f'Normalization: {exc}',attempts=result.attempts)
                outcomes.append(CollectionOutcome('ozon',start.isoformat(),False,rid,str(exc)))
                continue
            # Existing events retain original UTC timestamps, so their dates
            # can be repaired without deleting history or querying another day.
            self.repo.correct_ozon_posting_dates(connection_id)
            obs_by_day:dict[str,list[ProductDayObservation]]={}
            for obs in observations:
                if start.isoformat() <= obs.data_date <= end.isoformat():
                    obs_by_day.setdefault(obs.data_date,[]).append(obs)
            raw_postings=(result.data or {}).get('postings') or []
            raw_by_day:dict[str,list[dict]]={}
            for posting in raw_postings:
                if not isinstance(posting,dict): continue
                ds=posting_day(posting.get('created_at') or posting.get('in_process_at') or '')
                if ds and start.isoformat() <= ds <= end.isoformat(): raw_by_day.setdefault(ds,[]).append(posting)
            current=start
            while current <= end:
                ds=current.isoformat(); raw={'postings':raw_by_day.get(ds,[]),'scheme':scheme}
                request_with=(result.data or {}).get('request_with')
                if isinstance(request_with,dict):
                    raw['request_with']=dict(request_with)
                rid=self.repo.record_success(connection_id,endpoint,ds,raw,[],attempts=result.attempts)
                self._save_fulfillment_observations(shop_id=shop_id,connection_id=connection_id,
                    marketplace='ozon',source_run_id=rid,observations=obs_by_day.get(ds,[]))
                self._save_commerce_observations(shop_id=shop_id,connection_id=connection_id,
                    marketplace='ozon',source_run_id=rid,
                    observations=normalize_ozon_posting_events(raw,fulfillment_scheme=scheme,start_date=ds,end_date=ds))
                outcomes.append(CollectionOutcome('ozon',ds,True,rid,f'{scheme} fulfillment loaded'))
                current += timedelta(days=1)
        return outcomes

    async def collect_wb_sales_range(self, *, shop_id: int, connection_id: int,
                                     start: date, end: date) -> list[CollectionOutcome]:
        """Collect WB operational sales/returns for exact srid reconciliation."""
        endpoint='statistics/sales/reconciliation'
        if self.wb is None:
            rid=self.repo.record_failure(connection_id,endpoint,start.isoformat(),'WB client is not configured')
            return [CollectionOutcome('wildberries',start.isoformat(),False,rid,'WB client is not configured')]
        result=await self.wb.sales_since(start.isoformat())
        if not result.ok:
            return [self._failure(connection_id,endpoint,start.isoformat(),'wildberries',result)]
        try:
            observations=normalize_wb_sales_events(result.data,start_date=start.isoformat(),end_date=end.isoformat())
        except ReconciliationNormalizationError as exc:
            rid=self.repo.record_failure(connection_id,endpoint,start.isoformat(),f'Normalization: {exc}',attempts=result.attempts)
            return [CollectionOutcome('wildberries',start.isoformat(),False,rid,str(exc))]
        grouped: dict[str,list[CommerceObservation]]={}
        raw_by_day: dict[str,list[dict]]={}
        for obs in observations: grouped.setdefault(obs.data_date,[]).append(obs)
        for row in result.data or []:
            ds=str(row.get('date') or '')[:10]
            if start.isoformat() <= ds <= end.isoformat(): raw_by_day.setdefault(ds,[]).append(row)
        outcomes=[]; current=start
        while current <= end:
            ds=current.isoformat(); raw=raw_by_day.get(ds,[])
            rid=self.repo.record_success(connection_id,endpoint,ds,raw,[],attempts=result.attempts)
            self._save_commerce_observations(shop_id=shop_id,connection_id=connection_id,
                marketplace='wildberries',source_run_id=rid,observations=grouped.get(ds,[]))
            outcomes.append(CollectionOutcome('wildberries',ds,True,rid,'sales/returns loaded'))
            current += timedelta(days=1)
        return outcomes

    async def collect_inventory(self, *, shop_id: int, wb_connection_id: int | None = None,
                                ozon_connection_id: int | None = None,
                                data_date: date | None = None,
                                automatic: bool = False) -> list[CollectionOutcome]:
        """Refresh current stock snapshots. Each stock family is an independent run.

        Automatic refreshes skip WB stock endpoints that returned 401/403 earlier
        in this process. Manual refreshes still probe them; a successful manual
        probe clears the suppression immediately.
        """
        ds=(data_date or date.today()).isoformat()
        outcomes: list[CollectionOutcome] = []

        if wb_connection_id is not None:
            for kind, scheme in (('wb','FBW'),('seller','FBS')):
                endpoint=f'analytics/stocks/{kind}-warehouses'
                if automatic and endpoint in self._auto_disabled_endpoints:
                    continue
                if self.wb is None:
                    rid=self.repo.record_failure(wb_connection_id,endpoint,ds,'WB client is not configured')
                    outcomes.append(CollectionOutcome('wildberries',ds,False,rid,'WB client is not configured'))
                    continue
                result=await self.wb.stock_report_all(kind)
                if not result.ok:
                    outcomes.append(self._failure(wb_connection_id,endpoint,ds,'wildberries',result))
                    if result.status_code in {401,403}:
                        if endpoint not in self._auto_disabled_endpoints:
                            log.warning(
                                'Suppressing automatic WB inventory endpoint after HTTP %s until restart or manual success: %s',
                                result.status_code,endpoint)
                        self._auto_disabled_endpoints.add(endpoint)
                    continue
                self._auto_disabled_endpoints.discard(endpoint)
                try:
                    observations=normalize_wb_stocks(result.data,fulfillment_scheme=scheme)
                    rid=self.repo.record_success(wb_connection_id,endpoint,ds,result.data,[],attempts=result.attempts,store_raw=False)
                    self._save_inventory_observations(shop_id=shop_id,connection_id=wb_connection_id,
                        marketplace='wildberries',source_run_id=rid,observations=observations)
                    outcomes.append(CollectionOutcome('wildberries',ds,True,rid,f'{scheme} stocks loaded'))
                except (ProductNormalizationError,ValueError) as exc:
                    rid=self.repo.record_failure(wb_connection_id,endpoint,ds,f'Normalization: {exc}',attempts=result.attempts)
                    outcomes.append(CollectionOutcome('wildberries',ds,False,rid,str(exc)))

        if ozon_connection_id is not None:
            endpoint='product/info/stocks'
            if self.ozon is None:
                rid=self.repo.record_failure(ozon_connection_id,endpoint,ds,'Ozon client is not configured')
                outcomes.append(CollectionOutcome('ozon',ds,False,rid,'Ozon client is not configured'))
            else:
                result=await self.ozon.product_stocks_all()
                if not result.ok:
                    outcomes.append(self._failure(ozon_connection_id,endpoint,ds,'ozon',result))
                else:
                    try:
                        observations=normalize_ozon_stocks(result.data)
                        rid=self.repo.record_success(ozon_connection_id,endpoint,ds,result.data,[],attempts=result.attempts,store_raw=False)
                        self._save_inventory_observations(shop_id=shop_id,connection_id=ozon_connection_id,
                            marketplace='ozon',source_run_id=rid,observations=observations)
                        outcomes.append(CollectionOutcome('ozon',ds,True,rid,'stocks loaded'))
                    except (ProductNormalizationError,ValueError) as exc:
                        rid=self.repo.record_failure(ozon_connection_id,endpoint,ds,f'Normalization: {exc}',attempts=result.attempts)
                        outcomes.append(CollectionOutcome('ozon',ds,False,rid,str(exc)))
        return outcomes

    async def collect_finance(self, *, start: date, end: date,
                              wb_connection_id: int | None = None,
                              ozon_connection_id: int | None = None,
                              shop_id: int | None = None) -> list[CollectionOutcome]:
        """Refresh delayed official finance. Missing dates remain missing; never synthesized as zero."""
        outcomes: list[CollectionOutcome]=[]
        if wb_connection_id is not None:
            endpoint='finance/sales-reports/list'
            if self.wb is None:
                rid=self.repo.record_failure(wb_connection_id,endpoint,start.isoformat(),'WB client is not configured')
                outcomes.append(CollectionOutcome('wildberries',start.isoformat(),False,rid,'WB client is not configured'))
            else:
                result=await self.wb.finance_sales_reports_list_all(start.isoformat(),end.isoformat(),period='daily')
                if not result.ok:
                    outcomes.append(self._failure(wb_connection_id,endpoint,start.isoformat(),'wildberries',result))
                else:
                    rows=result.data if isinstance(result.data,list) else []
                    # Daily reports can have multiple types; one metric version
                    # must contain all reports rather than retain only the last.
                    unique={}
                    for index,row in enumerate(rows):
                        key=('report',str(row['reportId'])) if isinstance(row,dict) and row.get('reportId') is not None else ('row',index)
                        unique[key]=row
                    grouped_reports={}
                    for row in unique.values():
                        try:
                            ds,points=normalize_wb_finance_report(row,wb_connection_id)
                            if not (start.isoformat() <= ds <= end.isoformat()): continue
                            bucket=grouped_reports.setdefault(ds,{'rows':[],'points':{}})
                            bucket['rows'].append(row)
                            for point in points:
                                old=bucket['points'].get(point.metric_key)
                                bucket['points'][point.metric_key]=MetricPoint(wb_connection_id,ds,point.metric_key,
                                    point.value+(old.value if old else 0),point.unit,False,
                                    max(point.as_of or '',old.as_of or '') if old else point.as_of)
                        except (FinanceNormalizationError,ValueError) as exc:
                            rid=self.repo.record_failure(wb_connection_id,endpoint,start.isoformat(),f'Normalization: {exc}',attempts=result.attempts)
                            outcomes.append(CollectionOutcome('wildberries',start.isoformat(),False,rid,str(exc)))
                    for ds,bucket in grouped_reports.items():
                        rid=self.repo.record_success(wb_connection_id,endpoint,ds,{'reports':bucket['rows']},
                            list(bucket['points'].values()),attempts=result.attempts)
                        outcomes.append(CollectionOutcome('wildberries',ds,True,rid,'finance loaded'))
                    if shop_id is not None:
                        detail_endpoint='finance/sales-reports/detailed'
                        detail=await self.wb.finance_sales_report_detailed_all(start.isoformat(),end.isoformat(),period='daily')
                        if not detail.ok:
                            outcomes.append(self._failure(wb_connection_id,detail_endpoint,start.isoformat(),'wildberries',detail))
                        else:
                            try:
                                observations=normalize_wb_product_finance(detail.data)
                                commerce_observations=normalize_wb_finance_events(detail.data,start_date=start.isoformat(),end_date=end.isoformat())
                                commerce_grouped: dict[str,list[CommerceObservation]]={}
                                for event in commerce_observations:
                                    commerce_grouped.setdefault(event.data_date,[]).append(event)
                                grouped: dict[str,list[ProductFinanceObservation]]={}
                                for obs in observations:
                                    if start.isoformat() <= obs.data_date <= end.isoformat():
                                        grouped.setdefault(obs.data_date,[]).append(obs)
                                for ds,items in grouped.items():
                                    raw={'day':ds,'items':[{'sku':x.marketplace_sku,'metrics':x.metrics} for x in items],
                                         'event_fingerprints':[x.fingerprint for x in commerce_grouped.get(ds,[])]}
                                    rid=self.repo.record_success(wb_connection_id,detail_endpoint,ds,raw,[],attempts=detail.attempts,store_raw=False)
                                    self._save_product_finance_observations(shop_id=shop_id,connection_id=wb_connection_id,
                                        marketplace='wildberries',source_run_id=rid,observations=items)
                                    self._save_commerce_observations(shop_id=shop_id,connection_id=wb_connection_id,
                                        marketplace='wildberries',source_run_id=rid,observations=commerce_grouped.get(ds,[]))
                                    outcomes.append(CollectionOutcome('wildberries',ds,True,rid,'SKU finance loaded'))
                            except (FinanceNormalizationError,ValueError) as exc:
                                rid=self.repo.record_failure(wb_connection_id,detail_endpoint,start.isoformat(),f'Normalization: {exc}',attempts=detail.attempts)
                                outcomes.append(CollectionOutcome('wildberries',start.isoformat(),False,rid,str(exc)))
        if ozon_connection_id is not None:
            endpoint='finance/accrual/by-day'
            if self.ozon is None:
                rid=self.repo.record_failure(ozon_connection_id,endpoint,start.isoformat(),'Ozon client is not configured')
                outcomes.append(CollectionOutcome('ozon',start.isoformat(),False,rid,'Ozon client is not configured'))
            else:
                current=start
                while current<=end:
                    ds=current.isoformat(); result=await self.ozon.finance_accrual_by_day_all(ds)
                    if not result.ok:
                        outcomes.append(self._failure(ozon_connection_id,endpoint,ds,'ozon',result))
                    else:
                        try:
                            points=normalize_ozon_accruals(result.data,ozon_connection_id,ds)
                            product_finance=normalize_ozon_product_finance(result.data,ds)
                            commerce=normalize_ozon_finance_events(result.data,data_date=ds) if shop_id is not None else []
                            rid=self.repo.record_success(ozon_connection_id,endpoint,ds,result.data,points,attempts=result.attempts)
                            if shop_id is not None:
                                self._save_product_finance_observations(shop_id=shop_id,connection_id=ozon_connection_id,
                                    marketplace='ozon',source_run_id=rid,observations=product_finance,replace_finance_snapshot=True)
                                self._save_commerce_observations(shop_id=shop_id,connection_id=ozon_connection_id,
                                    marketplace='ozon',source_run_id=rid,
                                    observations=commerce,
                                    replace_finance_snapshot=True)
                            outcomes.append(CollectionOutcome('ozon',ds,True,rid,'finance loaded'))
                        except (FinanceNormalizationError,ValueError) as exc:
                            rid=self.repo.record_failure(ozon_connection_id,endpoint,ds,f'Normalization: {exc}',attempts=result.attempts)
                            outcomes.append(CollectionOutcome('ozon',ds,False,rid,str(exc)))
                    current += timedelta(days=1)
        return outcomes

    async def collect_advertising(self, *, start: date, end: date,
                                  wb_connection_id: int | None = None,
                                  ozon_connection_id: int | None = None) -> list[CollectionOutcome]:
        outcomes: list[CollectionOutcome]=[]
        if wb_connection_id is not None and self.wb is not None:
            endpoint='promotion/fullstats'
            result=await self.wb.promotion_fullstats_all(start.isoformat(),end.isoformat())
            if not result.ok:
                outcomes.append(self._failure(wb_connection_id,endpoint,start.isoformat(),'wildberries',result))
            else:
                try:
                    grouped=normalize_wb_ad_stats(result.data,wb_connection_id)
                    campaigns,products=normalize_wb_ad_detail(result.data,wb_connection_id)
                except (FinanceNormalizationError,AdvertisingNormalizationError) as exc:
                    rid=self.repo.record_failure(wb_connection_id,endpoint,start.isoformat(),f'Normalization: {exc}',attempts=result.attempts)
                    outcomes.append(CollectionOutcome('wildberries',start.isoformat(),False,rid,str(exc)))
                else:
                    campaign_days={}
                    product_days={}
                    for row in campaigns: campaign_days.setdefault(row.data_date,[]).append(row)
                    for row in self._resolve_ad_product_listings(wb_connection_id,products): product_days.setdefault(row.data_date,[]).append(row)
                    all_days=[(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
                    for ds in all_days:
                        points=grouped.get(ds,[])
                        if not points:
                            points=[MetricPoint(wb_connection_id,ds,'ad_spend',0,'RUB'),
                                    MetricPoint(wb_connection_id,ds,'ad_attributed_sales',0,'RUB')]
                        detail_payload={
                            'day':ds,
                            'campaigns':[vars(x) for x in campaign_days.get(ds,[])],
                            'products':[vars(x) for x in product_days.get(ds,[])],
                        }
                        rid=self.repo.record_success(wb_connection_id,endpoint,ds,detail_payload,points,
                            attempts=result.attempts,store_raw=False)
                        self.repo.save_ad_details(rid,campaigns=campaign_days.get(ds,[]),products=product_days.get(ds,[]),
                            replace_campaign_snapshot=True,replace_product_snapshot=True)
                        outcomes.append(CollectionOutcome('wildberries',ds,True,rid,'ads loaded'))

        if ozon_connection_id is not None and self.ozon_performance is not None:
            current=start
            while current <= end:
                ds=current.isoformat(); endpoint='performance/product-stats'
                result=await self.ozon_performance.product_campaign_stats(ds,ds)
                if not result.ok:
                    outcomes.append(self._failure(ozon_connection_id,endpoint,ds,'ozon',result))
                else:
                    try:
                        campaigns=normalize_ozon_ad_campaign_detail(result.data,ozon_connection_id,ds)
                        grouped=normalize_ozon_ad_stats(result.data,ozon_connection_id)
                    except (FinanceNormalizationError,AdvertisingNormalizationError) as exc:
                        rid=self.repo.record_failure(ozon_connection_id,endpoint,ds,f'Normalization: {exc}',attempts=result.attempts)
                        outcomes.append(CollectionOutcome('ozon',ds,False,rid,str(exc)))
                    else:
                        # Campaign endpoint can be period-shaped without a date. For one-day requests, force the requested date.
                        points=grouped.get(ds,[])
                        if not points and campaigns:
                            spend=sum(x.spend for x in campaigns); sales=sum(x.attributed_sales for x in campaigns)
                            now=campaigns[0].data_date
                            points=[MetricPoint(ozon_connection_id,ds,'ad_spend',spend,'RUB',False,now),
                                    MetricPoint(ozon_connection_id,ds,'ad_attributed_sales',sales,'RUB',False,now)]
                        if not points:
                            points=[MetricPoint(ozon_connection_id,ds,'ad_spend',0,'RUB'),
                                    MetricPoint(ozon_connection_id,ds,'ad_attributed_sales',0,'RUB')]
                        payload={'day':ds,'campaigns':[vars(x) for x in campaigns]}
                        rid=self.repo.record_success(ozon_connection_id,endpoint,ds,payload,points,
                            attempts=result.attempts,store_raw=False)
                        self.repo.save_ad_details(rid,campaigns=campaigns,replace_campaign_snapshot=True)
                        outcomes.append(CollectionOutcome('ozon',ds,True,rid,'campaign ads loaded'))

                # SKU statistics is a newer Performance endpoint. Keep the collector
                # compatible with injected/legacy clients that do not implement it yet.
                product_stats=getattr(self.ozon_performance,'product_sku_stats',None)
                if callable(product_stats):
                    product_endpoint='performance/products-sku'
                    detail=await product_stats(ds,ds)
                    if not detail.ok:
                        outcomes.append(self._failure(ozon_connection_id,product_endpoint,ds,'ozon',detail))
                    else:
                        try:
                            products=normalize_ozon_ad_product_detail(detail.data,ozon_connection_id,ds)
                            products=self._resolve_ad_product_listings(ozon_connection_id,products)
                        except AdvertisingNormalizationError as exc:
                            rid=self.repo.record_failure(ozon_connection_id,product_endpoint,ds,f'Normalization: {exc}',attempts=detail.attempts)
                            outcomes.append(CollectionOutcome('ozon',ds,False,rid,str(exc)))
                        else:
                            payload={'day':ds,'products':[vars(x) for x in products]}
                            rid=self.repo.record_success(ozon_connection_id,product_endpoint,ds,payload,[],
                                attempts=detail.attempts,store_raw=False)
                            self.repo.save_ad_details(rid,products=products,replace_product_snapshot=True)
                            outcomes.append(CollectionOutcome('ozon',ds,True,rid,'SKU ads loaded'))
                current += timedelta(days=1)
        return outcomes


    async def collect_inbound(self, *, shop_id: int, wb_connection_id: int | None = None,
                              ozon_connection_id: int | None = None,
                              data_date: date | None = None) -> list[CollectionOutcome]:
        """Refresh marketplace inbound supplies used by replenishment planning."""
        ds=(data_date or date.today()).isoformat(); outcomes=[]

        if wb_connection_id is not None:
            endpoint='supplies/fbw/inbound'
            if self.wb is None:
                rid=self.repo.record_failure(wb_connection_id,endpoint,ds,'WB client is not configured')
                outcomes.append(CollectionOutcome('wildberries',ds,False,rid,'WB client is not configured'))
            else:
                result=await self.wb.fbw_supplies_all(status_ids=[1,2,3,4,6])
                if not result.ok:
                    outcomes.append(self._failure(wb_connection_id,endpoint,ds,'wildberries',result))
                else:
                    normalized=[]; raw=[]; attempts=result.attempts
                    active_external_ids=[]
                    for row in result.data or []:
                        if row.get('supplyID') is not None:
                            active_external_ids.append(f"supply:{row.get('supplyID')}")
                        elif row.get('preorderID') is not None:
                            active_external_ids.append(f"preorder:{row.get('preorderID')}")
                        supply_id=row.get('supplyID')
                        preorder_id=row.get('preorderID')
                        lookup=supply_id if supply_id is not None else preorder_id
                        if lookup is None: continue
                        goods=await self.wb.fbw_supply_goods_all(int(lookup),is_preorder_id=supply_id is None)
                        attempts += goods.attempts
                        if not goods.ok:
                            # A single malformed/inaccessible supply should not erase all known inbound state.
                            continue
                        normalized.append(normalize_wb_supply(row,goods.data or []))
                        raw.append({'supply':row,'goods':goods.data or []})
                    rid=self.repo.record_success(wb_connection_id,endpoint,ds,raw,[],attempts=attempts,store_raw=False,
                                                 status='success' if len(normalized)==len(result.data or []) else 'partial')
                    self.repo.upsert_inbound_shipments(
                        wb_connection_id,rid,'wildberries',normalized,active_external_ids=active_external_ids)
                    outcomes.append(CollectionOutcome('wildberries',ds,True,rid,f'inbound supplies loaded: {len(normalized)}'))

        if ozon_connection_id is not None:
            endpoint='supply-order/inbound'
            if self.ozon is None:
                rid=self.repo.record_failure(ozon_connection_id,endpoint,ds,'Ozon client is not configured')
                outcomes.append(CollectionOutcome('ozon',ds,False,rid,'Ozon client is not configured'))
            else:
                listed=await self.ozon.supply_orders_all()
                if not listed.ok:
                    outcomes.append(self._failure(ozon_connection_id,endpoint,ds,'ozon',listed))
                else:
                    order_ids=list((listed.data or {}).get('order_ids') or [])
                    normalized=[]; raw=[]; attempts=listed.attempts; partial=False
                    for i in range(0,len(order_ids),100):
                        details=await self.ozon.supply_orders_get(order_ids[i:i+100]); attempts += details.attempts
                        if not details.ok:
                            partial=True; continue
                        orders=(details.data or {}).get('orders') or []
                        for order in orders:
                            bundles={}
                            for supply in order.get('supplies') or []:
                                bundle_id=str((supply or {}).get('bundle_id') or '')
                                if not bundle_id: continue
                                bundle=await self.ozon.supply_bundle_all(bundle_id); attempts += bundle.attempts
                                if not bundle.ok:
                                    partial=True; continue
                                bundles[bundle_id]=list((bundle.data or {}).get('items') or [])
                            normalized.extend(normalize_ozon_order(order,bundles))
                            raw.append({'order':order,'bundles':bundles})
                    rid=self.repo.record_success(ozon_connection_id,endpoint,ds,raw,[],attempts=attempts,store_raw=False,
                                                 status='partial' if partial else 'success')
                    self.repo.upsert_inbound_shipments(
                        ozon_connection_id,rid,'ozon',normalized,
                        active_external_ids=None if partial else [x['external_supply_id'] for x in normalized])
                    outcomes.append(CollectionOutcome('ozon',ds,True,rid,f'inbound supplies loaded: {len(normalized)}'))
        return outcomes


    async def collect_promotions(self, *, shop_id: int, as_of: date,
                                 wb_connection_id: int | None=None,
                                 ozon_connection_id: int | None=None,
                                 history_days: int=120, future_days: int=60) -> list[CollectionOutcome]:
        """Refresh promotion calendars and exact participating products.

        Product-detail failure makes a marketplace run partial, but the calendar
        itself is still stored. That lets reports remain useful without pretending
        SKU participation is known.
        """
        start=as_of-timedelta(days=max(1,int(history_days)))
        end=as_of+timedelta(days=max(1,int(future_days)))
        outcomes=[]; ds=as_of.isoformat()
        if wb_connection_id is not None:
            endpoint='calendar/promotions'
            if self.wb is None:
                rid=self.repo.record_failure(wb_connection_id,endpoint,ds,'WB client is not configured')
                outcomes.append(CollectionOutcome('wildberries',ds,False,rid,'WB client is not configured'))
            else:
                result=await self.wb.calendar_promotions_all(start.isoformat()+'T00:00:00Z',end.isoformat()+'T23:59:59Z',all_promo=True)
                if not result.ok:
                    outcomes.append(self._failure(wb_connection_id,endpoint,ds,'wildberries',result))
                else:
                    promos=((result.data or {}).get('data') or {}).get('promotions') or []
                    product_payloads={}; attempts=result.attempts; partial=False
                    for promo in promos:
                        if not isinstance(promo,dict) or promo.get('id') is None: continue
                        # WB documents nomenclatures as not applicable to auto promotions.
                        # Keep the calendar event, but never turn this expected limitation into a retry storm.
                        if 'auto' in str(promo.get('type') or '').casefold():
                            continue
                        detail=await self.wb.calendar_promotion_products_all(int(promo['id']),in_action=True)
                        attempts += detail.attempts
                        if detail.ok: product_payloads[str(promo['id'])]=detail.data
                        else: partial=True
                    try:
                        normalized=normalize_wb_promotions(result.data,product_payloads)
                        raw={'calendar':result.data,'participating_products':product_payloads}
                        rid=self.repo.record_success(wb_connection_id,endpoint,ds,raw,[],attempts=attempts,
                                                     store_raw=False,status='partial' if partial else 'success')
                        self.repo.upsert_promotions(wb_connection_id,rid,'wildberries',normalized,
                            complete_external_ids=[str(x.get('id')) for x in promos if isinstance(x,dict) and x.get('id') is not None])
                        outcomes.append(CollectionOutcome('wildberries',ds,not partial,rid,
                            'promotion calendar loaded' if not partial else 'promotion calendar loaded partially'))
                    except (PromotionNormalizationError,ValueError) as exc:
                        rid=self.repo.record_failure(wb_connection_id,endpoint,ds,f'Normalization: {exc}',attempts=attempts)
                        outcomes.append(CollectionOutcome('wildberries',ds,False,rid,str(exc)))

        if ozon_connection_id is not None:
            endpoint='actions/promotions'
            if self.ozon is None:
                rid=self.repo.record_failure(ozon_connection_id,endpoint,ds,'Ozon client is not configured')
                outcomes.append(CollectionOutcome('ozon',ds,False,rid,'Ozon client is not configured'))
            else:
                result=await self.ozon.promotions_list()
                if not result.ok:
                    outcomes.append(self._failure(ozon_connection_id,endpoint,ds,'ozon',result))
                else:
                    actions=(result.data or {}).get('result') or []
                    products_by_action={}; product_ids=set(); attempts=result.attempts; partial=False
                    for action in actions:
                        if not isinstance(action,dict) or action.get('id') is None: continue
                        detail=await self.ozon.promotion_products_all(int(action['id']))
                        attempts += detail.attempts
                        if not detail.ok:
                            partial=True; continue
                        products_by_action[str(action['id'])]=detail.data
                        for item in ((detail.data or {}).get('result') or {}).get('products') or []:
                            if isinstance(item,dict) and item.get('id') is not None:
                                try: product_ids.add(int(item['id']))
                                except (TypeError,ValueError): pass
                    info_map={}
                    for chunk_start in range(0,len(product_ids),1000):
                        chunk=list(sorted(product_ids))[chunk_start:chunk_start+1000]
                        if not chunk: continue
                        info=await self.ozon.product_info_list(product_ids=chunk); attempts += info.attempts
                        if not info.ok:
                            partial=True; continue
                        body=info.data if isinstance(info.data,dict) else {}; inner=body.get('result') or {}
                        rows=inner.get('items') if isinstance(inner,dict) else None
                        rows=rows if isinstance(rows,list) else body.get('items') or []
                        for row in rows:
                            if not isinstance(row,dict): continue
                            pid=row.get('id') if row.get('id') is not None else row.get('product_id')
                            if pid is not None: info_map[str(pid)]=row
                    try:
                        normalized=normalize_ozon_promotions(result.data,products_by_action,info_map)
                        raw={'actions':result.data,'products':products_by_action}
                        rid=self.repo.record_success(ozon_connection_id,endpoint,ds,raw,[],attempts=attempts,
                                                     store_raw=False,status='partial' if partial else 'success')
                        self.repo.upsert_promotions(ozon_connection_id,rid,'ozon',normalized,complete_external_ids=None)
                        outcomes.append(CollectionOutcome('ozon',ds,not partial,rid,
                            'promotion calendar loaded' if not partial else 'promotion calendar loaded partially'))
                    except (PromotionNormalizationError,ValueError) as exc:
                        rid=self.repo.record_failure(ozon_connection_id,endpoint,ds,f'Normalization: {exc}',attempts=attempts)
                        outcomes.append(CollectionOutcome('ozon',ds,False,rid,str(exc)))
        return outcomes
