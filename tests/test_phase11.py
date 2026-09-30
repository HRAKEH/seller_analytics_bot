from datetime import date
import sqlite3
from pathlib import Path

from app.storage import Database, Repository, MetricPoint, LATEST_SCHEMA_VERSION
from app.storage.models import ProductMetricPoint, AdCampaignPoint, AdProductPoint
from app.services.advertising import normalize_wb_ad_detail, normalize_ozon_ad_product_detail
from app.reports.ads import build_advertising_report
from app.reports.management import build_management_report
from app.reports.sku_finance import build_sku_economics
from app.services.exporting import collect_export_tables


def make_repo(tmp_path: Path):
    db=Database(tmp_path/'phase11.sqlite3')
    assert db.initialize()==LATEST_SCHEMA_VERSION
    repo=Repository(db)
    seller=repo.ensure_seller(1,'Seller')
    shop=repo.ensure_shop(seller.id,'Shop')
    return db,repo,shop


def test_schema_v6_migrates_to_v7(tmp_path):
    from app.storage.database import _migration_1,_migration_2,_migration_3,_migration_4,_migration_5,_migration_6
    path=tmp_path/'v6.sqlite3'
    conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
    for version,fn in ((1,_migration_1),(2,_migration_2),(3,_migration_3),(4,_migration_4),(5,_migration_5),(6,_migration_6)):
        fn(conn); conn.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    conn.commit(); conn.close()
    db=Database(path)
    assert db.initialize()==LATEST_SCHEMA_VERSION
    with db.connect() as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'bot_users','user_shop_access','ad_campaign_daily','ad_product_daily'} <= tables
    assert db.integrity_check()


def test_role_permission_matrix_and_secure_shop_selection(tmp_path):
    _,repo,shop=make_repo(tmp_path)
    other=repo.ensure_shop(shop.seller_id,'Other')
    repo.grant_shop_access(101,shop.id,'viewer','Viewer')
    repo.grant_shop_access(102,shop.id,'analyst','Analyst')
    repo.grant_shop_access(103,shop.id,'owner','Owner')
    assert repo.can_user(101,shop.id,'view') and not repo.can_user(101,shop.id,'operate')
    assert repo.can_user(102,shop.id,'operate') and not repo.can_user(102,shop.id,'manage')
    assert repo.can_user(103,shop.id,'manage')
    try:
        repo.select_authorized_shop_for_user(101,other.id)
    except PermissionError:
        pass
    else:
        raise AssertionError('viewer switched to a shop without access')
    repo.grant_shop_access(101,other.id,'viewer')
    repo.select_authorized_shop_for_user(101,other.id)
    assert repo.selected_authorized_shop_for_user(101)==other.id


def test_wb_ad_normalizer_aggregates_same_nm_across_apps():
    payload=[{'advertId':77,'name':'Campaign','days':[{'date':'2026-09-28','sum':100,'sum_price':500,'orders':5,
        'apps':[{'appType':1,'nms':[{'nmId':123,'sum':30,'sum_price':150,'orders':2,'clicks':3,'views':20}]},
                {'appType':2,'nms':[{'nmId':123,'sum':20,'sum_price':100,'orders':1,'clicks':2,'views':10}]}]}]}]
    campaigns,products=normalize_wb_ad_detail(payload,9)
    assert len(campaigns)==1 and campaigns[0].spend==100
    assert len(products)==1
    p=products[0]
    assert p.marketplace_sku=='123' and p.spend==50 and p.attributed_sales==250 and p.orders==3
    assert p.clicks==5 and p.impressions==30


def test_ozon_sku_ad_parser_is_tolerant():
    payload={'rows':[{'date':'2026-09-28','sku':'9001','campaignId':'12','expense':40,
                      'ordersMoney':200,'orders':2,'clicks':10,'views':100}]}
    rows=normalize_ozon_ad_product_detail(payload,5)
    assert len(rows)==1
    assert rows[0].marketplace_sku=='9001' and rows[0].spend==40 and rows[0].attributed_sales==200


