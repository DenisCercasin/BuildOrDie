"""Deterministic money helpers. All amounts are SGD Decimals rounded to cents."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Iterable, Sequence

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def q(x: Decimal | int | str) -> Decimal:
    """Round to cents, half-up."""
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def total(xs: Iterable[Decimal]) -> Decimal:
    return q(sum(xs, ZERO))


def split_amount(amount: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """Split ``amount`` into cent amounts proportional to ``weights``.

    Uses the largest-remainder method so the parts always sum to exactly ``amount``.
    Ties go to the earlier index, so the result is deterministic.
    """
    n = len(weights)
    if n == 0:
        return []
    cents = int((q(amount) * 100).to_integral_value())
    w = [Decimal(x) for x in weights]
    wsum = sum(w, Decimal(0))
    if wsum <= 0:
        w = [Decimal(1)] * n
        wsum = Decimal(n)
    raw = [Decimal(cents) * x / wsum for x in w]
    floors = [int(r.to_integral_value(rounding=ROUND_DOWN)) for r in raw]
    remainder = cents - sum(floors)
    order = sorted(range(n), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[:remainder]:
        floors[i] += 1
    return [q(Decimal(c) / 100) for c in floors]


def fmt(x: Decimal) -> str:
    return f"S${q(x):,.2f}"
