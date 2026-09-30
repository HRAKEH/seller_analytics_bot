from __future__ import annotations
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from app.storage import Database, Repository, MetricPoint, ProductMetricPoint, InventoryPoint, LATEST_SCHEMA_VERSION
from app.services.inbound import normalize_wb_supply, normalize_ozon_order
from app.services.supply import build_supply_plan, evaluate_forecast_quality
from app.services.actions import build_action_center
from app.services.exporting import collect_export_tables


def make_repo(tmp_path: Path):
    db=Database(tmp_path/'phase15.sqlite3'); db.initialize(); repo=Repository(db)
    seller=repo.ensure_seller(15,'Seller'); shop=repo.ensure_shop(seller.id,'Shop')
    conn=repo.ensure_connection(shop.id,'ozon','Ozon')
    return db,repo,shop,conn


def add_history(repo,shop,conn,sku,end,days=56,units=2,stock=10):
    p=repo.ensure_product(shop.id,sku,sku); listing=repo.ensure_listing(p.id,conn.id,sku)
    for i in range(days):
        d=end-timedelta(days=days-1-i); ds=d.isoformat()
        run=repo.record_success(conn.id,'analytics/orders',ds,{'d':ds,'u':units},[
            MetricPoint(conn.id,ds,'ordered_units',units,'units',True,ds)])
        repo.save_product_metrics(run,[ProductMetricPoint(listing.id,ds,'ordered_units',units,'units','ALL',True,ds)])
    if stock is not None:
        run=repo.record_success(conn.id,'product/info/stocks',end.isoformat(),{'stock':stock},[])
        repo.save_inventory(run,[InventoryPoint(listing.id,stock,0,'FBO')])
    return p,listing


def test_schema_v10_plus_tables_exist(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path)
    assert db.schema_version()==LATEST_SCHEMA_VERSION
    with db.connect() as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'inbound_shipments','inbound_shipment_items','forecast_quality_snapshots'} <= tables


def test_wb_inbound_normalization_keeps_remaining_quantity():
    row={'supplyID':123,'statusID':3,'supplyDate':'2026-10-03T00:00:00+03:00'}
    goods=[{'nmID':555,'quantity':20,'acceptedQuantity':4}]
    x=normalize_wb_supply(row,goods)
    assert x['external_supply_id']=='supply:123'
    assert x['status']=='SHIPMENT_ALLOWED'
    assert x['items'][0]['remaining_units']==16


def test_ozon_inbound_normalization_uses_bundle_and_eta():
    order={'order_id':10,'state':'ORDER_STATE_READY_TO_SUPPLY','timeslot':{'timeslot':{'from':'2026-10-04T10:00:00Z'}},
           'supplies':[{'supply_id':20,'bundle_id':'B1','state':'READY_TO_SUPPLY','storage_warehouse':{'name':'WH'}}]}
    rows=normalize_ozon_order(order,{'B1':[{'sku':777,'quantity':12}]})
    assert rows[0]['external_supply_id']=='supply:20'
    assert rows[0]['items'][0]['marketplace_sku']=='777'
    assert rows[0]['items'][0]['remaining_units']==12
    assert rows[0]['planned_at']=='2026-10-04T10:00:00Z'


