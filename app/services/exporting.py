"""Portable shop export to XLSX or a ZIP of CSV files."""
from __future__ import annotations
import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from app.storage import Repository
from app.reports.finance import build_finance_report
from app.reports.reconciliation import build_reconciliation_report
from app.reports.ads import build_advertising_report
from app.reports.management import build_management_report
from app.services.supply import build_supply_plan, evaluate_forecast_quality, build_supply_calibration
from app.services.actions import build_action_center
from app.access import item_visible
from .spreadsheet_format import SHEETS, write_sheet, add_descriptions, description_rows

# Export explicit operational fields so a future financial field added to a
# repository query cannot silently become visible to a manager.
OPERATIONAL_METRICS=frozenset({'ordered_units','ordered_revenue','cancellations_units','cancelled_units',
    'delivered_units','returned_units','fulfillment_units','buyer_price_units',
    'buyer_price_revenue','ad_spend','ad_sales','ad_orders','ad_clicks','ad_impressions',
    'ad_attributed_sales','stock_units','sold_units','redeemed_units','returns_units',
    'sales_units','sales_revenue','return_units','return_revenue'})
PRODUCT_FIELDS=frozenset({'product_id','internal_sku','name','listing_id','marketplace_sku',
    'offer_id','marketplace','ordered_units','ordered_revenue'})
INVENTORY_FIELDS=PRODUCT_FIELDS|{'connection_id','fulfillment_scheme','available_units','reserved_units','captured_at'}
INBOUND_FIELDS=frozenset({'marketplace','external_supply_id','status','planned_at','arrival_at',
    'warehouse_name','marketplace_sku','planned_units','accepted_units','remaining_units',
    'product_id','internal_sku','name','updated_at'})
PROMOTION_FIELDS=frozenset({'marketplace','external_promotion_id','promotion_name','promo_type',
    'start_at','end_at','marketplace_sku','in_action','base_price','promo_price','discount_pct',
    'listing_id','product_id','internal_sku','product_name'})


def _fields(rows, allowed):
    return [{key:value for key,value in row.items() if key in allowed} for row in rows]


@dataclass(frozen=True)
class ExportResult:
    path: Path
    format: str
    start: str
    end: str


def _safe_spreadsheet_value(value: Any) -> Any:
    """Prevent CSV/XLSX formula injection from marketplace/user-controlled text."""
    if not isinstance(value,str):
        return value
    probe=value.lstrip(' \t\r\n')
    if probe.startswith(('=','+','-','@')):
        return "'"+value
    return value


def _safe_row(row: dict[str,Any]) -> dict[str,Any]:
    return {k:_safe_spreadsheet_value(v) for k,v in row.items()}


def _daily_rows(repo: Repository, shop_id: int, start: str, end: str) -> list[dict[str,Any]]:
    with repo.db.connect() as c:
        rows=c.execute('''WITH ranked AS (
            SELECT mv.*,mc.marketplace,ROW_NUMBER() OVER(
              PARTITION BY mv.connection_id,mv.data_date,mv.metric_key
              ORDER BY mv.fetched_at DESC,mv.id DESC) rn
            FROM metric_values mv JOIN source_runs sr ON sr.id=mv.source_run_id
            JOIN marketplace_connections mc ON mc.id=mv.connection_id
            WHERE mc.shop_id=? AND mv.data_date BETWEEN ? AND ? AND sr.status IN ('success','partial'))
          SELECT data_date,marketplace,metric_key,value,unit,is_preliminary,as_of,fetched_at
          FROM ranked WHERE rn=1 ORDER BY data_date,marketplace,metric_key''',(shop_id,start,end)).fetchall()
    return [dict(r) for r in rows]


def _cost_rows(repo: Repository, shop_id: int) -> list[dict[str,Any]]:
    with repo.db.connect() as c:
        rows=c.execute('''SELECT p.internal_sku,p.name,mc.marketplace,pl.marketplace_sku,pl.offer_id,
            h.effective_date,h.cost_price,h.source
          FROM products p JOIN product_listings pl ON pl.product_id=p.id
          JOIN marketplace_connections mc ON mc.id=pl.connection_id
          LEFT JOIN product_cost_history h ON h.product_id=p.id
          WHERE p.shop_id=? AND p.active=1
          ORDER BY p.internal_sku,mc.marketplace,pl.marketplace_sku,h.effective_date''',(shop_id,)).fetchall()
    return [dict(r) for r in rows]


