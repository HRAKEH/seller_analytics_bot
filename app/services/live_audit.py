"""Bounded, read-only host probes. Reports contain no credentials or raw data."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import time
from typing import Any

import httpx

from app.config import Settings
from app.integrations import OzonClient, WildberriesClient
from app.storage import LATEST_SCHEMA_VERSION
from .finance import normalize_ozon_accruals, normalize_wb_finance_report, normalize_wb_product_finance
from .normalization import normalize_ozon_order_analytics, normalize_wb_orders
from .product_analytics import normalize_ozon_stocks, normalize_wb_stocks


@dataclass(frozen=True)
class Probe:
    key: str
    method: str
    path: str
    rate_key: str
    body: dict | None = None
    params: dict | None = None
    critical: bool = False


def inspect_database(path: Path, shop_id: int) -> tuple[dict[str,Any],str]:
    """Read an existing DB without migrations, new shops, metrics or leases."""
    report={'present':path.is_file(),'ok':False,'shop_id':shop_id}
    profile='DEFAULT'
    if not report['present']:
        report['error_kind']='database_missing'; return report,profile
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            conn.execute('PRAGMA trusted_schema=OFF')
            deadline=time.monotonic()+5
            conn.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
            report['quick_check']=conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            report['schema_version']=conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
            shop=conn.execute('SELECT id,active,credential_profile FROM shops WHERE id=?',(shop_id,)).fetchone()
            report['shop_active']=bool(shop and shop['active'])
            if shop: profile=str(shop['credential_profile'] or 'DEFAULT')
            rows=conn.execute('SELECT id,credential_profile FROM shops WHERE active=1 ORDER BY id').fetchall()
            report['active_shop_ids']=[int(r['id']) for r in rows]
            report['shops_with_same_profile']=sum(str(r['credential_profile'] or 'DEFAULT')==profile for r in rows)
            report['ok']=bool(report['quick_check'] and report['schema_version']==LATEST_SCHEMA_VERSION and report['shop_active'])
    except sqlite3.Error:
        report['error_kind']='database_read_failed'
    return report,profile


def _rows(data: Any, *keys: str) -> list:
    value=data
    for key in keys:
        if not isinstance(value,dict) or key not in value: raise ValueError('Unexpected response shape')
        value=value[key]
    if not isinstance(value,list): raise ValueError('Unexpected response shape')
    return value


def validate_probe(key: str, data: Any, day: date) -> dict[str,Any]:
    """Return counts and field names only: no names, SKU, sales or identifiers."""
    ds=day.isoformat(); count=None; metrics=[]; sample_only=False
    if key=='wb_auth':
        if not isinstance(data,dict) or not any(data.get(k) for k in ('name','tradeMark','sid')):
            raise ValueError('Unexpected seller response')
    elif key=='ozon_auth':
        if not isinstance(data,dict) or not isinstance(data.get('company'),dict) or not data['company']:
            raise ValueError('Unexpected seller response')
    elif key=='ozon_roles':
        if not isinstance(data,dict) or not isinstance(data.get('roles'),list): raise ValueError('Unexpected roles response')
    elif key=='wb_orders':
        if not isinstance(data,list): raise ValueError('Orders must be a list')
        points=normalize_wb_orders(data,0,ds); count=len(data); metrics=[p.metric_key for p in points]
        sample_only=len(data)>=79000
    elif key=='ozon_orders':
        rows=_rows(data,'result','data'); points=normalize_ozon_order_analytics(data,0,ds)
        count=len(rows); metrics=[p.metric_key for p in points]; sample_only=len(rows)>=1000
    elif key=='ozon_stocks':
        rows=_rows(data,'items'); count=len(normalize_ozon_stocks(data))
        sample_only=bool(data.get('cursor')) and bool(rows)
    elif key in {'wb_stocks_fbw','wb_stocks_fbs'}:
        if data is None: data={'data':{'items':[]}}
        rows=_rows(data,'data','items') if isinstance(data,dict) and 'data' in data else _rows(data,'items')
        count=len(normalize_wb_stocks(data,fulfillment_scheme='FBW' if key.endswith('fbw') else 'FBS'))
        sample_only=len(rows)>=1000
    elif key=='wb_finance':
        if data is None: data=[]
        if not isinstance(data,list): raise ValueError('Finance must be a list')
        for row in data:
            _,points=normalize_wb_finance_report(row,0); metrics.extend(p.metric_key for p in points)
        count=len(data); sample_only=len(data)>=1000
    elif key=='wb_sku_finance':
        if data is None: data=[]
        if not isinstance(data,list): raise ValueError('Finance detail must be a list')
        rows=normalize_wb_product_finance(data); count=len(rows)
        metrics=[k for row in rows for k in row.metrics]; sample_only=bool(data)
    elif key=='ozon_finance':
        points=normalize_ozon_accruals(data,0,ds); count=len(_rows(data,'accruals'))
        metrics=[p.metric_key for p in points]; sample_only=bool(data.get('last_id'))
    else:
        raise ValueError('Unknown probe')
    return {'shape_ok':True,'row_count':count,'metric_keys':sorted(set(metrics)),
            'sample_only':sample_only,'empty':count==0 if count is not None else False}


def probes_for(source: str, day: date, *, include_wb_stocks: bool=False) -> list[Probe]:
    ds=day.isoformat(); finance_day=(day-timedelta(days=7)).isoformat()
    if source=='ozon':
        return [Probe('ozon_auth','POST','/v1/seller/info','seller_info',{},critical=True),
                Probe('ozon_roles','POST','/v1/roles','roles',{}),
                Probe('ozon_orders','POST','/v1/analytics/data','analytics',
                      {'date_from':ds,'date_to':ds,'metrics':['ordered_units','revenue'],
                       'dimension':['sku'],'limit':1000,'offset':0},critical=True),
                Probe('ozon_stocks','POST','/v4/product/info/stocks','stocks',
                      {'cursor':'','filter':{'visibility':'ALL','with_quant':{'created':True,'exists':True}},'limit':1000}),
                Probe('ozon_finance','POST','/v1/finance/accrual/by-day','finance',{'date':finance_day,'last_id':''})]
    probes=[Probe('wb_auth','GET','https://common-api.wildberries.ru/api/v1/seller-info','seller_info',critical=True),
            Probe('wb_orders','GET','/api/v1/supplier/orders','default',params={'dateFrom':ds,'flag':1},critical=True),
            Probe('wb_finance','POST','https://finance-api.wildberries.ru/api/finance/v1/sales-reports/list',
                  'finance_list',{'dateFrom':finance_day,'dateTo':finance_day,'period':'daily','limit':1000,'offset':0}),
            Probe('wb_sku_finance','POST','https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed',
                  'finance_detailed',{'dateFrom':finance_day,'dateTo':finance_day,'period':'daily','limit':1000,'rrdId':0})]
    if include_wb_stocks:
        for key,endpoint in [('wb_stocks_fbw','wb-warehouses'),('wb_stocks_fbs','seller-warehouses')]:
            probes.append(Probe(key,'POST',f'https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/{endpoint}',
                                'analytics_stocks',{'limit':1000,'offset':0}))
    return probes


async def _marketplace_probes(client, probes: list[Probe], day: date) -> list[dict[str,Any]]:
    rows=[]; blocked=False
    for probe in probes:
        row={'key':probe.key,'critical':probe.critical,'ok':False}
        if blocked:
            row.update(status='skipped',error_kind='previous_auth_or_rate_limit'); rows.append(row); continue
        try:
            result=await asyncio.wait_for(client.request(probe.method,probe.path,json=probe.body,params=probe.params,
                headers=client._headers(),rate_key=probe.rate_key,retry_on_429=False,fail_fast_rate_limit=True),timeout=15)
            row.update(status='checked',http_status=result.status_code,attempts=result.attempts)
            if result.ok:
                validation_day=day-timedelta(days=7) if 'finance' in probe.key else day
                row.update(validate_probe(probe.key,result.data,validation_day)); row['ok']=True
            else:
                row['error_kind']='rate_limit' if result.status_code==429 else ('access_denied' if result.status_code in {401,403} else 'http_or_network_error')
                if result.status_code==429:
                    row['retry_after_seconds']=int(client.cooldown_remaining(probe.rate_key)+0.999)
                blocked=result.status_code==429 or (probe.key.endswith('_auth') and result.status_code in {401,403})
        except (asyncio.TimeoutError,httpx.TimeoutException): row.update(status='checked',error_kind='timeout')
        except Exception: row.update(status='checked',error_kind='response_or_transport_error')
        rows.append(row)
    return rows


async def _telegram_probe(token: str, *, transport=None) -> dict[str,Any]:
    report={'key':'telegram_auth','critical':True,'ok':False,'configured':bool(token)}
    if not token: return report
    try:
        async with httpx.AsyncClient(timeout=10,transport=transport) as client:
            response=await client.get(f'https://api.telegram.org/bot{token}/getMe')
            body=response.json()
            report.update(http_status=response.status_code,ok=response.status_code==200 and body.get('ok') is True
                          and isinstance(body.get('result'),dict) and body['result'].get('is_bot') is True)
    except Exception: report['error_kind']='telegram_probe_failed'
    return report


async def run_live_audit(settings: Settings, *, shop_id: int=1, day: date,
                         include_wb_stocks: bool=False, transport=None) -> dict[str,Any]:
    database,profile=inspect_database(settings.db_file,shop_id)
    report={'generated_at_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'version':(Path(__file__).resolve().parents[2]/'VERSION').read_text().strip(),
            'order_date':day.isoformat(),'finance_date':(day-timedelta(days=7)).isoformat(),
            'read_only':True,'database':database,'owner_configured':bool(settings.owner_ids),'probes':[],
            'warnings':(['Several active shops use the same credential profile; inspect duplicates.']
                        if database.get('shops_with_same_profile',0)>1 else []),
            'limitations':['Single-page probes; no historical import, Telegram messages or polling.',
                           'No raw responses, credentials, seller names, SKU or monetary totals in this report.',
                           'Advertising and supply APIs are not probed; optional capabilities need separate checks.']}
    if not database.get('ok'):
        report['ok']=False; report['limitations'].append('Database/shop check failed; API probes skipped.'); return report
    creds=settings.credentials_for_profile(profile)
    report['probes'].append(await _telegram_probe(settings.telegram_token,transport=transport))
    configured=[]
    for source,present in [('wildberries',creds.has_wb),('ozon',creds.has_ozon)]:
        if not present: continue
        configured.append(source)
        client=(WildberriesClient(creds.wb_api_token,timeout=10,min_interval=settings.wb_min_interval,max_retries=0,transport=transport)
                if source=='wildberries' else OzonClient(creds.ozon_client_id,creds.ozon_api_key,timeout=10,
                     min_interval=settings.ozon_min_interval,max_retries=0,transport=transport))
        try: report['probes'].extend(await _marketplace_probes(client,probes_for(source,day,include_wb_stocks=include_wb_stocks),day))
        finally: await client.close()
    report['configured_sources']=configured
    critical=[r for r in report['probes'] if r['critical']]
    report['ok']=bool(database['ok'] and settings.owner_ids and configured and critical and all(r['ok'] for r in critical))
    report['optional_failures']=[r['key'] for r in report['probes'] if not r['critical'] and not r['ok']]
    report['optional_status']='partial' if report['optional_failures'] else 'checked'
    return report
