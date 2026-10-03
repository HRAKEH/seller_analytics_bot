"""Transparent demand classification, forecast quality and replenishment planning.

The model is intentionally auditable.  It uses complete operational order days,
latest successful inventory, explicit marketplace inbound supplies and a bounded
weekday seasonality factor.  Missing inventory or unknown inbound ETA is never
silently converted to available stock.
"""
from __future__ import annotations
import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.storage import Repository
from .promotions import historical_promo_factor

METHOD_VERSION='calibrated-promo-bias-wma-v6'
QUALITY_METHOD_VERSION='seasonal-wma-v3-backtest'
CALIBRATION_METHOD_VERSION='risk-buffer-v1'


@dataclass(frozen=True)
class SupplyRow:
    product_id: int
    internal_sku: str
    name: str
    abc_class: str
    xyz_class: str
    avg_daily_units: float
    forecast_daily_units: float
    forecast_next_7_units: float
    seasonality_strength: float
    bias_correction: float
    promo_factor: float
    promo_days: int
    trend_pct: float | None
    available_units: float | None
    inbound_units: float
    effective_units: float | None
    days_cover: float | None
    lead_time_days: int
    lead_buffer_days: int
    effective_lead_time_days: int
    safety_stock_days: int
    safety_buffer_days: int
    effective_safety_stock_days: int
    target_stock_days: int
    reorder_point_units: float
    target_units: float
    recommended_order_units: float
    pack_size: float
    min_order_qty: float
    confidence: str
    history_days: int
    first_history_date: str | None
    last_history_date: str | None
    inventory_as_of: str | None
    inventory_age_days: int | None
    inventory_stale: bool
    calibration_confidence: str
    marketplaces: tuple[str,...] = ()
    missing_inventory_marketplaces: tuple[str,...] = ()

    @property
    def needs_order(self) -> bool:
        return self.recommended_order_units > 0


@dataclass(frozen=True)
class SupplyPlan:
    shop_id: int
    as_of: date
    lookback_days: int
    complete_days: int
    abc_basis: str
    method_version: str
    rows: tuple[SupplyRow,...]

    @property
    def order_now_count(self) -> int:
        return sum(1 for r in self.rows if r.needs_order)


@dataclass(frozen=True)
class ForecastQualityItem:
    product_id: int
    internal_sku: str
    name: str
    samples: int
    predicted_units: float
    actual_units: float
    mae_units: float
    wape_pct: float | None
    bias_pct: float | None


@dataclass(frozen=True)
class ForecastQualityReport:
    shop_id: int
    as_of: date
    horizon_days: int
    method_version: str
    overall_wape_pct: float | None
    overall_bias_pct: float | None
    items: tuple[ForecastQualityItem,...]


@dataclass(frozen=True)
class SupplyCalibrationRow:
    product_id: int
    internal_sku: str
    name: str
    forecast_samples: int
    forecast_wape_pct: float | None
    forecast_bias_pct: float | None
    inventory_samples: int
    zero_stock_rate_pct: float | None
    inbound_delay_samples: int
    avg_inbound_delay_days: float | None
    p75_inbound_delay_days: float | None
    lead_buffer_days: int
    safety_buffer_days: int
    confidence: str
    reasons: tuple[str,...]


@dataclass(frozen=True)
class SupplyCalibrationReport:
    shop_id: int
    as_of: date
    method_version: str
    auto_apply: bool
    rows: tuple[SupplyCalibrationRow,...]


def _weighted_forecast(values: list[float]) -> float:
    if not values: return 0.0
    avg=sum(values)/len(values)
    window=values[-min(28,len(values)):]
    weights=list(range(1,len(window)+1))
    wma=sum(v*w for v,w in zip(window,weights))/sum(weights)
    if len(values)<7: return max(0.0,avg)
    return max(0.0,0.65*wma+0.35*avg)


def _trend(values: list[float]) -> float | None:
    if len(values)<14: return None
    recent=values[-14:]; previous=values[-28:-14] if len(values)>=28 else values[:-14]
    if not previous: return None
    a=sum(recent)/len(recent); b=sum(previous)/len(previous)
    if b==0: return None if a>0 else 0.0
    return (a-b)/b*100.0


