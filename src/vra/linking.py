"""Link each ledger entry to its best supporting document.

Scoring is deterministic and explainable: reference match, amount, counterparty
and date proximity each contribute. Reuse of one document by several ledger
entries is allowed here on purpose, so the vouching tests can flag it.
"""
from __future__ import annotations

from dataclasses import dataclass

from .policy import Policy
from .records import SourceFile, Transaction, Voucher
from .textutil import normalize_name, normalize_number, similarity


@dataclass(frozen=True)
class Link:
    txn_id: str
    voucher_id: str | None   # None if nothing cleared the threshold
    score: float
    reasons: tuple[str, ...]
    closest_id: str | None = None  # best sub-threshold candidate, shown to the reviewer


def score_pair(t: Transaction, v: Voucher, filename: str, p: Policy) -> tuple[float, tuple[str, ...]]:
    score, why = 0.0, []
    t_ref = normalize_number(t.reference)
    if t_ref:
        v_refs = {normalize_number(r) for r in v.references}
        if t_ref in v_refs or t_ref in normalize_number(filename):
            score += 0.6
            why.append("reference")
    if v.amount is not None and abs(v.amount - t.amount) <= p.abs_tolerance:
        score += 0.3
        why.append("amount")
    t_name = normalize_name(t.counterparty)
    if t_name and v.parties and max(similarity(t_name, normalize_name(x)) for x in v.parties) >= p.vendor_threshold:
        score += 0.2
        why.append("counterparty")
    if v.date and abs((v.date - t.date).days) <= p.link_date_window_days:
        score += 0.1
        why.append("date")
    return round(score, 3), tuple(why)


def link_documents(txns: list[Transaction], vouchers: list[Voucher],
                   sources: dict[str, SourceFile], policy: Policy) -> dict[str, Link]:
    out: dict[str, Link] = {}
    for t in txns:
        ranked = []
        for v in vouchers:
            s, why = score_pair(t, v, sources[v.source_doc].filename if v.source_doc in sources else v.source_doc, policy)
            ranked.append((-s, v.id, s, why))
        ranked.sort()
        if ranked and ranked[0][2] >= policy.link_threshold:
            _, vid, s, why = ranked[0]
            out[t.id] = Link(t.id, vid, s, why)
        else:
            closest = ranked[0] if ranked and ranked[0][2] >= 0.25 else None
            out[t.id] = Link(t.id, None, closest[2] if closest else 0.0, closest[3] if closest else (),
                             closest[1] if closest else None)
    return out
