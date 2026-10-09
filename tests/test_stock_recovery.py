"""Stock recovery must be supported by complete inventory, not lost rows."""
from datetime import date, timedelta

import httpx
import pytest

from app.integrations.wildberries import WildberriesClient
from app.reports.alerts import format_active_alerts, format_alert_detail, format_alert_digest
from app.reports.formatter import format_stock_report
from app.reports.products import build_product_report
from app.services.alerts import AlertEngine
from app.services.product_analytics import normalize_wb_stocks, ProductNormalizationError
from app.storage import Database, Repository, MetricPoint, ProductMetricPoint, InventoryPoint

TODAY=date(2026,10,10)
END=TODAY-timedelta(days=1)
SUBJECT='wildberries:1001'
KW=dict(today=TODAY,order_drop_pct=35,order_lookback_days=7,
        api_stale_hours=26,drr_pct=25,stock_risk_days=14,stock_velocity_days=14)


@pytest.fixture
def stock_shop(tmp_path):
    db=Database(tmp_path/'stock.sqlite3');db.initialize();repo=Repository(db)
    seller=repo.ensure_seller(1);shop=repo.ensure_shop(seller.id)
    conn=repo.ensure_connection(shop.id,'wildberries')
    product=repo.ensure_product(shop.id,'item','Товар <&>')
    listing=repo.ensure_listing(product.id,conn.id,'1001')
    # At midnight the oldest 1-unit day leaves the window. Yesterday is not
    # loaded yet: stock remains 3, but runway changes from 14 to 19.5 days.
    for index in range(14):
        day=(END-timedelta(days=14-index)).isoformat()
        units=1 if index==0 else 2 if index==7 else 0
        run=repo.record_success(conn.id,'analytics/orders',day,{'units':units},[
            MetricPoint(conn.id,day,'ordered_units',units,'units')])
        repo.save_product_metrics(run,[ProductMetricPoint(listing.id,day,'ordered_units',units,'units')])
    put_stock(repo,conn,listing,'FBW',3)
    repo.save_alert_state(shop.id,'low_stock',SUBJECT,active=True,value=14,fingerprint='old',
                          notified_at='2026-10-09T20:07:19+00:00')
    return repo,shop,conn,listing


def put_stock(repo,conn,listing,scheme,quantity,*,day=None):
    endpoint='analytics/stocks/'+('seller' if scheme=='FBS' else 'wb')+'-warehouses'
    run=repo.record_success(conn.id,endpoint,day or END.isoformat(),{'quantity':quantity},[])
    repo.save_inventory(run,[InventoryPoint(listing.id,quantity,0,scheme)])
    with repo.db.connect() as c:
        c.execute("UPDATE inventory_snapshots SET captured_at=? WHERE source_run_id=?",
                  ((day or END.isoformat())+'T21:07:21+00:00',run))
    return run


@pytest.mark.parametrize('previously_active',[True,False])
def test_disappearing_fbs_never_reports_recovery(stock_shop,previously_active):
    repo,shop,conn,item=stock_shop
    put_stock(repo,conn,item,'FBS',130,day='2026-10-02')
    repo.record_success(conn.id,'analytics/stocks/seller-warehouses',END.isoformat(),{'data':{'items':[]}},[])
    if not previously_active:
        repo.save_alert_state(shop.id,'low_stock',SUBJECT,active=False,value=None,fingerprint=None,
                              resolved_at='2026-10-09T21:07:46+00:00')
    report=build_product_report(repo,shop.id,END)
    row=report.stock_risks[0]
    assert row.available_units==3 and row.days_left==pytest.approx(19.5)
    assert row.unconfirmed_schemes==('FBS',) and not row.inventory_confirmed
    notes=AlertEngine(repo).evaluate(shop.id,**KW)
    assert not any(n.rule_key=='low_stock' and n.severity=='resolved' for n in notes)
    unknown=next(n for n in notes if n.rule_key=='stock_unknown')
    assert 'FBW 3 шт.' in unknown.message and 'FBS: остаток не подтверждён' in unknown.message
    assert repo.get_alert_state(shop.id,'low_stock',SUBJECT)['active']==int(previously_active)
    assert repo.get_alert_state(shop.id,'stock_unknown',SUBJECT)['active']==1
    assert 'Получено по API (неполно)' in format_stock_report(report)
    assert 'FBS: остаток не подтверждён' in format_active_alerts(repo,shop.id,report)
    detail=format_alert_detail(repo.get_alert_state(shop.id,'stock_unknown',SUBJECT),report)
    assert 'Проверить остатки' in detail and 'Товар &lt;&amp;&gt;' in detail


