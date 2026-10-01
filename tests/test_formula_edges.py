from datetime import date, timedelta

import pytest

from app.storage import Database, Repository, MetricPoint, ProductMetricPoint, InventoryPoint
from app.services.collection import CollectionService
from app.services.product_analytics import ProductDayObservation, normalize_ozon_postings
from app.services.reconciliation import normalize_ozon_posting_events
from app.services.normalization import normalize_wb_orders, normalize_ozon_order_analytics, NormalizationError
from app.services.finance import normalize_ozon_accruals, FinanceNormalizationError
from app.services.advertising import normalize_ozon_ad_campaign_detail, AdvertisingNormalizationError
from app.services.alerts import AlertEngine, AlertNotification
from app.services.actions import _management_actions
from app.services.supply import build_supply_plan, evaluate_forecast_quality
from app.reports.management import build_management_report
from app.reports.finance import build_finance_report
from app.reports.sku_finance import build_sku_economics
from app.reports.products import build_product_report
from app.reports.formatter import format_stock_report

DAY='2026-09-28'
END=date.fromisoformat(DAY)


@pytest.fixture
def setup(tmp_path):
    db=Database(tmp_path/'edges.sqlite3'); db.initialize(); repo=Repository(db)
    seller=repo.ensure_seller(1); shop=repo.ensure_shop(seller.id)
    conn=repo.ensure_connection(shop.id,'ozon')
    product=repo.ensure_product(shop.id,'A','Item',cost_price=10)
    listing=repo.ensure_listing(product.id,conn.id,'100')
    return db,repo,shop,conn,product,listing


def order(repo,conn,listing,day,units,revenue,*,product_units=None,endpoint='analytics/orders',raw=None):
    run=repo.record_success(conn.id,endpoint,day,raw or {'units':units,'revenue':revenue},[
        MetricPoint(conn.id,day,'ordered_units',units,'units'),
        MetricPoint(conn.id,day,'ordered_revenue',revenue,'RUB')])
    if product_units is not None:
        repo.save_product_metrics(run,[ProductMetricPoint(listing.id,day,'ordered_units',product_units,'units'),
                                      ProductMetricPoint(listing.id,day,'ordered_revenue',revenue,'RUB')])
    return run


@pytest.mark.parametrize('last_endpoint',['analytics/orders','analytics/orders/backfill'])
def test_repeated_historical_response_becomes_latest(setup,last_endpoint):
    _,repo,shop,conn,_,listing=setup
    first=order(repo,conn,listing,DAY,1,100,product_units=1)
    order(repo,conn,listing,DAY,2,200,product_units=2)
    last=order(repo,conn,listing,DAY,1,100,product_units=1,endpoint=last_endpoint)
    assert last!=first
    assert repo.metrics_for_day(conn.id,DAY)['ordered_units']==1
    assert repo.product_metric_series(listing.id,DAY,DAY,'ordered_units')[DAY]==1
    assert build_management_report(repo,shop.id,END,1).sources[0].estimated_result==90


def test_unchanged_success_refreshes_freshness_without_duplicate_metrics(setup,monkeypatch):
    _,repo,_,conn,_,listing=setup
    import app.storage.repositories as module
    monkeypatch.setattr(module,'source_utcnow',lambda:'2026-09-28T00:00:00+00:00')
    first=order(repo,conn,listing,DAY,1,100)
    monkeypatch.setattr(module,'source_utcnow',lambda:'2026-09-29T00:00:00+00:00')
    assert order(repo,conn,listing,DAY,1,100)==first
    assert repo.last_successful_order_run(conn.id).finished_at=='2026-09-29T00:00:00+00:00'
    assert repo.count('metric_values')==2