def _xyz(values: list[float], weeks: int, days: list[str] | None=None) -> tuple[str,float | None]:
    chunks=[]
    if days is not None:
        # Missing API days cannot be stitched into an artificial seven-day week.
        buckets={}
        for ds,value in zip(days,values):
            d=date.fromisoformat(ds); monday=d-timedelta(days=d.weekday())
            buckets.setdefault(monday,{})[d.weekday()]=value
        chunks=[sum(bucket.values()) for _,bucket in sorted(buckets.items()) if len(bucket)==7][-weeks:]
    else:
        recent=values[-weeks*7:]
        while len(recent)>=7:
            chunks.append(sum(recent[-7:])); recent=recent[:-7]
        chunks.reverse()
    if len(chunks)<4: return '?',None
    mean=sum(chunks)/len(chunks)
    if mean<=0: return 'Z',None
    cv=statistics.pstdev(chunks)/mean
    if cv<=0.25: return 'X',cv
    if cv<=0.50: return 'Y',cv
    return 'Z',cv


def _weekday_factors(days: list[str], values: list[float], *, enabled: bool=True) -> dict[int,float]:
    """Bounded weekday factors. Sparse history intentionally collapses to 1.0."""
    base={i:1.0 for i in range(7)}
    if not enabled or len(values)<28 or len(days)!=len(values): return base
    overall=sum(values)/len(values)
    if overall<=1e-12: return base
    buckets={i:[] for i in range(7)}
    for ds,v in zip(days,values):
        try: buckets[date.fromisoformat(ds).weekday()].append(float(v))
        except ValueError: pass
    if any(len(buckets[i])<3 for i in range(7)): return base
    raw={i:max(0.5,min(1.5,(sum(buckets[i])/len(buckets[i]))/overall)) for i in range(7)}
    mean=sum(raw.values())/7.0
    if mean<=0: return base
    return {i:raw[i]/mean for i in range(7)}


def _demand_for_dates(baseline: float, dates: list[date], factors: dict[int,float],
                      *, promo_dates: set[str] | None=None, promo_factor: float=1.0) -> float:
    promo_dates=promo_dates or set()
    return sum(max(0.0,baseline)*factors.get(d.weekday(),1.0)*
               (promo_factor if d.isoformat() in promo_dates else 1.0) for d in dates)


def _future_dates(as_of: date, days: int) -> list[date]:
    return [as_of+timedelta(days=i) for i in range(1,max(0,days)+1)]


def _round_order(qty: float, pack_size: float, min_order_qty: float) -> float:
    if qty<=0: return 0.0
    qty=max(qty,max(0.0,min_order_qty)); pack=max(1e-9,pack_size or 1.0)
    return math.ceil(qty/pack-1e-12)*pack


def _planned_day(value: Any) -> date | None:
    if not value: return None
    try: return datetime.fromisoformat(str(value).replace('Z','+00:00')).date()
    except ValueError:
        try: return date.fromisoformat(str(value)[:10])
        except ValueError: return None


def _percentile(values: list[float], q: float) -> float | None:
    if not values: return None
    xs=sorted(float(x) for x in values); q=max(0.0,min(1.0,q))
    if len(xs)==1: return xs[0]
    pos=(len(xs)-1)*q; lo=int(math.floor(pos)); hi=int(math.ceil(pos))
    if lo==hi: return xs[lo]
    frac=pos-lo
    return xs[lo]*(1-frac)+xs[hi]*frac