def test_used_fbs_zero_stays_critical_even_when_fbw_has_stock(stock_shop):
    repo,shop,conn,item=stock_shop
    put_stock(repo,conn,item,'FBS',130,day='2026-10-02')
    put_stock(repo,conn,item,'FBS',0)
    notes=AlertEngine(repo).evaluate(shop.id,**KW)
    note=next(n for n in notes if n.rule_key=='low_stock')
    assert note.severity=='critical' and note.value==0
    assert 'нет остатка: FBS' in note.message and 'FBW 3 шт.' in note.message
    assert not any(n.rule_key=='low_stock' and n.severity=='resolved' for n in notes)
    # Escalation is sent immediately, but an unchanged critical issue respects cooldown.
    assert not any(n.rule_key=='low_stock' for n in AlertEngine(repo).evaluate(shop.id,**KW))


def test_recovery_identifies_forecast_and_current_schemes(stock_shop):
    repo,shop,conn,item=stock_shop
    notes=AlertEngine(repo).evaluate(shop.id,**KW)
    note=next(n for n in notes if n.rule_key=='low_stock' and n.severity=='resolved')
    assert 'Предупреждение по запасу снято' in note.message
    assert 'FBW 3 шт.' in note.message and 'прогноз 19.5 дн.' in note.message
    assert 'Восстановлено: остаток' not in note.message
    assert 'снято предупреждений: 1' in format_alert_digest([note])


def test_unused_zero_scheme_does_not_create_false_shortage(stock_shop):
    repo,shop,conn,item=stock_shop
    put_stock(repo,conn,item,'FBS',0)
    row=build_product_report(repo,shop.id,END).stock_risks[0]
    assert row.inventory_confirmed and row.out_of_stock_schemes==()
    assert any(n.rule_key=='low_stock' and n.severity=='resolved' for n in AlertEngine(repo).evaluate(shop.id,**KW))


def test_failed_stock_request_cannot_clear_existing_warning(stock_shop):
    repo,shop,conn,item=stock_shop
    put_stock(repo,conn,item,'FBS',5)
    repo.record_failure(conn.id,'analytics/stocks/seller-warehouses',END.isoformat(),'HTTP 500')
    notes=AlertEngine(repo).evaluate(shop.id,**KW)
    assert not any(n.rule_key=='low_stock' and n.severity=='resolved' for n in notes)
    assert any(n.rule_key=='stock_unknown' for n in notes)


def test_missing_orders_cannot_clear_stock_warning(stock_shop):
    repo,shop,conn,item=stock_shop
    with repo.db.connect() as c:
        c.execute("DELETE FROM product_metric_values")
        c.execute("DELETE FROM source_runs WHERE endpoint='analytics/orders'")
    notes=AlertEngine(repo).evaluate(shop.id,**KW)
    assert not any(n.rule_key=='low_stock' and n.severity=='resolved' for n in notes)


@pytest.mark.parametrize('body',[None,{}, {'data':{}}, {'data':{'items':None}}, {'items':{}}, {'items':[None]}])
def test_malformed_stock_payload_is_not_confirmed_empty(body):
    with pytest.raises(ProductNormalizationError):normalize_wb_stocks(body,fulfillment_scheme='FBS')


@pytest.mark.asyncio
@pytest.mark.parametrize('body',[None,{}, {'data':{}}, {'data':{'items':None}}, {'items':{}}])
async def test_stock_client_rejects_missing_items(body):
    client=WildberriesClient('test-'+repr(body),max_retries=0,
        transport=httpx.MockTransport(lambda request:httpx.Response(200,json=body)))
    try:
        assert not (await client.stock_report_all('seller')).ok
    finally:await client.close()


def test_explicit_zero_and_empty_are_valid_but_missing_is_not_zero():
    assert normalize_wb_stocks({'data':{'items':[]}},fulfillment_scheme='FBS')==[]
    zero=normalize_wb_stocks({'items':[{'nmId':1001,'quantity':0}]},fulfillment_scheme='FBS')
    assert len(zero)==1 and zero[0].available_units==0