def test_empty_order_refresh_clears_old_sku_and_preserves_posting_flow(setup):
    _,repo,shop,conn,_,listing=setup
    service=CollectionService(repo)
    run=order(repo,conn,listing,DAY,2,200)
    service._save_product_observations(shop_id=shop.id,connection_id=conn.id,marketplace='ozon',
        source_run_id=run,observations=[ProductDayObservation(DAY,'100','Item',ordered_units=2,ordered_revenue=200)])
    postings=repo.record_success(conn.id,'postings/fbo',DAY,{'postings':2},[])
    repo.save_product_metrics(postings,[ProductMetricPoint(listing.id,DAY,'fulfillment_units',2,'units','FBO')])
    empty=order(repo,conn,listing,DAY,0,0)
    service._save_product_observations(shop_id=shop.id,connection_id=conn.id,marketplace='ozon',source_run_id=empty,observations=[])
    assert repo.product_metric_series(listing.id,DAY,DAY,'ordered_units')[DAY]==0
    assert repo.product_metric_series(listing.id,DAY,DAY,'ordered_revenue')[DAY]==0
    assert repo.fulfillment_totals(shop.id,DAY,DAY)[0]['value']==2
    assert repo.estimated_order_cogs(shop.id,DAY,DAY)['ozon']['estimated_cost']==0


def test_partial_source_cannot_replace_full_order_snapshot(setup):
    _,repo,_,conn,_,_=setup
    run=repo.record_success(conn.id,'analytics/orders',DAY,{'partial':True},[],status='partial')
    with pytest.raises(ValueError,match='complete'):
        repo.save_product_metrics(run,[],replace_order_snapshot=True)
    assert repo.successful_order_dates(conn.id,DAY,DAY)==[]


def test_missing_product_rows_reduce_cost_coverage_and_hide_management_result(setup):
    _,repo,shop,conn,_,listing=setup
    order(repo,conn,listing,DAY,10,1000,product_units=5)
    report=build_management_report(repo,shop.id,END,1).sources[0]
    assert report.cogs_coverage_pct==50
    assert report.estimated_result is None
    assert build_finance_report(repo,shop.id,END,1).sources[0].cogs_coverage_pct==50


def test_daily_cogs_mismatch_cannot_cancel_out_in_period_total(setup):
    _,repo,shop,conn,_,listing=setup
    order(repo,conn,listing,'2026-09-27',1,100,product_units=2)
    order(repo,conn,listing,DAY,2,200,product_units=1)
    assert repo.estimated_order_cogs(shop.id,'2026-09-27',DAY)['ozon']['basis_complete'] is False
    assert build_management_report(repo,shop.id,END,2).sources[0].estimated_result is None


def test_explicit_zero_orders_support_expense_only_result_and_actions(setup):
    _,repo,shop,conn,_,listing=setup
    order(repo,conn,listing,DAY,0,0)
    repo.record_success(conn.id,'finance',DAY,{'expense':100},[MetricPoint(conn.id,DAY,'services',100,'RUB')])
    assert build_management_report(repo,shop.id,END,1).sources[0].estimated_result==-100
    assert _management_actions(repo,shop.id,END,1)[0].action_key=='management:negative:ozon'


def test_action_center_does_not_calculate_with_missing_cogs(setup):
    _,repo,shop,conn,_,listing=setup
    order(repo,conn,listing,DAY,5,100)
    repo.record_success(conn.id,'finance',DAY,{'expense':200},[MetricPoint(conn.id,DAY,'services',200,'RUB')])
    assert build_management_report(repo,shop.id,END,1).sources[0].estimated_result is None
    assert _management_actions(repo,shop.id,END,1)==[]


def test_sku_contribution_subtracts_acquiring(setup):
    _,repo,shop,conn,_,listing=setup
    order(repo,conn,listing,DAY,2,200,product_units=2)
    run=repo.record_success(conn.id,'finance',DAY,{'acquiring':15},[])
    repo.save_product_metrics(run,[ProductMetricPoint(listing.id,DAY,'acquiring',15,'RUB')])
    assert build_sku_economics(repo,shop.id,END,1).rows[0].contribution_after_known_expenses==165


def test_unknown_stock_demand_is_not_displayed_as_infinite_runway(setup):
    _,repo,shop,conn,_,listing=setup
    run=repo.record_success(conn.id,'product/info/stocks',DAY,{'stock':5},[])
    repo.save_inventory(run,[InventoryPoint(listing.id,5,0,'FBO')])
    text=format_stock_report(build_product_report(repo,shop.id,END))
    assert 'нет данных для оценки' in text
    assert '∞' not in text


