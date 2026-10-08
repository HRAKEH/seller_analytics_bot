"""Advertising normalization with explicit campaign and SKU attribution."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.storage.models import AdCampaignPoint, AdProductPoint
from .numeric import finite_number


class AdvertisingNormalizationError(ValueError):
    pass


OZON_SPEND_FIELDS = ('expense', 'spend', 'moneySpent', 'money_spent', 'sum')
OZON_SALES_FIELDS = ('ordersMoney', 'orders_money', 'sales', 'revenue', 'sum_price')


def ozon_ad_money(row: dict[str, Any], *names: str) -> float:
    """A missing/blank monetary field is unknown, never a confirmed zero."""
    value = _first(row, *names)
    if isinstance(value, dict):
        value = _first(value, 'amount', 'value')
    if value is None or isinstance(value, bool) or isinstance(value, str) and not value.strip():
        raise AdvertisingNormalizationError('Ozon не передал подтверждённую сумму: ' + names[0])
    if isinstance(value, str):
        value = value.replace('\u00a0', '').replace('\u202f', '')
    number = _num(value)
    if not 0 <= number <= 1e18:
        raise AdvertisingNormalizationError('Ozon передал некорректную рекламную сумму: ' + names[0])
    return number


def _num(value: Any) -> float:
    try:
        return finite_number(value)
    except (TypeError, ValueError) as exc:
        raise AdvertisingNormalizationError('Advertising metric is not a finite number') from exc


def _first(row: dict[str, Any], *names: str, default=None):
    for name in names:
        if name in row and row.get(name) is not None:
            return row.get(name)
    return default


def normalize_wb_ad_detail(payload: Any, connection_id: int) -> tuple[list[AdCampaignPoint], list[AdProductPoint]]:
    """WB /adv/v3/fullstats: campaign/day plus nmId product detail inside apps[].nms[]."""
    if not isinstance(payload, list) or any(not isinstance(row,dict) for row in payload):
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
    def validated(value):
        if any(not isinstance(x,dict) for x in value):
            raise AdvertisingNormalizationError('Ozon advertising rows must be objects')
        return value
    if isinstance(payload, list):
        return validated(payload)
    if not isinstance(payload, dict):
        raise AdvertisingNormalizationError('Ozon advertising response must contain rows')
    for key in ('rows','items','data','result','statistics','list'):
        value=payload.get(key)
        if isinstance(value,list):
            return validated(value)
        if isinstance(value,dict):
            for sub in ('rows','items','data','list'):
                nested=value.get(sub)
                if isinstance(nested,list):
                    return validated(nested)
    raise AdvertisingNormalizationError('Ozon advertising response has no supported row list')


def ozon_ad_campaign_ids(payload: Any) -> list[str]:
    """Use all campaigns in the period, including stopped/archived campaigns."""
    ids = []
    for row in _rows(payload):
        value = _first(row, 'campaignId', 'campaign_id', 'id')
        cid = str(value).strip() if value is not None and not isinstance(value, bool) else ''
        if not cid.isascii() or not cid.isdigit() or not 0 < int(cid) < 2**64:
            raise AdvertisingNormalizationError('Ozon не передал корректный ID рекламной кампании.')
        if cid not in ids:
            ids.append(cid)
    return ids


def _ozon_day(row: dict[str, Any], fallback_date: str | None) -> str:
    text = str(_first(row, 'date', 'day', 'dateFrom', default=fallback_date or '') or '')[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise AdvertisingNormalizationError('Ozon не передал корректную дату рекламной статистики.') from exc


def _unique_ozon_points(points, key):
    chosen = {}
    for point in points:
        identifier = key(point)
        if identifier in chosen and chosen[identifier] != point:
            raise AdvertisingNormalizationError('Ozon вернул противоречивые строки рекламной статистики.')
        chosen[identifier] = point
    return list(chosen.values())


def normalize_ozon_ad_campaign_detail(payload: Any, connection_id: int,
                                       fallback_date: str | None = None) -> list[AdCampaignPoint]:
    rows=_rows(payload)
    out=[]
    for row in rows:
        ds = _ozon_day(row, fallback_date)
        cid=str(_first(row,'campaignId','campaign_id','id',default='') or '')
        if not cid:
            raise AdvertisingNormalizationError('Ozon не передал ID рекламной кампании.')
        out.append(AdCampaignPoint(connection_id,ds,cid,
            str(_first(row,'campaignName','campaign_name','name','title',default='') or ''),
            spend=ozon_ad_money(row, *OZON_SPEND_FIELDS),
            attributed_sales=ozon_ad_money(row, *OZON_SALES_FIELDS),
            orders=_num(_first(row,'orders','ordersCount','orders_count')),
            clicks=_num(_first(row,'clicks')),
            impressions=_num(_first(row,'views','impressions'))))
    return _unique_ozon_points(out, lambda p: (p.data_date, p.campaign_id))


def normalize_ozon_ad_product_detail(payload: Any, connection_id: int,
                                      fallback_date: str | None = None) -> list[AdProductPoint]:
    """Tolerant parser for POST /api/client/statistics/products/sku responses."""
    rows=_rows(payload)
    out=[]
    for row in rows:
        ds = _ozon_day(row, fallback_date)
        sku=str(_first(row,'sku','SKU','ozonId','ozon_id','productId',default='') or '').strip()
        if not sku:
            raise AdvertisingNormalizationError('Ozon не передал SKU в рекламной статистике.')
        cid=str(_first(row,'campaignId','campaign_id','id',default='') or '')
        if not cid:
            raise AdvertisingNormalizationError('Ozon не передал ID кампании в статистике товара.')
        out.append(AdProductPoint(connection_id,ds,sku,cid,
            str(_first(row,'campaignName','campaign_name',default='') or ''),None,
            spend=ozon_ad_money(row, *OZON_SPEND_FIELDS),
            attributed_sales=ozon_ad_money(row, *OZON_SALES_FIELDS),
            orders=_num(_first(row,'orders','ordersCount','orders_count')),
            clicks=_num(_first(row,'clicks')),
            impressions=_num(_first(row,'views','impressions'))))
    return _unique_ozon_points(out, lambda p: (p.data_date, p.campaign_id, p.marketplace_sku))
