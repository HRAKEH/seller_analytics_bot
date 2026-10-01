from __future__ import annotations
from datetime import date, timedelta
from pathlib import Path
import sqlite3
import pytest

from app.storage import Database, Repository, MetricPoint, ProductMetricPoint, InventoryPoint, LATEST_SCHEMA_VERSION
from app.services.promotions import normalize_wb_promotions, normalize_ozon_promotions, historical_promo_factor
from app.services.supply import QUALITY_METHOD_VERSION, build_supply_plan
from app.services.exporting import collect_export_tables
from app.integrations.base import FetchResult
from app.integrations.wildberries import WildberriesClient
from app.integrations.ozon import OzonClient


def make_repo(tmp_path: Path):
    db=Database(tmp_path/'phase16.sqlite3'); db.initialize(); repo=Repository(db)
    seller=repo.ensure_seller(16,'Seller'); shop=repo.ensure_shop(seller.id,'Shop')
    conn=repo.ensure_connection(shop.id,'ozon','Ozon')
    return db,repo,shop,conn


def seed_product(repo,shop,conn,sku,end,days=56):
    product=repo.ensure_product(shop.id,sku,sku); listing=repo.ensure_listing(product.id,conn.id,sku,offer_id=sku)
    hist_start=end-timedelta(days=days-1)
    promo_start=end-timedelta(days=20); promo_end=end-timedelta(days=14)
    for i in range(days):
        d=hist_start+timedelta(days=i); units=5 if promo_start<=d<=promo_end else 2; ds=d.isoformat()
        run=repo.record_success(conn.id,'analytics/orders',ds,{'d':ds,'u':units},[MetricPoint(conn.id,ds,'ordered_units',units,'units',True,ds)])
        repo.save_product_metrics(run,[ProductMetricPoint(listing.id,ds,'ordered_units',units,'units','ALL',True,ds)])
    run=repo.record_success(conn.id,'product/info/stocks',end.isoformat(),{'stock':5},[])
    repo.save_inventory(run,[InventoryPoint(listing.id,5,0,'FBO')])
    prun=repo.record_success(conn.id,'actions/promotions',end.isoformat(),{'p':1},[])
    repo.upsert_promotions(conn.id,prun,'ozon',[
        {'external_promotion_id':'old','name':'Past','start_at':promo_start.isoformat(),'end_at':promo_end.isoformat(),
         'products':[{'marketplace_sku':sku,'offer_id':sku,'in_action':True}],'products_complete':True},
        {'external_promotion_id':'next','name':'Future','start_at':(end+timedelta(days=1)).isoformat(),'end_at':(end+timedelta(days=7)).isoformat(),
         'products':[{'marketplace_sku':sku,'offer_id':sku,'in_action':True}],'products_complete':True},
    ],complete_external_ids=['old','next'])
    return product,listing


def test_schema_v11_promotion_tables_and_supply_columns(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path)
    assert db.schema_version()==LATEST_SCHEMA_VERSION
    with db.connect() as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols={r[1] for r in c.execute('PRAGMA table_info(supply_recommendation_snapshots)')}
    assert {'promotions','promotion_products'} <= tables
    assert {'bias_correction','promo_factor','promo_days'} <= cols


def test_promo_uplift_requires_enough_history():
    days=[(date(2026,1,1)+timedelta(days=i)).isoformat() for i in range(30)]
    vals={d:2.0 for d in days}
    assert historical_promo_factor(vals,set(days[:3]),days)==1.0
    for d in days[:7]: vals[d]=4.0
    assert historical_promo_factor(vals,set(days[:7]),days)>1.0


