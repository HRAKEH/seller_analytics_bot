from __future__ import annotations
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from app.storage import Database, Repository, MetricPoint, ProductMetricPoint, InventoryPoint
from app.storage.database import (
    LATEST_SCHEMA_VERSION,_migration_1,_migration_2,_migration_3,_migration_4,_migration_5,
    _migration_6,_migration_7,_migration_8,
)
from app.services.supply import build_supply_plan
from app.services.exporting import collect_export_tables


def make_repo(tmp_path: Path):
    db=Database(tmp_path/'supply.sqlite3'); assert db.initialize()==LATEST_SCHEMA_VERSION
    repo=Repository(db); seller=repo.ensure_seller(10,'Seller'); shop=repo.ensure_shop(seller.id,'Shop')
    conn=repo.ensure_connection(shop.id,'ozon','Ozon')
    return db,repo,shop,conn


def add_product_history(repo,shop,conn,sku,units_per_day,end,days,stock=0):
    p=repo.ensure_product(shop.id,sku,sku); listing=repo.ensure_listing(p.id,conn.id,sku)
    for i in range(days):
        d=end-timedelta(days=days-1-i); ds=d.isoformat()
        run=repo.record_success(conn.id,'analytics/orders',ds,{'sku':sku,'day':ds,'u':units_per_day},[
            MetricPoint(conn.id,ds,'ordered_units',units_per_day,'units',True,ds)])
        repo.save_product_metrics(run,[ProductMetricPoint(listing.id,ds,'ordered_units',units_per_day,'units','ALL',True,ds)])
    if stock is not None:
        run=repo.record_success(conn.id,'product/info/stocks',end.isoformat(),{'sku':sku,'stock':stock},[])
        repo.save_inventory(run,[InventoryPoint(listing.id,stock,0,'FBO')])
    return p,listing


def test_schema_v8_migrates_to_v9(tmp_path):
    path=tmp_path/'v8.sqlite3'; conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
    for version,fn in ((1,_migration_1),(2,_migration_2),(3,_migration_3),(4,_migration_4),(5,_migration_5),(6,_migration_6),(7,_migration_7),(8,_migration_8)):
        fn(conn); conn.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    conn.commit(); conn.close()
    db=Database(path); assert db.initialize()==LATEST_SCHEMA_VERSION
    with db.connect() as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'shop_supply_preferences','product_supply_settings','supply_recommendation_snapshots'} <= tables


def test_constant_demand_is_x_and_recommends_replenishment(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    add_product_history(repo,shop,conn,'SKU-A',2,end,56,stock=10)
    repo.update_shop_supply_preferences(shop.id,default_lead_time_days=7,default_safety_stock_days=3,default_target_stock_days=20)
    plan=build_supply_plan(repo,shop.id,end,persist=True)
    row=next(r for r in plan.rows if r.internal_sku=='SKU-A')
    assert row.xyz_class=='X'
    assert row.abc_class=='A'
    assert abs(row.forecast_daily_units-2)<1e-9
    assert row.reorder_point_units==20
    assert row.recommended_order_units==50  # target=(7+3+20)*2=60, stock=10
    assert row.confidence=='high'
    assert repo.latest_supply_recommendations(shop.id,end.isoformat())[0]['recommended_order_units']==50


def test_abc_by_ordered_units(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    add_product_history(repo,shop,conn,'A',80,end,28,stock=99999)
    add_product_history(repo,shop,conn,'B',15,end,28,stock=99999)
    add_product_history(repo,shop,conn,'C',5,end,28,stock=99999)
    plan=build_supply_plan(repo,shop.id,end,persist=False)
    classes={r.internal_sku:r.abc_class for r in plan.rows}
    assert classes=={'A':'A','B':'B','C':'C'}


def test_pack_and_minimum_order_rounding(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    add_product_history(repo,shop,conn,'PACK',1,end,28,stock=0)
    repo.set_product_supply_settings(shop.id,'PACK',lead_time_days=1,safety_stock_days=0,target_stock_days=3,pack_size=6,min_order_qty=10)
    row=next(r for r in build_supply_plan(repo,shop.id,end,persist=False).rows if r.internal_sku=='PACK')
    # Raw need = 4 units, minimum order=10, rounded to 12 by pack of 6.
    assert row.recommended_order_units==12


def test_complete_dates_require_every_enabled_source(tmp_path):
    db,repo,shop,oz=make_repo(tmp_path); wb=repo.ensure_connection(shop.id,'wildberries','WB')
    ds='2026-09-28'
    repo.record_success(oz.id,'analytics/orders',ds,{'x':1},[MetricPoint(oz.id,ds,'ordered_units',1,'units')])
    assert repo.complete_order_dates(shop.id,ds,ds)==[]
    repo.record_success(wb.id,'statistics/orders',ds,{'x':2},[MetricPoint(wb.id,ds,'ordered_units',1,'units')])
    assert repo.complete_order_dates(shop.id,ds,ds)==[ds]


def test_supply_export_sheet_is_present(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    add_product_history(repo,shop,conn,'SKU',2,end,14,stock=3)
    tables=collect_export_tables(repo,shop.id,end,14)
    assert 'Supply' in tables and tables['Supply']
    assert tables['Supply'][0]['internal_sku']=='SKU'


def test_supply_preference_validation(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path)
    try:
        repo.update_shop_supply_preferences(shop.id,lookback_days=5)
    except ValueError as exc:
        assert 'lookback_days' in str(exc)
    else:
        raise AssertionError('validation did not reject too-short lookback')

def test_missing_inventory_is_not_treated_as_zero_stock(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    add_product_history(repo,shop,conn,'NO-STOCK',3,end,28,stock=None)
    row=next(r for r in build_supply_plan(repo,shop.id,end,persist=False).rows if r.internal_sku=='NO-STOCK')
    assert row.available_units is None
    assert row.days_cover is None
    assert row.recommended_order_units==0

def test_linking_products_preserves_supply_override(tmp_path):
    db=Database(tmp_path/'link.sqlite3'); db.initialize(); repo=Repository(db)
    seller=repo.ensure_seller(1); shop=repo.ensure_shop(seller.id); oz=repo.ensure_connection(shop.id,'ozon','Ozon'); wb=repo.ensure_connection(shop.id,'wildberries','WB')
    p1=repo.ensure_product(shop.id,'oz:1','Item'); repo.ensure_listing(p1.id,oz.id,'1')
    p2=repo.ensure_product(shop.id,'wb:2','Item'); repo.ensure_listing(p2.id,wb.id,'2')
    repo.set_product_supply_settings(shop.id,'wb:2',lead_time_days=21,safety_stock_days=5,target_stock_days=30,pack_size=4,min_order_qty=8)
    merged=repo.link_marketplace_listings(shop.id,'MASTER',[('ozon','1'),('wildberries','2')])
    row=next(x for x in repo.products_for_supply(shop.id) if x['product_id']==merged.id)
    assert row['lead_time_days']==21 and row['pack_size']==4 and row['min_order_qty']==8


def test_stale_inventory_is_flagged(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    p,listing=add_product_history(repo,shop,conn,'STALE',1,end,28,stock=5)
    with db.connect() as c:
        c.execute("UPDATE inventory_snapshots SET captured_at='2026-09-20T00:00:00+00:00' WHERE listing_id=?",(listing.id,))
    row=next(r for r in build_supply_plan(repo,shop.id,end,persist=False).rows if r.internal_sku=='STALE')
    assert row.inventory_stale is True and row.inventory_age_days==8
