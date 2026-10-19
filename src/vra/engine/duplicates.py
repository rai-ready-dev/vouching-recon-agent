"""Duplicate and split-invoice detection."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from ..findings import Finding, FindingCode, Severity, num
from ..money import q2
from ..policy import Policy
from ..records import Invoice
from ..textutil import normalize_name, normalize_number, similarity


def _vendor_groups(invoices: list[Invoice]) -> dict[str, list[Invoice]]:
    groups: dict[str, list[Invoice]] = defaultdict(list)
    for inv in invoices:
        groups[normalize_name(inv.vendor)].append(inv)
    for g in groups.values():
        g.sort(key=lambda i: (i.date, i.id))
    return groups


def find_duplicates(invoices: list[Invoice], policy: Policy) -> list[Finding]:
    """Findings attach to the *later* invoice of each suspicious pair/group."""
    out: list[Finding] = []
    flagged_pairs: set[tuple[str, str]] = set()

    for group in _vendor_groups(invoices).values():
        for j, later in enumerate(group):
            for earlier in group[:j]:
                key = (earlier.id, later.id)
                n1, n2 = normalize_number(earlier.number), normalize_number(later.number)
                if n1 and n1 == n2:
                    flagged_pairs.add(key)
                    out.append(Finding(
                        code=FindingCode.DUPLICATE_EXACT, severity=Severity.HIGH,
                        subject_id=later.evidence_id, amount_at_risk=q2(later.total),
                        message=(f"Invoice number {later.number} from {later.vendor} was already "
                                 f"recorded as {earlier.id} (amount {q2(earlier.total):,.2f}); "
                                 f"this one is {q2(later.total):,.2f}."),
                        evidence_ids=(later.evidence_id, earlier.evidence_id),
                        numbers=(num(later, "total"), num(earlier, "total"))))
                elif (later.total == earlier.total
                      and abs((later.date - earlier.date).days) <= policy.duplicate_window_days
                      and (n1 == n2 or similarity(n1, n2) >= policy.fuzzy_threshold
                           or later.date == earlier.date)):
                    flagged_pairs.add(key)
                    out.append(Finding(
                        code=FindingCode.DUPLICATE_FUZZY, severity=Severity.HIGH,
                        subject_id=later.evidence_id, amount_at_risk=q2(later.total),
                        message=(f"Possible duplicate: {later.id} ({later.number}) has the same vendor "
                                 f"and amount {q2(later.total):,.2f} as {earlier.id} ({earlier.number}) "
                                 f"within {policy.duplicate_window_days} days."),
                        evidence_ids=(later.evidence_id, earlier.evidence_id),
                        numbers=(num(later, "total"), num(earlier, "total"))))

        # split invoices: several sub-limit invoices that together breach the approval limit
        seen: set[frozenset[str]] = set()
        for i, start in enumerate(group):
            if start.total >= policy.approval_limit:
                continue
            members, running = [start], start.total
            for nxt in group[i + 1:]:
                if (nxt.date - start.date).days > policy.split_window_days:
                    break
                if nxt.total >= policy.approval_limit:
                    continue
                members.append(nxt)
                running += nxt.total
                ids = frozenset(m.id for m in members)
                if running >= policy.approval_limit and len(members) >= 2 and ids not in seen:
                    seen.add(ids)
                    last = members[-1]
                    out.append(Finding(
                        code=FindingCode.SPLIT_INVOICE, severity=Severity.HIGH,
                        subject_id=last.evidence_id, amount_at_risk=q2(running),
                        message=(f"{len(members)} invoices from {last.vendor} within "
                                 f"{policy.split_window_days} days each fall below the approval limit of "
                                 f"{policy.approval_limit:,.2f} but total {q2(running):,.2f}."),
                        evidence_ids=tuple(m.evidence_id for m in members),
                        numbers=tuple(num(m, "total") for m in members)))
                    break
    return out
