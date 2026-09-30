import pytest
from app.services.metrics import sum_comparable, definition

def test_ordered_units_are_the_cross_marketplace_main_metric():
    assert sum_comparable({'wb': 4, 'ozon': 8}, 'ordered_units') == 12

def test_order_money_is_not_combined_until_methodologies_match():
    assert sum_comparable({'wb': 100, 'ozon': 50}, 'ordered_revenue') is None
    assert sum_comparable({'wb': 100, 'ozon': 50}, 'revenue') is None

def test_unknown_metric_is_rejected():
    with pytest.raises(KeyError): definition('magic_profit')