def test_supply_uses_exact_future_promo_and_bias_correction(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    product,_=seed_product(repo,shop,conn,'SKU-PROMO',end)
    # Three evaluated forecasts under-predicted by 20% -> damped correction should exceed 1.
    repo.save_forecast_quality(shop.id,[
        {'product_id':product.id,'as_of_date':(end-timedelta(days=30-i)).isoformat(),'horizon_days':7,'predicted_units':8,'actual_units':10}
        for i in range(3)
    ],method_version=QUALITY_METHOD_VERSION)
    row=next(r for r in build_supply_plan(repo,shop.id,end,persist=True).rows if r.internal_sku=='SKU-PROMO')
    assert row.bias_correction>1.0
    assert row.promo_factor>1.0
    assert row.promo_days>=7
    assert row.forecast_next_7_units > row.forecast_daily_units*7
    saved=repo.latest_supply_recommendations(shop.id,end.isoformat())[0]
    assert saved['promo_factor']>1 and saved['bias_correction']>1


def test_promotions_are_exported(tmp_path):
    db,repo,shop,conn=make_repo(tmp_path); end=date(2026,9,28)
    seed_product(repo,shop,conn,'SKU-EXPORT',end)
    tables=collect_export_tables(repo,shop.id,end,30)
    assert 'Promotions' in tables and tables['Promotions']


def test_wb_and_ozon_normalization_keep_exact_participation():
    wb=normalize_wb_promotions({'data':{'promotions':[{'id':1,'name':'WB','startDateTime':'2026-10-01T00:00:00Z','endDateTime':'2026-10-05T00:00:00Z'}]}},
        {'1':{'data':{'nomenclatures':[{'id':555,'inAction':True,'price':1000,'planPrice':800}]}}})
    assert wb[0]['products'][0]['marketplace_sku']=='555' and wb[0]['products'][0]['in_action']
    oz=normalize_ozon_promotions({'result':[{'id':2,'title':'Ozon','date_start':'2026-10-01','date_end':'2026-10-05'}]},
        {'2':{'result':{'products':[{'id':777,'price':1000,'action_price':700}]}}},
        {'777':{'id':777,'offer_id':'OFFER-777'}})
    assert oz[0]['products'][0]['offer_id']=='OFFER-777'


@pytest.mark.asyncio
async def test_wb_promo_calendar_paginates(monkeypatch):
    client=WildberriesClient('token',min_interval=0); calls=[]
    async def fake(start_at,end_at,*,all_promo=True,limit=1000,offset=0):
        calls.append(offset)
        rows=[{'id':1},{'id':2}] if offset==0 else [{'id':3}]
        return FetchResult.success('wildberries',{'data':{'promotions':rows}},200,1)
    monkeypatch.setattr(client,'calendar_promotions_page',fake)
    result=await client.calendar_promotions_all('a','b',limit=2)
    assert result.ok and [x['id'] for x in result.data['data']['promotions']]==[1,2,3] and calls==[0,2]
    await client.close()


@pytest.mark.asyncio
async def test_ozon_promo_products_cursor_paginates(monkeypatch):
    client=OzonClient('cid','key',min_interval=0); calls=[]
    async def fake(action_id,*,last_id='',limit=100):
        calls.append(last_id)
        if not last_id:
            return FetchResult.success('ozon',{'result':{'products':[{'id':1}],'last_id':'x','total':2}},200,1)
        return FetchResult.success('ozon',{'result':{'products':[{'id':2}],'last_id':'','total':2}},200,1)
    monkeypatch.setattr(client,'promotion_products_page',fake)
    result=await client.promotion_products_all(5)
    assert result.ok and [x['id'] for x in result.data['result']['products']]==[1,2] and calls==['','x']
    await client.close()

@pytest.mark.asyncio
async def test_wb_auto_promotion_does_not_request_unsupported_nomenclatures(tmp_path):
    from app.services.collection import CollectionService
    db,repo,shop,conn=make_repo(tmp_path)
    class FakeWB:
        async def calendar_promotions_all(self,*args,**kwargs):
            return FetchResult.success('wildberries',{'data':{'promotions':[{
                'id':9,'name':'Auto','type':'auto','startDateTime':'2026-09-01T00:00:00Z','endDateTime':'2026-10-30T00:00:00Z'}]}},200,1)
        async def calendar_promotion_products_all(self,*args,**kwargs):
            raise AssertionError('auto promotion nomenclatures must not be requested')
    service=CollectionService(repo,wildberries=FakeWB())
    out=await service.collect_promotions(shop_id=shop.id,as_of=date(2026,9,28),wb_connection_id=conn.id)
    assert len(out)==1 and out[0].ok
    rows=repo.promotions_for_shop(shop.id,'2026-09-28','2026-10-30')
    assert rows and rows[0]['promo_type']=='auto'


def test_schema_v10_migrates_to_v11_preserving_existing_supply_snapshot(tmp_path):
    import app.storage.database as dbmod
    path=tmp_path/'v10.sqlite3'; conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
    for version in range(1,11):
        dbmod.MIGRATIONS[version](conn); conn.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    conn.execute("INSERT INTO sellers(telegram_user_id,name,timezone,active,created_at) VALUES(1,'S','Europe/Moscow',1,datetime('now'))")
    sid=conn.execute('SELECT id FROM sellers').fetchone()[0]
    conn.execute("INSERT INTO shops(seller_id,name,currency,active,created_at) VALUES(?,'Shop','RUB',1,datetime('now'))",(sid,))
    shop_id=conn.execute('SELECT id FROM shops').fetchone()[0]
    conn.execute("INSERT INTO products(shop_id,internal_sku,name,active,created_at) VALUES(?,'SKU','SKU',1,datetime('now'))",(shop_id,))
    pid=conn.execute('SELECT id FROM products').fetchone()[0]
    conn.execute("""INSERT INTO supply_recommendation_snapshots(shop_id,product_id,as_of_date,generated_at,abc_class,xyz_class,
        avg_daily_units,forecast_daily_units,reorder_point_units,target_units,recommended_order_units,confidence,history_days,method_version)
        VALUES(?,?, '2026-09-28',datetime('now'),'A','X',2,2,10,20,5,'high',56,'seasonal-wma-v2')""",(shop_id,pid))
    conn.commit(); conn.close()
    db=Database(path); assert db.initialize()==LATEST_SCHEMA_VERSION
    with db.connect() as c:
        row=c.execute('SELECT recommended_order_units,bias_correction,promo_factor,promo_days FROM supply_recommendation_snapshots').fetchone()
    assert tuple(row)==(5.0,1.0,1.0,0)

@pytest.mark.asyncio
async def test_ozon_promotion_collection_links_product_id_via_offer_id(tmp_path):
    from app.services.collection import CollectionService
    db,repo,shop,conn=make_repo(tmp_path)
    product=repo.ensure_product(shop.id,'INTERNAL','Product')
    repo.ensure_listing(product.id,conn.id,'SKU-777',offer_id='OFFER-777')
    class FakeOzon:
        async def promotions_list(self):
            return FetchResult.success('ozon',{'result':[{'id':12,'title':'Sale','date_start':'2026-10-01','date_end':'2026-10-07'}]},200,1)
        async def promotion_products_all(self, action_id):
            return FetchResult.success('ozon',{'result':{'products':[{'id':777,'price':1000,'action_price':800}],'total':1,'last_id':''}},200,1)
        async def product_info_list(self,*,product_ids=None,offer_ids=None):
            return FetchResult.success('ozon',{'result':{'items':[{'id':777,'offer_id':'OFFER-777'}]}},200,1)
    service=CollectionService(repo,ozon=FakeOzon())
    out=await service.collect_promotions(shop_id=shop.id,as_of=date(2026,9,28),ozon_connection_id=conn.id)
    assert out and out[0].ok
    rows=repo.promotion_products_for_shop(shop.id,'2026-09-28','2026-10-10')
    assert rows and rows[0]['product_id']==product.id and rows[0]['internal_sku']=='INTERNAL'
