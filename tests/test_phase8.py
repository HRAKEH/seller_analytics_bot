from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
from app.services.imports import import_costs, read_tabular
from app.services.preferences import defaults_from_settings, update_validated
from app.services.finance import normalize_wb_product_finance, normalize_ozon_product_finance
from app.storage import Database, Repository, MetricPoint
from app.storage.models import ProductMetricPoint
from app.reports import build_sku_economics, format_sku_economics


def make_repo(tmp_path: Path):
    db=Database(tmp_path/'p8.sqlite3'); assert db.initialize()>=4
    repo=Repository(db); seller=repo.ensure_seller(1,'Seller'); shop=repo.ensure_shop(seller.id,'Shop')
    wb=repo.ensure_connection(shop.id,'wildberries','WB'); oz=repo.ensure_connection(shop.id,'ozon','Ozon')
    return repo,shop,wb,oz


def add_order(repo, shop, conn, product, listing, day, units, revenue, payload_key):
    run=repo.record_success(conn.id,'orders',day,{'k':payload_key},[
        MetricPoint(conn.id,day,'ordered_units',units,'units',True),
        MetricPoint(conn.id,day,'ordered_revenue',revenue,'RUB',True),
    ])
    repo.save_product_metrics(run,[
        ProductMetricPoint(listing.id,day,'ordered_units',units,'units'),
        ProductMetricPoint(listing.id,day,'ordered_revenue',revenue,'RUB'),
    ])


def test_shop_preferences_are_persistent_and_validated(tmp_path):
    repo,shop,_,_=make_repo(tmp_path)
    settings=Settings.from_env()
    p=repo.ensure_shop_preferences(shop.id,defaults_from_settings(settings))
    assert p.report_time
    p=update_validated(repo,shop.id,timezone='Europe/Berlin',report_time='08:05',
                       stock_risk_days='9',alert_order_drop_pct='42,5')
    assert p.timezone=='Europe/Berlin' and p.report_time=='08:05'
    assert p.stock_risk_days==9 and p.alert_order_drop_pct==42.5
    with pytest.raises(ValueError): update_validated(repo,shop.id,timezone='Not/AZone')


def test_historical_cost_is_used_for_historical_orders(tmp_path):
    repo,shop,_,oz=make_repo(tmp_path)
    product=repo.ensure_product(shop.id,'P1','Product',cost_price=10)
    listing=repo.ensure_listing(product.id,oz.id,'100')
    assert repo.set_product_cost(product.id,20,effective_date='2026-09-15')
    add_order(repo,shop,oz,product,listing,'2026-09-10',2,200,'a')
    add_order(repo,shop,oz,product,listing,'2026-09-20',2,200,'b')
    out=repo.estimated_order_cogs(shop.id,'2026-09-01','2026-09-30')['ozon']
    assert out['estimated_cost']==60
    assert out['covered_units']==4


def test_link_marketplace_listings_preserves_history(tmp_path):
    repo,shop,wb,oz=make_repo(tmp_path)
    p1=repo.ensure_product(shop.id,'wildberries:11','WB product',cost_price=30)
    p2=repo.ensure_product(shop.id,'ozon:22','Ozon product')
    l1=repo.ensure_listing(p1.id,wb.id,'11'); l2=repo.ensure_listing(p2.id,oz.id,'22')
    add_order(repo,shop,wb,p1,l1,'2026-09-20',1,100,'wb')
    add_order(repo,shop,oz,p2,l2,'2026-09-20',2,240,'oz')
    linked=repo.link_marketplace_listings(shop.id,'PHYS-1',[('wb','11'),('ozon','22')],name='Physical')
    assert repo.listing_for_shop(shop.id,'wildberries','11')['product_id']==linked.id
    assert repo.listing_for_shop(shop.id,'ozon','22')['product_id']==linked.id
    rows=repo.sku_economics(shop.id,'2026-09-20','2026-09-20')
    assert {r['internal_sku'] for r in rows}=={'PHYS-1'}
    assert repo.product_cost_history(linked.id)[0]['cost_price']==30


def test_csv_import_links_and_sets_cost(tmp_path):
    repo,shop,wb,oz=make_repo(tmp_path)
    p1=repo.ensure_product(shop.id,'wb:101','WB'); p2=repo.ensure_product(shop.id,'oz:202','Ozon')
    repo.ensure_listing(p1.id,wb.id,'101'); repo.ensure_listing(p2.id,oz.id,'202')
    path=tmp_path/'costs.csv'
    path.write_text('marketplace,sku,cost_price,effective_date,internal_sku,name\n'
                    'wb,101,55,2026-09-01,COMMON-1,Item\n'
                    'ozon,202,55,2026-09-01,COMMON-1,Item\n',encoding='utf-8')
    summary=import_costs(repo,shop.id,path)
    assert summary.status=='success' and summary.applied_rows==2
    wbrow=repo.listing_for_shop(shop.id,'wildberries','101'); ozrow=repo.listing_for_shop(shop.id,'ozon','202')
    assert wbrow['product_id']==ozrow['product_id']
    assert repo.product_cost_history(wbrow['product_id'])[0]['cost_price']==55
    assert repo.count('import_batches')==1


