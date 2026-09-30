from __future__ import annotations
from datetime import date, timedelta
from pathlib import Path

from app.storage import Database, Repository, MetricPoint, ProductMetricPoint, InventoryPoint, LATEST_SCHEMA_VERSION
from app.storage.models import AdCampaignPoint
from app.services.actions import build_action_center
from app.services.exporting import collect_export_tables


def make_repo(tmp_path: Path):
    db=Database(tmp_path/'phase18.sqlite3'); assert db.initialize()==LATEST_SCHEMA_VERSION
    repo=Repository(db); seller=repo.ensure_seller(180,'Seller'); shop=repo.ensure_shop(seller.id,'Shop')
    oz=repo.ensure_connection(shop.id,'ozon','Ozon')
    repo.ensure_shop_preferences(shop.id)
    return db,repo,shop,oz


def seed_supply(repo,shop,conn,end,sku='SKU',units=3,stock=0):
    product=repo.ensure_product(shop.id,sku,sku); listing=repo.ensure_listing(product.id,conn.id,sku,offer_id=sku)
    for i in range(28):
        d=end-timedelta(days=27-i); ds=d.isoformat()
        run=repo.record_success(conn.id,'analytics/orders',ds,{'u':units,'d':ds},[MetricPoint(conn.id,ds,'ordered_units',units,'units',True,ds)])
        repo.save_product_metrics(run,[ProductMetricPoint(listing.id,ds,'ordered_units',units,'units','ALL',True,ds)])
    run=repo.record_success(conn.id,'product/info/stocks',end.isoformat(),{'stock':stock},[])
    repo.save_inventory(run,[InventoryPoint(listing.id,stock,0,'FBO')])
    return product,listing


def test_schema_v13_has_action_workflow(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path)
    assert db.schema_version()==LATEST_SCHEMA_VERSION
    with db.connect() as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'action_center_state','action_center_history'} <= tables


def test_action_state_ack_snooze_and_auto_resolve(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path)
    row={'action_key':'supply:1','priority':1,'category':'supply','title':'Order','detail':'Need stock','evidence':['stock 0']}
    repo.sync_action_center(shop.id,'2026-09-29',[row])
    assert repo.set_action_status(shop.id,'supply:1','acknowledged',telegram_user_id=180)
    assert repo.action_states(shop.id)[0]['status']=='acknowledged'
    assert repo.set_action_status(shop.id,'supply:1','snoozed',telegram_user_id=180,snooze_hours=24)
    assert repo.action_states(shop.id)[0]['status']=='snoozed'
    repo.sync_action_center(shop.id,'2026-09-30',[])
    assert repo.action_states(shop.id)==[]
    assert repo.action_states(shop.id,include_resolved=True)[0]['status']=='resolved'


def test_supply_and_promotion_are_one_action(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,29)
    product,listing=seed_supply(repo,shop,conn,end,stock=0)
    run=repo.record_success(conn.id,'actions/promotions',end.isoformat(),{'promo':1},[])
    repo.upsert_promotions(conn.id,run,'ozon',[{
        'external_promotion_id':'P1','name':'Big Promo','promo_type':'regular',
        'start_at':(end+timedelta(days=2)).isoformat(),'end_at':(end+timedelta(days=8)).isoformat(),
        'products_complete':True,'products':[{'marketplace_sku':'SKU','offer_id':'SKU','in_action':True}],
    }],complete_external_ids=['P1'])
    center=build_action_center(repo,shop.id,end,persist=True)
    supply=[x for x in center.items if x.action_key==f'supply:{product.id}']
    assert len(supply)==1
    assert supply[0].priority==1
    assert any('Big Promo' in x for x in supply[0].evidence)
    assert not any(x.action_key.startswith('promo:') for x in center.items)


def test_high_drr_uses_configured_threshold(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,29)
    repo.update_shop_preferences(shop.id,alert_drr_pct=25)
    run=repo.record_success(conn.id,'performance/ads',end.isoformat(),{'ok':1},[])
    repo.save_ad_details(run,campaigns=[AdCampaignPoint(conn.id,end.isoformat(),'C1','Campaign',spend=300,attributed_sales=600,orders=2)])
    center=build_action_center(repo,shop.id,end,persist=False)
    row=next(x for x in center.items if x.action_key=='ads:drr:ozon:C1')
    assert row.priority==2 and '50.0%' in row.detail


def test_snoozed_actions_hidden_but_history_exported(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,29)
    product,_=seed_supply(repo,shop,conn,end,stock=0)
    center=build_action_center(repo,shop.id,end,persist=True)
    key=f'supply:{product.id}'; assert any(x.action_key==key for x in center.items)
    repo.set_action_status(shop.id,key,'snoozed',telegram_user_id=180,snooze_hours=24)
    hidden=build_action_center(repo,shop.id,end,persist=False)
    assert not any(x.action_key==key for x in hidden.items)
    assert hidden.snoozed_count>=1
    tables=collect_export_tables(repo,shop.id,end,30)
    assert 'Actions' in tables and 'ActionHistory' in tables
    # Export is current visible work; durable history remains queryable separately.
    hist=repo.action_history(shop.id,(end-timedelta(days=29)).isoformat(),end.isoformat())
    assert any(r['action_key']==key for r in hist)

def test_v12_migrates_to_v13_preserving_existing_data(tmp_path):
    import sqlite3
    import app.storage.database as dbmod
    path=tmp_path/'v12.sqlite3'; c=sqlite3.connect(path)
    c.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
    for version in range(1,13):
        dbmod.MIGRATIONS[version](c); c.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    c.execute("INSERT INTO sellers(telegram_user_id,name,timezone,active,created_at) VALUES(1,'S','Europe/Moscow',1,datetime('now'))")
    sid=c.execute('SELECT id FROM sellers').fetchone()[0]
    c.execute("INSERT INTO shops(seller_id,name,currency,active,created_at) VALUES(?,'Shop','RUB',1,datetime('now'))",(sid,))
    c.commit(); c.close()
    db=Database(path); assert db.initialize()==LATEST_SCHEMA_VERSION
    assert db.quick_check()
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM shops').fetchone()[0]==1
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'action_center_state','action_center_history'} <= tables
