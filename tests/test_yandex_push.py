from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from app.storage import Database, Repository
from app.services.yandex_push import (parse_cancel,save_cancel,sync_scope,scope,cancellation_day,
    pending,finish,verify_order,validate_secret)
from app.services.daily_events import build_daily_events


def test_cancel_dedup_api_confirmation_and_conflict(tmp_path):
    db=Database(tmp_path/'ym.sqlite');db.initialize()
    repo=Repository(db);seller=repo.ensure_seller(1,'Seller');shop=repo.ensure_shop(seller.id,'Shop')
    conn=repo.ensure_connection(shop.id,'yandex_market','Market')
    creds=SimpleNamespace(yandex_market_business_id='10',yandex_market_campaign_ids=(20,21,22))
    sync_scope(db,conn.id,creds)
    account=scope(creds)
    payload={'notificationType':'ORDER_CANCELLED','campaignId':20,'orderId':5,
             'cancelledAt':'2026-11-01T22:00:00Z','items':[{'offerId':'A','count':2}]}
    event=parse_cancel(payload);day=event[3]
    assert day=='2026-11-02'
    save_cancel(db,conn.id,account,event);save_cancel(db,conn.id,account,event)
    assert cancellation_day(db,conn.id,day)['units']==0
    row,=pending(db,conn.id,account)
    order={'orderId':5,'campaignId':20,'status':'CANCELLED','fake':False,
           'creationDate':'2026-11-01T12:00:00Z','items':[{'offerId':'A','count':2}]}
    assert verify_order(order,row)
    assert not verify_order(dict(order,fake=True),row)
    assert not verify_order(dict(order,campaignId=21),row)
    finish(db,row,True)
    assert cancellation_day(db,conn.id,day)['units']==2
    report=build_daily_events(repo,conn.id,'yandex_market',datetime.fromisoformat(day).date())
    assert report.cancellations.units is None and report.cancellations.available_units==2
    payload['items'][0]['count']=3
    save_cancel(db,conn.id,account,parse_cancel(payload))
    assert cancellation_day(db,conn.id,day)['units']==0
    sync_scope(db,conn.id,SimpleNamespace(yandex_market_business_id='11',yandex_market_campaign_ids=(20,)))
    assert cancellation_day(db,conn.id,day) is None


def test_invalid_notification_and_secret():
    with pytest.raises(ValueError): validate_secret('short')
    with pytest.raises(ValueError): parse_cancel({'notificationType':'ORDER_CANCELLATION_REQUEST'})
