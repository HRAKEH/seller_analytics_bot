"""Financial normalization with conservative, source-explicit semantics."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from app.storage.models import MetricPoint

class FinanceNormalizationError(ValueError):
    pass


@dataclass
class ProductFinanceObservation:
    data_date: str
    marketplace_sku: str
    name: str
    offer_id: str | None = None
    metrics: dict[str,float] = field(default_factory=dict)
    as_of: str | None = None

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def _num(value: Any) -> float:
    if value is None or value == '': return 0.0
    if isinstance(value, dict): value = value.get('amount', 0)
    try: return float(str(value).replace(' ', '').replace(',', '.'))
    except (TypeError, ValueError) as exc: raise FinanceNormalizationError(f'Некорректная денежная сумма: {value!r}') from exc

def normalize_wb_finance_report(row: dict[str, Any], connection_id: int) -> tuple[str, list[MetricPoint]]:
    if not isinstance(row, dict): raise FinanceNormalizationError('WB finance row must be object')
    day = str(row.get('dateTo') or row.get('dateFrom') or row.get('createDate') or '')[:10]
    if len(day) != 10: raise FinanceNormalizationError('WB finance row has no report date')
    as_of = str(row.get('createDate') or _now())
    mapping = {
        'financial_sales': 'retailAmountSum', 'goods_payable': 'forPaySum',
        'bank_payment': 'bankPaymentSum', 'logistics': 'deliveryServiceSum',
        'storage': 'paidStorageSum', 'acceptance': 'paidAcceptanceSum',
        'services': 'deductionSum', 'penalties': 'penaltySum',
        'compensation': 'additionalPaymentSum',
    }
    points=[]
    for metric, field in mapping.items():
        if field in row and row.get(field) is not None:
            value=_num(row.get(field))
            if metric in {'logistics','storage','acceptance','services','penalties'}: value=abs(value)
            points.append(MetricPoint(connection_id,day,metric,value,'RUB',False,as_of))
    if not points: raise FinanceNormalizationError('WB finance row has no supported totals')
    return day, points

def normalize_ozon_accruals(payload: Any, connection_id: int, data_date: str) -> list[MetricPoint]:
    if not isinstance(payload, dict): raise FinanceNormalizationError('Ozon finance payload must be object')
    rows=payload.get('accruals') or []
    if not isinstance(rows,list): raise FinanceNormalizationError('Ozon finance accruals must be list')
    sales=commission=logistics=services=0.0
    net=0.0
    for row in rows:
        if not isinstance(row,dict): continue
        net += _num(row.get('total_amount'))
        category=str(row.get('accrued_category') or '').upper()
        posting=row.get('posting') or {}
        products=posting.get('products') or posting.get('items') or []
        if isinstance(products,list):
            for item in products:
                if not isinstance(item,dict): continue
                c=item.get('commission') or {}
                seller_price=_num(c.get('seller_price'))
                sale_comm=_num(c.get('sale_commission'))
                if category in {'POSTING',''}: sales += seller_price
                if sale_comm < 0: commission += -sale_comm
                delivery=item.get('delivery') or {}
                d=_num(delivery.get('total_accrued'))
                if d < 0: logistics += -d
        def walk_accrued(node):
            total=0.0
            if isinstance(node,dict):
                if isinstance(node.get('accrued'),dict):
                    total += _num(node['accrued'])
                for key,value in node.items():
                    if key != 'accrued': total += walk_accrued(value)
            elif isinstance(node,list):
                for value in node: total += walk_accrued(value)
            return total
        fee_signed = walk_accrued(row.get('item_fees')) + walk_accrued(row.get('non_item_fee')) + walk_accrued(row.get('container_fees'))
        if fee_signed < 0: services += -fee_signed
    now=_now()
    return [
        MetricPoint(connection_id,data_date,'financial_sales',sales,'RUB',False,now),
        MetricPoint(connection_id,data_date,'commission',commission,'RUB',False,now),
        MetricPoint(connection_id,data_date,'logistics',logistics,'RUB',False,now),
        MetricPoint(connection_id,data_date,'services',services,'RUB',False,now),
        MetricPoint(connection_id,data_date,'marketplace_net',net,'RUB',False,now),
    ]


def _pick(row: dict[str,Any], *names: str) -> Any:
    for name in names:
        if name in row and row.get(name) is not None:
            return row.get(name)
    return None


def normalize_wb_product_finance(payload: Any) -> list[ProductFinanceObservation]:
    """Normalize detailed WB finance rows without inventing missing components."""
    if not isinstance(payload,list):
        raise FinanceNormalizationError('WB detailed finance payload must be list')
    grouped: dict[tuple[str,str],ProductFinanceObservation]={}
    now=_now()
    for row in payload:
        if not isinstance(row,dict): continue
        raw_day=_pick(row,'saleDt','sale_dt','rrDt','rr_dt','date','createDate','create_dt')
        day=str(raw_day or '')[:10]
        if len(day)!=10: continue
        sku=str(_pick(row,'nmId','nm_id','nmID','nmid') or '').strip()
        if not sku: continue
        offer=str(_pick(row,'supplierArticle','supplier_article','sa_name','vendorCode') or '').strip() or None
        name=str(_pick(row,'subjectName','subject_name','brandName','brand_name') or offer or f'WB {sku}')
        key=(day,sku); obs=grouped.get(key)
        if obs is None:
            obs=grouped[key]=ProductFinanceObservation(day,sku,name,offer_id=offer,as_of=now)
        fields={
            'financial_sales':('retailAmount','retail_amount','retailPriceWithdiscRub','retail_price_withdisc_rub'),
            'goods_payable':('ppvzForPay','ppvz_for_pay'),
            'commission':('commission','commissionRub','commission_rub'),
            'logistics':('deliveryRub','delivery_rub'),
            'storage':('storageFee','storage_fee'),
            'acceptance':('acceptanceFee','acceptance_fee'),
            'services':('deduction','deductionSum','deduction_sum'),
            'penalties':('penalty','penaltySum','penalty_sum'),
        }
        for metric,names in fields.items():
            raw=_pick(row,*names)
            if raw is None: continue
            value=_num(raw)
            if metric in {'commission','logistics','storage','acceptance','services','penalties'}:
                value=abs(value)
            obs.metrics[metric]=obs.metrics.get(metric,0.0)+value
    return sorted(grouped.values(),key=lambda x:(x.data_date,x.marketplace_sku))


def normalize_ozon_product_finance(payload: Any, data_date: str) -> list[ProductFinanceObservation]:
    """Extract SKU-level finance from product-bound Ozon accrual components only."""
    if not isinstance(payload,dict):
        raise FinanceNormalizationError('Ozon finance payload must be object')
    rows=payload.get('accruals') or []
    if not isinstance(rows,list):
        raise FinanceNormalizationError('Ozon finance accruals must be list')
    grouped: dict[str,ProductFinanceObservation]={}; now=_now()

    def obs_for(sku: Any, name: Any = None, offer: Any = None) -> ProductFinanceObservation | None:
        clean=str(sku or '').strip()
        if not clean: return None
        obs=grouped.get(clean)
        if obs is None:
            off=str(offer or '').strip() or None
            obs=grouped[clean]=ProductFinanceObservation(data_date,clean,str(name or off or f'Ozon {clean}'),off,as_of=now)
        return obs

    for row in rows:
        if not isinstance(row,dict): continue
        posting=row.get('posting') or {}
        products=posting.get('products') or posting.get('items') or []
        for item in products if isinstance(products,list) else []:
            if not isinstance(item,dict): continue
            obs=obs_for(item.get('sku'),item.get('name'),item.get('offer_id'))
            if obs is None: continue
            commission=item.get('commission') or {}
            seller=_num(commission.get('seller_price'))
            sale_comm=_num(commission.get('sale_commission'))
            delivery=item.get('delivery') or {}
            delivery_total=_num(delivery.get('total_accrued'))
            obs.metrics['financial_sales']=obs.metrics.get('financial_sales',0.0)+seller
            if sale_comm:
                obs.metrics['commission']=obs.metrics.get('commission',0.0)+abs(sale_comm)
            if delivery_total:
                obs.metrics['logistics']=obs.metrics.get('logistics',0.0)+abs(delivery_total)

        fee_root=row.get('item_fees') or {}
        fee_groups=fee_root.get('fees') if isinstance(fee_root,dict) else None
        for group in fee_groups if isinstance(fee_groups,list) else []:
            if not isinstance(group,dict): continue
            obs=obs_for(group.get('sku'))
            if obs is None: continue
            total=0.0
            for fee in group.get('fees') or []:
                if isinstance(fee,dict): total += abs(_num(fee.get('accrued')))
            if total: obs.metrics['services']=obs.metrics.get('services',0.0)+total
    return sorted(grouped.values(),key=lambda x:x.marketplace_sku)

def normalize_wb_ad_stats(payload: Any, connection_id: int) -> dict[str,list[MetricPoint]]:
    if not isinstance(payload,list): raise FinanceNormalizationError('WB ad stats must be list')
    totals: dict[str,list[float]]={}
    for campaign in payload:
        if not isinstance(campaign,dict): continue
        for day in campaign.get('days') or []:
            if not isinstance(day,dict): continue
            ds=str(day.get('date') or '')[:10]
            if len(ds)!=10: continue
            bucket=totals.setdefault(ds,[0.0,0.0])
            bucket[0]+=_num(day.get('sum'))
            bucket[1]+=_num(day.get('sum_price') if day.get('sum_price') is not None else day.get('sumPrice'))
    now=_now()
    return {d:[MetricPoint(connection_id,d,'ad_spend',v[0],'RUB',False,now),
               MetricPoint(connection_id,d,'ad_attributed_sales',v[1],'RUB',False,now)] for d,v in totals.items()}

def normalize_ozon_ad_stats(payload: Any, connection_id: int) -> dict[str,list[MetricPoint]]:
    rows=payload if isinstance(payload,list) else (payload.get('rows') or payload.get('result') or payload.get('data') or []) if isinstance(payload,dict) else []
    if isinstance(rows,dict): rows=rows.get('rows') or rows.get('data') or []
    totals: dict[str,list[float]]={}
    for row in rows if isinstance(rows,list) else []:
        if not isinstance(row,dict): continue
        ds=str(row.get('date') or row.get('day') or '')[:10]
        if len(ds)!=10: continue
        b=totals.setdefault(ds,[0.0,0.0])
        b[0]+=_num(row.get('expense') if row.get('expense') is not None else row.get('spend'))
        b[1]+=_num(row.get('sales') if row.get('sales') is not None else row.get('revenue'))
    now=_now()
    return {d:[MetricPoint(connection_id,d,'ad_spend',v[0],'RUB',False,now), MetricPoint(connection_id,d,'ad_attributed_sales',v[1],'RUB',False,now)] for d,v in totals.items()}
