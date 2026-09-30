from __future__ import annotations
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from app.storage import Database, Repository, MetricPoint, ProductMetricPoint, InventoryPoint, LATEST_SCHEMA_VERSION
from app.services.supply import build_supply_calibration, build_supply_plan
from app.services.exporting import collect_export_tables


def make_repo(tmp_path: Path):
    db=Database(tmp_path/'phase17.sqlite3'); assert db.initialize()==LATEST_SCHEMA_VERSION
    repo=Repository(db); seller=repo.ensure_seller(17,'Seller'); shop=repo.ensure_shop(seller.id,'Shop')
    oz=repo.ensure_connection(shop.id,'ozon','Ozon')
    return db,repo,shop,oz


def seed_orders(repo,shop,conn,sku,end,days=56,units=2,stock=10):
    p=repo.ensure_product(shop.id,sku,sku); listing=repo.ensure_listing(p.id,conn.id,sku,offer_id=sku)
    for i in range(days):
        d=end-timedelta(days=days-1-i); ds=d.isoformat()
        run=repo.record_success(conn.id,'analytics/orders',ds,{'d':ds,'u':units},[
            MetricPoint(conn.id,ds,'ordered_units',units,'units',True,ds)])
        repo.save_product_metrics(run,[ProductMetricPoint(listing.id,ds,'ordered_units',units,'units','ALL',True,ds)])
    if stock is not None:
        run=repo.record_success(conn.id,'product/info/stocks',end.isoformat(),{'stock':stock},[])
        repo.save_inventory(run,[InventoryPoint(listing.id,stock,0,'FBO')])
    return p,listing


def add_inventory_history(repo,conn,listing,end,values):
    for i,value in enumerate(values):
        d=end-timedelta(days=len(values)-1-i); ds=d.isoformat()
        run=repo.record_success(conn.id,'product/info/stocks',ds,{'stock':value,'i':i},[])
        repo.save_inventory(run,[InventoryPoint(listing.id,value,0,'FBO')])
        # save_inventory timestamps at ingestion time; backtest fixtures need historical capture days.
        with repo.db.connect() as c:
            c.execute('UPDATE inventory_snapshots SET captured_at=? WHERE source_run_id=?',(ds+'T12:00:00+00:00',run))


def test_schema_v12_has_calibration_state(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path)
    assert db.schema_version()==LATEST_SCHEMA_VERSION
    with db.connect() as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        prefs={r[1] for r in c.execute('PRAGMA table_info(shop_supply_preferences)')}
        snap={r[1] for r in c.execute('PRAGMA table_info(supply_recommendation_snapshots)')}
    assert 'supply_calibration_snapshots' in tables
    assert {'auto_calibration_enabled','max_lead_buffer_days','max_safety_buffer_days'} <= prefs
    assert {'lead_buffer_days','safety_buffer_days','effective_lead_time_days','effective_safety_stock_days','calibration_confidence'} <= snap