def collect_export_tables(repo: Repository, shop_id: int, end: date, days: int, *,
                          include_finance=True, include_technical=True) -> dict[str,list[dict[str,Any]]]:
    start=end-timedelta(days=days-1); start_s=start.isoformat(); end_s=end.isoformat()
    shop=repo.get_shop(shop_id)
    units=repo.product_period_totals(shop_id,start_s,end_s,'ordered_units')
    revenues=repo.product_period_totals(shop_id,start_s,end_s,'ordered_revenue')
    rev={(r['marketplace'],r['marketplace_sku']):float(r['value']) for r in revenues}
    products=[]
    for r in units:
        x=dict(r); x['ordered_units']=float(x.pop('value')); x['ordered_revenue']=rev.get((x['marketplace'],x['marketplace_sku']),0.0); products.append(x)
    inventory=repo.latest_inventory_by_scheme(shop_id)
    finance_rows=[]
    rec_rows=[]
    if include_finance:
        finance=build_finance_report(repo,shop_id,end,days)
        for src in finance.sources:
            for key,value in sorted(src.metrics.items()):
                finance_rows.append({'marketplace':src.marketplace,'metric_key':key,'value':value})
            finance_rows.append({'marketplace':src.marketplace,'metric_key':'estimated_order_cogs','value':src.estimated_order_cogs})
            finance_rows.append({'marketplace':src.marketplace,'metric_key':'cogs_coverage_pct','value':src.cogs_coverage_pct})
        rec=build_reconciliation_report(repo,shop_id,end,days)
        rec_rows=[r.__dict__ for r in rec.rows]
    ads=build_advertising_report(repo,shop_id,end,days)
    ad_rows=[]
    for r in ads.campaigns:
        ad_rows.append({'level':'campaign','marketplace':r.marketplace,'key':r.key,'name':r.name,
                        'spend':r.spend,'attributed_sales':r.attributed_sales,'orders':r.orders,
                        'clicks':r.clicks,'impressions':r.impressions,'drr_pct':r.drr,'roas':r.roas})
    for r in ads.products:
        ad_rows.append({'level':'sku','marketplace':r.marketplace,'key':r.key,'name':r.name,
                        'spend':r.spend,'attributed_sales':r.attributed_sales,'orders':r.orders,
                        'clicks':r.clicks,'impressions':r.impressions,'drr_pct':r.drr,'roas':r.roas})
    mgmt=build_management_report(repo,shop_id,end,days) if include_finance else None
    management_rows=[{
        'marketplace':r.marketplace,'ordered_revenue':r.ordered_revenue,'estimated_cogs':r.estimated_cogs,
        'cogs_coverage_pct':r.cogs_coverage_pct,'marketplace_expenses':r.marketplace_expenses,
        'ad_spend':r.ad_spend,'compensation':r.compensation,'estimated_result':r.estimated_result,
        'financial_sales':r.financial_sales,'marketplace_net':r.marketplace_net,
        'goods_payable':r.goods_payable,'bank_payment':r.bank_payment,
    } for r in (mgmt.sources if mgmt else ())]
    supply=build_supply_plan(repo,shop_id,end,lookback_days=max(days,14),persist=False)
    supply_rows=[{
        'internal_sku':r.internal_sku,'name':r.name,'abc_class':r.abc_class,'xyz_class':r.xyz_class,
        'avg_daily_units':r.avg_daily_units,'forecast_daily_units':r.forecast_daily_units,'forecast_next_7_units':r.forecast_next_7_units,
        'seasonality_strength':r.seasonality_strength,'bias_correction':r.bias_correction,'promo_factor':r.promo_factor,'promo_days':r.promo_days,'trend_pct':r.trend_pct,'available_units':r.available_units,
        'inbound_units':r.inbound_units,'effective_units':r.effective_units,'days_cover':r.days_cover,'lead_time_days':r.lead_time_days,
        'lead_buffer_days':r.lead_buffer_days,'effective_lead_time_days':r.effective_lead_time_days,
        'safety_stock_days':r.safety_stock_days,'safety_buffer_days':r.safety_buffer_days,'effective_safety_stock_days':r.effective_safety_stock_days,
        'target_stock_days':r.target_stock_days,'calibration_confidence':r.calibration_confidence,
        'reorder_point_units':r.reorder_point_units,'target_units':r.target_units,
        'recommended_order_units':r.recommended_order_units,'pack_size':r.pack_size,'min_order_qty':r.min_order_qty,
        'confidence':r.confidence,'history_days':r.history_days,'inventory_as_of':r.inventory_as_of,
        'inventory_age_days':r.inventory_age_days,'inventory_stale':r.inventory_stale,
    } for r in supply.rows]
    inbound_rows=repo.active_inbound_items(shop_id)
    promotion_rows=repo.promotion_products_for_shop(shop_id,start_s,(end+timedelta(days=60)).isoformat())
    quality=evaluate_forecast_quality(repo,shop_id,end,persist=False)
    quality_rows=[{'internal_sku':r.internal_sku,'name':r.name,'samples':r.samples,'predicted_units':r.predicted_units,
                   'actual_units':r.actual_units,'mae_units':r.mae_units,'wape_pct':r.wape_pct,'bias_pct':r.bias_pct}
                  for r in quality.items]
    calibration=build_supply_calibration(repo,shop_id,end,persist=False)
    calibration_rows=[{'internal_sku':r.internal_sku,'name':r.name,'forecast_samples':r.forecast_samples,
        'forecast_wape_pct':r.forecast_wape_pct,'forecast_bias_pct':r.forecast_bias_pct,
        'inventory_samples':r.inventory_samples,'zero_stock_rate_pct':r.zero_stock_rate_pct,
        'inbound_delay_samples':r.inbound_delay_samples,'avg_inbound_delay_days':r.avg_inbound_delay_days,
        'p75_inbound_delay_days':r.p75_inbound_delay_days,'lead_buffer_days':r.lead_buffer_days,
        'safety_buffer_days':r.safety_buffer_days,'confidence':r.confidence,'reasons':'; '.join(r.reasons)}
        for r in calibration.rows]
    actions=build_action_center(repo,shop_id,end)
    action_rows=[{'action_key':r.action_key,'priority':r.priority,'category':r.category,'status':r.status,'title':r.title,'detail':r.detail,'evidence':'; '.join(r.evidence),'hint':r.hint} for r in actions.items]
    action_history_rows=repo.action_history(shop_id,start_s,end_s,limit=1000)
    summary=[
        {'key':'shop_id','value':shop_id},{'key':'shop_name','value':shop.name if shop else ''},
        {'key':'credential_profile','value':shop.credential_profile if shop else ''},
        {'key':'period_start','value':start_s},{'key':'period_end','value':end_s},
        {'key':'generated_at_utc','value':datetime.now(timezone.utc).isoformat(timespec='seconds')},
        {'key':'schema_version','value':repo.db.schema_version()},
    ]
    tables={'Summary':summary,'Daily':_daily_rows(repo,shop_id,start_s,end_s),'Products':products,
            'Inventory':inventory,'Inbound':inbound_rows,'Promotions':promotion_rows,'Supply':supply_rows,'ForecastQuality':quality_rows,'Calibration':calibration_rows,'Actions':action_rows,'ActionHistory':action_history_rows,
            'Finance':finance_rows,'Advertising':ad_rows,'Management':management_rows,'Reconciliation':rec_rows,'Costs':_cost_rows(repo,shop_id) if include_finance else []}
    if not include_technical:
        tables['Summary']=[row for row in summary if row['key'] not in {'credential_profile','schema_version'}]
    tables['Actions']=[row for row in action_rows if item_visible(row,finance=include_finance,technical=include_technical)]
    tables['ActionHistory']=[row for row in action_history_rows if item_visible(row,finance=include_finance,technical=include_technical)]
    if not include_finance:
        for name in ('Finance','Management','Reconciliation','Costs'):tables.pop(name)
        tables['Daily']=[row for row in tables['Daily'] if row['metric_key'] in OPERATIONAL_METRICS]
        tables['Products']=_fields(products,PRODUCT_FIELDS)
        tables['Inventory']=_fields(inventory,INVENTORY_FIELDS)
        tables['Inbound']=_fields(inbound_rows,INBOUND_FIELDS)
        tables['Promotions']=_fields(promotion_rows,PROMOTION_FIELDS)
    return tables


