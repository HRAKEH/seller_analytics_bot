from datetime import date
import pytest
from app.services.finance import normalize_wb_finance_report, normalize_ozon_accruals, normalize_wb_ad_stats
from app.storage import Database, Repository, MetricPoint
from app.services.alerts import AlertEngine


def test_wb_finance_normalization():
    day, pts=normalize_wb_finance_report({
        'dateFrom':'2026-09-01','dateTo':'2026-09-01','createDate':'2026-09-02T05:00:00',
        'retailAmountSum':'1000.50','forPaySum':'720','deliveryServiceSum':'-80',
        'paidStorageSum':'20','penaltySum':'5','bankPaymentSum':'600'}, 1)
    m={p.metric_key:p.value for p in pts}
    assert day=='2026-09-01' and m['financial_sales']==1000.5
    assert m['logistics']==80 and m['bank_payment']==600


def test_ozon_accrual_normalization_nested_fees():
    pts=normalize_ozon_accruals({'accruals':[{
        'accrued_category':'POSTING','total_amount':{'amount':'650','currency':'RUB'},
        'posting':{'products':[{'commission':{'seller_price':{'amount':'1000'},'sale_commission':{'amount':'-150'}},
                                'delivery':{'total_accrued':{'amount':'-100'}}}]},
        'item_fees':{'fees':[{'sku':'1','fees':[{'type_id':1,'accrued':{'amount':'-20'}}]}]},
        'non_item_fee':{'type_id':77,'accrued':{'amount':'-80'}},
    }]},1,'2026-09-01')
    m={p.metric_key:p.value for p in pts}
    assert m['financial_sales']==1000 and m['commission']==150 and m['logistics']==100
    assert m['services']==100 and m['marketplace_net']==650


def test_wb_ads_group_by_day():
    out=normalize_wb_ad_stats([{'days':[{'date':'2026-09-01','sum':100,'sum_price':500},{'date':'2026-09-02','sum':50,'sum_price':250}]}],1)
    assert {p.metric_key:p.value for p in out['2026-09-01']}['ad_spend']==100


def _repo(tmp_path):
    db=Database(tmp_path/'f.sqlite3'); assert db.initialize()>=3
    repo=Repository(db); seller=repo.ensure_seller(1); shop=repo.ensure_shop(seller.id)
    conn=repo.ensure_connection(shop.id,'ozon')
    return repo,shop,conn


def test_cost_estimate_and_finance_totals(tmp_path):
    repo,shop,conn=_repo(tmp_path)
    product=repo.ensure_product(shop.id,'x','X',cost_price=30); listing=repo.ensure_listing(product.id,conn.id,'100')
    run=repo.record_success(conn.id,'analytics/orders','2026-09-01',{'x':1},[MetricPoint(conn.id,'2026-09-01','ordered_units',3,'units')])
    from app.storage.models import ProductMetricPoint
    repo.save_product_metrics(run,[ProductMetricPoint(listing.id,'2026-09-01','ordered_units',3,'units')])
    repo.record_success(conn.id,'finance/accrual/by-day','2026-09-01',{'f':1},[MetricPoint(conn.id,'2026-09-01','marketplace_net',200,'RUB')])
    assert repo.financial_metric_totals(shop.id,'2026-09-01','2026-09-01')['ozon']['marketplace_net']==200
    assert repo.estimated_order_cogs(shop.id,'2026-09-01','2026-09-01')['ozon']['estimated_cost']==90


def test_alert_cooldown_deduplicates(tmp_path):
    repo,shop,conn=_repo(tmp_path)
    engine=AlertEngine(repo,cooldown_minutes=1440)
    # No successful order run => API stale. First evaluation notifies, second does not.
    kw=dict(shop_id=shop.id,today=date(2026,9,29),order_drop_pct=35,order_lookback_days=7,
            api_stale_hours=1,drr_pct=25,stock_risk_days=14,stock_velocity_days=14)
    first=engine.evaluate(**kw); second=engine.evaluate(**kw)
    assert any(n.rule_key=='api_stale' for n in first)
    assert not any(n.rule_key=='api_stale' for n in second)


def test_alert_cooldown_ignores_changing_message(tmp_path):
    from app.services.alerts import AlertNotification
    repo,shop,conn=_repo(tmp_path)
    engine=AlertEngine(repo,cooldown_minutes=1440)
    first=engine._emit(shop.id,AlertNotification('low_stock','ozon:sku-1','warning','📦 Товар: запас ≈ 4.0 дн.',4.0))
    second=engine._emit(shop.id,AlertNotification('low_stock','ozon:sku-1','warning','📦 Товар: запас ≈ 3.8 дн.',3.8))
    assert first is not None
    assert second is None
    assert repo.count('alert_events') == 1


def test_alert_resolution_is_emitted_once(tmp_path):
    from app.services.alerts import AlertNotification
    repo,shop,conn=_repo(tmp_path)
    engine=AlertEngine(repo,cooldown_minutes=1440)
    assert engine._emit(shop.id,AlertNotification('api_stale','ozon','critical','🔌 Ozon: stale',30)) is not None
    resolved=engine._resolve_missing(shop.id,set(),{'api_stale'})
    assert len(resolved)==1 and resolved[0].severity=='resolved'
    assert repo.active_alert_states(shop.id)==[]
    assert engine._resolve_missing(shop.id,set(),{'api_stale'})==[]
    assert repo.count('alert_events')==2
