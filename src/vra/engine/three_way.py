"""Three-way match: Invoice vs Purchase Order vs Goods Receipt."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Mapping, Sequence

from ..findings import Finding, FindingCode, Severity, num, severity_for
from ..money import q2, within
from ..policy import Policy
from ..records import GoodsReceipt, Invoice, PurchaseOrder
from ..textutil import normalize_name, similarity


def three_way_match(
    inv: Invoice,
    po: PurchaseOrder | None,
    grns: Sequence[GoodsReceipt],
    billed_before: Mapping[str, Decimal],
    policy: Policy,
) -> list[Finding]:
    """`billed_before` = qty already invoiced per SKU against the same PO by earlier invoices."""
    out: list[Finding] = []
    eid = inv.evidence_id

    if po is None:
        sev = Severity.HIGH if inv.total >= policy.approval_limit else Severity.MEDIUM
        why = f"PO reference {inv.po_ref} was not found" if inv.po_ref else "No PO reference on the invoice"
        out.append(Finding(
            code=FindingCode.NO_PO, severity=sev, subject_id=eid, amount_at_risk=q2(inv.total),
            message=f"{why}; invoice total {q2(inv.total):,.2f} has no purchase authority.",
            evidence_ids=(eid,), numbers=(num(inv, "total"),)))
        return out

    po_id = po.evidence_id
    if similarity(normalize_name(inv.vendor), normalize_name(po.vendor)) < policy.vendor_threshold:
        out.append(Finding(
            code=FindingCode.PO_VENDOR_MISMATCH, severity=Severity.HIGH, subject_id=eid,
            amount_at_risk=q2(inv.total),
            message=f"Invoice vendor '{inv.vendor}' does not match PO vendor '{po.vendor}'.",
            evidence_ids=(eid, po_id), numbers=(num(inv, "total"),)))

    po_lines = {l.sku: (i, l) for i, l in enumerate(po.lines)}
    received: dict[str, Decimal] = defaultdict(Decimal)
    for g in grns:
        for l in g.lines:
            received[l.sku] += l.qty
    grn_ids = tuple(g.evidence_id for g in grns)

    if not grns:
        sev = Severity.HIGH if inv.total >= policy.approval_limit else Severity.MEDIUM
        out.append(Finding(
            code=FindingCode.NO_GRN, severity=sev, subject_id=eid, amount_at_risk=q2(inv.total),
            message=f"No goods receipt exists for {po.id}; goods/services are unconfirmed for "
                    f"invoice total {q2(inv.total):,.2f}.",
            evidence_ids=(eid, po_id), numbers=(num(inv, "total"),)))

    for i, line in enumerate(inv.lines):
        hit = po_lines.get(line.sku)
        if hit is None:
            out.append(Finding(
                code=FindingCode.ITEM_NOT_ON_PO, severity=Severity.HIGH, subject_id=eid,
                amount_at_risk=q2(line.amount),
                message=f"Item {line.sku} billed at {q2(line.amount):,.2f} is not on {po.id}.",
                evidence_ids=(eid, po_id), numbers=(num(inv, f"lines.{i}.amount"),)))
            continue
        pi, pl = hit

        if not within(line.unit_price, pl.unit_price, Decimal(0), policy.pct_tolerance):
            risk = abs(line.unit_price - pl.unit_price) * line.qty
            out.append(Finding(
                code=FindingCode.PRICE_VARIANCE, severity=severity_for(risk, policy),
                subject_id=eid, amount_at_risk=q2(risk),
                message=(f"{line.sku}: invoiced unit price {q2(line.unit_price):,.2f} vs PO price "
                         f"{q2(pl.unit_price):,.2f}; exposure {q2(risk):,.2f} on {line.qty} units."),
                evidence_ids=(eid, po_id),
                numbers=(num(inv, f"lines.{i}.unit_price"), num(po, f"lines.{pi}.unit_price"))))

        cumulative = billed_before.get(line.sku, Decimal(0)) + line.qty
        if cumulative > pl.qty:
            risk = (cumulative - pl.qty) * line.unit_price
            out.append(Finding(
                code=FindingCode.QTY_OVER_PO, severity=severity_for(risk, policy),
                subject_id=eid, amount_at_risk=q2(risk),
                message=(f"{line.sku}: cumulative billed quantity {cumulative} exceeds PO quantity "
                         f"{pl.qty}; exposure {q2(risk):,.2f}."),
                evidence_ids=(eid, po_id), numbers=(num(po, f"lines.{pi}.qty"),)))

        if grns and cumulative > received.get(line.sku, Decimal(0)):
            got = received.get(line.sku, Decimal(0))
            risk = (cumulative - got) * line.unit_price
            out.append(Finding(
                code=FindingCode.QTY_OVER_RECEIVED, severity=severity_for(risk, policy),
                subject_id=eid, amount_at_risk=q2(risk),
                message=(f"{line.sku}: cumulative billed quantity {cumulative} exceeds received "
                         f"quantity {got}; exposure {q2(risk):,.2f}."),
                evidence_ids=(eid, po_id, *grn_ids), numbers=()))
    return out
