from datetime import date, datetime

import pytest

from app.integrations.base import FetchResult
from app.services.collection import CollectionService
from app.services.ozon_dates import posting_day, posting_range
from app.services.product_analytics import normalize_ozon_postings
from app.services.reconciliation import normalize_ozon_posting_events
from app.storage import Database, Repository, MetricPoint
from app.storage.models import CommerceEventPoint, ProductMetricPoint


@pytest.mark.parametrize('stamp,expected',[
    ('2026-09-29T22:29:53Z','2026-09-30'),
    ('2026-09-30T22:36:03Z','2026-10-01'),
    ('2026-10-01T01:36:03+03:00','2026-10-01'),
    ('2026-09-30T22:36:03','2026-10-01'),
    ('2026-09-30','2026-09-30'),
    ('invalid',None),
    (None,None),
])
def test_posting_day_uses_moscow_calendar(stamp,expected):
    assert posting_day(stamp)==expected


def posting(number,stamp,sku,price=700):
    return {'posting_number':number,'in_process_at':stamp,'status':'delivering',
            'products':[{'sku':sku,'quantity':1,'price':{'amount':str(price),'currency':'RUB'}}]}


def test_product_and_event_dates_use_same_boundaries():
    payload={'postings':[
        posting('early','2026-09-29T22:29:53Z',101,750),
        posting('day','2026-09-30T12:00:00Z',102),
        posting('late','2026-09-30T22:36:03Z',103),
    ]}
    products=normalize_ozon_postings(payload,fulfillment_scheme='FBS',target_date='2026-09-30')
    events=normalize_ozon_posting_events(payload,fulfillment_scheme='FBS',start_date='2026-09-30',end_date='2026-09-30')
    assert {p.marketplace_sku for p in products}=={'101','102'}
    assert {p.marketplace_sku for p in events}=={'101','102'}
    assert sum(p.gross_amount for p in events)==1450
    assert events[0].event_time=='2026-09-29T22:29:53Z'


def make_repo(tmp_path):
    db=Database(tmp_path/'dates.sqlite3');db.initialize();repo=Repository(db)
    seller=repo.ensure_seller(1,'Seller');shop=repo.ensure_shop(seller.id,'Shop')
    conn=repo.ensure_connection(shop.id,'ozon','Ozon')
    return repo,shop,conn


class PostingClient:
    def __init__(self,rows):self.rows=rows;self.calls=[]

    async def postings_all(self,scheme,since,to):
        self.calls.append((scheme,since,to))
        first=datetime.fromisoformat(since.replace('Z','+00:00'))
        last=datetime.fromisoformat(to.replace('Z','+00:00'))
        rows=[p for p in self.rows if first<=datetime.fromisoformat(p['in_process_at'].replace('Z','+00:00'))<=last] if scheme=='FBS' else []
        return FetchResult.success('ozon',{'postings':rows},200,1)


@pytest.mark.asyncio
async def test_recollection_repairs_utc_dates_and_clears_removed_sku(tmp_path):
    repo,shop,conn=make_repo(tmp_path)
    old=repo.record_success(conn.id,'postings/fbs','2026-09-30',{'old':True},[])
    product=repo.ensure_product(shop.id,'late','Late');listing=repo.ensure_listing(product.id,conn.id,'103')
    repo.save_product_metrics(old,[ProductMetricPoint(listing.id,'2026-09-30','fulfillment_units',1,'units','FBS')])
    repo.save_commerce_events(old,[CommerceEventPoint(listing.id,'2026-09-30','posting','ozon_postings','late-event',
        event_time='2026-09-30T22:36:03Z',external_order_id='late',quantity=1,gross_amount=700,fulfillment_scheme='FBS')])
    analytics=repo.record_success(conn.id,'analytics/orders','2026-09-30',{'analytics':True},[
        MetricPoint(conn.id,'2026-09-30','ordered_units',38,'units',True),
        MetricPoint(conn.id,'2026-09-30','ordered_revenue',39952,'RUB',True)])
    client=PostingClient([
        posting('early','2026-09-29T22:29:53Z',101,750),
        posting('day','2026-09-30T12:00:00Z',102),
        posting('late','2026-09-30T22:36:03Z',103),
    ])
    service=CollectionService(repo,ozon=client)
    outcomes=await service.collect_ozon_fulfillment_range(shop_id=shop.id,connection_id=conn.id,start=date(2026,9,30),end=date(2026,9,30))
    assert all(x.ok for x in outcomes)
    assert client.calls==[
        ('FBO','2026-09-29T21:00:00.000Z','2026-09-30T20:59:59.999Z'),
        ('FBS','2026-09-29T21:00:00.000Z','2026-09-30T20:59:59.999Z')]
    assert repo.product_metric_series(listing.id,'2026-09-30','2026-09-30','fulfillment_units','FBS')=={'2026-09-30':0}
    assert repo.latest_metric(conn.id,'2026-09-30','ordered_units')['source_run_id']==analytics
    assert repo.latest_metric(conn.id,'2026-09-30','ordered_revenue')['value']==39952
    with repo.db.connect() as c:
        late=c.execute("SELECT data_date,gross_amount FROM commerce_events WHERE fingerprint='late-event'").fetchone()
        assert tuple(late)==('2026-10-01',700)
    assert repo.correct_ozon_posting_dates(conn.id)==0


def test_empty_snapshot_keeps_other_scheme_and_order_analytics(tmp_path):
    repo,shop,conn=make_repo(tmp_path)
    product=repo.ensure_product(shop.id,'p','P');listing=repo.ensure_listing(product.id,conn.id,'101')
    for scheme in ('FBO','FBS'):
        run=repo.record_success(conn.id,f'postings/{scheme.lower()}','2026-09-30',{'scheme':scheme},[])
        repo.save_product_metrics(run,[ProductMetricPoint(listing.id,'2026-09-30','fulfillment_units',2,'units',scheme)])
    run=repo.record_success(conn.id,'postings/fbs','2026-09-30',{'postings':[]},[])
    repo.save_product_metrics(run,[],replace_fulfillment_snapshot=True)
    assert repo.product_metric_series(listing.id,'2026-09-30','2026-09-30','fulfillment_units','FBS')=={'2026-09-30':0}
    assert repo.product_metric_series(listing.id,'2026-09-30','2026-09-30','fulfillment_units','FBO')=={'2026-09-30':2}


def test_partial_posting_snapshot_cannot_clear_history(tmp_path):
    repo,shop,conn=make_repo(tmp_path)
    run=repo.record_success(conn.id,'postings/fbs','2026-09-30',{'postings':[]},[],status='partial')
    with pytest.raises(ValueError,match='complete'):
        repo.save_product_metrics(run,[],replace_fulfillment_snapshot=True)


def test_range_rejects_reversed_days():
    with pytest.raises(ValueError):posting_range(date(2026,10,1),date(2026,9,30))
