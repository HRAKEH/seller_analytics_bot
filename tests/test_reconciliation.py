from datetime import date
import json
from pathlib import Path

from app.services.reconciliation import (
    normalize_wb_order_events, normalize_wb_sales_events, normalize_wb_finance_events,
    normalize_ozon_posting_events, normalize_ozon_finance_events,
)
from app.reports.reconciliation import build_reconciliation_report, format_reconciliation
from app.storage import Database, Repository, CommerceEventPoint, MetricPoint
from app.storage.models import ProductMetricPoint


def test_wb_srid_chain_normalization():
    orders=[{'date':'2026-09-01T10:00:00','nmId':11,'supplierArticle':'A','srid':'S-RID-1',
             'priceWithDisc':900,'isCancel':True,'cancelDate':'2026-09-01T12:00:00'}]
    ev=normalize_wb_order_events(orders,data_date='2026-09-01')
    assert [x.event_kind for x in ev]==['order','cancel']
    assert all(x.external_order_id=='S-RID-1' for x in ev)

    sales=normalize_wb_sales_events([
        {'date':'2026-09-02T10:00:00','nmId':11,'srid':'S-RID-2','saleID':'S123','priceWithDisc':1000,'forPay':700},
        {'date':'2026-09-04T10:00:00','nmId':11,'srid':'S-RID-2','saleID':'R123','priceWithDisc':1000,'forPay':700},
    ])
    assert [x.event_kind for x in sales]==['sale','return']
    assert sales[0].external_order_id==sales[1].external_order_id=='S-RID-2'

    finance=normalize_wb_finance_events([{'saleDt':'2026-09-02','nmId':11,'srid':'S-RID-2','rrdId':99,
                                         'retailAmount':'1000','ppvzForPay':'690','docTypeName':'Продажа'}])
    assert finance[0].external_order_id=='S-RID-2'
    assert finance[0].external_event_id=='99'
    assert finance[0].net_amount==690


def test_ozon_posting_finance_link():
    postings=normalize_ozon_posting_events({'postings':[{
        'posting_number':'123-ABC','created_at':'2026-09-01T10:00:00Z','status':'delivered',
        'products':[{'sku':77,'offer_id':'O77','name':'Item','quantity':2,'price':'500'}]
    }]},fulfillment_scheme='FBS')
    assert postings[0].event_kind=='posting'
    assert postings[0].external_order_id=='123-ABC'
    assert postings[0].quantity==2

    finance=normalize_ozon_finance_events({'accruals':[{
        'date':'2026-09-03','posting':{'posting_number':'123-ABC','products':[{
            'sku':77,'offer_id':'O77','commission':{'seller_price':{'amount':'1000'},'sale_commission':{'amount':'-150'}},
            'delivery':{'total_accrued':{'amount':'-80'}}}]}
    }]},data_date='2026-09-03')
    assert finance[0].external_order_id=='123-ABC'
    assert finance[0].gross_amount==1000
    assert finance[0].metadata['sale_commission']==-150


def _setup(tmp_path: Path):
    db=Database(tmp_path/'r.sqlite3'); db.initialize()
    repo=Repository(db); seller=repo.ensure_seller(1); shop=repo.ensure_shop(seller.id)
    wb=repo.ensure_connection(shop.id,'wildberries','WB')
    p=repo.ensure_product(shop.id,'P1','Product 1'); listing=repo.ensure_listing(p.id,wb.id,'11')
    return repo,shop,wb,listing


def test_commerce_events_are_idempotent_and_match(tmp_path):
    repo,shop,conn,listing=_setup(tmp_path)
    run1=repo.record_success(conn.id,'statistics/orders','2026-09-01',{'a':1},[])
    order=CommerceEventPoint(listing.id,'2026-09-01','order','wb_orders','fp-order',
                             '2026-09-01T10:00:00','RID-1',None,1,1000,None,'ALL',True,'{}')
    assert repo.save_commerce_events(run1,[order])==1
    # same fingerprint from another successful run must not duplicate the lifecycle event
    run2=repo.record_success(conn.id,'statistics/orders','2026-09-01',{'a':2},[])
    assert repo.save_commerce_events(run2,[order])==0

    run3=repo.record_success(conn.id,'statistics/sales','2026-09-02',{'b':1},[])
    sale=CommerceEventPoint(listing.id,'2026-09-02','sale','wb_sales','fp-sale',
                            '2026-09-02T10:00:00','RID-1','S1',1,1000,700,'ALL',True,'{}')
    repo.save_commerce_events(run3,[sale])
    stats=repo.reconciliation_match_stats(shop.id,'2026-09-01','2026-09-03')['wildberries']
    assert stats['total']==1 and stats['matched']==1 and stats['coverage_pct']==100


