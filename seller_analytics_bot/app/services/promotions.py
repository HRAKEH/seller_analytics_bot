"""Promotion calendar normalization and conservative forecast helpers.

A promotion affects demand only when exact SKU participation is known. Merely
being eligible for a promotion never changes a forecast.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any


class PromotionNormalizationError(ValueError):
    pass


def _f(value: Any) -> float | None:
    if value is None or value == '': return None
    try: return float(value)
    except (TypeError, ValueError): return None


def normalize_wb_promotions(calendar_payload: Any,
                            product_payloads: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(calendar_payload, dict):
        raise PromotionNormalizationError('WB promotion calendar must be an object')
    data=calendar_payload.get('data') or {}
    rows=data.get('promotions') or []
    if not isinstance(rows,list):
        raise PromotionNormalizationError('WB promotion calendar promotions must be a list')
    out=[]
    for row in rows:
        if not isinstance(row,dict) or row.get('id') is None: continue
        ext=str(row['id']); prod_raw=product_payloads.get(ext)
        products=[]; products_complete=False
        if isinstance(prod_raw,dict):
            inner=prod_raw.get('data') or {}; noms=inner.get('nomenclatures') or []
            if isinstance(noms,list):
                products_complete=True
                for item in noms:
                    if not isinstance(item,dict) or item.get('id') is None: continue
                    products.append({
                        'marketplace_sku':str(item['id']),
                        'in_action':bool(item.get('inAction',True)),
                        'base_price':_f(item.get('price')),
                        'promo_price':_f(item.get('planPrice')),
                        'discount_pct':_f(item.get('planDiscount') if item.get('planDiscount') is not None else item.get('discount')),
                        'metadata':item,
                    })
        out.append({
            'external_promotion_id':ext,'name':str(row.get('name') or f'WB promo {ext}'),
            'promo_type':str(row.get('type') or ''),'start_at':row.get('startDateTime'),
            'end_at':row.get('endDateTime'),'metadata':row,'products':products,
            'products_complete':products_complete,
        })
    return out


def normalize_ozon_promotions(actions_payload: Any,
                              products_by_action: dict[str, Any],
                              product_info: dict[str, dict[str, Any]] | None=None) -> list[dict[str, Any]]:
    if not isinstance(actions_payload,dict):
        raise PromotionNormalizationError('Ozon actions response must be an object')
    rows=actions_payload.get('result') or []
    if not isinstance(rows,list):
        raise PromotionNormalizationError('Ozon actions result must be a list')
    product_info=product_info or {}; out=[]
    for row in rows:
        if not isinstance(row,dict) or row.get('id') is None: continue
        ext=str(row['id']); raw=products_by_action.get(ext); products=[]; complete=False
        if isinstance(raw,dict):
            result=raw.get('result') or {}; items=result.get('products') or []
            if isinstance(items,list):
                complete=True
                for item in items:
                    if not isinstance(item,dict) or item.get('id') is None: continue
                    product_id=str(item.get('id')); info=product_info.get(product_id) or {}
                    offer=str(info.get('offer_id') or '').strip()
                    # product_id is kept as a fallback key; repository will prefer offer_id when available.
                    products.append({
                        'marketplace_sku':product_id,'offer_id':offer or None,'in_action':True,
                        'base_price':_f(item.get('price')),'promo_price':_f(item.get('action_price')),
                        'discount_pct':None,'metadata':item,
                    })
        out.append({
            'external_promotion_id':ext,'name':str(row.get('title') or f'Ozon promo {ext}'),
            'promo_type':str(row.get('action_type') or ''),'start_at':row.get('date_start'),
            'end_at':row.get('date_end'),'metadata':row,'products':products,'products_complete':complete,
        })
    return out


def historical_promo_factor(values_by_day: dict[str,float], promo_days: set[str], complete_days: list[str]) -> float:
    """Estimate a shrunk bounded promo uplift from observed product history.

    Requires at least 4 promotion days and 14 ordinary days. Otherwise returns
    neutral 1.0. The raw ratio is clipped and shrunk toward 1.0 to avoid a few
    volatile days dominating replenishment.
    """
    promo=[float(values_by_day.get(d,0.0)) for d in complete_days if d in promo_days]
    normal=[float(values_by_day.get(d,0.0)) for d in complete_days if d not in promo_days]
    if len(promo)<4 or len(normal)<14: return 1.0
    p=sum(promo)/len(promo); n=sum(normal)/len(normal)
    if n<=1e-9: return 1.0
    raw=max(0.80,min(1.50,p/n))
    shrink=min(0.75,len(promo)/14.0)
    return 1.0+(raw-1.0)*shrink


def dates_in_window(start_at: str | None, end_at: str | None,
                    start: date, end: date) -> set[str]:
    try: a=date.fromisoformat(str(start_at or start.isoformat())[:10])
    except ValueError: a=start
    try: b=date.fromisoformat(str(end_at or end.isoformat())[:10])
    except ValueError: b=end
    a=max(a,start); b=min(b,end); out=set()
    while a<=b:
        out.add(a.isoformat()); a += timedelta(days=1)
    return out
