"""Financial normalization with conservative, source-explicit semantics."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from app.storage.models import MetricPoint
from .numeric import finite_number
from .money import decimal_amount, money_value, money_sum

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
    if isinstance(value,dict) and value.get('currency') not in (None,'','RUB','RUR','руб'):
        raise FinanceNormalizationError('Unsupported finance currency; RUB is required')
    try: return finite_number(value)
    except (TypeError, ValueError) as exc: raise FinanceNormalizationError('Некорректная денежная сумма') from exc

def normalize_wb_finance_report(row: dict[str, Any], connection_id: int) -> tuple[str, list[MetricPoint]]:
    if not isinstance(row, dict): raise FinanceNormalizationError('WB finance row must be object')
    day = str(row.get('dateTo') or row.get('dateFrom') or row.get('createDate') or '')[:10]
    try: date.fromisoformat(day)
    except ValueError as exc: raise FinanceNormalizationError('WB finance row has no valid report date') from exc
    if row.get('currency') not in (None,'','RUB','RUR','руб'):
        raise FinanceNormalizationError('Unsupported finance currency; RUB is required')
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

def _accrual_rows(payload: Any) -> list[dict[str,Any]]:
    if not isinstance(payload,dict) or 'accruals' not in payload:
        raise FinanceNormalizationError('Ozon finance payload must contain accruals')
    rows=payload['accruals']
    if not isinstance(rows,list) or any(not isinstance(x,dict) for x in rows):
        raise FinanceNormalizationError('Ozon finance accruals must be a list of objects')
    return rows


def _walk_fee_decimal(node: Any, *, ads_only: bool=False):
    """Accumulate recursively without intermediate rounding."""
    if isinstance(node,dict):
        include='accrued' in node and (not ads_only or node.get('type_id') in (41,54))
        own=decimal_amount(_num(node.get('accrued'))) if include else decimal_amount(0)
        return own+sum((_walk_fee_decimal(v,ads_only=ads_only) for k,v in node.items() if k!='accrued'),decimal_amount(0))
    if isinstance(node,list):return sum((_walk_fee_decimal(v,ads_only=ads_only) for v in node),decimal_amount(0))
    return decimal_amount(0)


def _walk_accrued(node: Any) -> float:
    """Sum signed fee components once, including scalar and Money amounts."""
    return money_value(_walk_fee_decimal(node))


def _walk_ad_accrued(node: Any) -> float:
    """Advertising fee IDs verified against the supplied Ozon accrual export.

    41: pay-per-click; 54: promotion paid per order. Other fee types remain in
    services, so unidentified charges are still included exactly once.
    """
    return money_value(_walk_fee_decimal(node,ads_only=True))


def _ozon_has_sales(row: dict[str,Any]) -> bool:
    return str(row.get('accrued_category') or '').upper() in {'POSTING',''}


def wb_document_amount(row: dict[str,Any], value: Any) -> float:
    """WB return documents carry positive amounts: subtract them exactly once."""
    amount=_num(value)
    doc=str(_pick(row,'docTypeName','doc_type_name') or '').strip().casefold()
    return -abs(amount) if doc=='возврат' else amount


def normalize_ozon_accruals(payload: Any, connection_id: int, data_date: str) -> list[MetricPoint]:
    rows=_accrual_rows(payload)
    sales=commission=logistics=services=finance_ad=decimal_amount(0)
    net=decimal_amount(0)
    for row in rows:
        if not isinstance(row,dict): continue
        net += decimal_amount(_num(row.get('total_amount')))
        posting=row.get('posting') or {}
        products=posting.get('products') or posting.get('items') or []
        if isinstance(products,list):
            for item in products:
                if not isinstance(item,dict): continue
                c=item.get('commission') or {}
                seller_price=decimal_amount(_num(c.get('seller_price')))
                sale_comm=decimal_amount(_num(c.get('sale_commission')))
                if _ozon_has_sales(row): sales += seller_price
                # Positive signed cash-flow corrections reduce expenses.
                commission -= sale_comm
                delivery=item.get('delivery') or {}
                d=decimal_amount(_num(delivery.get('total_accrued')))
                logistics -= d
        fee_signed = sum((_walk_fee_decimal(row.get(k)) for k in ('item_fees','non_item_fee','container_fees')),decimal_amount(0))
        services -= fee_signed
        finance_ad -= sum((_walk_fee_decimal(row.get(k),ads_only=True) for k in ('item_fees','non_item_fee','container_fees')),decimal_amount(0))
    now=_now()
    return [
        MetricPoint(connection_id,data_date,'financial_sales',money_value(sales),'RUB',False,now),
        MetricPoint(connection_id,data_date,'commission',money_value(commission),'RUB',False,now),
        MetricPoint(connection_id,data_date,'logistics',money_value(logistics),'RUB',False,now),
        MetricPoint(connection_id,data_date,'services',money_value(services),'RUB',False,now),
        MetricPoint(connection_id,data_date,'marketplace_net',money_value(net),'RUB',False,now),
        MetricPoint(connection_id,data_date,'finance_ad_spend',money_value(finance_ad),'RUB',False,now),
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
        raw_day=_pick(row,'saleDt','sale_dt','rrDate','rrDt','rr_dt','date','createDate','create_dt')
        day=str(raw_day or '')[:10]
        if len(day)!=10: continue
        sku=str(_pick(row,'nmId','nm_id','nmID','nmid') or '').strip()
        if not sku: continue
        offer=str(_pick(row,'vendorCode','supplierArticle','supplier_article','sa_name') or '').strip() or None
        name=str(_pick(row,'title','subjectName','subject_name','brandName','brand_name') or offer or f'WB {sku}')
        key=(day,sku); obs=grouped.get(key)
        if obs is None:
            obs=grouped[key]=ProductFinanceObservation(day,sku,name,offer_id=offer,as_of=now)
        fields={
            'financial_sales':('retailAmount','retail_amount','retailPriceWithdiscRub','retail_price_withdisc_rub'),
            'goods_payable':('forPay','ppvzForPay','ppvz_for_pay'),
            'commission':('commission','commissionRub','commission_rub'),
            'logistics':('deliveryService','deliveryRub','delivery_rub'),
            'storage':('paidStorage','storageFee','storage_fee'),
            'acceptance':('paidAcceptance','acceptanceFee','acceptance_fee','acceptance'),
            'services':('deduction','deductionSum','deduction_sum'),
            'penalties':('penalty','penaltySum','penalty_sum'),
            'compensation':('additionalPayment','additional_payment'),
        }
        for metric,names in fields.items():
            raw=_pick(row,*names)
            if raw is None: continue
            value=_num(raw)
            if metric in {'financial_sales','goods_payable'}:
                value=wb_document_amount(row,raw)
            if metric in {'commission','logistics','storage','acceptance','services','penalties'}:
                value=abs(value)
            obs.metrics[metric]=obs.metrics.get(metric,0.0)+value
    return sorted(grouped.values(),key=lambda x:(x.data_date,x.marketplace_sku))


def normalize_ozon_product_finance(payload: Any, data_date: str) -> list[ProductFinanceObservation]:
    """Extract SKU-level finance from product-bound Ozon accrual components only."""
    rows=_accrual_rows(payload)
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
            if _ozon_has_sales(row):
                obs.metrics['financial_sales']=obs.metrics.get('financial_sales',0.0)+seller
            if sale_comm:
                obs.metrics['commission']=obs.metrics.get('commission',0.0)-sale_comm
            if delivery_total:
                obs.metrics['logistics']=obs.metrics.get('logistics',0.0)-delivery_total

        fee_root=row.get('item_fees') or {}
        fee_groups=fee_root.get('fees') if isinstance(fee_root,dict) else None
        for group in fee_groups if isinstance(fee_groups,list) else []:
            if not isinstance(group,dict): continue
            obs=obs_for(group.get('sku'))
            if obs is None: continue
            total=-_walk_accrued(group.get('fees'))
            if total: obs.metrics['services']=obs.metrics.get('services',0.0)+total
            # Billed advertising is a subset of services, never a second fee.
            billed=-_walk_ad_accrued(group.get('fees'))
            obs.metrics['finance_ad_spend']=money_sum([obs.metrics.get('finance_ad_spend',0),billed])
    for obs in grouped.values():
        obs.metrics={key:money_value(value) for key,value in obs.metrics.items()}
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
        b[0]+=_num(_pick(row,'expense','spend','sum'))
        b[1]+=_num(_pick(row,'ordersMoney','orders_money','sales','revenue','sum_price'))
    now=_now()
    return {d:[MetricPoint(connection_id,d,'ad_spend',v[0],'RUB',False,now), MetricPoint(connection_id,d,'ad_attributed_sales',v[1],'RUB',False,now)] for d,v in totals.items()}
