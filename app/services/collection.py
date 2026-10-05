"""Orchestrates API -> normalization -> durable storage per marketplace."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable
import json
import logging
import asyncio

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
    normalize_wb_stocks, normalize_ozon_stocks, normalize_ozon_fbo_stocks, normalize_ozon_postings,
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
from .promotions import normalize_wb_promotions, normalize_ozon_promotions, PromotionNormalizationError, wb_promotion_finished
from .ozon_dates import MOSCOW, posting_day, posting_range
from .wb_funnel import normalize_wb_funnel
from .daily_events import (
    WB_SALES, WB_CANCELS, OZON_RETURNS, OZON_REALIZATION, DailyEventError,
    split_event_rows, normalize_ozon_realization_day, normalize_wb_sales_day, wb_event_day,
    normalize_wb_cancellations_day, normalize_ozon_returns_day,
)

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
    _shared_ozon_event_disabled: dict[str, set[str]] = {}

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
        scope=getattr(ozon,'rate_scope',None)
        self._event_disabled=self._shared_ozon_event_disabled.setdefault(scope,set()) if scope else set()

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

    async def collect_ozon_buyout_prices_range(self, *, connection_id: int,
                                              start: date, end: date,
                                              as_of: date | None = None) -> list[CollectionOutcome]:
        """Keep raw buyout reports for a bounded order range, with delayed dates.

        Look up to 60 subsequent calendar days (never beyond today), split
        financial periods into disjoint pages of at most 31 days. The window
        is a lookup policy, not a guarantee of when Ozon finalizes a buyout.
        Store once per refresh, rather than duplicating a large report per day.
        """
        today = as_of or datetime.now(MOSCOW).date()
        if not 0 <= (end-start).days < 90 or end > today:
            raise ValueError('Buyout order range: 1–90 days, ending no later than today')
        endpoint = 'finance/products/buyout'
        report_end = min(today,end+timedelta(days=60))
        reports=[]; errors=[]; attempts=0
        first=start
        while first <= report_end:
            last=min(first+timedelta(days=30),report_end)
            try:
                result = (await self.ozon.finance_products_buyout(first.isoformat(),last.isoformat())
                          if self.ozon else FetchResult.failure('ozon','Ozon client is not configured',None,1))
            except Exception:
                log.warning('Ozon buyout request failed before a response')
                result = FetchResult.failure('ozon','Buyout request failed before a response',None,1)
            attempts += result.attempts
            entry={'date_from':first.isoformat(),'date_to':last.isoformat(),
                   'http_status':result.status_code,'ok':result.ok}
            if result.ok:
                entry['response']=result.data
                valid = (isinstance(result.data,dict) and isinstance(result.data.get('products'),list)
                         and all(isinstance(row,dict) for row in result.data['products']))
                if not valid:
                    entry['ok']=False
                    entry['error']='Buyout response has no valid products list'
                    errors.append((entry['error'],result.status_code))
            else:
                entry['error']=result.error or 'Ozon buyout report failed'
                errors.append((entry['error'],result.status_code))
            reports.append(entry)
            if not result.ok and (self.ozon is None or result.status_code in {401,403}):
                break
            first=last+timedelta(days=1)
        run_id=0
        # Preserve unexpected successful HTTP bodies too, for contract review.
        if any('response' in entry for entry in reports):
            raw={'order_date_from':start.isoformat(),'order_date_to':end.isoformat(),
                 'report_date_from':start.isoformat(),'report_date_to':report_end.isoformat(),
                 'as_of':today.isoformat(),'reports':reports}
            run_id=self.repo.record_success(connection_id,endpoint,end.isoformat(),raw,[],
                attempts=attempts,status='partial' if errors else 'success')
        if errors:
            error='; '.join(dict.fromkeys(message for message,_ in errors))
            day=start
            while day <= end:
                failure_id=self.repo.record_failure(connection_id,endpoint,day.isoformat(),error,
                    http_status=errors[0][1],attempts=attempts)
                if not run_id:run_id=failure_id
                day+=timedelta(days=1)
        return [CollectionOutcome('ozon',end.isoformat(),not errors,run_id,
                                  'buyout reports captured' if not errors else 'buyout report incomplete')]

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
            list_run_ids: dict[str, int] = {}
            current=start
            while current <= end:
                ds=current.isoformat(); raw={'postings':raw_by_day.get(ds,[]),'scheme':scheme}
                request_with=(result.data or {}).get('request_with')
                if isinstance(request_with,dict):
                    raw['request_with']=dict(request_with)
                rid=self.repo.record_success(connection_id,endpoint,ds,raw,[],attempts=result.attempts)
                list_run_ids[ds] = rid
                self._save_fulfillment_observations(shop_id=shop_id,connection_id=connection_id,
                    marketplace='ozon',source_run_id=rid,observations=obs_by_day.get(ds,[]))
                self._save_commerce_observations(shop_id=shop_id,connection_id=connection_id,
                    marketplace='ozon',source_run_id=rid,
                    observations=normalize_ozon_posting_events(raw,fulfillment_scheme=scheme,start_date=ds,end_date=ds))
                outcomes.append(CollectionOutcome('ozon',ds,True,rid,f'{scheme} fulfillment loaded'))
                current += timedelta(days=1)
            if scheme == 'FBO':
                outcomes.extend(await self._collect_ozon_fbo_details_range(
                    connection_id=connection_id, raw_by_day=raw_by_day, list_run_ids=list_run_ids,
                    start=start, end=end))
        return outcomes

    async def _collect_ozon_fbo_details_range(self, *, connection_id: int,
                                             raw_by_day: dict[str, list[dict]],
                                             list_run_ids: dict[str, int],
                                             start: date, end: date) -> list[CollectionOutcome]:
        """Capture individual FBO responses independently of the list snapshot.

        Use the list's Moscow date and unique posting number as the request
        basis, including cancellations. Do not replace list fields or produce
        order/finance metrics from these still-unverified customer prices.
        """
        outcomes: list[CollectionOutcome] = []
        blocked_status: int | None = None
        current = start
        while current <= end:
            ds = current.isoformat()
            numbers: list[str] = []
            errors: list[dict] = []
            seen: set[str] = set()
            for index, posting in enumerate(raw_by_day.get(ds, [])):
                number = posting.get('posting_number')
                if not isinstance(number, str) or not number.strip():
                    errors.append({'posting_number': None, 'list_index': index,
                                   'message': 'FBO list entry has no posting_number',
                                   'http_status': None, 'skipped': True})
                    continue
                number = number.strip()
                if number not in seen:
                    numbers.append(number)
                    seen.add(number)
            raw = {'scheme': 'FBO', 'request_endpoint': '/v2/posting/fbo/get',
                   'request_with': {'financial_data': True},
                   'list_source_run_id': list_run_ids[ds], 'expected_postings': numbers,
                   'requested_postings': [], 'responses': [], 'errors': errors}
            attempts = 0
            for number in numbers:
                if blocked_status is not None:
                    errors.append({'posting_number': number, 'http_status': None,
                                   'blocked_http_status': blocked_status, 'skipped': True,
                                   'message': f'Lookup skipped after HTTP {blocked_status} in this refresh'})
                    continue
                raw['requested_postings'].append(number)
                result = await self.ozon.fbo_posting_details(number)
                attempts += result.attempts
                body = result.data
                detail = body.get('result') if isinstance(body, dict) else None
                error = result.error or 'FBO detail request failed'
                if result.ok:
                    if not isinstance(detail, dict):
                        error = 'FBO detail response has no result object'
                    elif detail.get('posting_number') != number:
                        error = 'FBO detail response posting_number does not match the request'
                    else:
                        raw['responses'].append({'posting_number': number,
                                                 'http_status': result.status_code,
                                                 'response': body})
                        continue
                failure = {'posting_number': number, 'http_status': result.status_code,
                           'message': error[:1500], 'skipped': False}
                if result.ok:
                    # Retain malformed successful HTTP bodies for diagnosis,
                    # without accepting another posting's prices as this one's.
                    failure['response'] = body
                errors.append(failure)
                self.repo.record_failure(connection_id, 'postings/fbo/get', ds,
                                         f'{number}: {error}', http_status=result.status_code,
                                         attempts=result.attempts)
                if result.status_code in {401, 403, 429}:
                    blocked_status = result.status_code
            complete = not errors
            raw['complete'] = complete
            rid = self.repo.record_success(connection_id, 'postings/fbo/details', ds, raw, [],
                                           attempts=attempts, status='success' if complete else 'partial')
            outcomes.append(CollectionOutcome('ozon', ds, complete, rid,
                                               f'FBO details: {len(raw["responses"])}/{len(numbers)} loaded'))
            current += timedelta(days=1)
        return outcomes

    async def collect_wb_sales_range(self, *, shop_id: int, connection_id: int,
                                     start: date, end: date) -> list[CollectionOutcome]:
        """Collect WB operational sales/returns for exact srid reconciliation."""
        if end < start:
            raise ValueError('Дата окончания должна быть не раньше даты начала')
        endpoint=WB_SALES

        def failed_range(result: FetchResult) -> list[CollectionOutcome]:
            return [self._failure(connection_id, endpoint,
                (start + timedelta(days=offset)).isoformat(), 'wildberries', result)
                for offset in range((end-start).days+1)]

        if self.wb is None:
            return failed_range(FetchResult.failure('wildberries', 'WB client is not configured', None, 0))
        try:
            result=await self.wb.sales_since(start.isoformat())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('WB operational sales/returns source failed')
            result=FetchResult.failure('wildberries', 'Не удалось загрузить продажи и возвраты WB.', None, 1)
        if not result.ok:
            return failed_range(result)
        try:
            observations=normalize_wb_sales_events(result.data,start_date=start.isoformat(),end_date=end.isoformat())
            normalize_wb_sales_day(result.data,start.isoformat())
        except (ReconciliationNormalizationError,DailyEventError) as exc:
            failed=[]
            for offset in range((end-start).days+1):
                ds=(start+timedelta(days=offset)).isoformat()
                rid=self.repo.record_failure(connection_id,endpoint,ds,f'Normalization: {exc}',attempts=result.attempts)
                failed.append(CollectionOutcome('wildberries',ds,False,rid,str(exc)))
            return failed
        grouped: dict[str,list[CommerceObservation]]={}
        raw_by_day: dict[str,list[dict]]={}
        for obs in observations: grouped.setdefault(obs.data_date,[]).append(obs)
        for row in result.data or []:
            ds=wb_event_day(row.get('date'))
            if ds is not None and start.isoformat() <= ds <= end.isoformat():raw_by_day.setdefault(ds,[]).append(row)
        outcomes=[]; current=start
        while current <= end:
            ds=current.isoformat(); raw=raw_by_day.get(ds,[])
            # Validate event-day quantities/prices separately from reconciliation.
            # Unknown event identities must not become a successful zero day.
            try:
                normalize_wb_sales_day(raw, ds)
            except DailyEventError as exc:
                rid=self.repo.record_failure(connection_id,endpoint,ds,str(exc),attempts=result.attempts)
                outcomes.append(CollectionOutcome('wildberries',ds,False,rid,str(exc)))
                current += timedelta(days=1)
                continue
            rid=self.repo.record_success(connection_id,endpoint,ds,raw,[],attempts=result.attempts)
            self._save_commerce_observations(shop_id=shop_id,connection_id=connection_id,
                marketplace='wildberries',source_run_id=rid,observations=grouped.get(ds,[]))
            outcomes.append(CollectionOutcome('wildberries',ds,True,rid,'sales/returns loaded'))
            current += timedelta(days=1)
        return outcomes

    async def collect_daily_events_range(self, *, start: date, end: date,
                                         wb_connection_id: int | None = None,
                                         ozon_connection_id: int | None = None) -> list[CollectionOutcome]:
        """Independent event snapshots; denied optional APIs never retry orders.

        WB sales are already collected by collect_wb_sales_range. Here we add
        cancellation dates, Ozon customer returns and its daily realization.
        """
        if end < start or (end-start).days >= 31:
            raise ValueError('События за день: период от 1 до 31 дня')
        days = [start + timedelta(days=i) for i in range((end-start).days+1)]
        outcomes = []

        def failed_range(connection_id, endpoint, market, result):
            for day in days:
                outcomes.append(self._failure(connection_id, endpoint, day.isoformat(), market, result))

        async def collect_range(connection_id, endpoint, market, client, method, *args):
            if client is None or getattr(client, method, None) is None:
                return  # An uninjected optional client remains visibly missing.
            if market == 'ozon' and endpoint in self._event_disabled:
                result = FetchResult.failure(market, 'Нет доступа к источнику событий Ozon; проверьте права API.', 403, 0)
            else:
                try:
                    result = await getattr(client, method)(*args)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception('Daily event source %s failed independently',endpoint)
                    result = FetchResult.failure(market, 'Не удалось загрузить источник событий.', None, 1)
            if not result.ok:
                if market == 'ozon' and result.status_code in {401, 402, 403}:
                    self._event_disabled.add(endpoint)
                failed_range(connection_id, endpoint, market, result)
                return
            try:
                grouped, undated = split_event_rows(result.data, endpoint, start.isoformat(), end.isoformat())
            except DailyEventError as exc:
                failed_range(connection_id, endpoint, market, FetchResult.failure(market, str(exc), 200, result.attempts))
                return
            for day in days:
                ds = day.isoformat()
                key = 'orders' if endpoint == WB_CANCELS else 'returns'
                payload = {key: grouped.get(ds, []), 'undated_rows': undated}
                reading = (normalize_wb_cancellations_day(payload, ds) if endpoint == WB_CANCELS
                           else normalize_ozon_returns_day(payload, ds))
                complete = reading.units is not None
                rid = self.repo.record_success(connection_id, endpoint, ds, payload, [], attempts=result.attempts,
                    status='success' if complete else 'partial', error=reading.warning)
                outcomes.append(CollectionOutcome(market, ds, complete, rid, reading.warning or 'daily events loaded'))

        if wb_connection_id is not None:
            await collect_range(wb_connection_id, WB_CANCELS, 'wildberries', self.wb,
                                'orders_since', start.isoformat())
        if ozon_connection_id is not None:
            since, to = posting_range(start, end)
            await collect_range(ozon_connection_id, OZON_RETURNS, 'ozon', self.ozon,
                                'customer_returns_all', since, to)
            if self.ozon is not None and getattr(self.ozon, 'realization_day', None) is not None:
                today = datetime.now(MOSCOW).date()
                for day in days:
                    if not 0 <= (today-day).days <= 31:
                        result = FetchResult.failure('ozon', 'Ozon: дневная реализация доступна только за последние 32 календарных дня.', None, 0)
                    elif OZON_REALIZATION in self._event_disabled:
                        result = FetchResult.failure('ozon', 'Ozon: нет доступа к дневной реализации; требуется Premium Plus/Pro и права API.', 403, 0)
                    else:
                        try:
                            result = await self.ozon.realization_day(day)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            log.exception('Daily Ozon realization source failed independently')
                            result = FetchResult.failure('ozon', 'Не удалось загрузить дневную реализацию.', None, 1)
                    ds = day.isoformat()
                    if not result.ok:
                        if result.status_code in {401, 402, 403}:
                            self._event_disabled.add(OZON_REALIZATION)
                        outcomes.append(self._failure(ozon_connection_id, OZON_REALIZATION, ds, 'ozon', result))
                        continue
                    try:
                        reading=normalize_ozon_realization_day(result.data)[0]
                    except DailyEventError as exc:
                        result = FetchResult.failure('ozon', str(exc), 200, result.attempts)
                        outcomes.append(self._failure(ozon_connection_id, OZON_REALIZATION, ds, 'ozon', result))
                        continue
                    complete=reading.units is not None
                    rid = self.repo.record_success(ozon_connection_id, OZON_REALIZATION, ds, result.data, [], attempts=result.attempts,
                        status='success' if complete else 'partial',error=reading.warning)
                    outcomes.append(CollectionOutcome('ozon', ds, complete, rid, reading.warning or 'daily realization loaded'))
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
                        # The repository prefers a successful dedicated FBO
                        # snapshot. Keep this fallback if that endpoint fails.
                        rid=self.repo.record_success(ozon_connection_id,endpoint,ds,result.data,[],attempts=result.attempts)
                        self._save_inventory_observations(shop_id=shop_id,connection_id=ozon_connection_id,
                            marketplace='ozon',source_run_id=rid,observations=observations)
                        outcomes.append(CollectionOutcome('ozon',ds,True,rid,f'Остатки Ozon: {len(observations)} товарных строк' + ('; пустой ответ API' if not observations else '')))
                    except (ProductNormalizationError,ValueError) as exc:
                        rid=self.repo.record_failure(ozon_connection_id,endpoint,ds,f'Normalization: {exc}',attempts=result.attempts)
                        outcomes.append(CollectionOutcome('ozon',ds,False,rid,str(exc)))
                fbo_method=getattr(self.ozon,'fbo_stocks',None)
                if callable(fbo_method):
                    with self.repo.db.connect() as c:
                        skus=[str(r['marketplace_sku']) for r in c.execute(
                            'SELECT marketplace_sku FROM product_listings WHERE connection_id=?',(ozon_connection_id,))]
                    # v4 product-info carries SKU identities even for FBO rows
                    # whose quantity must be obtained from the FBO endpoint.
                    if result.ok:
                        try:skus.extend(o.marketplace_sku for o in normalize_ozon_stocks(result.data))
                        except ProductNormalizationError:pass
                    endpoint='analytics/stocks/fbo'
                    fbo=await fbo_method(skus)
                    if not fbo.ok:outcomes.append(self._failure(ozon_connection_id,endpoint,ds,'ozon',fbo))
                    else:
                        try:
                            observations=normalize_ozon_fbo_stocks(fbo.data)
                            rid=self.repo.record_success(ozon_connection_id,endpoint,ds,fbo.data,[],attempts=fbo.attempts)
                            self._save_inventory_observations(shop_id=shop_id,connection_id=ozon_connection_id,
                                marketplace='ozon',source_run_id=rid,observations=observations)
                            outcomes.append(CollectionOutcome('ozon',ds,True,rid,f'FBO Ozon: {len(observations)} товарных строк'))
                        except (ProductNormalizationError,ValueError) as exc:
                            rid=self.repo.record_failure(ozon_connection_id,endpoint,ds,f'Normalization: {exc}',attempts=fbo.attempts)
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
        if ozon_connection_id is not None and self.ozon_performance is None:
            explanation='Реклама Ozon не подключена: нужны отдельные OZON_PERF_CLIENT_ID и OZON_PERF_CLIENT_SECRET из кабинета Ozon Performance. Seller API-ключ их не заменяет.'
            rid=self.repo.record_failure(ozon_connection_id,'performance/product-stats',start.isoformat(),explanation)
            outcomes.append(CollectionOutcome('ozon',start.isoformat(),False,rid,explanation))
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
                    complete=len(normalized)==len(result.data or [])
                    outcomes.append(CollectionOutcome('wildberries',ds,complete,rid,f'inbound supplies loaded: {len(normalized)}' +
                        ('; часть поставок не обновлена, ранее сохранённые данные сохранены' if not complete else '')))

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
                            bundles={}; order_complete=True
                            for supply in order.get('supplies') or []:
                                bundle_id=str((supply or {}).get('bundle_id') or '')
                                if not bundle_id:
                                    partial=True; order_complete=False; continue
                                bundle=await self.ozon.supply_bundle_all(bundle_id); attempts += bundle.attempts
                                if not bundle.ok:
                                    partial=True; order_complete=False; continue
                                items=(bundle.data or {}).get('items')
                                if not isinstance(items,list):
                                    partial=True; order_complete=False; continue
                                bundles[bundle_id]=items
                            # An inaccessible bundle is unknown, not an empty
                            # shipment: preserve the previously saved items.
                            if not order_complete:continue
                            normalized.extend(normalize_ozon_order(order,bundles))
                            raw.append({'order':order,'bundles':bundles})
                    rid=self.repo.record_success(ozon_connection_id,endpoint,ds,raw,[],attempts=attempts,store_raw=False,
                                                 status='partial' if partial else 'success')
                    self.repo.upsert_inbound_shipments(
                        ozon_connection_id,rid,'ozon',normalized,
                        active_external_ids=None if partial else [x['external_supply_id'] for x in normalized])
                    note=f'inbound supplies loaded: {len(normalized)}'
                    if partial:note+='; часть поставок не обновлена, ранее сохранённые данные сохранены'
                    outcomes.append(CollectionOutcome('ozon',ds,not partial,rid,note))
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
                    product_payloads={}; attempts=result.attempts; partial=False; errors=[]
                    for promo in promos:
                        if not isinstance(promo,dict) or promo.get('id') is None: continue
                        # WB documents nomenclatures as not applicable to auto promotions.
                        # Keep the calendar event, but never turn this expected limitation into a retry storm.
                        if 'auto' in str(promo.get('type') or '').casefold() or wb_promotion_finished(promo, as_of):
                            continue
                        detail=await self.wb.calendar_promotion_products_all(int(promo['id']),in_action=True)
                        attempts += detail.attempts
                        if detail.ok: product_payloads[str(promo['id'])]=detail.data
                        else:
                            partial=True
                            message=f'WB · акция {promo["id"]}: {detail.error or "не удалось загрузить товары акции"}'
                            errors.append({'promotion_id':str(promo['id']), 'http_status':detail.status_code, 'error':detail.error or 'API error'})
                            self.repo.record_failure(wb_connection_id, f'calendar/promotions/{promo["id"]}/products', ds,
                                message, http_status=detail.status_code, attempts=detail.attempts)
                    try:
                        normalized=normalize_wb_promotions(result.data,product_payloads)
                        raw={'calendar':result.data,'participating_products':product_payloads, 'errors':errors}
                        error_message='; '.join(f'WB · акция {x["promotion_id"]}: {x["error"]}' for x in errors) or None
                        rid=self.repo.record_success(wb_connection_id,endpoint,ds,raw,[],attempts=attempts,
                                                     store_raw=True,status='partial' if partial else 'success', error=error_message)
                        self.repo.upsert_promotions(wb_connection_id,rid,'wildberries',normalized,
                            complete_external_ids=[str(x.get('id')) for x in promos if isinstance(x,dict) and x.get('id') is not None])
                        outcomes.append(CollectionOutcome('wildberries',ds,not partial,rid,
                            'Календарь акций WB загружен' if not partial else 'Товары акций WB загружены не полностью. '+str(error_message)))
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