def test_ad_details_persist_and_build_report(tmp_path):
    _,repo,shop=make_repo(tmp_path)
    conn=repo.ensure_connection(shop.id,'wildberries','WB')
    product=repo.ensure_product(shop.id,'P1','Product')
    listing=repo.ensure_listing(product.id,conn.id,'123')
    run=repo.record_success(conn.id,'promotion/fullstats','2026-09-28',{'rev':1},[])
    saved=repo.save_ad_details(run,
        campaigns=[AdCampaignPoint(conn.id,'2026-09-28','7','Campaign',100,500,5,20,1000)],
        products=[AdProductPoint(conn.id,'2026-09-28','123','7','Campaign',listing.id,60,300,3,12,600)])
    assert saved==(1,1)
    report=build_advertising_report(repo,shop.id,date(2026,9,28),1)
    assert report.campaigns[0].spend==100 and report.campaigns[0].drr==20
    assert report.products[0].key=='123' and report.products[0].spend==60


def test_management_result_requires_complete_cost_and_subtracts_known_spend(tmp_path):
    _,repo,shop=make_repo(tmp_path)
    conn=repo.ensure_connection(shop.id,'ozon','Ozon')
    product=repo.ensure_product(shop.id,'P1','Product',cost_price=100)
    listing=repo.ensure_listing(product.id,conn.id,'9001')
    run=repo.record_success(conn.id,'combined','2026-09-28',{'x':1},[
        MetricPoint(conn.id,'2026-09-28','ordered_revenue',1000,'RUB',True),
        MetricPoint(conn.id,'2026-09-28','commission',100,'RUB'),
        MetricPoint(conn.id,'2026-09-28','logistics',50,'RUB'),
        MetricPoint(conn.id,'2026-09-28','ad_spend',70,'RUB'),
        MetricPoint(conn.id,'2026-09-28','compensation',20,'RUB'),
    ])
    repo.save_product_metrics(run,[ProductMetricPoint(listing.id,'2026-09-28','ordered_units',5,'units')])
    report=build_management_report(repo,shop.id,date(2026,9,28),1)
    row=report.sources[0]
    assert row.estimated_cogs==500 and row.cogs_coverage_pct==100
    assert row.marketplace_expenses==150 and row.ad_spend==70
    assert row.estimated_result==300  # 1000 - 500 - 150 - 70 + 20


def test_sku_economics_subtracts_attributed_ad_spend(tmp_path):
    _,repo,shop=make_repo(tmp_path)
    conn=repo.ensure_connection(shop.id,'ozon','Ozon')
    product=repo.ensure_product(shop.id,'P1','Product',cost_price=100)
    listing=repo.ensure_listing(product.id,conn.id,'9001')
    run=repo.record_success(conn.id,'orders','2026-09-28',{'orders':1},[])
    repo.save_product_metrics(run,[
        ProductMetricPoint(listing.id,'2026-09-28','ordered_units',2,'units'),
        ProductMetricPoint(listing.id,'2026-09-28','ordered_revenue',500,'RUB'),
    ])
    adrun=repo.record_success(conn.id,'performance/products-sku','2026-09-28',{'ads':1},[])
    repo.save_ad_details(adrun,products=[AdProductPoint(conn.id,'2026-09-28','9001','c1','C1',listing.id,50,250,1,3,20)])
    report=build_sku_economics(repo,shop.id,date(2026,9,28),1)
    row=report.rows[0]
    assert row.contribution_before_marketplace==300
    assert row.contribution_after_known_expenses==250
    assert row.advertising_metrics['ad_spend']==50


def test_export_contains_phase11_sheets(tmp_path):
    _,repo,shop=make_repo(tmp_path)
    tables=collect_export_tables(repo,shop.id,date(2026,9,28),7)
    assert {'Advertising','Management'} <= set(tables)
