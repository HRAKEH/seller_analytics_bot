"""Lifecycle reconciliation: order -> cancel -> sale/return/posting -> finance.

The module only creates source-faithful observations. Exact matching is performed
only with identifiers actually supplied by marketplaces: WB ``srid`` and Ozon
``posting_number``. No synthetic order matching is invented.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any
from .numeric import finite_number


class ReconciliationNormalizationError(ValueError):
    pass


@dataclass(frozen=True)
class CommerceObservation:
    marketplace_sku: str
    name: str
    data_date: str
    event_kind: str
    source_name: str
    fingerprint: str
    offer_id: str | None = None
    event_time: str | None = None
    external_order_id: str | None = None
    external_event_id: str | None = None
    quantity: float = 0.0
    gross_amount: float = 0.0
    net_amount: float | None = None
    fulfillment_scheme: str = 'ALL'
    is_preliminary: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def _fingerprint(prefix: str, *parts: Any) -> str:
    raw='|'.join([prefix, *(_canonical(x) for x in parts)])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _num(value: Any) -> float:
    try:
        return finite_number(value)
    except (TypeError, ValueError) as exc:
        raise ReconciliationNormalizationError(f'Non-numeric amount: {value!r}') from exc


def _pick(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row.get(name) is not None:
            return row.get(name)
    return None


def _day(value: Any) -> str | None:
    text=str(value or '')[:10]
    if len(text) != 10:
        return None
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return None
    return text


def normalize_wb_order_events(payload: Any, *, data_date: str | None = None) -> list[CommerceObservation]:
    if not isinstance(payload, list):
        raise ReconciliationNormalizationError('WB orders payload must be a list')
    out=[]
    for row in payload:
        if not isinstance(row, dict):
            continue
        day=_day(row.get('date'))
        if not day or (data_date and day != data_date):
            continue
        sku=str(row.get('nmId') or row.get('barcode') or row.get('supplierArticle') or '').strip()
        if not sku:
            continue
        srid=str(row.get('srid') or '').strip() or None
        offer=str(row.get('supplierArticle') or '').strip() or None
        name=' · '.join(x for x in (str(row.get('brand') or '').strip(), str(row.get('subject') or '').strip(), offer or '') if x) or f'WB {sku}'
        amount=_num(_pick(row,'priceWithDisc','finishedPrice','totalPrice'))
        base_id=srid or _fingerprint('wb-order-row', row)
        out.append(CommerceObservation(
            sku,name,day,'order','wb_orders',_fingerprint('wb-order',base_id,sku),offer,
            str(row.get('date') or '') or None,srid,None,1.0,amount,None,'ALL',True,
            {'g_number':row.get('gNumber'),'last_change_date':row.get('lastChangeDate')}
        ))
        if bool(row.get('isCancel')):
            cancel_time=str(row.get('cancelDate') or row.get('cancel_dt') or row.get('lastChangeDate') or row.get('date') or '') or None
            out.append(CommerceObservation(
                sku,name,day,'cancel','wb_orders',_fingerprint('wb-cancel',base_id,sku),offer,
                cancel_time,srid,None,1.0,amount,None,'ALL',True,
                {'g_number':row.get('gNumber'),'last_change_date':row.get('lastChangeDate')}
            ))
    return out


def normalize_wb_sales_events(payload: Any, *, start_date: str | None = None,
                               end_date: str | None = None) -> list[CommerceObservation]:
    if not isinstance(payload, list):
        raise ReconciliationNormalizationError('WB sales payload must be a list')
    out=[]
    for row in payload:
        if not isinstance(row, dict):
            continue
        day=_day(row.get('date'))
        if not day or (start_date and day < start_date) or (end_date and day > end_date):
            continue
        sku=str(row.get('nmId') or row.get('barcode') or row.get('supplierArticle') or '').strip()
        if not sku:
            continue
        sale_id=str(row.get('saleID') or row.get('saleId') or '').strip() or None
        srid=str(row.get('srid') or '').strip() or None
        kind='return' if (sale_id or '').upper().startswith('R') else 'sale'
        offer=str(row.get('supplierArticle') or '').strip() or None
        name=' · '.join(x for x in (str(row.get('brand') or '').strip(), str(row.get('subject') or '').strip(), offer or '') if x) or f'WB {sku}'
        gross=_num(_pick(row,'priceWithDisc','finishedPrice','totalPrice'))
        net=_num(row.get('forPay')) if row.get('forPay') is not None else None
        stable=sale_id or _fingerprint('wb-sale-row',row)
        out.append(CommerceObservation(
            sku,name,day,kind,'wb_sales',_fingerprint('wb-sale',stable,srid,sku),offer,
            str(row.get('date') or '') or None,srid,sale_id,1.0,gross,net,'ALL',True,
            {'warehouse':row.get('warehouseName'),'last_change_date':row.get('lastChangeDate')}
        ))
    return out


def normalize_wb_finance_events(payload: Any, *, start_date: str | None = None,
                                 end_date: str | None = None) -> list[CommerceObservation]:
    if not isinstance(payload, list):
        raise ReconciliationNormalizationError('WB finance payload must be a list')
    out=[]
    for row in payload:
        if not isinstance(row,dict):
            continue
        day=_day(_pick(row,'saleDt','sale_dt','rrDate','rrDt','rr_dt','orderDt','order_dt'))
        if not day or (start_date and day < start_date) or (end_date and day > end_date):
            continue
        sku=str(_pick(row,'nmId','nm_id') or '').strip()
        if not sku:
            continue
        srid=str(_pick(row,'srid') or '').strip() or None
        rrd=str(_pick(row,'rrdId','rrd_id') or '').strip() or None
        offer=str(_pick(row,'vendorCode','supplierArticle','sa_name') or '').strip() or None
        name=str(_pick(row,'title','subjectName','subject_name','brandName','brand_name') or offer or f'WB {sku}')
        gross=_num(_pick(row,'retailAmount','retail_amount'))
        raw_net=_pick(row,'ppvzForPay','ppvz_for_pay','forPay','for_pay')
        net=_num(raw_net) if raw_net is not None else None
        stable=rrd or _fingerprint('wb-finance-row',row)
        out.append(CommerceObservation(
            sku,name,day,'finance','wb_finance',_fingerprint('wb-finance',stable),offer,
            str(_pick(row,'rrDate','rrDt','rr_dt','saleDt','sale_dt') or '') or None,
            srid,rrd,float(_pick(row,'quantity') or 0),gross,net,'ALL',False,
            {'doc_type':_pick(row,'docTypeName','doc_type_name'),'operation':_pick(row,'sellerOperName','supplier_oper_name')}
        ))
    return out


def normalize_ozon_posting_events(payload: Any, *, fulfillment_scheme: str,
                                   start_date: str | None = None,
                                   end_date: str | None = None) -> list[CommerceObservation]:
    if not isinstance(payload,dict):
        raise ReconciliationNormalizationError('Ozon postings payload must be an object')
    rows=payload.get('postings') or (payload.get('result') or {}).get('postings') or []
    if not isinstance(rows,list):
        raise ReconciliationNormalizationError('Ozon postings must be a list')
    out=[]
    for posting in rows:
        if not isinstance(posting,dict):
            continue
        event_time=str(posting.get('created_at') or posting.get('in_process_at') or posting.get('shipment_date') or '')
        day=_day(event_time)
        if not day or (start_date and day < start_date) or (end_date and day > end_date):
            continue
        posting_number=str(posting.get('posting_number') or posting.get('postingNumber') or '').strip() or None
        status=str(posting.get('status') or posting.get('substatus') or '').strip()
        kind='cancel' if 'cancel' in status.casefold() else 'posting'
        for item in posting.get('products') or []:
            if not isinstance(item,dict):
                continue
            sku=str(item.get('sku') or '').strip()
            if not sku:
                continue
            offer=str(item.get('offer_id') or '').strip() or None
            name=str(item.get('name') or offer or f'Ozon {sku}')
            qty=_num(item.get('quantity', 1))
            price=_num(item.get('price')) if item.get('price') is not None else 0.0
            stable=posting_number or _fingerprint('ozon-posting-row',posting)
            out.append(CommerceObservation(
                sku,name,day,kind,'ozon_postings',_fingerprint('ozon-posting',fulfillment_scheme,stable,sku,kind),offer,
                event_time or None,posting_number,None,qty,price*qty,None,fulfillment_scheme,True,
                {'status':status,'substatus':posting.get('substatus')}
            ))
    return out


def normalize_ozon_finance_events(payload: Any, *, data_date: str) -> list[CommerceObservation]:
    if not isinstance(payload,dict):
        raise ReconciliationNormalizationError('Ozon finance payload must be an object')
    rows=payload.get('accruals') or []
    if not isinstance(rows,list):
        raise ReconciliationNormalizationError('Ozon finance accruals must be a list')
    out=[]
    occurrences: dict[str,int]={}
    for row in rows:
        if not isinstance(row,dict):
            continue
        # A pagination/reordering change must not invent new financial events.
        # Preserve multiplicity of identical rows without using their position.
        row_key=_fingerprint('ozon-finance-row',data_date,row)
        occurrence=occurrences.get(row_key,0); occurrences[row_key]=occurrence+1
        posting=row.get('posting') or {}
        posting_number=str(_pick(posting,'posting_number','postingNumber','number') or '').strip() or None
        products=posting.get('products') or posting.get('items') or []
        for item_index,item in enumerate(products if isinstance(products,list) else []):
            if not isinstance(item,dict):
                continue
            sku=str(item.get('sku') or '').strip()
            if not sku:
                continue
            offer=str(item.get('offer_id') or '').strip() or None
            name=str(item.get('name') or offer or f'Ozon {sku}')
            commission=item.get('commission') or {}
            gross=_num(commission.get('seller_price'))
            sale_comm=_num(commission.get('sale_commission'))
            delivery=_num((item.get('delivery') or {}).get('total_accrued'))
            stable=_fingerprint('ozon-finance-source',row_key,occurrence,item_index)
            out.append(CommerceObservation(
                sku,name,data_date,'finance','ozon_finance',_fingerprint('ozon-finance',stable,sku),offer,
                str(row.get('date') or data_date),posting_number,None,0.0,gross,None,'ALL',False,
                {'accrued_category':row.get('accrued_category'),'type_id':row.get('type_id'),
                 'sale_commission':sale_comm,'delivery_accrued':delivery}
            ))
    return out
