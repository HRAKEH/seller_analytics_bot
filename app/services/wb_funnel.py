"""Normalize the seller sales funnel without blending supplier order events."""
from datetime import datetime, timezone
from app.storage.models import MetricPoint
from .numeric import finite_number
from .normalization import NormalizationError
from .product_analytics import ProductDayObservation


def normalize_wb_funnel(payload, connection_id: int, data_date: str):
    data=payload.get('data') if isinstance(payload,dict) else None
    rows=data.get('products') if isinstance(data,dict) else None
    if not isinstance(rows,list):
        raise NormalizationError('WB funnel has no products list')
    now=datetime.now(timezone.utc).isoformat(timespec='seconds')
    observations=[]; seen=set()
    for row in rows:
        product=row.get('product') if isinstance(row,dict) else None
        statistic=row.get('statistic') if isinstance(row,dict) else None
        selected=statistic.get('selected') if isinstance(statistic,dict) else None
        if not isinstance(product,dict) or not isinstance(selected,dict):
            raise NormalizationError('WB funnel product has no selected statistics')
        sku=str(product.get('nmId') or '')
        if not sku.isdecimal() or int(sku)<=0 or sku in seen:
            raise NormalizationError('WB funnel has missing/repeated nmId')
        seen.add(sku)
        period=selected.get('period')
        if not isinstance(period,dict) or any(str(period.get(k) or '')[:10]!=data_date for k in ('start','end')):
            raise NormalizationError('WB funnel statistics belong to another date')
        if row.get('currency') not in (None,'','RUB'):
            raise NormalizationError('WB funnel currency must be RUB')
        values={}
        for key in ('orderCount','orderSum','cancelCount'):
            if key not in selected or selected[key] is None or isinstance(selected[key],bool):
                raise NormalizationError(f'WB funnel has no {key}')
            try:
                value=finite_number(selected[key])
            except (ValueError,TypeError) as exc:
                raise NormalizationError(f'WB funnel {key} is not numeric') from exc
            if value<0 or (key!='orderSum' and not value.is_integer()):
                raise NormalizationError(f'WB funnel {key} is invalid')
            values[key]=value
        observations.append(ProductDayObservation(data_date,sku,
            str(product.get('title') or f'WB {sku}'),
            offer_id=str(product.get('vendorCode') or '') or None,
            ordered_units=values['orderCount'],ordered_revenue=values['orderSum'],
            cancellations_units=values['cancelCount'],as_of=now))
    points=[MetricPoint(connection_id,data_date,key,sum(getattr(p,attr) for p in observations),unit,True,now)
            for key,attr,unit in [('ordered_units','ordered_units','units'),
                                 ('ordered_revenue','ordered_revenue','RUB'),
                                 ('cancellations_units','cancellations_units','units')]]
    return points,observations
