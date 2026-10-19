"""Ageing of open items into buckets."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from ..money import q2
from .matching import Item

BUCKETS = ((30, "0-30"), (60, "31-60"), (90, "61-90"))
OVER = "90+"


def age_items(items: list[Item], as_of: date) -> dict[str, dict]:
    out = {label: {"count": 0, "total": Decimal(0), "ids": []} for _, label in BUCKETS}
    out[OVER] = {"count": 0, "total": Decimal(0), "ids": []}
    for it in items:
        days = (as_of - it.date).days
        label = next((lab for limit, lab in BUCKETS if days <= limit), OVER)
        out[label]["count"] += 1
        out[label]["total"] += it.amount
        out[label]["ids"].append(it.id)
    for v in out.values():
        v["total"] = str(q2(v["total"]))
    return out
