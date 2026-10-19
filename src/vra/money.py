"""Exact money arithmetic. Floats never enter the calculation path."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def D(value) -> Decimal:
    """Coerce to Decimal. Floats go through str() so 0.1 stays 0.1."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def to_cents(value: Decimal) -> int:
    return int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))


def within(a: Decimal, b: Decimal, abs_tol: Decimal, pct_tol: Decimal = Decimal(0)) -> bool:
    """True if |a-b| is inside the larger of the absolute and percentage tolerance."""
    allowed = max(abs_tol, abs(b) * pct_tol)
    return abs(a - b) <= allowed
