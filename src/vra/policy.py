"""Policy: every threshold lives here (versioned config), never inside a prompt."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Policy:
    version: str = "default-1"
    # tolerances
    abs_tolerance: Decimal = Decimal("0.50")      # reconciliation amount tolerance
    pct_tolerance: Decimal = Decimal("0.005")     # unit-price tolerance (0.5%)
    rounding_tolerance: Decimal = Decimal("0.05") # invoice arithmetic tolerance
    # matching
    date_window_days: int = 5
    group_window_days: int = 15
    max_group: int = 5
    max_pool: int = 20
    fuzzy_threshold: float = 0.85
    vendor_threshold: float = 0.80
    # duplicates / controls
    duplicate_window_days: int = 14
    split_window_days: int = 7
    approval_limit: Decimal = Decimal("10000")
    # risk routing
    materiality: Decimal = Decimal("1000")
    trivial_amount: Decimal = Decimal("5.00")
    auto_clear_limit: Decimal = Decimal("5000")   # clean invoices above this are still sampled
    auto_clear_confidence: float = 0.85
    # document vouching
    min_extraction_confidence: float = 0.70
    link_threshold: float = 0.50
    link_date_window_days: int = 60
    # reconciling items
    outstanding_days: int = 30
    stale_days: int = 90

    def to_dict(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}

    @classmethod
    def from_file(cls, path: str | Path) -> "Policy":
        raw = json.loads(Path(path).read_text())
        kwargs = {}
        for f in fields(cls):
            if f.name in raw:
                kwargs[f.name] = Decimal(str(raw[f.name])) if f.type in (Decimal, "Decimal") else raw[f.name]
        return cls(**kwargs)
