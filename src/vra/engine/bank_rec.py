"""Bank-to-GL reconciliation built on the matching engine."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..findings import Finding, FindingCode, Severity, num, severity_for
from ..money import q2
from ..policy import Policy
from ..records import BankLine, GLEntry
from .aging import age_items
from .matching import Item, ReconResult, reconcile


@dataclass
class BankRecResult:
    recon: ReconResult
    findings: list[Finding]
    aging: dict[str, dict]
    proof: dict | None


def reconcile_bank_to_gl(
    bank: list[BankLine], gl: list[GLEntry], policy: Policy, as_of: date,
    bank_balance: Decimal | None = None, gl_balance: Decimal | None = None,
) -> BankRecResult:
    b_items = [Item(b.evidence_id, b.amount, b.date, b.reference) for b in bank]
    g_items = [Item(g.evidence_id, g.amount, g.date, g.reference) for g in gl]
    recon = reconcile(b_items, g_items, policy)
    by_id = {r.evidence_id: r for r in (*bank, *gl)}
    findings: list[Finding] = []

    for it in recon.unmatched_left:  # on the bank, not in the books
        rec = by_id[it.id]
        findings.append(Finding(
            code=FindingCode.BANK_ITEM_NOT_IN_GL,
            severity=max(severity_for(it.amount, policy), Severity.MEDIUM, key=_rank),
            subject_id=it.id, amount_at_risk=q2(abs(it.amount)),
            message=(f"Bank line dated {it.date} for {q2(abs(it.amount)):,.2f} "
                     f"('{rec.description or rec.reference}') has no matching GL entry."),
            evidence_ids=(it.id,), numbers=(num(rec, "amount"),)))

    for it in recon.unmatched_right:  # in the books, not (yet) on the bank
        rec = by_id[it.id]
        age = (as_of - it.date).days
        if age > policy.stale_days:
            code, sev = FindingCode.STALE_GL_ITEM, Severity.HIGH
            note = f"is {age} days old (stale after {policy.stale_days}); investigate or reverse"
        elif age > policy.outstanding_days:
            code, sev = FindingCode.GL_ITEM_NOT_CLEARED, Severity.MEDIUM
            note = f"has been uncleared for {age} days"
        else:
            code, sev = FindingCode.GL_ITEM_NOT_CLEARED, Severity.LOW
            note = f"is {age} days old; likely timing (outstanding item), confirm clearing after period end"
        findings.append(Finding(
            code=code, severity=sev, subject_id=it.id, amount_at_risk=q2(abs(it.amount)),
            message=f"GL entry dated {it.date} for {q2(abs(it.amount)):,.2f} {note}.",
            evidence_ids=(it.id,), numbers=(num(rec, "amount"),)))

    for m in recon.matches:
        if m.difference != 0:
            subject = m.left[0]
            findings.append(Finding(
                code=FindingCode.TOLERANCE_DIFFERENCE, severity=Severity.LOW, subject_id=subject,
                amount_at_risk=q2(abs(m.difference)),
                message=(f"Matched within tolerance ({m.rule}); residual difference "
                         f"{q2(abs(m.difference)):,.2f}."),
                evidence_ids=(*m.left, *m.right)))

    aging = age_items(recon.unmatched_right, as_of)
    proof = None
    if bank_balance is not None and gl_balance is not None:
        unm_bank = sum((i.amount for i in recon.unmatched_left), Decimal(0))
        unm_gl = sum((i.amount for i in recon.unmatched_right), Decimal(0))
        unexplained = (bank_balance - gl_balance) - (unm_bank - unm_gl)
        proof = {
            "bank_balance": str(bank_balance), "gl_balance": str(gl_balance),
            "unmatched_bank_total": str(unm_bank), "unmatched_gl_total": str(unm_gl),
            "unexplained_difference": str(q2(unexplained)),
            "reconciled": abs(unexplained) <= policy.abs_tolerance * max(1, len(recon.matches)),
        }
    return BankRecResult(recon, findings, aging, proof)


def _rank(s: Severity) -> int:
    return {"low": 0, "medium": 1, "high": 2}[s.value]