def build_supply_calibration(repo: Repository, shop_id: int, as_of: date, *, persist: bool=True) -> SupplyCalibrationReport:
    """Learn bounded risk buffers without overwriting seller policy.

    Forecast error and observed zero-stock days can only *add* a safety buffer.
    Observed WB fact-date lateness can only add a lead/acceptance buffer.  Base
    lead time and safety stock remain explicit user settings.
    """
    pref=repo.ensure_shop_supply_preferences(shop_id)
    products=repo.products_for_supply(shop_id)
    quality=repo.forecast_quality_samples_by_product(shop_id,limit_per_product=24,
        as_of_date=as_of.isoformat(),method_version=QUALITY_METHOD_VERSION,
        horizon_days=int(pref.get('forecast_horizon_days',7)))
    hist_start=(as_of-timedelta(days=max(90,int(pref.get('lookback_days',56))*2))).isoformat()
    inventory=repo.product_inventory_daily(shop_id,hist_start,as_of.isoformat())
    delays=repo.inbound_delay_samples_by_product(shop_id,(as_of-timedelta(days=365)).isoformat(),as_of.isoformat())
    max_lead=max(0,int(pref.get('max_lead_buffer_days',7)))
    max_safety=max(0,int(pref.get('max_safety_buffer_days',7)))
    rows=[]; persisted=[]
    for p in products:
        pid=int(p['product_id']); qs=quality.get(pid,[])
        predicted=sum(float(x.get('predicted_units') or 0) for x in qs)
        actual=sum(float(x.get('actual_units') or 0) for x in qs)
        absolute=sum(float(x.get('absolute_error') or 0) for x in qs)
        wape=(absolute/actual*100.0) if actual>0 else None
        bias=((predicted-actual)/actual*100.0) if actual>0 else None
        inv_values=list(inventory.get(pid,{}).values()); inv_samples=len(inv_values)
        zero_rate=(sum(1 for x in inv_values if x<=1e-9)/inv_samples*100.0) if inv_samples else None
        dvals=delays.get(pid,[]); p75=_percentile(dvals,0.75); avg_delay=(sum(dvals)/len(dvals)) if dvals else None
        safety=0; reasons=[]
        if len(qs)>=4 and wape is not None:
            if wape>=60: safety += 3
            elif wape>=35: safety += 2
            elif wape>=20: safety += 1
            if wape>=20: reasons.append(f'WAPE {wape:.0f}%')
        if inv_samples>=7 and zero_rate is not None:
            if zero_rate>=30: safety += 3
            elif zero_rate>=15: safety += 2
            elif zero_rate>=5: safety += 1
            if zero_rate>=5: reasons.append(f'нулевой остаток {zero_rate:.0f}% наблюдений')
        safety=min(max_safety,safety)
        lead=min(max_lead,int(math.ceil(p75 or 0))) if len(dvals)>=3 else 0
        if lead>0: reasons.append(f'P75 задержки приёмки {p75:.1f} дн.')
        if len(qs)>=8 and inv_samples>=14: confidence='high'
        elif len(qs)>=4 or inv_samples>=7 or len(dvals)>=3: confidence='medium'
        else: confidence='low'
        if not reasons: reasons.append('недостаточно подтверждённого риска для дополнительного буфера')
        row=SupplyCalibrationRow(pid,str(p['internal_sku']),str(p['name']),len(qs),wape,bias,inv_samples,zero_rate,
            len(dvals),avg_delay,p75,lead,safety,confidence,tuple(reasons))
        rows.append(row)
        persisted.append({'product_id':pid,'forecast_samples':len(qs),'forecast_wape_pct':wape,'forecast_bias_pct':bias,
            'inventory_samples':inv_samples,'zero_stock_rate_pct':zero_rate,'inbound_delay_samples':len(dvals),
            'avg_inbound_delay_days':avg_delay,'p75_inbound_delay_days':p75,'lead_buffer_days':lead,
            'safety_buffer_days':safety,'confidence':confidence,'details':{'reasons':list(reasons)}})
    rows.sort(key=lambda r:(-(r.safety_buffer_days+r.lead_buffer_days),{'high':0,'medium':1,'low':2}.get(r.confidence,9),r.internal_sku))
    if persist and persisted:
        repo.save_supply_calibrations(shop_id,as_of.isoformat(),persisted,method_version=CALIBRATION_METHOD_VERSION)
    return SupplyCalibrationReport(shop_id,as_of,CALIBRATION_METHOD_VERSION,bool(pref.get('auto_calibration_enabled',1)),tuple(rows))


