"""Normalization helpers for marketplace inbound supplies.

Only explicit marketplace quantities are used.  Unknown ETA stays unknown and is
never treated as available stock by the replenishment engine.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

WB_STATUS_LABELS={1:'NOT_PLANNED',2:'PLANNED',3:'SHIPMENT_ALLOWED',4:'ACCEPTANCE',5:'ACCEPTED',6:'SHIPPED_AT_GATE'}


def normalize_wb_supply(row: dict[str,Any], goods: list[dict[str,Any]]) -> dict[str,Any]:
    supply_id=row.get('supplyID')
    preorder_id=row.get('preorderID')
    external=(f'supply:{supply_id}' if supply_id is not None else f'preorder:{preorder_id}')
    items=[]
    for item in goods or []:
        sku=item.get('nmID')
        if sku is None: continue
        planned=float(item.get('quantity') or 0)
        accepted=float(item.get('acceptedQuantity') or item.get('readyForSaleQuantity') or 0)
        items.append({'marketplace_sku':str(sku),'planned_units':planned,'accepted_units':accepted,
                      'remaining_units':max(0.0,planned-accepted)})
    return {
        'external_supply_id':external,
        'status':WB_STATUS_LABELS.get(int(row.get('statusID') or 0),str(row.get('statusID') or 'UNKNOWN')),
        'planned_at':row.get('supplyDate'),
        'arrival_at':row.get('factDate'),
        'warehouse_name':str(row.get('warehouseName') or row.get('actualWarehouseName') or ''),
        'metadata':{'supplyID':supply_id,'preorderID':preorder_id,'statusID':row.get('statusID'),
                    'boxTypeID':row.get('boxTypeID'),'updatedDate':row.get('updatedDate')},
        'items':items,
    }


def normalize_ozon_order(order: dict[str,Any], bundles: dict[str,list[dict[str,Any]]]) -> list[dict[str,Any]]:
    out=[]
    timeslot=((order.get('timeslot') or {}).get('timeslot') or {}) if isinstance(order.get('timeslot'),dict) else {}
    order_planned=timeslot.get('from') or timeslot.get('from_in_timezone')
    order_id=str(order.get('order_id') or order.get('order_number') or '')
    for supply in order.get('supplies') or []:
        if not isinstance(supply,dict): continue
        supply_id=str(supply.get('supply_id') or order_id)
        bundle_id=str(supply.get('bundle_id') or '')
        wh=supply.get('storage_warehouse') or {}
        planned=wh.get('arrival_date') or order_planned
        items=[]
        for item in bundles.get(bundle_id,[]):
            sku=item.get('sku')
            if sku is None: continue
            qty=float(item.get('quantity') or 0)
            items.append({'marketplace_sku':str(sku),'planned_units':qty,'accepted_units':0.0,'remaining_units':qty})
        out.append({
            'external_supply_id':f'supply:{supply_id}',
            'status':str(supply.get('state') or order.get('state') or 'UNKNOWN'),
            'planned_at':planned,
            'arrival_at':wh.get('arrival_date'),
            'warehouse_name':str(wh.get('name') or (order.get('dropoff_warehouse') or {}).get('name') or ''),
            'metadata':{'order_id':order_id,'order_number':order.get('order_number'),'bundle_id':bundle_id,
                        'is_crossdock':supply.get('is_crossdock'),'state_updated_date':order.get('state_updated_date')},
            'items':items,
        })
    return out
