"""Finite numeric values for imported marketplace observations."""
from __future__ import annotations
import math
from typing import Any


def finite_number(value: Any) -> float:
    if value is None or value == '':
        return 0.0
    if isinstance(value, dict):
        value = value.get('amount', value.get('value', 0))
    number = float(str(value).replace(' ', '').replace(',', '.'))
    if not math.isfinite(number):
        raise ValueError('Numeric value must be finite')
    return number