def build_supply_plan(repo: Repository, shop_id: int, as_of: date, *, lookback_days: int | None=None,
                      persist: bool=True) -> SupplyPlan:
    pref=repo.ensure_shop_supply_preferences(shop_id)
    calibration=build_supply_calibration(repo,shop_id,as_of,persist=persist)
    calibration_by_product={r.product_id:r for r in calibration.rows}
    auto_calibration=bool(pref.get('auto_calibration_enabled',1))
    lookback=max(14,min(int(lookback_days or pref['lookback_days']),365))
    start=as_of-timedelta(days=lookback-1)
    complete_dates=repo.complete_order_dates(shop_id,start.isoformat(),as_of.isoformat())
    daily=repo.physical_product_daily_units(shop_id,start.isoformat(),as_of.isoformat())
    products=repo.products_for_supply(shop_id)
    historical_promo_dates=repo.promotion_dates_by_product(shop_id,start.isoformat(),as_of.isoformat())
    future_promo_dates=repo.promotion_dates_by_product(shop_id,(as_of+timedelta(days=1)).isoformat(),(as_of+timedelta(days=180)).isoformat())
    bias_corrections=repo.forecast_bias_corrections(shop_id,as_of_date=as_of.isoformat(),
        method_version=QUALITY_METHOD_VERSION,horizon_days=int(pref.get('forecast_horizon_days',7)))

    inventory_rows=repo.latest_inventory_by_listing(shop_id); inventory: dict[int,dict[str,Any]]={}
    known_listings={int(r['listing_id']) for r in inventory_rows}
    listing_markets={}; missing_markets={}
    with repo.db.connect() as c:
        listing_rows=c.execute('''SELECT pl.id,pl.product_id,mc.marketplace FROM product_listings pl
            JOIN products p ON p.id=pl.product_id JOIN marketplace_connections mc ON mc.id=pl.connection_id
            WHERE p.shop_id=? AND p.active=1 AND mc.enabled=1''',(shop_id,)).fetchall()
    for row in listing_rows:
        pid=int(row['product_id']); listing_markets.setdefault(pid,set()).add(str(row['marketplace']))
        if int(row['id']) not in known_listings:missing_markets.setdefault(pid,set()).add(str(row['marketplace']))
    for row in inventory_rows:
        pid=int(row['product_id']); x=inventory.setdefault(pid,{'available':0.0,'as_of':None})
        x['available']+=float(row['available_units'] or 0); ts=row.get('captured_at')
        # The oldest contributing inventory limits freshness of the total.
        if ts and (x['as_of'] is None or str(ts)<str(x['as_of'])): x['as_of']=str(ts)

    inbound_by_product: dict[int,list[dict[str,Any]]]={}
    for row in repo.active_inbound_items(shop_id):
        if row.get('product_id') is not None:
            inbound_by_product.setdefault(int(row['product_id']),[]).append(row)

    prepared=[]
    for p in products:
        pid=int(p['product_id']); first_listing=(str(p['first_listing_at'])[:10] if p.get('first_listing_at') else None)
        series_map=daily.get(pid,{}); first_metric=min(series_map) if series_map else None
        starts=[x for x in (first_listing,first_metric) if x]; first_active=min(starts) if starts else None
        eligible=[d for d in complete_dates if first_active is None or d>=first_active]
        values=[float(series_map.get(d,0.0)) for d in eligible]
        prepared.append((p,eligible,values,sum(values)))

    total_units=sum(x[3] for x in prepared); abc={}; cumulative=0.0
    for p,eligible,values,total in sorted(prepared,key=lambda x:x[3],reverse=True):
        pid=int(p['product_id'])
        if total_units<=0: abc[pid]='C'; continue
        before=cumulative/total_units; cumulative+=total
        abc[pid]='A' if before<0.80 else ('B' if before<0.95 else 'C')

    rows=[]; snapshot=[]; xyz_weeks=int(pref['xyz_weeks']); min_history=int(pref['min_history_days'])
    seasonality_enabled=bool(pref.get('seasonality_enabled',1))
    for p,eligible,values,total in prepared:
        pid=int(p['product_id']); avg=(sum(values)/len(values)) if values else 0.0
        promo_factor=historical_promo_factor(daily.get(pid,{}),historical_promo_dates.get(pid,set()),eligible)
        # Remove the learned promotion uplift from training observations before
        # applying it to future promotion days; otherwise it is counted twice.
        historical_promos=historical_promo_dates.get(pid,set())
        baseline_values=[v/promo_factor if d in historical_promos else v for d,v in zip(eligible,values)]
        raw_baseline=_weighted_forecast(baseline_values); bias_correction=float(bias_corrections.get(pid,1.0))
        baseline=max(0.0,raw_baseline*bias_correction); factors=_weekday_factors(eligible,baseline_values,enabled=seasonality_enabled)
        promo_dates=future_promo_dates.get(pid,set())
        next7_dates=_future_dates(as_of,7)
        next7=_demand_for_dates(baseline,next7_dates,factors,promo_dates=promo_dates,promo_factor=promo_factor)
        seasonal_strength=max(abs(v-1.0) for v in factors.values()) if factors else 0.0
        trend=_trend(values); xyz,_cv=_xyz(values,xyz_weeks,eligible)
        inv=inventory.get(pid); available=float(inv['available']) if inv is not None and pid not in missing_markets else None
        days_cover=(available/baseline) if available is not None and baseline>1e-9 else None
        lead=int(p['lead_time_days']); safety=int(p['safety_stock_days']); target=int(p['target_stock_days'])
        cal=calibration_by_product.get(pid)
        lead_buffer=(cal.lead_buffer_days if auto_calibration and cal else 0)
        safety_buffer=(cal.safety_buffer_days if auto_calibration and cal else 0)
        effective_lead=lead+lead_buffer; effective_safety=safety+safety_buffer
        reorder_dates=_future_dates(as_of,effective_lead+effective_safety); target_dates=_future_dates(as_of,effective_lead+effective_safety+target)
        reorder=_demand_for_dates(baseline,reorder_dates,factors,promo_dates=promo_dates,promo_factor=promo_factor); target_units=_demand_for_dates(baseline,target_dates,factors,promo_dates=promo_dates,promo_factor=promo_factor)
        promo_days=sum(1 for d in target_dates if d.isoformat() in promo_dates)
        inbound_reorder=0.0; inbound_target=0.0
        reorder_cutoff=as_of+timedelta(days=effective_lead+effective_safety); target_cutoff=as_of+timedelta(days=effective_lead+effective_safety+target)
        for item in inbound_by_product.get(pid,[]):
            eta=_planned_day(item.get('planned_at'))
            if eta is None or eta<as_of: continue
            qty=float(item.get('remaining_units') or 0)
            if eta<=target_cutoff: inbound_target += qty
            if eta<=reorder_cutoff: inbound_reorder += qty
        effective=(available+inbound_target) if available is not None else None
        trigger_stock=(available+inbound_reorder) if available is not None else None
        raw=max(0.0,target_units-available-inbound_target) if available is not None and trigger_stock is not None and trigger_stock<=reorder+1e-9 and baseline>0 else 0.0
        recommended=_round_order(raw,float(p['pack_size'] or 1),float(p['min_order_qty'] or 0))
        if len(values)<min_history or xyz=='?': confidence='low'
        elif xyz=='X' and len(values)>=42: confidence='high'
        else: confidence='medium'
        inv_as_of=(inv or {}).get('as_of'); inv_age=None
        if inv_as_of:
            try: inv_age=max(0,(as_of-datetime.fromisoformat(str(inv_as_of).replace('Z','+00:00')).date()).days)
            except ValueError: pass
        inv_stale=bool(inv_age is not None and inv_age>2)
        row=SupplyRow(pid,str(p['internal_sku']),str(p['name']),abc.get(pid,'C'),xyz,avg,baseline,next7,
            seasonal_strength,bias_correction,promo_factor,promo_days,trend,available,inbound_target,effective,days_cover,
            lead,lead_buffer,effective_lead,safety,safety_buffer,effective_safety,target,reorder,target_units,
            recommended,float(p['pack_size'] or 1),float(p['min_order_qty'] or 0),confidence,len(values),
            eligible[0] if eligible else None,eligible[-1] if eligible else None,inv_as_of,inv_age,inv_stale,
            cal.confidence if cal else 'low',tuple(sorted(listing_markets.get(pid,()))),tuple(sorted(missing_markets.get(pid,()))))
        rows.append(row)
        snapshot.append({'product_id':pid,'abc_class':row.abc_class,'xyz_class':row.xyz_class,
            'avg_daily_units':row.avg_daily_units,'forecast_daily_units':row.forecast_daily_units,
            'bias_correction':row.bias_correction,'promo_factor':row.promo_factor,'promo_days':row.promo_days,
            'lead_buffer_days':row.lead_buffer_days,'safety_buffer_days':row.safety_buffer_days,
            'effective_lead_time_days':row.effective_lead_time_days,'effective_safety_stock_days':row.effective_safety_stock_days,
            'calibration_confidence':row.calibration_confidence,
            'trend_pct':row.trend_pct,'available_units':row.available_units,'days_cover':row.days_cover,
            'reorder_point_units':row.reorder_point_units,'target_units':row.target_units,
            'recommended_order_units':row.recommended_order_units,'confidence':row.confidence,'history_days':row.history_days})

    abc_rank={'A':0,'B':1,'C':2}; xyz_rank={'X':0,'Y':1,'Z':2,'?':3}
    rows.sort(key=lambda r:(0 if r.needs_order else 1,r.days_cover if r.days_cover is not None else float('inf'),
                            abc_rank.get(r.abc_class,9),xyz_rank.get(r.xyz_class,9),r.internal_sku))
    if persist and snapshot: repo.save_supply_recommendations(shop_id,as_of.isoformat(),snapshot,method_version=METHOD_VERSION)
    return SupplyPlan(shop_id,as_of,lookback,len(complete_dates),'ordered_units',METHOD_VERSION,tuple(rows))


