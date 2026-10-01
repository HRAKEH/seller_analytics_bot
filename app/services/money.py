"""Exact decimal arithmetic at financial calculation boundaries.

SQLite's legacy REAL columns remain compatible; totals are rounded once to
kopecks before crossing that boundary. No schema/data rewrite is required.
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

KOPECK = Decimal('0.01')


def decimal_amount(value) -> Decimal:
    if isinstance(value, dict):
        value = value.get('amount')
    amount = Decimal(str(value if value not in (None, '') else 0))
    if not amount.is_finite():
        raise ValueError('Денежная сумма должна быть конечным числом')
    return amount


def money_value(value) -> float:
    return float(decimal_amount(value).quantize(KOPECK, rounding=ROUND_HALF_UP))


def money_sum(values: Iterable) -> float:
    return money_value(sum((decimal_amount(v) for v in values), Decimal(0)))
