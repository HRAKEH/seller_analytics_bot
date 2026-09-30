import pytest
from app.services.normalization import normalize_wb_orders, normalize_ozon_order_analytics, NormalizationError

def as_map(points):
    return {p.metric_key: p.value for p in points}

def test_wb_orders_keep_gross_cancellations_and_order_money_separate():
    rows = [
        {'date':'2026-09-28T10:00:00','lastChangeDate':'2026-09-28T10:05:00','isCancel':False,'priceWithDisc':1000},
        {'date':'2026-09-28T11:00:00','lastChangeDate':'2026-09-28T11:05:00','isCancel':True,'priceWithDisc':500},
        {'date':'2026-09-27T11:00:00','lastChangeDate':'2026-09-27T11:05:00','isCancel':False,'priceWithDisc':999},
    ]
    m = as_map(normalize_wb_orders(rows, 1, '2026-09-28'))
    assert m['ordered_units'] == 2
    assert m['cancellations_units'] == 1
    assert m['ordered_revenue'] == 1500

def test_ozon_order_analytics_maps_explicit_metrics():
    payload = {'result': {'data': [{'metrics': [7, 12345.67]}]}}
    m = as_map(normalize_ozon_order_analytics(payload, 1, '2026-09-28'))
    assert m['ordered_units'] == 7
    assert m['ordered_revenue'] == 12345.67

def test_ozon_invalid_shape_is_not_converted_to_zero():
    with pytest.raises(NormalizationError):
        normalize_ozon_order_analytics({'result': {}}, 1, '2026-09-28')

def test_split_ozon_range_uses_day_dimension():
    from app.services.normalization import split_ozon_daily_analytics
    payload={'result':{'data':[
        {'dimensions':[{'id':'2026-09-27'}],'metrics':[2,200]},
        {'dimensions':[{'value':'2026-09-28'}],'metrics':[3,450]},
    ]}}
    out=split_ozon_daily_analytics(payload,9)
    assert set(out)=={'2026-09-27','2026-09-28'}
    assert as_map(out['2026-09-28'])['ordered_units']==3