def test_older_inventory_scheme_limits_total_freshness(setup):
    db,repo,shop,conn,_,listing=setup
    for scheme in ('FBO','FBS'):
        run=repo.record_success(conn.id,'stocks/'+scheme,DAY,{'scheme':scheme},[])
        repo.save_inventory(run,[InventoryPoint(listing.id,5,0,scheme)])
    with db.connect() as c:
        c.execute("UPDATE inventory_snapshots SET captured_at='2026-09-20T00:00:00+00:00' WHERE fulfillment_scheme='FBO'")
        c.execute("UPDATE inventory_snapshots SET captured_at='2026-09-28T00:00:00+00:00' WHERE fulfillment_scheme='FBS'")
    report=build_supply_plan(repo,shop.id,END,persist=False)
    assert report.rows[0].inventory_age_days==8
    assert report.rows[0].inventory_stale
    assert build_product_report(repo,shop.id,END).stock_risks[0].captured_at.startswith('2026-09-20')


def test_overdue_unreceived_inbound_does_not_reduce_order(setup):
    _,repo,shop,conn,product,listing=setup
    for i in range(28):
        day=(END-timedelta(days=i)).isoformat()
        order(repo,conn,listing,day,2,200,product_units=2)
    stock=repo.record_success(conn.id,'product/info/stocks',DAY,{'stock':0},[])
    repo.save_inventory(stock,[InventoryPoint(listing.id,0,0,'FBO')])
    before=build_supply_plan(repo,shop.id,END,persist=False).rows[0].recommended_order_units
    run=repo.record_success(conn.id,'inbound',DAY,{'inbound':1},[])
    repo.upsert_inbound_shipments(conn.id,run,'ozon',[{'external_supply_id':'overdue','status':'READY_TO_SUPPLY',
        'planned_at':'2026-09-20T00:00:00Z','items':[{'marketplace_sku':'100','planned_units':1000,'remaining_units':1000}]}])
    after=build_supply_plan(repo,shop.id,END,persist=False).rows[0]
    assert before>0 and after.recommended_order_units==before
    assert after.inbound_units==0


def test_forecast_bias_includes_zero_actual_horizons(setup):
    _,repo,shop,_,product,_=setup
    repo.save_forecast_quality(shop.id,[{'product_id':product.id,'as_of_date':f'2026-09-{i:02d}',
        'horizon_days':7,'predicted_units':10,'actual_units':actual} for i,actual in enumerate((10,10,0),1)],method_version='test')
    assert repo.forecast_bias_corrections(shop.id)[product.id]==pytest.approx(0.85)


def test_forecast_backtest_excludes_days_before_product_history(setup):
    _,repo,shop,conn,_,listing=setup
    for i in range(56):
        day=(END-timedelta(days=i)).isoformat()
        order(repo,conn,listing,day,1,100,product_units=1 if i<7 else None)
    assert evaluate_forecast_quality(repo,shop.id,END,persist=False).items==()


def test_missing_data_does_not_send_false_order_or_drr_recovery(setup):
    _,repo,shop,_,_,_=setup
    engine=AlertEngine(repo)
    for rule,subject in (('order_drop','shop'),('high_drr','ozon')):
        engine._emit(shop.id,AlertNotification(rule,subject,'warning','old alert',50))
    notes=engine.evaluate(shop.id,today=END+timedelta(days=1),order_drop_pct=35,order_lookback_days=7,
        api_stale_hours=26,drr_pct=25,stock_risk_days=14,stock_velocity_days=14)
    assert not any(n.severity=='resolved' and n.rule_key in {'order_drop','high_drr'} for n in notes)
    assert {s['rule_key'] for s in repo.active_alert_states(shop.id)}>={'order_drop','high_drr'}