def test_known_inbound_reduces_replenishment_but_unknown_eta_does_not(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    p,listing=add_history(repo,shop,conn,'SKU',end,units=2,stock=10)
    repo.update_shop_supply_preferences(shop.id,default_lead_time_days=7,default_safety_stock_days=3,default_target_stock_days=20)
    run=repo.record_success(conn.id,'supply-order/inbound',end.isoformat(),{'x':1},[])
    repo.upsert_inbound_shipments(conn.id,run,'ozon',[{
        'external_supply_id':'supply:1','status':'READY_TO_SUPPLY','planned_at':'2026-10-03T00:00:00Z',
        'items':[{'marketplace_sku':'SKU','planned_units':5,'remaining_units':5}]}])
    row=next(r for r in build_supply_plan(repo,shop.id,end,persist=False).rows if r.internal_sku=='SKU')
    assert row.inbound_units==5 and row.recommended_order_units==45

    run2=repo.record_success(conn.id,'supply-order/inbound','2026-09-29',{'x':2},[])
    repo.upsert_inbound_shipments(conn.id,run2,'ozon',[{
        'external_supply_id':'supply:1','status':'READY_TO_SUPPLY','planned_at':None,
        'items':[{'marketplace_sku':'SKU','planned_units':5,'remaining_units':5}]}])
    row2=next(r for r in build_supply_plan(repo,shop.id,end,persist=False).rows if r.internal_sku=='SKU')
    assert row2.inbound_units==0 and row2.recommended_order_units==50


def test_forecast_backtest_constant_demand_is_exact(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    add_history(repo,shop,conn,'CONST',end,days=90,units=3,stock=100)
    report=evaluate_forecast_quality(repo,shop.id,end,horizon_days=7,persist=True)
    assert report.items
    item=next(x for x in report.items if x.internal_sku=='CONST')
    assert item.wape_pct is not None and item.wape_pct < 1e-9
    assert report.overall_wape_pct is not None and report.overall_wape_pct < 1e-9
    assert repo.count('forecast_quality_snapshots')>0


def test_forecast_backtest_skips_incomplete_horizon(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    add_history(repo,shop,conn,'GAPPED',end,days=90,units=3,stock=100)
    # Remove one completed order day from the most recent 7-day validation horizon.
    with repo.db.connect() as c:
        run=c.execute("""SELECT id FROM source_runs WHERE connection_id=? AND data_date='2026-09-25'
                         AND endpoint='analytics/orders' LIMIT 1""",(conn.id,)).fetchone()
        assert run is not None
        c.execute('DELETE FROM source_runs WHERE id=?',(int(run['id']),))
    report=evaluate_forecast_quality(repo,shop.id,end,horizon_days=7,persist=False)
    item=next(x for x in report.items if x.internal_sku=='GAPPED')
    assert item.samples==3


def test_action_center_and_export_include_new_operational_layers(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    add_history(repo,shop,conn,'URGENT',end,days=56,units=2,stock=0)
    center=build_action_center(repo,shop.id,end)
    assert any(i.category=='supply' and i.priority==1 for i in center.items)
    tables=collect_export_tables(repo,shop.id,end,30)
    assert {'Inbound','ForecastQuality','Actions'} <= set(tables)


def test_completed_supply_disappearing_from_active_snapshot_is_closed(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); day=date(2026,9,28)
    p,listing=add_history(repo,shop,conn,'SKU-CLOSE',day,days=7,units=1,stock=3)
    run1=repo.record_success(conn.id,'supply-order/inbound',day.isoformat(),{'active':['supply:1']},[])
    repo.upsert_inbound_shipments(conn.id,run1,'ozon',[{
        'external_supply_id':'supply:1','status':'READY_TO_SUPPLY','planned_at':'2026-10-02T00:00:00Z',
        'items':[{'marketplace_sku':'SKU-CLOSE','planned_units':9,'remaining_units':9}]}],
        active_external_ids=['supply:1'])
    assert repo.inbound_units_by_product(shop.id).get(p.id)==9

    run2=repo.record_success(conn.id,'supply-order/inbound','2026-09-29',{'active':[]},[])
    repo.upsert_inbound_shipments(conn.id,run2,'ozon',[],active_external_ids=[])
    assert p.id not in repo.inbound_units_by_product(shop.id)
    with db.connect() as c:
        state=c.execute("SELECT status FROM inbound_shipments WHERE connection_id=? AND external_supply_id='supply:1'",(conn.id,)).fetchone()[0]
    assert state=='NOT_ACTIVE'


def test_partial_snapshot_does_not_close_known_inbound(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); day=date(2026,9,28)
    p,listing=add_history(repo,shop,conn,'SKU-PARTIAL',day,days=7,units=1,stock=3)
    run1=repo.record_success(conn.id,'supply-order/inbound',day.isoformat(),{'active':['supply:1']},[])
    repo.upsert_inbound_shipments(conn.id,run1,'ozon',[{
        'external_supply_id':'supply:1','status':'READY_TO_SUPPLY','planned_at':'2026-10-02T00:00:00Z',
        'items':[{'marketplace_sku':'SKU-PARTIAL','planned_units':7,'remaining_units':7}]}],
        active_external_ids=['supply:1'])
    run2=repo.record_success(conn.id,'supply-order/inbound','2026-09-29',{'partial':True},[],status='partial')
    repo.upsert_inbound_shipments(conn.id,run2,'ozon',[],active_external_ids=None)
    assert repo.inbound_units_by_product(shop.id).get(p.id)==7


def test_schema_v9_migrates_to_v10_without_losing_supply_preferences(tmp_path):
    import app.storage.database as dbmod
    path=tmp_path/'v9.sqlite3'
    conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
    for version in range(1,10):
        dbmod.MIGRATIONS[version](conn)
        conn.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    # Seed a pre-v10 shop preference to prove the row survives ALTER TABLE.
    conn.execute("INSERT INTO sellers(telegram_user_id,name,timezone,active,created_at) VALUES(1,'S','Europe/Moscow',1,datetime('now'))")
    seller_id=conn.execute('SELECT id FROM sellers').fetchone()[0]
    conn.execute("INSERT INTO shops(seller_id,name,currency,active,created_at) VALUES(?,'Shop','RUB',1,datetime('now'))",(seller_id,))
    shop_id=conn.execute('SELECT id FROM shops').fetchone()[0]
    conn.execute("INSERT INTO shop_supply_preferences(shop_id,lookback_days,xyz_weeks,default_lead_time_days,default_safety_stock_days,default_target_stock_days,updated_at) VALUES(?,56,8,11,4,20,datetime('now'))",(shop_id,))
    conn.commit(); conn.close()
    db=Database(path)
    assert db.initialize()==LATEST_SCHEMA_VERSION
    with db.connect() as c:
        row=c.execute('SELECT default_lead_time_days,seasonality_enabled,forecast_horizon_days FROM shop_supply_preferences WHERE shop_id=?',(shop_id,)).fetchone()
    assert tuple(row)==(11,1,7)

import pytest
from app.integrations.base import FetchResult
from app.integrations.wildberries import WildberriesClient
from app.integrations.ozon import OzonClient


@pytest.mark.asyncio
async def test_wb_fbw_supply_pagination_advances_offset(monkeypatch):
    client=WildberriesClient('token',min_interval=0)
    calls=[]
    async def fake_page(*,status_ids=None,limit=1000,offset=0):
        calls.append(offset)
        data=[{'supplyID':offset+i} for i in range(2)] if offset==0 else [{'supplyID':99}]
        return FetchResult.success('wildberries',data,200,1)
    monkeypatch.setattr(client,'fbw_supplies_page',fake_page)
    result=await client.fbw_supplies_all(limit=2)
    assert result.ok and len(result.data)==3 and calls==[0,2]
    await client.close()


@pytest.mark.asyncio
async def test_ozon_supply_pagination_and_bundle_cursor(monkeypatch):
    client=OzonClient('cid','key',min_interval=0)
    list_calls=[]
    async def fake_list(*,states=None,last_id='',limit=100):
        list_calls.append(last_id)
        if not last_id:
            return FetchResult.success('ozon',{'order_ids':['1','2'],'last_id':'cursor-1'},200,1)
        return FetchResult.success('ozon',{'order_ids':['3'],'last_id':''},200,1)
    monkeypatch.setattr(client,'supply_orders_page',fake_list)
    listed=await client.supply_orders_all()
    assert listed.ok and listed.data['order_ids']==['1','2','3'] and list_calls==['','cursor-1']

    bundle_calls=[]
    async def fake_bundle(bundle_id,*,last_id='',limit=100):
        bundle_calls.append(last_id)
        if not last_id:
            return FetchResult.success('ozon',{'items':[{'sku':1}],'has_next':True,'last_id':'b1'},200,1)
        return FetchResult.success('ozon',{'items':[{'sku':2}],'has_next':False},200,1)
    monkeypatch.setattr(client,'supply_bundle_page',fake_bundle)
    bundle=await client.supply_bundle_all('B')
    assert bundle.ok and [x['sku'] for x in bundle.data['items']]==[1,2] and bundle_calls==['','b1']
    await client.close()
