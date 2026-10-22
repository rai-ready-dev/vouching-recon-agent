"""Vouching programmes: which assertions to test for each kind of transaction.

A programme is a list of `{"test": name, ...params}`. Add a transaction type or
change a programme by editing `PROGRAMMES` or by loading a JSON override file,
no code change needed. Each test returns typed `Finding`s with checkable figures.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .findings import Finding, FindingCode, Severity, num, severity_for
from .linking import Link
from .money import q2
from .policy import Policy
from .records import Transaction, Voucher
from .records import SourceFile
from .textutil import canon_name, normalize_name, normalize_number, similarity


@dataclass
class Ctx:
    policy: Policy
    period: tuple | None = None                    # (start, end) of the audit period
    usage: dict[str, list[Transaction]] = field(default_factory=dict)  # voucher id -> ledger entries using it
    sources: dict[str, SourceFile] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)  # reviewer-approved name aliases


def _f(code, sev, t: Transaction, v: Voucher | None, msg: str, risk=Decimal(0), numbers=(), extra=()) -> Finding:
    ids = (t.evidence_id, *( [v.evidence_id] if v else []), *extra)
    return Finding(code=code, severity=sev, subject_id=t.evidence_id, message=msg,
                   amount_at_risk=q2(risk), evidence_ids=ids, numbers=tuple(numbers))


# ------------------------------------------------------------------ tests
def doc_type(t, v, link, ctx, allowed=()):
    if v.doc_type != "unknown" and allowed and v.doc_type not in allowed:
        return [_f(FindingCode.WRONG_DOC_TYPE, Severity.MEDIUM, t, v,
                   f"Supporting document is a {v.doc_type.replace('_', ' ')}; expected "
                   f"{' / '.join(a.replace('_', ' ') for a in allowed)} for a {t.type.replace('_', ' ')} entry.")]
    return []


def amount_agrees(t, v, link, ctx):
    if v.amount is None:
        return [_f(FindingCode.FIELD_MISSING, Severity.MEDIUM, t, v,
                   "No total amount could be read from the supporting document.")]
    diff = abs(t.amount - v.amount)
    if diff <= ctx.policy.abs_tolerance:
        return []
    return [_f(FindingCode.AMOUNT_MISMATCH, severity_for(diff, ctx.policy), t, v,
               f"Ledger amount {q2(t.amount):,.2f} differs from the document total {q2(v.amount):,.2f} "
               f"(difference {q2(diff):,.2f}).", diff, (num(t, "amount"), num(v, "amount")))]


def counterparty_agrees(t, v, link, ctx):
    if not t.counterparty:
        return []
    if not v.parties:
        return [_f(FindingCode.FIELD_MISSING, Severity.MEDIUM, t, v,
                   "No counterparty name could be read from the supporting document.")]
    want = canon_name(t.counterparty, ctx.aliases)
    best = max(similarity(want, canon_name(p, ctx.aliases)) for p in v.parties)
    if best >= ctx.policy.vendor_threshold:
        return []
    return [_f(FindingCode.COUNTERPARTY_MISMATCH, Severity.HIGH, t, v,
               f"Ledger counterparty '{t.counterparty}' is not named on the document "
               f"(document names: {', '.join(v.parties)}).", t.amount, (num(t, "amount"),))]


def reference_agrees(t, v, link, ctx):
    if not t.reference or v.method == "combined":  # a payment covering several invoices has its own reference
        return []
    if not v.references:
        return [_f(FindingCode.FIELD_MISSING, Severity.LOW, t, v,
                   f"No document number could be read to agree to ledger reference {t.reference}.")]
    want = normalize_number(t.reference)
    if any(want == normalize_number(r) for r in v.references):
        return []
    return [_f(FindingCode.REFERENCE_MISMATCH, Severity.MEDIUM, t, v,
               f"Ledger reference {t.reference} does not appear on the document "
               f"(document numbers: {', '.join(v.references)}).")]


def currency_agrees(t, v, link, ctx):
    if t.currency and v.currency and t.currency.upper() != v.currency.upper():
        return [_f(FindingCode.CURRENCY_MISMATCH, Severity.HIGH, t, v,
                   f"Ledger currency {t.currency} differs from document currency {v.currency}.",
                   t.amount, (num(t, "amount"),))]
    return []


def date_ok(t, v, link, ctx, max_days_after=3, max_days_before=120):
    if v.date is None:
        return [_f(FindingCode.FIELD_MISSING, Severity.MEDIUM, t, v, "No date could be read from the supporting document.")]
    if ctx.period:
        start, end = ctx.period
        if start <= t.date <= end and not (start <= v.date <= end):
            return [_f(FindingCode.CUTOFF_ISSUE, severity_for(t.amount, ctx.policy), t, v,
                       f"Posted on {t.date} (inside the period) but the document is dated {v.date} "
                       f"(outside {start} to {end}); possible cut-off error.", t.amount, (num(t, "amount"),))]
    delta = (v.date - t.date).days
    if delta > max_days_after:
        return [_f(FindingCode.DATE_MISMATCH, Severity.MEDIUM, t, v,
                   f"Document dated {v.date} is {delta} days after the posting date {t.date}.")]
    if -delta > max_days_before:
        return [_f(FindingCode.DATE_MISMATCH, Severity.LOW, t, v,
                   f"Document dated {v.date} is {-delta} days before the posting date {t.date}.")]
    return []


def voucher_not_reused(t, v, link, ctx):
    users = sorted(ctx.usage.get(v.id, []), key=lambda x: (x.date, x.id))
    if len(users) > 1 and users[0].id != t.id:
        first = users[0]
        if v.amount is not None and sum((u.amount for u in users), Decimal(0)) <= v.amount + ctx.policy.abs_tolerance:
            return []  # e.g. a statement or remittance that covers several entries whose total it equals
        return [_f(FindingCode.VOUCHER_REUSED, Severity.HIGH, t, v,
                   f"The same document ({v.source_doc}) already supports ledger entry {first.id} "
                   f"({q2(first.amount):,.2f}); this entry is {q2(t.amount):,.2f}.", t.amount,
                   (num(t, "amount"), num(first, "amount")), extra=(first.evidence_id,))]
    return []


def approval_present(t, v, link, ctx, limit=None):
    limit = Decimal(str(limit)) if limit is not None else ctx.policy.approval_limit
    if t.amount >= limit and not (t.approver or v.approvals):
        return [_f(FindingCode.MISSING_APPROVAL, Severity.HIGH, t, v,
                   f"Amount {q2(t.amount):,.2f} is at or above the approval limit of {q2(limit):,.2f} "
                   f"but no approver is recorded on the ledger or the document.", t.amount, (num(t, "amount"),))]
    return []


def extraction_quality(t, v, link, ctx):
    if v.confidence < ctx.policy.min_extraction_confidence or v.notes:
        why = "; ".join(v.notes) if v.notes else "document layout was hard to read"
        return [_f(FindingCode.LOW_CONFIDENCE_EXTRACTION, Severity.MEDIUM, t, v,
                   f"Extraction confidence is low ({v.confidence:.2f}): {why}. A person should read the document.")]
    return []


def doc_arithmetic(t, v, link, ctx):
    if v.subtotal is None or v.tax is None or v.amount is None:
        return []
    expected = v.subtotal + v.tax
    diff = abs(expected - v.amount)
    if diff <= max(ctx.policy.rounding_tolerance, Decimal("0.01")):
        return []
    return [_f(FindingCode.DOC_ARITHMETIC, severity_for(diff, ctx.policy), t, v,
               f"On the document, subtotal {q2(v.subtotal):,.2f} plus tax {q2(v.tax):,.2f} is {q2(expected):,.2f}, but the "
               f"stated total is {q2(v.amount):,.2f} (difference {q2(diff):,.2f}).", diff,
               (num(v, "subtotal"), num(v, "tax"), num(v, "amount")))]


def document_integrity(t, v, link, ctx):
    src = ctx.sources.get(v.source_doc)
    if not src or not src.flags:
        return []
    return [_f(FindingCode.DOC_TAMPER_SUSPECTED, Severity.MEDIUM, t, v,
               f"Possible alteration of {src.filename}: {'; '.join(src.flags)}. This can be innocent (a re-saved or signed "
               f"copy) but the original should be obtained.", extra=(src.evidence_id,))]


def risk_signals(t, v, link, ctx):
    """Optional low-severity analytics. They carry no amount at risk, so alone they never need a decision."""
    if not ctx.policy.risk_signals:
        return []
    out = []
    limit = ctx.policy.approval_limit
    if limit * Decimal("0.9") <= t.amount < limit:
        out.append(_f(FindingCode.JUST_BELOW_LIMIT, Severity.LOW, t, v,
                      f"Amount {q2(t.amount):,.2f} is just below the approval limit of {q2(limit):,.2f}.", numbers=(num(t, "amount"),)))
    if t.date.weekday() >= 5:
        out.append(_f(FindingCode.WEEKEND_POSTING, Severity.LOW, t, v,
                      f"Posted on a {t.date.strftime('%A')} ({t.date})."))
    if t.amount >= 5000 and t.amount % 1000 == 0:
        out.append(_f(FindingCode.ROUND_AMOUNT, Severity.LOW, t, v,
                      f"Amount {q2(t.amount):,.2f} is a round thousand.", numbers=(num(t, "amount"),)))
    return out


TESTS: dict[str, Callable] = {
    "doc_type": doc_type, "amount_agrees": amount_agrees, "counterparty_agrees": counterparty_agrees,
    "reference_agrees": reference_agrees, "currency_agrees": currency_agrees, "date_ok": date_ok,
    "voucher_not_reused": voucher_not_reused, "approval_present": approval_present,
    "extraction_quality": extraction_quality, "doc_arithmetic": doc_arithmetic,
    "document_integrity": document_integrity, "risk_signals": risk_signals,
}

_COMMON = [{"test": "amount_agrees"}, {"test": "counterparty_agrees"}, {"test": "currency_agrees"},
           {"test": "voucher_not_reused"}, {"test": "extraction_quality"}, {"test": "doc_arithmetic"},
           {"test": "document_integrity"}, {"test": "risk_signals"}]

PROGRAMMES: dict[str, list[dict]] = {
    "purchase": [{"test": "doc_type", "allowed": ["invoice", "receipt", "credit_note"]},
                 {"test": "reference_agrees"}, {"test": "date_ok", "max_days_after": 3, "max_days_before": 120},
                 {"test": "approval_present"}, *_COMMON],
    "sales": [{"test": "doc_type", "allowed": ["invoice", "receipt", "contract", "credit_note"]},
              {"test": "reference_agrees"}, {"test": "date_ok", "max_days_after": 3, "max_days_before": 120}, *_COMMON],
    "expense": [{"test": "doc_type", "allowed": ["receipt", "invoice", "expense_report"]},
                {"test": "date_ok", "max_days_after": 3, "max_days_before": 90},
                {"test": "approval_present", "limit": 500}, *_COMMON],
    "payroll": [{"test": "doc_type", "allowed": ["payslip", "statement"]},
                {"test": "date_ok", "max_days_after": 5, "max_days_before": 45}, *_COMMON],
    "fixed_asset": [{"test": "doc_type", "allowed": ["invoice", "contract", "purchase_order"]},
                    {"test": "reference_agrees"}, {"test": "date_ok", "max_days_after": 3, "max_days_before": 180},
                    {"test": "approval_present", "limit": 5000}, *_COMMON],
    "bank_payment": [{"test": "doc_type", "allowed": ["remittance", "bank_advice", "invoice", "receipt", "statement"]},
                     {"test": "reference_agrees"}, {"test": "date_ok", "max_days_after": 3, "max_days_before": 120}, *_COMMON],
    "other": [{"test": "date_ok", "max_days_after": 3, "max_days_before": 180}, *_COMMON],
}

# friendly spellings seen in real ledgers
TYPE_ALIASES = {"purchases": "purchase", "ap": "purchase", "payable": "purchase", "cost": "purchase",
                "sale": "sales", "revenue": "sales", "ar": "sales", "receivable": "sales",
                "expenses": "expense", "claim": "expense", "travel": "expense", "salary": "payroll",
                "wages": "payroll", "asset": "fixed_asset", "capex": "fixed_asset", "fa": "fixed_asset",
                "payment": "bank_payment", "bank": "bank_payment"}


def load_programmes(path: str | Path | None = None) -> dict[str, list[dict]]:
    progs = {k: [dict(s) for s in v] for k, v in PROGRAMMES.items()}
    if path:
        for k, steps in json.loads(Path(path).read_text()).items():
            for s in steps:
                if s["test"] not in TESTS:
                    raise ValueError(f"unknown test '{s['test']}' in programme '{k}' (known: {sorted(TESTS)})")
            progs[k] = steps
    return progs


def programme_for(txn_type: str, programmes: dict[str, list[dict]]) -> tuple[str, list[dict]]:
    key = TYPE_ALIASES.get(txn_type.lower(), txn_type.lower())
    return (key, programmes[key]) if key in programmes else ("other", programmes["other"])


def run_programme(t: Transaction, v: Voucher | None, link: Link, steps: list[dict], ctx: Ctx,
                  sources_note: str = "") -> list[Finding]:
    if v is None:
        sev = Severity.HIGH if t.amount >= ctx.policy.materiality else Severity.MEDIUM
        hint = (f" Closest document by content: {link.closest_id} (match score {link.score:.2f})."
                if link.closest_id else "")
        return [_f(FindingCode.NO_VOUCHER, sev, t, None,
                   f"No supporting document found for {t.type.replace('_', ' ')} entry {t.id} of "
                   f"{q2(t.amount):,.2f}.{hint}{sources_note}", t.amount, (num(t, "amount"),),
                   extra=((f"Voucher:{link.closest_id}",) if link.closest_id else ()))]
    out: list[Finding] = []
    if "ai-suggested" in link.reasons:
        out.append(_f(FindingCode.AI_SUGGESTED_LINK, Severity.MEDIUM, t, v,
                      f"The rules found no matching document; the AI suggested {v.source_doc}"
                      + (f" ({link.note})" if link.note else "") + ". Confirm that it really supports this entry.",
                      t.amount, (num(t, "amount"),)))
    for step in steps:
        params = {k: val for k, val in step.items() if k != "test"}
        out.extend(TESTS[step["test"]](t, v, link, ctx, **params))
    return out
