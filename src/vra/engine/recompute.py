"""Recalculate invoice arithmetic from first principles."""
from __future__ import annotations

from decimal import Decimal

from ..findings import Finding, FindingCode, Severity, num, severity_for
from ..money import q2
from ..policy import Policy
from ..records import Invoice


def recompute_invoice(inv: Invoice, policy: Policy) -> list[Finding]:
    out: list[Finding] = []
    lines_sum = sum((l.amount for l in inv.lines), Decimal(0))
    if inv.lines and abs(lines_sum - inv.subtotal) > policy.rounding_tolerance:
        diff = abs(lines_sum - inv.subtotal)
        out.append(Finding(
            code=FindingCode.INVOICE_ARITHMETIC, severity=severity_for(diff, policy),
            subject_id=inv.evidence_id, amount_at_risk=q2(diff),
            message=(f"Line items recompute to {q2(lines_sum):,.2f} but the invoice states a "
                     f"subtotal of {q2(inv.subtotal):,.2f} (difference {q2(diff):,.2f})."),
            evidence_ids=(inv.evidence_id,), numbers=(num(inv, "subtotal"),)))
    expected_total = inv.subtotal + inv.tax
    if abs(expected_total - inv.total) > policy.rounding_tolerance:
        diff = abs(expected_total - inv.total)
        out.append(Finding(
            code=FindingCode.INVOICE_ARITHMETIC, severity=severity_for(diff, policy),
            subject_id=inv.evidence_id, amount_at_risk=q2(diff),
            message=(f"Subtotal plus tax is {q2(expected_total):,.2f} but the invoice total is "
                     f"{q2(inv.total):,.2f} (difference {q2(diff):,.2f})."),
            evidence_ids=(inv.evidence_id,), numbers=(num(inv, "total"), num(inv, "tax"))))
    return out