def test_sparse_evidence_does_not_invent_buffers(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    seed_orders(repo,shop,conn,'SKU',end,days=20,stock=10)
    report=build_supply_calibration(repo,shop.id,end,persist=False)
    row=next(r for r in report.rows if r.internal_sku=='SKU')
    assert row.lead_buffer_days==0 and row.safety_buffer_days==0
    assert row.confidence in {'low','medium'}


def test_forecast_error_and_zero_stock_add_bounded_safety_buffer(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    product,listing=seed_orders(repo,shop,conn,'RISK',end,days=56,units=2,stock=None)
    add_inventory_history(repo,conn,listing,end,[5,5,0,5,5,0,5,5,5,5])  # 20% zero-stock observations
    repo.save_forecast_quality(shop.id,[
        {'product_id':product.id,'as_of_date':(end-timedelta(days=40-i)).isoformat(),'horizon_days':7,'predicted_units':10,'actual_units':20}
        for i in range(6)
    ],method_version='old-model')
    cal=build_supply_calibration(repo,shop.id,end,persist=True)
    row=next(r for r in cal.rows if r.internal_sku=='RISK')
    assert row.forecast_wape_pct==50.0
    assert row.zero_stock_rate_pct==20.0
    assert row.safety_buffer_days==4  # +2 WAPE, +2 observed zero stock
    plan=build_supply_plan(repo,shop.id,end,persist=True)
    srow=next(r for r in plan.rows if r.internal_sku=='RISK')
    assert srow.safety_stock_days==7
    assert srow.safety_buffer_days==4
    assert srow.effective_safety_stock_days==11
    saved=repo.latest_supply_recommendations(shop.id,end.isoformat())[0]
    assert saved['safety_buffer_days']==4 and saved['effective_safety_stock_days']==11


def test_auto_calibration_can_be_disabled_without_losing_suggestion(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    product,listing=seed_orders(repo,shop,conn,'OFF',end,days=56,units=2,stock=None)
    add_inventory_history(repo,conn,listing,end,[0,0,5,5,5,5,5,5])
    repo.save_forecast_quality(shop.id,[
        {'product_id':product.id,'as_of_date':(end-timedelta(days=20-i)).isoformat(),'horizon_days':7,'predicted_units':5,'actual_units':10}
        for i in range(5)
    ],method_version='old')
    cal=build_supply_calibration(repo,shop.id,end,persist=True)
    suggestion=next(r for r in cal.rows if r.internal_sku=='OFF')
    assert suggestion.safety_buffer_days>0
    repo.update_shop_supply_preferences(shop.id,auto_calibration_enabled=0)
    plan=build_supply_plan(repo,shop.id,end,persist=False)
    row=next(r for r in plan.rows if r.internal_sku=='OFF')
    assert row.safety_buffer_days==0
    assert row.effective_safety_stock_days==row.safety_stock_days


def test_wb_fact_date_lateness_can_add_lead_buffer_but_ozon_planned_date_cannot(tmp_path):
    db,repo,shop,oz=make_repo(tmp_path); end=date(2026,9,28)
    product,_=seed_orders(repo,shop,oz,'MASTER',end,days=56,units=2,stock=10)
    wb=repo.ensure_connection(shop.id,'wildberries','WB'); wb_listing=repo.ensure_listing(product.id,wb.id,'777')
    for i,delay in enumerate((2,3,4),1):
        planned=end-timedelta(days=20-i*3); actual=planned+timedelta(days=delay)
        run=repo.record_success(wb.id,'supplies',actual.isoformat(),{'id':i},[])
        repo.upsert_inbound_shipments(wb.id,run,'wildberries',[{
            'external_supply_id':f'wb:{i}','status':'ACCEPTED','planned_at':planned.isoformat(),'arrival_at':actual.isoformat(),
            'warehouse_name':'WH','items':[{'marketplace_sku':'777','planned_units':10,'accepted_units':10,'remaining_units':0}],
        }])
    # Ozon rows deliberately have a large apparent gap; current normalized arrival_at is not an actual fact date.
    for i in range(3):
        planned=end-timedelta(days=30-i); actual=planned+timedelta(days=20)
        run=repo.record_success(oz.id,'supply-orders',actual.isoformat(),{'id':i},[])
        repo.upsert_inbound_shipments(oz.id,run,'ozon',[{
            'external_supply_id':f'oz:{i}','status':'COMPLETED','planned_at':planned.isoformat(),'arrival_at':actual.isoformat(),
            'warehouse_name':'WH','items':[{'marketplace_sku':'MASTER','planned_units':10,'accepted_units':10,'remaining_units':0}],
        }])
    cal=build_supply_calibration(repo,shop.id,end,persist=False)
    row=next(r for r in cal.rows if r.internal_sku=='MASTER')
    assert row.inbound_delay_samples==3
    assert row.p75_inbound_delay_days==3.5
    assert row.lead_buffer_days==4


def test_calibration_export_and_supply_fields(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    seed_orders(repo,shop,conn,'SKU',end,days=28,units=2,stock=5)
    tables=collect_export_tables(repo,shop.id,end,30)
    assert 'Calibration' in tables and tables['Calibration']
    assert {'lead_buffer_days','safety_buffer_days','effective_lead_time_days','effective_safety_stock_days','calibration_confidence'} <= set(tables['Supply'][0])


def test_v11_migrates_to_v12_preserving_snapshot(tmp_path):
    import app.storage.database as dbmod
    path=tmp_path/'v11.sqlite3'; c=sqlite3.connect(path)
    c.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
    for version in range(1,12):
        dbmod.MIGRATIONS[version](c); c.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    c.execute("INSERT INTO sellers(telegram_user_id,name,timezone,active,created_at) VALUES(1,'S','Europe/Moscow',1,datetime('now'))")
    sid=c.execute('SELECT id FROM sellers').fetchone()[0]
    c.execute("INSERT INTO shops(seller_id,name,currency,active,created_at) VALUES(?,'Shop','RUB',1,datetime('now'))",(sid,))
    shop_id=c.execute('SELECT id FROM shops').fetchone()[0]
    c.execute("INSERT INTO products(shop_id,internal_sku,name,active,created_at) VALUES(?,'SKU','SKU',1,datetime('now'))",(shop_id,))
    pid=c.execute('SELECT id FROM products').fetchone()[0]
    c.execute("""INSERT INTO supply_recommendation_snapshots(shop_id,product_id,as_of_date,generated_at,abc_class,xyz_class,
      avg_daily_units,forecast_daily_units,reorder_point_units,target_units,recommended_order_units,confidence,history_days,method_version)
      VALUES(?,?,'2026-09-28',datetime('now'),'A','X',2,2,10,20,5,'high',56,'old')""",(shop_id,pid))
    c.commit(); c.close()
    db=Database(path); assert db.initialize()==LATEST_SCHEMA_VERSION
    with db.connect() as c:
        row=c.execute('SELECT recommended_order_units,lead_buffer_days,safety_buffer_days,calibration_confidence FROM supply_recommendation_snapshots').fetchone()
    assert tuple(row)==(5.0,0,0,'none')