def _headers(rows: list[dict[str,Any]]) -> list[str]:
    out=[]
    for row in rows:
        for k in row:
            if k not in out: out.append(k)
    return out


def export_xlsx(repo: Repository, shop_id: int, end: date, days: int, path: Path, *,
                include_finance=True, include_technical=True) -> ExportResult:
    tables=collect_export_tables(repo,shop_id,end,days,include_finance=include_finance,
        include_technical=include_technical); path.parent.mkdir(parents=True,exist_ok=True)
    wb=Workbook(); wb.remove(wb.active)
    pref=repo.get_shop_preferences(shop_id)
    timezone=pref.timezone if pref else 'Europe/Moscow'
    for name,rows in tables.items():
        write_sheet(wb,name,rows,timezone=timezone)
    add_descriptions(wb,tables)
    wb.save(path)
    return ExportResult(path,'xlsx',(end-timedelta(days=days-1)).isoformat(),end.isoformat())


def export_csv_zip(repo: Repository, shop_id: int, end: date, days: int, path: Path, *,
                   include_finance=True, include_technical=True) -> ExportResult:
    tables=collect_export_tables(repo,shop_id,end,days,include_finance=include_finance,
        include_technical=include_technical); path.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        for name,rows in tables.items():
            headers=_headers(rows); buf=io.StringIO(newline='')
            writer=csv.DictWriter(buf,fieldnames=headers or ['message'])
            writer.writeheader()
            if rows:
                writer.writerows(_safe_row(row) for row in rows)
            else:
                writer.writerow({'message':'Нет данных'})
            z.writestr(name.lower()+'.csv',buf.getvalue().encode('utf-8-sig'))
        descriptions=description_rows(tables)
        buf=io.StringIO(newline='')
        writer=csv.DictWriter(buf,fieldnames=('sheet','field','label','meaning'))
        writer.writeheader();writer.writerows(_safe_row(row) for row in descriptions)
        z.writestr('Описание_полей.csv',buf.getvalue().encode('utf-8-sig'))
        legend=['Данные магазина из сохранённой базы.', 'CSV сохраняет исходные коды колонок и значения.',
                'В «Описание_полей.csv» находятся русские названия и пояснения.', '']
        legend.extend(name.lower()+'.csv — '+SHEETS[name][0]+': '+SHEETS[name][1] for name in tables)
        z.writestr('Описание_файлов.txt','\n'.join(legend).encode('utf-8-sig'))
    return ExportResult(path,'csv',(end-timedelta(days=days-1)).isoformat(),end.isoformat())
