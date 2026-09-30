from datetime import date, timedelta
from pathlib import Path
import httpx
import pytest

from app.integrations.base import MarketplaceClient
from app.integrations.ozon import OzonClient
from app.services.product_analytics import (
    normalize_wb_product_orders, normalize_ozon_product_analytics,
    normalize_wb_stocks, normalize_ozon_stocks,
)
from app.reports.products import build_product_report
from app.storage import Database, Repository, MetricPoint, ProductMetricPoint, InventoryPoint


def make_repo(tmp_path: Path):
    db=Database(tmp_path/'products.sqlite3'); assert db.initialize() >= 2
    repo=Repository(db); seller=repo.ensure_seller(55,'Seller'); shop=repo.ensure_shop(seller.id,'Shop')
    return repo,shop


def test_wb_product_orders_group_by_sku_and_scheme():
    rows=[
        {'date':'2026-09-28T10:00:00','nmId':11,'supplierArticle':'A','brand':'B','subject':'S',
         'priceWithDisc':100,'warehouseType':'Склад WB','isCancel':False,'lastChangeDate':'2026-09-28T11:00:00'},
        {'date':'2026-09-28T12:00:00','nmId':11,'supplierArticle':'A','priceWithDisc':80,
         'warehouseType':'Склад продавца','isCancel':True,'lastChangeDate':'2026-09-28T13:00:00'},
    ]
    out=normalize_wb_product_orders(rows,data_date='2026-09-28')
    assert len(out)==1
    assert out[0].ordered_units==2 and out[0].ordered_revenue==180 and out[0].cancellations_units==1
    assert out[0].fulfillment_units=={'FBW':1.0,'FBS':1.0}


def test_ozon_product_analytics_accepts_sku_day_dimensions_in_any_order():
    payload={'result':{'data':[
        {'dimensions':[{'id':'2026-09-28','name':'2026-09-28'},{'id':'501','name':'Товар 1'}],'metrics':[3,900]},
        {'dimensions':[{'id':'502','name':'Товар 2'},{'id':'2026-09-28'}],'metrics':[2,400]},
    ]}}
    out=normalize_ozon_product_analytics(payload)
    assert [(x.marketplace_sku,x.ordered_units) for x in out]==[('501',3.0),('502',2.0)]


def test_stock_normalizers_aggregate_sizes_and_accept_ozon_stock_dict():
    wb={'data':{'items':[
        {'nmId':10,'warehouseName':'Коледино','quantity':2},
        {'nmId':10,'warehouseName':'Коледино','quantity':3},
    ]}}
    w=normalize_wb_stocks(wb,fulfillment_scheme='FBW')
    assert len(w)==1 and w[0].available_units==5

    oz={'items':[{'product_id':1,'offer_id':'offer', 'stocks':{
        'fbo':{'type':'fbo','present':10,'reserved':2,'sku':100},
        'fbs':{'type':'fbs','present':4,'reserved':1,'sku':101},
    }}]}
    o=normalize_ozon_stocks(oz)
    assert {(x.fulfillment_scheme,x.available_units,x.reserved_units) for x in o}=={('FBO',10.0,2.0),('FBS',4.0,1.0)}


def test_product_metrics_are_idempotent_and_inventory_keeps_schemes(tmp_path):
    repo,shop=make_repo(tmp_path); conn=repo.ensure_connection(shop.id,'ozon','Ozon')
    product=repo.ensure_product(shop.id,'ozon:sku','Item'); listing=repo.ensure_listing(product.id,conn.id,'sku','offer')
    run=repo.record_success(conn.id,'analytics/orders','2026-09-28',{'a':1},[])
    point=ProductMetricPoint(listing.id,'2026-09-28','ordered_units',2,'units')
    assert repo.save_product_metrics(run,[point])==1
    assert repo.save_product_metrics(run,[point])==0

    r1=repo.record_success(conn.id,'product/info/stocks','2026-09-29',{'a':2},[])
    repo.save_inventory(r1,[InventoryPoint(listing.id,10,2,'FBO'),InventoryPoint(listing.id,4,1,'FBS')])
    rows=repo.latest_inventory_by_scheme(shop.id)
    assert {(r['fulfillment_scheme'],r['available_units']) for r in rows}=={('FBO',10.0),('FBS',4.0)}