def test_xlsx_reader(tmp_path):
    from openpyxl import Workbook
    path=tmp_path/'costs.xlsx'; wb=Workbook(); ws=wb.active
    ws.append(['marketplace','sku','cost_price']); ws.append(['wb','1',12.5]); wb.save(path)
    rows=read_tabular(path)
    assert rows[0]['marketplace']=='wb' and rows[0]['cost_price']==12.5


def test_wb_product_finance_normalization():
    rows=normalize_wb_product_finance([{
        'saleDt':'2026-09-10T10:00:00','nmId':123,'supplierArticle':'A',
        'retailAmount':1000,'ppvzForPay':700,'deliveryRub':80,'storageFee':10,'penalty':5,
    }])
    assert len(rows)==1 and rows[0].marketplace_sku=='123'
    assert rows[0].metrics['financial_sales']==1000
    assert rows[0].metrics['goods_payable']==700 and rows[0].metrics['logistics']==80


def test_ozon_product_finance_normalization():
    rows=normalize_ozon_product_finance({'accruals':[{
        'posting':{'products':[{'sku':'77','offer_id':'O-77','commission':{
            'seller_price':{'amount':'1000'},'sale_commission':{'amount':'-150'}},
            'delivery':{'total_accrued':{'amount':'-90'}}}]},
        'item_fees':{'fees':[{'sku':'77','fees':[{'accrued':{'amount':'-20'}}]}]},
    }]},'2026-09-10')
    m=rows[0].metrics
    assert m['financial_sales']==1000 and m['commission']==150 and m['logistics']==90 and m['services']==20


def test_sku_economics_includes_actual_financial_components(tmp_path):
    repo,shop,_,oz=make_repo(tmp_path)
    product=repo.ensure_product(shop.id,'P','Product',cost_price=25); listing=repo.ensure_listing(product.id,oz.id,'77')
    add_order(repo,shop,oz,product,listing,'2026-09-10',2,200,'order')
    run=repo.record_success(oz.id,'finance','2026-09-10',{'finance':1},[])
    repo.save_product_metrics(run,[
        ProductMetricPoint(listing.id,'2026-09-10','financial_sales',180,'RUB'),
        ProductMetricPoint(listing.id,'2026-09-10','commission',20,'RUB'),
        ProductMetricPoint(listing.id,'2026-09-10','logistics',10,'RUB'),
    ])
    report=build_sku_economics(repo,shop.id,date(2026,9,10),1)
    row=report.rows[0]
    assert row.financial_metrics['financial_sales']==180
    text=format_sku_economics(report)
    assert 'фин. продажи источника: 180.00 ₽' in text
    assert 'известные расходы маркетплейса по SKU: 30.00 ₽' in text

@pytest.mark.asyncio
async def test_collection_persists_wb_sku_finance(tmp_path):
    from app.integrations.base import FetchResult
    from app.services.collection import CollectionService
    repo,shop,wb,_=make_repo(tmp_path)
    class FakeWBFinance:
        async def finance_sales_reports_list_all(self,*args,**kwargs):
            return FetchResult.success('wildberries',[{
                'dateFrom':'2026-09-10','dateTo':'2026-09-10','createDate':'2026-09-11T00:00:00',
                'retailAmountSum':1000,'forPaySum':700,'deliveryServiceSum':80,
            }],200,1)
        async def finance_sales_report_detailed_all(self,*args,**kwargs):
            return FetchResult.success('wildberries',[{
                'saleDt':'2026-09-10T10:00:00','nmId':123,'supplierArticle':'A',
                'retailAmount':1000,'ppvzForPay':700,'deliveryRub':80,
            }],200,1)
    service=CollectionService(repo,wildberries=FakeWBFinance())
    out=await service.collect_finance(start=date(2026,9,10),end=date(2026,9,10),
                                      wb_connection_id=wb.id,shop_id=shop.id)
    assert any(x.ok and x.message=='SKU finance loaded' for x in out)
    totals=repo.sku_financial_totals(shop.id,'2026-09-10','2026-09-10')
    assert totals[('wildberries','123')]['financial_sales']==1000
    assert totals[('wildberries','123')]['goods_payable']==700


@pytest.mark.asyncio
async def test_collection_persists_ozon_sku_finance(tmp_path):
    from app.integrations.base import FetchResult
    from app.services.collection import CollectionService
    repo,shop,_,oz=make_repo(tmp_path)
    payload={'accruals':[{
        'accrued_category':'POSTING','total_amount':{'amount':'700'},
        'posting':{'products':[{'sku':'77','offer_id':'A77','commission':{
            'seller_price':{'amount':'1000'},'sale_commission':{'amount':'-150'}},
            'delivery':{'total_accrued':{'amount':'-90'}}}]}
    }], 'last_id':''}
    class FakeOzonFinance:
        async def finance_accrual_by_day_all(self,*args,**kwargs):
            return FetchResult.success('ozon',payload,200,1)
    service=CollectionService(repo,ozon=FakeOzonFinance())
    out=await service.collect_finance(start=date(2026,9,10),end=date(2026,9,10),
                                      ozon_connection_id=oz.id,shop_id=shop.id)
    assert all(x.ok for x in out)
    totals=repo.sku_financial_totals(shop.id,'2026-09-10','2026-09-10')
    assert totals[('ozon','77')]['financial_sales']==1000
    assert totals[('ozon','77')]['commission']==150
