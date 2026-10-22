"""Link each ledger entry to its best supporting document(s).

Order of attack, cheapest and most certain first:
1. reviewer decisions (forced links);
2. a single document, scored on reference, amount, counterparty and date;
3. split payments: several documents from one counterparty that together add up to the entry;
4. optionally an AI adjudicator for what is left (it can only choose from real candidates, and anything it
   chooses is flagged for human confirmation).

Reuse of one document by several ledger entries is allowed here on purpose so the vouching tests can judge it.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import combinations
from typing import Protocol

from .policy import Policy
from .records import SourceFile, Transaction, Voucher
from .textutil import canon_name, normalize_number, similarity


@dataclass(frozen=True)
class Link:
    txn_id: str
    voucher_id: str | None   # None if nothing cleared the threshold
    score: float
    reasons: tuple[str, ...]
    closest_id: str | None = None  # best sub-threshold candidate, shown to the reviewer
    note: str = ""                 # e.g. the AI adjudicator's reason


class Adjudicator(Protocol):
    def choose(self, txn: Transaction, candidates: list[Voucher]) -> tuple[str | None, str]: ...


@dataclass
class LinkResult:
    links: dict[str, Link]
    synthetic: list[Voucher] = field(default_factory=list)          # combined vouchers for split payments
    combined: dict[str, list[str]] = field(default_factory=dict)    # synthetic id -> constituent voucher ids
    ai_calls: int = 0
    ai_links: int = 0
    ai_errors: int = 0


def score_pair(t: Transaction, v: Voucher, filename: str, p: Policy,
               aliases: dict[str, str] | None = None) -> tuple[float, tuple[str, ...]]:
    score, why = 0.0, []
    amount_ok = v.amount is not None and abs(v.amount - t.amount) <= p.abs_tolerance
    t_ref = normalize_number(t.reference)
    if t_ref:
        v_refs = {normalize_number(r) for r in v.references if r}
        if t_ref in v_refs or t_ref in normalize_number(filename):
            score += 0.6
            why.append("reference")
        elif amount_ok and v_refs and max(similarity(t_ref, r) for r in v_refs) >= 0.85:
            # a typo in the ledger or on the document, but the amount agrees. Never on its own:
            # consecutive invoice numbers differ by one digit all the time.
            score += 0.3
            why.append("similar reference")
    if amount_ok:
        score += 0.3
        why.append("amount")
    t_name = canon_name(t.counterparty, aliases)
    if t_name and v.parties and max(similarity(t_name, canon_name(x, aliases)) for x in v.parties) >= p.vendor_threshold:
        score += 0.2
        why.append("counterparty")
    if v.date and abs((v.date - t.date).days) <= p.link_date_window_days:
        score += 0.1
        why.append("date")
    return round(score, 3), tuple(why)


def _combined_voucher(parts: list[Voucher]) -> Voucher:
    ids = sorted(v.id for v in parts)
    digest = hashlib.sha1("|".join(ids).encode()).hexdigest()[:10]
    dates = [v.date for v in parts if v.date]
    parties, refs, appr = [], [], []
    for v in parts:
        parties += [x for x in v.parties if x not in parties]
        refs += [x for x in v.references if x not in refs]
        appr += [x for x in v.approvals if x not in appr]
    taxes = [v.tax for v in parts if v.tax is not None]
    return Voucher(
        id=f"combined#{digest}", source_doc=parts[0].source_doc, doc_type=parts[0].doc_type,
        date=max(dates) if dates else None, amount=sum((v.amount for v in parts), Decimal(0)),
        tax=sum(taxes, Decimal(0)) if taxes else None, currency=parts[0].currency, parties=parties,
        references=refs, approvals=appr, confidence=min(v.confidence for v in parts), method="combined")


def _find_combo(t: Transaction, pool: list[Voucher], p: Policy, aliases) -> tuple[Voucher, ...] | None:
    want = canon_name(t.counterparty, aliases)
    if not want:
        return None
    cands = [v for v in pool if v.amount is not None and 0 < v.amount < t.amount and v.parties
             and max(similarity(want, canon_name(x, aliases)) for x in v.parties) >= p.vendor_threshold
             and (v.date is None or abs((v.date - t.date).days) <= p.link_date_window_days)]
    cands.sort(key=lambda v: (abs((v.date - t.date).days) if v.date else 9999, v.id))
    cands = cands[:12]
    for k in range(2, p.combine_max_docs + 1):
        best, best_gap = None, None
        for combo in combinations(cands, k):
            gap = abs(sum((v.amount for v in combo), Decimal(0)) - t.amount)
            if gap <= p.abs_tolerance and (best_gap is None or gap < best_gap):
                best, best_gap = combo, gap
        if best:
            return best
    return None


def link_all(txns: list[Transaction], vouchers: list[Voucher], sources: dict[str, SourceFile], policy: Policy,
             forced: dict[str, str | None] | None = None, aliases: dict[str, str] | None = None,
             adjudicator: Adjudicator | None = None) -> LinkResult:
    """`forced` = reviewer decisions: txn id -> voucher id (or None = 'there is no document')."""
    res = LinkResult(links={})
    by_id = {v.id: v for v in vouchers}
    fname = lambda v: sources[v.source_doc].filename if v.source_doc in sources else v.source_doc
    pending: list[tuple[Transaction, list]] = []

    for t in txns:
        if forced and t.id in forced and (forced[t.id] is None or forced[t.id] in by_id):
            vid = forced[t.id]
            res.links[t.id] = Link(t.id, vid, 1.0 if vid else 0.0, ("manual link",) if vid else ("marked: no document",))
            continue
        ranked = sorted(((-s, v.id, s, why) for v in vouchers
                         for s, why in [score_pair(t, v, fname(v), policy, aliases)]))
        if ranked and ranked[0][2] >= policy.link_threshold:
            _, vid, s, why = ranked[0]
            res.links[t.id] = Link(t.id, vid, s, why)
        else:
            pending.append((t, ranked))

    # split payments: only documents that no single link already claimed
    claimed = {l.voucher_id for l in res.links.values() if l.voucher_id}
    free = [v for v in vouchers if v.id not in claimed]
    still: list[tuple[Transaction, list]] = []
    for t, ranked in pending:
        combo = _find_combo(t, free, policy, aliases) if policy.combine_max_docs >= 2 else None
        if combo:
            syn = _combined_voucher(list(combo))
            res.synthetic.append(syn)
            res.combined[syn.id] = [v.id for v in combo]
            res.links[t.id] = Link(t.id, syn.id, 0.9, ("split payment: " + " + ".join(v.references[0] if v.references else v.id for v in combo),))
            used = {v.id for v in combo}
            free = [v for v in free if v.id not in used]
        else:
            still.append((t, ranked))

    # AI adjudication for what is left
    for t, ranked in still:
        closest = ranked[0] if ranked and ranked[0][2] >= 0.25 else None
        link = Link(t.id, None, closest[2] if closest else 0.0, closest[3] if closest else (),
                    closest[1] if closest else None)
        if adjudicator is not None:
            claimed_now = {l.voucher_id for l in res.links.values() if l.voucher_id}
            cands = [by_id[r[1]] for r in ranked if r[2] >= 0.2 and r[1] not in claimed_now][:3]
            if cands:
                res.ai_calls += 1
                try:
                    choice, reason = adjudicator.choose(t, cands)
                except Exception:
                    res.ai_errors += 1
                    choice, reason = None, ""
                if choice and choice in {c.id for c in cands}:
                    res.ai_links += 1
                    link = Link(t.id, choice, 0.55, ("ai-suggested",), note=reason)
        res.links[t.id] = link
    return res


def link_documents(txns: list[Transaction], vouchers: list[Voucher], sources: dict[str, SourceFile],
                   policy: Policy, forced: dict[str, str | None] | None = None,
                   aliases: dict[str, str] | None = None) -> dict[str, Link]:
    return link_all(txns, vouchers, sources, policy, forced, aliases).links
