from datetime import date
from pathlib import Path
import pytest
from app.integrations.base import FetchResult
from app.services.collection import CollectionService
from app.storage import Database, Repository

class FakeWB:
    def __init__(self, result): self.result = result
    async def orders(self, date_from, flag=0): return self.result

class FakeOzon:
    def __init__(self, result): self.result = result
    async def analytics(self, payload): return self.result

@pytest.fixture
def ctx(tmp_path: Path):
    db=Database(tmp_path/'c.sqlite3'); db.initialize(); repo=Repository(db)
    seller=repo.ensure_seller(1,'S'); shop=repo.ensure_shop(seller.id,'Shop')
    wb=repo.ensure_connection(shop.id,'wildberries','WB')
    oz=repo.ensure_connection(shop.id,'ozon','Ozon')
    return repo, wb, oz

@pytest.mark.asyncio
async def test_partial_marketplace_failure_does_not_remove_other_marketplace(ctx):
    repo, wbconn, ozconn = ctx
    wb_payload=[{'date':'2026-09-28T10:00:00','lastChangeDate':'2026-09-28T10:30:00','isCancel':False}]
    service=CollectionService(repo,
        wildberries=FakeWB(FetchResult.success('wildberries',wb_payload,200,1)),
        ozon=FakeOzon(FetchResult.failure('ozon','HTTP 500',500,4)))
    wb_out=await service.collect_wb_orders_day(wbconn.id,date(2026,9,28))
    oz_out=await service.collect_ozon_orders_day(ozconn.id,date(2026,9,28))
    assert wb_out.ok and not oz_out.ok
    assert repo.metrics_for_day(wbconn.id,'2026-09-28')['ordered_units'] == 1
    assert repo.metrics_for_day(ozconn.id,'2026-09-28') == {}

@pytest.mark.asyncio
async def test_normalization_failure_is_recorded_not_zeroed(ctx):
    repo, _, ozconn = ctx
    service=CollectionService(repo, ozon=FakeOzon(FetchResult.success('ozon',{'result':{}},200,1)))
    out=await service.collect_ozon_orders_day(ozconn.id,date(2026,9,28))
    assert not out.ok
    assert repo.metrics_for_day(ozconn.id,'2026-09-28') == {}
    assert repo.count('source_runs') == 1

class FakeWBBackfill(FakeWB):
    async def orders_since(self, date_from): return self.result

@pytest.mark.asyncio
async def test_batch_backfill_partitions_days_and_preserves_zero_wb_day(ctx):
    repo, wbconn, ozconn = ctx
    wb_rows=[{'date':'2026-09-27T10:00:00','lastChangeDate':'2026-09-27T10:30:00','isCancel':False,'priceWithDisc':100}]
    oz_payload={'result':{'data':[
        {'dimensions':[{'id':'2026-09-27'}],'metrics':[2,250]},
        {'dimensions':[{'id':'2026-09-28'}],'metrics':[3,400]},
    ]}}
    service=CollectionService(repo,
        wildberries=FakeWBBackfill(FetchResult.success('wildberries',wb_rows,200,1)),
        ozon=FakeOzon(FetchResult.success('ozon',oz_payload,200,1)))
    out=await service.backfill_orders(start=date(2026,9,27),end=date(2026,9,28),
                                      wb_connection_id=wbconn.id,ozon_connection_id=ozconn.id)
    assert all(x.ok for x in out)
    assert repo.metrics_for_day(wbconn.id,'2026-09-27')['ordered_units']==1
    assert repo.metrics_for_day(wbconn.id,'2026-09-28')['ordered_units']==0
    assert repo.metrics_for_day(ozconn.id,'2026-09-28')['ordered_units']==3

@pytest.mark.asyncio
async def test_wb_order_collection_persists_reconciliation_events(ctx):
    repo, wbconn, _ = ctx
    shop=repo.get_shop(wbconn.shop_id)
    payload=[{'date':'2026-09-28T10:00:00','lastChangeDate':'2026-09-28T11:00:00',
              'nmId':123,'supplierArticle':'A','srid':'RID123','priceWithDisc':500,'isCancel':True}]
    service=CollectionService(repo,wildberries=FakeWB(FetchResult.success('wildberries',payload,200,1)))
    out=await service.collect_wb_orders_day(wbconn.id,date(2026,9,28),shop_id=shop.id)
    assert out.ok
    counts=repo.commerce_event_counts(shop.id,'2026-09-28','2026-09-28')['wildberries']
    assert counts['order']==1 and counts['cancel']==1

class FakeWBSales(FakeWB):
    async def sales_since(self, date_from): return self.result

@pytest.mark.asyncio
async def test_wb_sales_collection_persists_sale_and_return(ctx):
    repo, wbconn, _ = ctx
    shop=repo.get_shop(wbconn.shop_id)
    payload=[
        {'date':'2026-09-28T10:00:00','nmId':123,'srid':'RID1','saleID':'S1','priceWithDisc':500,'forPay':350},
        {'date':'2026-09-29T10:00:00','nmId':123,'srid':'RID1','saleID':'R1','priceWithDisc':500,'forPay':350},
    ]
    service=CollectionService(repo,wildberries=FakeWBSales(FetchResult.success('wildberries',payload,200,1)))
    out=await service.collect_wb_sales_range(shop_id=shop.id,connection_id=wbconn.id,
                                             start=date(2026,9,28),end=date(2026,9,29))
    assert all(x.ok for x in out)
    counts=repo.commerce_event_counts(shop.id,'2026-09-28','2026-09-29')['wildberries']
    assert counts['sale']==1 and counts['return']==1


class FakeWBStocks:
    def __init__(self, result):
        self.result=result
        self.calls=[]

    async def stock_report_all(self, kind):
        self.calls.append(kind)
        return self.result


@pytest.mark.asyncio
async def test_automatic_inventory_suppresses_wb_401_403_until_manual_probe(ctx):
    repo, wbconn, _ = ctx
    shop=repo.get_shop(wbconn.shop_id)
    wb=FakeWBStocks(FetchResult.failure('wildberries','HTTP 403: forbidden',403,1))
    service=CollectionService(repo,wildberries=wb)

    first=await service.collect_inventory(
        shop_id=shop.id,wb_connection_id=wbconn.id,automatic=True,data_date=date(2026,9,30))
    assert len(first)==2 and all(not x.ok for x in first)
    assert wb.calls==['wb','seller']

    # Next hourly/automatic cycle must not hit the same forbidden endpoints again.
    second=await service.collect_inventory(
        shop_id=shop.id,wb_connection_id=wbconn.id,automatic=True,data_date=date(2026,9,30))
    assert second==[]
    assert wb.calls==['wb','seller']

    # Manual refresh remains a deliberate re-check and may recover after token/scope changes.
    third=await service.collect_inventory(
        shop_id=shop.id,wb_connection_id=wbconn.id,automatic=False,data_date=date(2026,9,30))
    assert len(third)==2 and all(not x.ok for x in third)
    assert wb.calls==['wb','seller','wb','seller']