def evaluate_forecast_quality(repo: Repository, shop_id: int, as_of: date, *, horizon_days: int | None=None,
                              persist: bool=True, samples: int=4) -> ForecastQualityReport:
    pref=repo.ensure_shop_supply_preferences(shop_id); horizon=max(1,min(int(horizon_days or pref.get('forecast_horizon_days',7)),30))
    lookback=max(28,int(pref['lookback_days'])); earliest=as_of-timedelta(days=lookback+samples*horizon+7)
    complete=repo.complete_order_dates(shop_id,earliest.isoformat(),as_of.isoformat())
    daily=repo.physical_product_daily_units(shop_id,earliest.isoformat(),as_of.isoformat())
    products={int(p['product_id']):p for p in repo.products_for_supply(shop_id)}; by_product={}
    persisted=[]
    for pid,p in products.items():
        series=daily.get(pid,{})
        starts=[d for d in (min(series) if series else None,
                           str(p['first_listing_at'])[:10] if p.get('first_listing_at') else None) if d]
        first_active=min(starts) if starts else None
        item_samples=[]
        for n in range(samples,0,-1):
            cutoff=as_of-timedelta(days=n*horizon)
            future=[d for d in complete if cutoff.isoformat()<d<=(cutoff+timedelta(days=horizon)).isoformat()]
            history=[d for d in complete if (cutoff-timedelta(days=lookback-1)).isoformat()<=d<=cutoff.isoformat()
                     and (first_active is None or d>=first_active)]
            # A horizon sample is valid only when every calendar day in that
            # horizon is complete across all enabled marketplaces. Otherwise the
            # backtest would silently score a 7-day forecast on fewer than 7 days.
            if len(history)<14 or len(future)!=horizon: continue
            values=[float(series.get(d,0.0)) for d in history]; baseline=_weighted_forecast(values)
            factors=_weekday_factors(history,values,enabled=bool(pref.get('seasonality_enabled',1)))
            predicted=sum(baseline*factors.get(date.fromisoformat(d).weekday(),1.0) for d in future)
            actual=sum(float(series.get(d,0.0)) for d in future)
            item_samples.append((cutoff.isoformat(),predicted,actual))
            persisted.append({'product_id':pid,'as_of_date':cutoff.isoformat(),'horizon_days':horizon,
                              'predicted_units':predicted,'actual_units':actual})
        if not item_samples: continue
        predicted=sum(x[1] for x in item_samples); actual=sum(x[2] for x in item_samples)
        errors=[abs(x[1]-x[2]) for x in item_samples]; mae=sum(errors)/len(errors)
        wape=(sum(errors)/actual*100.0) if actual>0 else None
        bias=((predicted-actual)/actual*100.0) if actual>0 else None
        by_product[pid]=ForecastQualityItem(pid,str(p['internal_sku']),str(p['name']),len(item_samples),predicted,actual,mae,wape,bias)
    if persist and persisted: repo.save_forecast_quality(shop_id,persisted,method_version=QUALITY_METHOD_VERSION)
    items=tuple(sorted(by_product.values(),key=lambda x:(x.wape_pct is not None,-1 if x.wape_pct is None else x.wape_pct),reverse=True))
    total_pred=sum(x.predicted_units for x in items); total_actual=sum(x.actual_units for x in items)
    total_abs=sum(x.mae_units*x.samples for x in items)
    overall_wape=(total_abs/total_actual*100.0) if total_actual>0 else None
    overall_bias=((total_pred-total_actual)/total_actual*100.0) if total_actual>0 else None
    return ForecastQualityReport(shop_id,as_of,horizon,QUALITY_METHOD_VERSION,overall_wape,overall_bias,items)
