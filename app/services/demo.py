"""Deterministic local demo dataset for onboarding without marketplace keys."""
from __future__ import annotations
from datetime import date, timedelta, datetime, timezone
import math
from app.storage import MetricPoint, ProductMetricPoint, InventoryPoint
from app.storage.models import AdCampaignPoint

DEMO_PRODUCTS=(
    ('DEMO-A','Демо · Товар A',620.0,5.0),
    ('DEMO-B','Демо · Товар B',910.0,3.2),
    ('DEMO-C','Демо · Товар C',340.0,7.0),
)


def enable_demo(repo, shop_id: int, *, today: date | None=None) -> dict[str,int]:
    pref=repo.get_shop_preferences(shop_id)
    if pref and pref.demo_mode:
        return {'products':3,'runs':0}
    with repo.db.connect() as c:
        real_runs=int(c.execute("""SELECT COUNT(*) FROM source_runs sr JOIN marketplace_connections mc ON mc.id=sr.connection_id
            WHERE mc.shop_id=? AND sr.endpoint NOT LIKE 'demo/%'""",(shop_id,)).fetchone()[0])
        real_products=int(c.execute("SELECT COUNT(*) FROM products WHERE shop_id=? AND internal_sku NOT LIKE 'DEMO-%'",(shop_id,)).fetchone()[0])
    if real_runs or real_products:
        raise ValueError('Демо можно включить только в пустом магазине. Создайте отдельный магазин для DEMO, чтобы не смешивать синтетические и реальные данные.')
    today=today or date.today(); end=today-timedelta(days=1)
    wb=repo.ensure_connection(shop_id,'wildberries','Wildberries Demo')
    oz=repo.ensure_connection(shop_id,'ozon','Ozon Demo')
    listings=[]
    for idx,(sku,name,cost,base) in enumerate(DEMO_PRODUCTS,1):
        p=repo.ensure_product(shop_id,sku,name,cost)
        listings.append((p,repo.ensure_listing(p.id,wb.id,f'WB-{idx}',offer_id=sku),repo.ensure_listing(p.id,oz.id,f'OZ-{idx}',offer_id=sku),base))
    runs=0
    for i in range(35):
        d=end-timedelta(days=34-i); ds=d.isoformat(); weekday=1.18 if d.weekday() in {4,5} else .92 if d.weekday()==0 else 1.0
        for conn,market in ((wb,'wildberries'),(oz,'ozon')):
            units=0.0; revenue=0.0; product_points=[]
            for idx,(p,wbl,ozl,base) in enumerate(listings,1):
                listing=wbl if market=='wildberries' else ozl
                wave=1+0.12*math.sin((i+idx)*0.65)
                u=max(0,round(base*weekday*wave*(0.92 if market=='wildberries' else 1.08)))
                price=1450+idx*430
                units+=u; revenue+=u*price
                product_points += [
                    ProductMetricPoint(listing.id,ds,'ordered_units',u,'units','ALL',True,ds),
                    ProductMetricPoint(listing.id,ds,'ordered_revenue',u*price,'RUB','ALL',True,ds),
                ]
            metrics=[MetricPoint(conn.id,ds,'ordered_units',units,'units',True,ds),MetricPoint(conn.id,ds,'ordered_revenue',revenue,'RUB',True,ds)]
            run=repo.record_success(conn.id,'demo/orders',ds,{'demo':True,'marketplace':market,'date':ds},metrics)
            repo.save_product_metrics(run,product_points); runs+=1
            fin=[
                MetricPoint(conn.id,ds,'commission',revenue*.13,'RUB',False,ds),
                MetricPoint(conn.id,ds,'logistics',revenue*.055,'RUB',False,ds),
                MetricPoint(conn.id,ds,'financial_sales',revenue*.88,'RUB',False,ds),
                MetricPoint(conn.id,ds,'marketplace_net',revenue*.80,'RUB',False,ds),
            ]
            repo.record_success(conn.id,'demo/finance',ds,{'demo':True,'finance':market,'date':ds},fin); runs+=1
    captured=end.isoformat()
    for conn,market in ((wb,'wildberries'),(oz,'ozon')):
        run=repo.record_success(conn.id,'demo/stocks',captured,{'demo':True,'stocks':market},[])
        inv=[]
        for idx,(p,wbl,ozl,base) in enumerate(listings,1):
            listing=wbl if market=='wildberries' else ozl
            inv.append(InventoryPoint(listing.id,max(0,round(18+idx*14-base*2)),0,'FBW' if market=='wildberries' else 'FBO','DEMO',captured))
        repo.save_inventory(run,inv); runs+=1
        ad=repo.record_success(conn.id,'demo/ads',captured,{'demo':True,'ads':market},[])
        repo.save_ad_details(ad,campaigns=[AdCampaignPoint(conn.id,captured,f'DEMO-{market}',f'Демо кампания {market}',spend=1800+500*(market=='ozon'),attributed_sales=9800,orders=7,clicks=85,impressions=8400)])
    repo.update_shop_preferences(shop_id,demo_mode=True,setup_completed=True,onboarding_version='19')
    return {'products':len(listings),'runs':runs}


def disable_demo(repo, shop_id: int) -> None:
    # Demo data is disposable and must never survive into a real shop where it
    # could contaminate business totals. Delete only objects created by this seeder.
    with repo.db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("DELETE FROM marketplace_connections WHERE shop_id=? AND display_name IN ('Wildberries Demo','Ozon Demo')",(shop_id,))
        c.execute("DELETE FROM products WHERE shop_id=? AND internal_sku LIKE 'DEMO-%'",(shop_id,))
        c.commit()
    repo.update_shop_preferences(shop_id,demo_mode=False)
