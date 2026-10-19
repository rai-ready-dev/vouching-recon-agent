"""Multi-pass reconciliation engine.

Passes (most to least certain): exact reference+amount, amount within date
window, amount within tolerance, then one-to-many / many-to-one subset matching.
Each item is used at most once. All arithmetic is on Decimal / integer cents.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import combinations

from ..money import to_cents
from ..policy import Policy
from ..textutil import normalize_ref, similarity


@dataclass(frozen=True)
class Item:
    id: str
    amount: Decimal
    date: date
    ref: str = ""

    @property
    def ref_norm(self) -> str:
        return normalize_ref(self.ref)


@dataclass(frozen=True)
class Match:
    left: tuple[str, ...]
    right: tuple[str, ...]
    rule: str
    confidence: float
    difference: Decimal  # sum(left) - sum(right)


@dataclass
class ReconResult:
    matches: list[Match] = field(default_factory=list)
    unmatched_left: list[Item] = field(default_factory=list)
    unmatched_right: list[Item] = field(default_factory=list)


def _days(a: Item, b: Item) -> int:
    return abs((a.date - b.date).days)


class _Reconciler:
    def __init__(self, left: list[Item], right: list[Item], policy: Policy) -> None:
        self.p = policy
        self.left = {i.id: i for i in left}
        self.right = {i.id: i for i in right}
        self.matches: list[Match] = []

    # -- bookkeeping -------------------------------------------------------
    def _take(self, lids: tuple[str, ...], rids: tuple[str, ...], rule: str, conf: float) -> None:
        diff = sum((self.left[i].amount for i in lids), Decimal(0)) - sum(
            (self.right[i].amount for i in rids), Decimal(0))
        self.matches.append(Match(lids, rids, rule, round(conf, 3), diff))
        for i in lids:
            del self.left[i]
        for i in rids:
            del self.right[i]

    def _greedy(self, pairs: list[tuple], rule: str, conf_fn) -> None:
        """pairs: (sort_key..., left_id, right_id); first free pair wins."""
        for *_, lid, rid in sorted(pairs):
            if lid in self.left and rid in self.right:
                self._take((lid,), (rid,), rule, conf_fn(self.left[lid], self.right[rid]))

    # -- passes ------------------------------------------------------------
    def pass_exact_ref(self) -> None:
        pairs = []
        for l in self.left.values():
            if not l.ref_norm:
                continue
            for r in self.right.values():
                if r.ref_norm == l.ref_norm and r.amount == l.amount:
                    pairs.append((_days(l, r), l.id, r.id))
        self._greedy(pairs, "exact_ref_amount", lambda l, r: 1.0)

    def pass_amount_date(self) -> None:
        by_cents: dict[int, list[Item]] = {}
        for r in self.right.values():
            by_cents.setdefault(to_cents(r.amount), []).append(r)
        pairs = []
        for l in self.left.values():
            for r in by_cents.get(to_cents(l.amount), []):
                d = _days(l, r)
                if d <= self.p.date_window_days:
                    pairs.append((d, -similarity(l.ref_norm, r.ref_norm), l.id, r.id))
        self._greedy(pairs, "amount_date_window", lambda l, r: min(
            0.99, max(0.6, 0.95 - 0.02 * _days(l, r)) + (0.03 if similarity(l.ref_norm, r.ref_norm) >= 0.8 else 0)))

    def pass_tolerance(self) -> None:
        pairs = []
        for l in self.left.values():
            for r in self.right.values():
                d, gap = _days(l, r), abs(l.amount - r.amount)
                if d <= self.p.date_window_days and gap <= self.p.abs_tolerance:
                    pairs.append((gap, d, l.id, r.id))
        self._greedy(pairs, "amount_tolerance", lambda l, r: max(0.5, 0.8 - 0.02 * _days(l, r)))

    def _subset(self, target: Item, pool: list[Item]) -> tuple[Item, ...] | None:
        same_sign = [p for p in pool
                     if p.amount * target.amount > 0
                     and abs(p.amount) < abs(target.amount)
                     and _days(p, target) <= self.p.group_window_days]
        same_sign.sort(key=lambda p: (_days(p, target), p.id))
        same_sign = same_sign[: self.p.max_pool]
        goal = to_cents(target.amount)
        tol = to_cents(self.p.abs_tolerance)
        for k in range(2, self.p.max_group + 1):
            best, best_gap = None, None
            for combo in combinations(same_sign, k):
                gap = abs(sum(to_cents(c.amount) for c in combo) - goal)
                if gap <= tol and (best_gap is None or gap < best_gap):
                    best, best_gap = combo, gap
            if best:
                return best
        return None

    def pass_groups(self) -> None:
        for l in sorted(list(self.left.values()), key=lambda i: -abs(i.amount)):
            if l.id not in self.left:
                continue
            combo = self._subset(l, list(self.right.values()))
            if combo:
                self._take((l.id,), tuple(c.id for c in combo), "one_to_many",
                           max(0.5, 0.75 - 0.02 * (len(combo) - 2)))
        for r in sorted(list(self.right.values()), key=lambda i: -abs(i.amount)):
            if r.id not in self.right:
                continue
            combo = self._subset(r, list(self.left.values()))
            if combo:
                self._take(tuple(c.id for c in combo), (r.id,), "many_to_one",
                           max(0.5, 0.75 - 0.02 * (len(combo) - 2)))

    def run(self) -> ReconResult:
        self.pass_exact_ref()
        self.pass_amount_date()
        self.pass_tolerance()
        self.pass_groups()
        return ReconResult(self.matches, list(self.left.values()), list(self.right.values()))


def reconcile(left: list[Item], right: list[Item], policy: Policy) -> ReconResult:
    return _Reconciler(left, right, policy).run()
