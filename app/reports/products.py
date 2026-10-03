"""Product analytics report built only from normalized durable data."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from app.storage import Repository


@dataclass(frozen=True)
class ProductRank:
    marketplace: str
    listing_id: int
    name: str
    sku: str
    units: float
    order_amount: float


@dataclass(frozen=True)
class ProductChange:
    marketplace: str
    name: str
    sku: str
    current_units: float
    previous_units: float
    change_pct: float | None


@dataclass(frozen=True)
class StockRisk:
    marketplace: str
    name: str
    sku: str
    available_units: float
    reserved_units: float
    avg_daily_units: float | None
    days_left: float | None
    coverage_days: int
    captured_at: str | None
    scheme_units: tuple[tuple[str,float], ...] = ()


@dataclass(frozen=True)
class ProductReport:
    start: date
    end: date
    days: int
    top: dict[str, list[ProductRank]] = field(default_factory=dict)
    growth: list[ProductChange] = field(default_factory=list)
    decline: list[ProductChange] = field(default_factory=list)
    fulfillment_orders: list[dict[str, Any]] = field(default_factory=list)
    inventory_schemes: list[dict[str, Any]] = field(default_factory=list)
    stock_risks: list[StockRisk] = field(default_factory=list)
    risk_days: int = 14
    comparison_complete: bool = False
    marketplaces: tuple[str, ...] = ()
    inventory_warnings: tuple[str, ...] = ()


def _index(rows: list[dict[str,Any]]) -> dict[int,dict[str,Any]]:
    return {int(r['listing_id']): r for r in rows}


def build_product_report(repo: Repository, shop_id: int, end: date, *, days: int = 7,
                         top_n: int = 5, stock_lookback_days: int = 14, stock_risk_days: int = 14) -> ProductReport:
    if days < 1:
        raise ValueError('days must be positive')
    start=end-timedelta(days=days-1)
    prev_end=start-timedelta(days=1)
    prev_start=prev_end-timedelta(days=days-1)

    current_units=repo.product_period_totals(shop_id,start.isoformat(),end.isoformat(),'ordered_units')
    previous_units=repo.product_period_totals(shop_id,prev_start.isoformat(),prev_end.isoformat(),'ordered_units')
    current_money=repo.product_period_totals(shop_id,start.isoformat(),end.isoformat(),'ordered_revenue')
    unit_idx=_index(current_units); prev_idx=_index(previous_units); money_idx=_index(current_money)

    top: dict[str,list[ProductRank]]={}
    listing_ids=set(unit_idx)|set(money_idx)
    for lid in listing_ids:
        base=money_idx.get(lid) or unit_idx.get(lid)
        if not base: continue
        market=str(base['marketplace'])
        row=ProductRank(market,lid,str(base['name']),str(base['marketplace_sku']),
                        float(unit_idx.get(lid,{}).get('value',0) or 0),
                        float(money_idx.get(lid,{}).get('value',0) or 0))
        top.setdefault(market,[]).append(row)
    for market, rows in top.items():
        rows.sort(key=lambda x:(x.order_amount,x.units),reverse=True)
        top[market]=rows[:top_n]

    # Growth/decline is a period-over-period formula and must not compare
    # different data coverage. Top lists can still show available data, but
    # change rankings are suppressed unless both periods are complete for every
    # enabled core connection.
    conns=[c for c in repo.list_connections(shop_id) if c.enabled and c.marketplace in {'ozon','wildberries'}]
    comparison_complete=bool(conns) and all(
        len(repo.successful_order_dates(c.id,start.isoformat(),end.isoformat()))==days
        and len(repo.successful_order_dates(c.id,prev_start.isoformat(),prev_end.isoformat()))==days
        for c in conns)
    if comparison_complete:
        current_dates=[(start+timedelta(days=i)).isoformat() for i in range(days)]
        previous_dates=[(prev_start+timedelta(days=i)).isoformat() for i in range(days)]
        comparison_complete=all(c.marketplace!='wildberries' or
            repo.order_sources_comparable(c.id,current_dates,previous_dates) for c in conns)
    changes:list[ProductChange]=[]
    if comparison_complete:
        all_lids=set(unit_idx)|set(prev_idx)
        for lid in all_lids:
            cur=float(unit_idx.get(lid,{}).get('value',0) or 0)
            prev=float(prev_idx.get(lid,{}).get('value',0) or 0)
            base=unit_idx.get(lid) or prev_idx.get(lid)
            if not base or (cur==0 and prev==0): continue
            pct=((cur-prev)/prev*100.0) if prev>0 else None
            changes.append(ProductChange(str(base['marketplace']),str(base['name']),
                                         str(base['marketplace_sku']),cur,prev,pct))
    # Ignore tiny one-unit bases in percentage fall ranking: they generate noise.
    decline=sorted((x for x in changes if x.previous_units>=2 and x.current_units<x.previous_units),
                   key=lambda x: ((x.change_pct if x.change_pct is not None else 0), x.current_units-x.previous_units))[:top_n]
    growth=sorted((x for x in changes if x.current_units>x.previous_units),
                  key=lambda x: ((x.change_pct if x.change_pct is not None else 10**9), x.current_units-x.previous_units),
                  reverse=True)[:top_n]

    fulfillment_orders=repo.fulfillment_totals(shop_id,start.isoformat(),end.isoformat())

    inventory_rows=repo.latest_inventory_by_scheme(shop_id)
    inventory_schemes_map:dict[tuple[str,str],dict[str,Any]]={}
    listing_inventory:dict[int,dict[str,Any]]={}
    for row in inventory_rows:
        market=str(row['marketplace']); scheme=str(row['fulfillment_scheme'])
        present=float(row['available_units'] or 0); reserved=float(row['reserved_units'] or 0)
        available=max(0.0,present)
        key=(market,scheme)
        entry=inventory_schemes_map.setdefault(key,{'marketplace':market,'fulfillment_scheme':scheme,
                                                    'available_units':0.0,'reserved_units':0.0})
        entry['available_units']+=available; entry['reserved_units']+=reserved
        lid=int(row['listing_id'])
        agg=listing_inventory.setdefault(lid,dict(row,total_available=0.0,total_reserved=0.0,schemes=[]))
        agg['total_available']+=available; agg['total_reserved']+=reserved
        agg['schemes'].append((scheme,available))
        if str(row.get('captured_at') or '') < str(agg.get('captured_at') or ''):
            agg['captured_at']=row.get('captured_at')

    inventory_schemes=sorted(inventory_schemes_map.values(),key=lambda x:(x['marketplace'],x['fulfillment_scheme']))
    stock_start=end-timedelta(days=stock_lookback_days-1)
    stock_risks:list[StockRisk]=[]
    for lid,row in listing_inventory.items():
        connection_id=int(row['connection_id'])
        completed=repo.successful_order_dates(connection_id,stock_start.isoformat(),end.isoformat())
        series=repo.product_metric_series(lid,stock_start.isoformat(),end.isoformat(),'ordered_units')
        coverage=len(completed)
        avg=(sum(series.get(d,0.0) for d in completed)/coverage) if coverage else None
        available=float(row['total_available']); reserved=float(row['total_reserved'])
        days_left=(available/avg) if avg and avg>0 else None
        stock_risks.append(StockRisk(str(row['marketplace']),str(row['name']),str(row['marketplace_sku']),
                                     available,reserved,avg,days_left,coverage,row.get('captured_at'),tuple(row['schemes'])))
    # Out of stock first, then the shortest calculated runway. Items with unknown demand go last.
    stock_risks.sort(key=lambda x:(0 if x.available_units<=0 else 1, x.days_left if x.days_left is not None else 10**12, x.available_units))

    warnings=[]
    for conn in conns:
        market='WB' if conn.marketplace=='wildberries' else 'Ozon'
        if not any(r.marketplace==conn.marketplace for r in stock_risks):
            warnings.append(f'{market}: нет товарного снимка остатков. Это не означает нулевой остаток.')
        endpoints=(('analytics/stocks/wb-warehouses','analytics/stocks/seller-warehouses')
                   if conn.marketplace=='wildberries' else ('product/info/stocks','analytics/stocks/fbo'))
        for endpoint in endpoints:
            run=repo.latest_run(conn.id,endpoint)
            if run and run.status!='success':
                scheme=('FBW' if endpoint.endswith('wb-warehouses') else 'FBO' if endpoint.endswith('/fbo') else 'FBS')
                warnings.append(f'{market} {scheme}: последний запрос не завершён. Показаны ранее сохранённые данные, если они есть.')
    return ProductReport(
        start=start,end=end,days=days,top=top,growth=growth,decline=decline,
        fulfillment_orders=fulfillment_orders,inventory_schemes=inventory_schemes,
        stock_risks=stock_risks,risk_days=stock_risk_days,
        comparison_complete=comparison_complete,marketplaces=tuple(c.marketplace for c in conns),
        inventory_warnings=tuple(warnings))