def test_product_report_uses_successful_days_for_stock_runway(tmp_path):
    repo,shop=make_repo(tmp_path); conn=repo.ensure_connection(shop.id,'ozon','Ozon')
    product=repo.ensure_product(shop.id,'ozon:1','Кружка'); listing=repo.ensure_listing(product.id,conn.id,'1')
    end=date(2026,9,28)
    for i in range(14):
        d=end-timedelta(days=13-i); ds=d.isoformat()
        rid=repo.record_success(conn.id,'analytics/orders',ds,{'day':ds},[
            MetricPoint(conn.id,ds,'ordered_units',2,'units',True,ds),
            MetricPoint(conn.id,ds,'ordered_revenue',200,'RUB',True,ds),
        ])
        repo.save_product_metrics(rid,[
            ProductMetricPoint(listing.id,ds,'ordered_units',2,'units','ALL',True,ds),
            ProductMetricPoint(listing.id,ds,'ordered_revenue',200,'RUB','ALL',True,ds),
        ])
    inv=repo.record_success(conn.id,'product/info/stocks','2026-09-29',{'stock':1},[])
    repo.save_inventory(inv,[InventoryPoint(listing.id,10,2,'FBO')])
    report=build_product_report(repo,shop.id,end,days=7)
    assert report.top['ozon'][0].units==14
    assert report.stock_risks[0].coverage_days==14
    assert report.stock_risks[0].avg_daily_units==2
    assert report.stock_risks[0].days_left==5


@pytest.mark.asyncio
async def test_http_204_is_success():
    async def handler(request): return httpx.Response(204)
    client=MarketplaceClient('x','https://example.com',min_interval=0,transport=httpx.MockTransport(handler))
    result=await client.request('GET','/')
    assert result.ok and result.status_code==204 and result.data is None
    await client.close()


@pytest.mark.asyncio
async def test_ozon_analytics_all_uses_offset_pagination():
    offsets=[]
    async def handler(request):
        import json
        body=json.loads(request.content.decode())
        offsets.append(body['offset'])
        if body['offset']==0:
            return httpx.Response(200,json={'result':{'data':[{'metrics':[1,10]},{'metrics':[2,20]}]}})
        return httpx.Response(200,json={'result':{'data':[{'metrics':[3,30]}]}})
    client=OzonClient('c','k',min_interval=0,transport=httpx.MockTransport(handler))
    result=await client.analytics_all({'metrics':['ordered_units','revenue'],'dimension':['sku'],'limit':2,'offset':0})
    assert result.ok and len(result.data['result']['data'])==3 and offsets==[0,2]
    await client.close()


@pytest.mark.asyncio
async def test_ozon_stock_request_uses_current_quantity_filter():
    seen={}
    async def handler(request):
        import json
        seen.update(json.loads(request.content.decode()))
        return httpx.Response(200,json={'cursor':'','items':[],'total':0})
    client=OzonClient('c','k',min_interval=0,transport=httpx.MockTransport(handler))
    result=await client.product_stocks_all()
    assert result.ok
    assert seen['filter']['with_quant']=={'created':True,'exists':True}
    await client.close()

@pytest.mark.asyncio
async def test_ozon_current_postings_cursor_pagination():
    calls=[]
    async def handler(request):
        import json
        body=json.loads(request.content.decode()); calls.append(body.get('cursor'))
        if not body.get('cursor'):
            return httpx.Response(200,json={'postings':[{'posting_number':'1'}],'has_next':True,'cursor':'next'})
        return httpx.Response(200,json={'postings':[{'posting_number':'2'}],'has_next':False,'cursor':''})
    client=OzonClient('c','k',min_interval=0,transport=httpx.MockTransport(handler))
    # posting bucket is intentionally paced; disable it for this isolated test.
    original=client.fbs_postings
    async def fast(payload):
        return await client.request('POST','/v4/posting/fbs/list',json=payload,headers=client._headers(),rate_key='test-postings',min_interval=0)
    client.fbs_postings=fast
    result=await client.postings_all('FBS','2026-09-28T00:00:00Z','2026-09-28T23:59:59Z')
    assert result.ok and [x['posting_number'] for x in result.data['postings']]==['1','2']
    assert calls==['','next']
    client.fbs_postings=original
    await client.close()