def test_reconciliation_report_uses_order_metrics_and_events(tmp_path):
    repo,shop,conn,listing=_setup(tmp_path)
    run=repo.record_success(conn.id,'orders','2026-09-01',{'x':1},[
        MetricPoint(conn.id,'2026-09-01','ordered_units',3,'units'),
        MetricPoint(conn.id,'2026-09-01','ordered_revenue',3000,'RUB')])
    repo.save_product_metrics(run,[
        ProductMetricPoint(listing.id,'2026-09-01','ordered_units',3,'units'),
        ProductMetricPoint(listing.id,'2026-09-01','ordered_revenue',3000,'RUB')])
    repo.save_commerce_events(run,[
        CommerceEventPoint(listing.id,'2026-09-01','order','wb_orders','o1',external_order_id='RID1',quantity=1),
        CommerceEventPoint(listing.id,'2026-09-01','order','wb_orders','o2',external_order_id='RID2',quantity=1),
        CommerceEventPoint(listing.id,'2026-09-01','order','wb_orders','o3',external_order_id='RID3',quantity=1),
        CommerceEventPoint(listing.id,'2026-09-01','cancel','wb_orders','c1',external_order_id='RID3',quantity=1),
    ])
    run2=repo.record_success(conn.id,'sales','2026-09-02',{'s':1},[])
    repo.save_commerce_events(run2,[
        CommerceEventPoint(listing.id,'2026-09-02','sale','wb_sales','s1',external_order_id='RID1',quantity=1),
        CommerceEventPoint(listing.id,'2026-09-02','sale','wb_sales','s2',external_order_id='RID2',quantity=1),
    ])
    report=build_reconciliation_report(repo,shop.id,date(2026,9,3),days=3)
    row=next(r for r in report.rows if r.marketplace=='wildberries')
    assert row.ordered_units==3 and row.cancelled_units==1 and row.sale_units==2
    text=format_reconciliation(report)
    assert 'gap +0' in text and 'srid' in text


def test_delayed_finance_is_included_when_linked_to_period_order(tmp_path):
    repo,shop,conn,listing=_setup(tmp_path)
    run=repo.record_success(conn.id,'orders','2026-09-01',{'o':1},[])
    repo.save_commerce_events(run,[CommerceEventPoint(
        listing.id,'2026-09-01','order','wb_orders','ord-delay',external_order_id='RID-LATE',quantity=1)])
    later=repo.record_success(conn.id,'finance','2026-09-05',{'f':1},[])
    repo.save_commerce_events(later,[CommerceEventPoint(
        listing.id,'2026-09-05','finance','wb_finance','fin-delay',external_order_id='RID-LATE',
        external_event_id='500',gross_amount=1200,net_amount=800)])
    rows=repo.reconciliation_summary(shop.id,'2026-09-01','2026-09-03')
    row=next(r for r in rows if r['marketplace_sku']=='11')
    assert row['finance_gross']==1200
    assert row['finance_net']==800


def test_schema_v4_migrates_to_latest_without_losing_costs(tmp_path):
    import sqlite3
    from app.storage.database import _migration_1,_migration_2,_migration_3,_migration_4,LATEST_SCHEMA_VERSION
    path=tmp_path/'v4.sqlite3'
    conn=sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for version,fn in ((1,_migration_1),(2,_migration_2),(3,_migration_3),(4,_migration_4)):
        fn(conn); conn.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    conn.commit(); conn.close()
    db=Database(path)
    assert db.initialize()==LATEST_SCHEMA_VERSION
    with db.connect() as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'commerce_events' in tables
    assert db.integrity_check()