@pytest.mark.parametrize('value',['NaN','inf','-inf','broken'])
def test_invalid_numbers_are_rejected_across_normalizers(value):
    with pytest.raises(NormalizationError):
        normalize_ozon_order_analytics({'result':{'totals':[1,value]}},1,DAY)
    with pytest.raises(FinanceNormalizationError):
        normalize_ozon_accruals({'accruals':[{'total_amount':value}]},1,DAY)
    with pytest.raises(AdvertisingNormalizationError):
        normalize_ozon_ad_campaign_detail({'rows':[{'date':DAY,'expense':value}]},1)


def test_malformed_ozon_rows_do_not_become_successful_zero_days():
    with pytest.raises(NormalizationError):
        normalize_ozon_order_analytics({'result':{'data':[{'metrics':[1]}]}},1,DAY)


def test_wb_money_fallback_matches_product_normalization():
    assert normalize_wb_orders([{'date':DAY,'totalPrice':100}],1,DAY)[1].value==100


def test_zero_posting_quantity_is_preserved_in_flow_and_reconciliation():
    payload={'postings':[{'posting_number':'P','created_at':DAY,'products':[{'sku':'100','quantity':0,'price':100}]}]}
    assert normalize_ozon_postings(payload,fulfillment_scheme='FBO')[0].fulfillment_units['FBO']==0
    event=normalize_ozon_posting_events(payload,fulfillment_scheme='FBO')[0]
    assert event.quantity==0 and event.gross_amount==0


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1])
def test_invalid_cost_is_rejected_before_storage(setup,value):
    _,repo,shop,_,product,_=setup
    with pytest.raises(ValueError): repo.set_product_cost(product.id,value,effective_date=DAY)
    with pytest.raises(ValueError): repo.ensure_product(shop.id,'invalid','Invalid',cost_price=value)
    assert repo.count('products')==1


def test_schema_14_migration_preserves_history_and_makes_backup(tmp_path):
    import sqlite3
    from app.storage.database import MIGRATIONS
    path=tmp_path/'old.sqlite3'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
        for version,fn in sorted(MIGRATIONS.items()):
            if version>14: break
            fn(c); c.execute("INSERT INTO schema_migrations VALUES(?,datetime('now'))",(version,))
    db=Database(path); repo=Repository(db)
    seller=repo.ensure_seller(1); shop=repo.ensure_shop(seller.id); conn=repo.ensure_connection(shop.id,'ozon')
    listing=repo.ensure_listing(repo.ensure_product(shop.id,'A','Item').id,conn.id,'100')
    order(repo,conn,listing,DAY,1,100)
    order(repo,conn,listing,DAY,2,200)
    assert db.initialize_safely(tmp_path/'backups')==15
    backups=list((tmp_path/'backups').glob('pre_migration_v14_*.sqlite3'))
    assert len(backups)==1 and Database(backups[0]).schema_version()==14
    assert repo.count('source_runs')==2 and repo.metrics_for_day(conn.id,DAY)['ordered_units']==2
    order(repo,conn,listing,DAY,1,100)
    assert repo.count('source_runs')==3 and db.quick_check()


def test_promo_uplift_is_not_applied_twice_to_forecast_baseline(setup,monkeypatch):
    _,repo,shop,conn,product,listing=setup
    import app.services.supply as module
    promos={(END-timedelta(days=i)).isoformat() for i in range(14)}
    for i in range(28):
        day=(END-timedelta(days=i)).isoformat(); units=6 if day in promos else 4
        order(repo,conn,listing,day,units,units*100,product_units=units)
    monkeypatch.setattr(module,'historical_promo_factor',lambda *args:1.5)
    monkeypatch.setattr(repo,'promotion_dates_by_product',lambda sid,start,end:{product.id:promos} if start<=DAY else {})
    row=build_supply_plan(repo,shop.id,END,persist=False).rows[0]
    assert row.promo_factor==1.5
    assert row.forecast_daily_units==pytest.approx(4)
    assert row.forecast_next_7_units==pytest.approx(28)


