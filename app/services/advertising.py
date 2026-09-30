"""Advertising normalization with explicit campaign and SKU attribution."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

from app.storage.models import AdCampaignPoint, AdProductPoint


class AdvertisingNormalizationError(ValueError):
    pass


def _num(value: Any) -> float:
    if value is None or value == '':
        return 0.0
    if isinstance(value, dict):
        value = value.get('amount', value.get('value', 0))
    try:
        return float(str(value).replace(' ', '').replace(',', '.'))
    except (TypeError, ValueError):
        return 0.0


def _first(row: dict[str, Any], *names: str, default=None):
    for name in names:
        if name in row and row.get(name) is not None:
            return row.get(name)
    return default


def normalize_wb_ad_detail(payload: Any, connection_id: int) -> tuple[list[AdCampaignPoint], list[AdProductPoint]]:
    """WB /adv/v3/fullstats: campaign/day plus nmId product detail inside apps[].nms[]."""
    if not isinstance(payload, list):
        raise AdvertisingNormalizationError('WB ad stats must be list')
    campaigns: dict[tuple[str, str], AdCampaignPoint] = {}
    products: dict[tuple[str, str, str], AdProductPoint] = {}
    for campaign in payload:
        if not isinstance(campaign, dict):
            continue
        cid = str(_first(campaign, 'advertId', 'advert_id', 'id', default='') or '')
        cname = str(_first(campaign, 'name', 'campaignName', 'campaign_name', default='') or '')
        for day in campaign.get('days') or []:
            if not isinstance(day, dict):
                continue
            ds = str(_first(day, 'date', 'day', default='') or '')[:10]
            if len(ds) != 10:
                continue
            ck=(ds,cid)
            campaigns[ck]=AdCampaignPoint(
                connection_id, ds, cid, cname,
                spend=_num(_first(day,'sum','spend','expense')),
                attributed_sales=_num(_first(day,'sum_price','sumPrice','sales','revenue')),
                orders=_num(_first(day,'orders','orderCount')),
                clicks=_num(_first(day,'clicks')),
                impressions=_num(_first(day,'views','impressions')),
            )
            # Product rows are split by platform appType. Aggregate them back to nmId.
            for app in day.get('apps') or []:
                if not isinstance(app, dict):
                    continue
                for nm in app.get('nms') or []:
                    if not isinstance(nm, dict):
                        continue
                    sku=str(_first(nm,'nmId','nm_id','nmID',default='') or '').strip()
                    if not sku:
                        continue
                    key=(ds,cid,sku)
                    prev=products.get(key)
                    values=dict(
                        spend=_num(_first(nm,'sum','spend','expense')),
                        attributed_sales=_num(_first(nm,'sum_price','sumPrice','sales','revenue')),
                        orders=_num(_first(nm,'orders','orderCount')),
                        clicks=_num(_first(nm,'clicks')),
                        impressions=_num(_first(nm,'views','impressions')),
                    )
                    if prev is None:
                        products[key]=AdProductPoint(connection_id,ds,sku,cid,cname,None,**values)
                    else:
                        products[key]=AdProductPoint(connection_id,ds,sku,cid,cname,None,
                            spend=prev.spend+values['spend'],
                            attributed_sales=prev.attributed_sales+values['attributed_sales'],
                            orders=prev.orders+values['orders'],clicks=prev.clicks+values['clicks'],
                            impressions=prev.impressions+values['impressions'])
    return list(campaigns.values()), list(products.values())


def _rows(payload: Any) -> list[dict[str,Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x,dict)]
    if not isinstance(payload, dict):
        return []
    for key in ('rows','items','data','result','statistics','list'):
        value=payload.get(key)
        if isinstance(value,list):
            return [x for x in value if isinstance(x,dict)]
        if isinstance(value,dict):
            for sub in ('rows','items','data','list'):
                nested=value.get(sub)
                if isinstance(nested,list):
                    return [x for x in nested if isinstance(x,dict)]
    return []


def normalize_ozon_ad_campaign_detail(payload: Any, connection_id: int,
                                       fallback_date: str | None = None) -> list[AdCampaignPoint]:
    rows=_rows(payload)
    out=[]
    for row in rows:
        ds=str(_first(row,'date','day','dateFrom',default=fallback_date or '') or '')[:10]
        if len(ds)!=10: continue
        cid=str(_first(row,'campaignId','campaign_id','id',default='') or '')
        out.append(AdCampaignPoint(connection_id,ds,cid,
            str(_first(row,'campaignName','campaign_name','name',default='') or ''),
            spend=_num(_first(row,'expense','spend','sum')),
            attributed_sales=_num(_first(row,'ordersMoney','orders_money','sales','revenue','sum_price')),
            orders=_num(_first(row,'orders','ordersCount','orders_count')),
            clicks=_num(_first(row,'clicks')),
            impressions=_num(_first(row,'views','impressions'))))
    return out


def normalize_ozon_ad_product_detail(payload: Any, connection_id: int,
                                      fallback_date: str | None = None) -> list[AdProductPoint]:
    """Tolerant parser for POST /api/client/statistics/products/sku responses."""
    rows=_rows(payload)
    out=[]
    for row in rows:
        ds=str(_first(row,'date','day','dateFrom',default=fallback_date or '') or '')[:10]
        if len(ds)!=10: continue
        sku=str(_first(row,'sku','SKU','ozonId','ozon_id','productId',default='') or '').strip()
        if not sku: continue
        cid=str(_first(row,'campaignId','campaign_id','id',default='') or '')
        out.append(AdProductPoint(connection_id,ds,sku,cid,
            str(_first(row,'campaignName','campaign_name',default='') or ''),None,
            spend=_num(_first(row,'expense','spend','sum')),
            attributed_sales=_num(_first(row,'ordersMoney','orders_money','sales','revenue','sum_price')),
            orders=_num(_first(row,'orders','ordersCount','orders_count')),
            clicks=_num(_first(row,'clicks')),
            impressions=_num(_first(row,'views','impressions'))))
    return out
