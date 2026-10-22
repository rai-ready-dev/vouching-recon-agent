"""Monetary-unit (dollar-unit) sampling: the audit-standard way to pick items from a population."""
from __future__ import annotations

import random
from decimal import Decimal


def mus_sample(items: list[tuple[str, Decimal]], interval: Decimal, seed: int = 1) -> set[str]:
    """Pick items so that every `interval` monetary units of the population is hit once.

    Items worth at least one interval are always picked. The result is deterministic for a given seed and
    item set, so a reviewer can reproduce which items were selected and why.
    """
    if interval <= 0:
        raise ValueError("sampling interval must be positive")
    next_hit = Decimal(str(random.Random(seed).random())) * interval
    chosen: set[str] = set()
    cum = Decimal(0)
    for item_id, amount in sorted(items, key=lambda x: x[0]):
        amount = abs(amount)
        lo, hi = cum, cum + amount
        if amount >= interval:
            chosen.add(item_id)
        while next_hit < hi:
            if next_hit >= lo:
                chosen.add(item_id)
            next_hit += interval
        cum = hi
    return chosen