def test_wape_does_not_cancel_opposing_forecast_errors(setup,monkeypatch):
    _,repo,shop,conn,_,listing=setup
    import app.services.supply as module
    for i in range(56):
        units=3 if i<7 else 1 if i<14 else 2
        day=(END-timedelta(days=i)).isoformat()
        order(repo,conn,listing,day,units,units*100,product_units=units)
    monkeypatch.setattr(module,'_weighted_forecast',lambda values:2)
    monkeypatch.setattr(module,'_weekday_factors',lambda *args,**kwargs:{i:1 for i in range(7)})
    report=evaluate_forecast_quality(repo,shop.id,END,persist=False,samples=2)
    assert report.items[0].mae_units==pytest.approx(7)
    assert report.overall_wape_pct==pytest.approx(50)
    assert report.overall_bias_pct==pytest.approx(0)


@pytest.mark.parametrize('spend,sales,drr,roas',[(0,0,None,None),(0,100,0,None),(10,0,None,0),(20,100,20,5)])
def test_ad_ratios_zero_denominators(spend,sales,drr,roas):
    from app.reports.ads import AdRow
    row=AdRow('ozon','1','Campaign',spend,sales,0,0,0)
    assert row.drr==drr and row.roas==roas


def test_sku_missing_order_revenue_has_no_contribution(setup):
    _,repo,shop,conn,_,listing=setup
    run=repo.record_success(conn.id,'orders',DAY,{'units':2},[])
    repo.save_product_metrics(run,[ProductMetricPoint(listing.id,DAY,'ordered_units',2,'units')])
    row=build_sku_economics(repo,shop.id,END,1).rows[0]
    assert row.contribution_before_marketplace is None
    assert row.contribution_after_known_expenses is None


def test_finance_event_reordering_preserves_identity_and_multiplicity():
    from app.services.reconciliation import normalize_ozon_finance_events
    rows=[{'posting':{'posting_number':str(i),'products':[{'sku':'100','commission':{'seller_price':100*i}}]}}
          for i in (1,2)]
    original=normalize_ozon_finance_events({'accruals':rows+[rows[0]]},data_date=DAY)
    reordered=normalize_ozon_finance_events({'accruals':[rows[0],rows[0],rows[1]]},data_date=DAY)
    assert len({x.fingerprint for x in original})==3
    assert {x.fingerprint for x in original}=={x.fingerprint for x in reordered}


def test_ozon_finance_refresh_replaces_legacy_events_and_clears_empty_day(setup):
    from app.storage.models import CommerceEventPoint
    from app.services.reconciliation import normalize_ozon_finance_events
    _,repo,shop,conn,_,listing=setup
    payload={'accruals':[{'posting':{'posting_number':'P','products':[{'sku':'100','commission':{'seller_price':100}}]}}]}
    run=repo.record_success(conn.id,'finance/accrual/by-day',DAY,payload,[])
    repo.save_commerce_events(run,[CommerceEventPoint(listing.id,DAY,'finance','ozon_finance','legacy-position',gross_amount=100)])
    service=CollectionService(repo)
    service._save_commerce_observations(shop_id=shop.id,connection_id=conn.id,marketplace='ozon',source_run_id=run,
        observations=normalize_ozon_finance_events(payload,data_date=DAY),replace_finance_snapshot=True)
    assert repo.count('commerce_events')==1
    assert repo.reconciliation_summary(shop.id,DAY,DAY)[0]['finance_gross']==100
    empty=repo.record_success(conn.id,'finance/accrual/by-day',DAY,{'accruals':[]},[])
    repo.save_product_metrics(run,[ProductMetricPoint(listing.id,DAY,'commission',15,'RUB')])
    service._save_product_finance_observations(shop_id=shop.id,connection_id=conn.id,marketplace='ozon',source_run_id=empty,
        observations=[],replace_finance_snapshot=True)
    assert repo.sku_financial_totals(shop.id,DAY,DAY)[('ozon','100')]['commission']==0
    service._save_commerce_observations(shop_id=shop.id,connection_id=conn.id,marketplace='ozon',source_run_id=empty,
        observations=[],replace_finance_snapshot=True)
    assert repo.count('commerce_events')==0
