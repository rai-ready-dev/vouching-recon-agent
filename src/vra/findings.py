"""Findings: the typed output of the deterministic engine."""
from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict

from .money import D
from .policy import Policy


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FindingCode(str, Enum):
    # vouching
    INVOICE_ARITHMETIC = "INVOICE_ARITHMETIC"
    NO_PO = "NO_PO"
    PO_VENDOR_MISMATCH = "PO_VENDOR_MISMATCH"
    ITEM_NOT_ON_PO = "ITEM_NOT_ON_PO"
    PRICE_VARIANCE = "PRICE_VARIANCE"
    QTY_OVER_PO = "QTY_OVER_PO"
    NO_GRN = "NO_GRN"
    QTY_OVER_RECEIVED = "QTY_OVER_RECEIVED"
    DUPLICATE_EXACT = "DUPLICATE_EXACT"
    DUPLICATE_FUZZY = "DUPLICATE_FUZZY"
    SPLIT_INVOICE = "SPLIT_INVOICE"
    # document vouching (any transaction type)
    NO_VOUCHER = "NO_VOUCHER"
    WRONG_DOC_TYPE = "WRONG_DOC_TYPE"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    COUNTERPARTY_MISMATCH = "COUNTERPARTY_MISMATCH"
    REFERENCE_MISMATCH = "REFERENCE_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    CUTOFF_ISSUE = "CUTOFF_ISSUE"
    DATE_MISMATCH = "DATE_MISMATCH"
    VOUCHER_REUSED = "VOUCHER_REUSED"
    MISSING_APPROVAL = "MISSING_APPROVAL"
    LOW_CONFIDENCE_EXTRACTION = "LOW_CONFIDENCE_EXTRACTION"
    FIELD_MISSING = "FIELD_MISSING"
    # reconciliation
    BANK_ITEM_NOT_IN_GL = "BANK_ITEM_NOT_IN_GL"
    GL_ITEM_NOT_CLEARED = "GL_ITEM_NOT_CLEARED"
    STALE_GL_ITEM = "STALE_GL_ITEM"
    TOLERANCE_DIFFERENCE = "TOLERANCE_DIFFERENCE"


class NumberRef(BaseModel):
    """A figure plus the exact evidence field it came from; checkable by the verifier."""

    model_config = ConfigDict(frozen=True)
    evidence_id: str
    path: str
    value: Decimal


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: FindingCode
    severity: Severity
    subject_id: str  # evidence id the case is about
    message: str
    amount_at_risk: Decimal = Decimal(0)
    evidence_ids: tuple[str, ...]
    numbers: tuple[NumberRef, ...] = ()


def num(record, path: str) -> NumberRef:
    """Build a NumberRef by reading the value straight off the record."""
    obj = record
    for part in path.split("."):
        obj = obj[int(part)] if part.isdigit() else getattr(obj, part)
    return NumberRef(evidence_id=record.evidence_id, path=path, value=D(obj))


def severity_for(amount: Decimal, policy: Policy) -> Severity:
    amount = abs(amount)
    if amount >= policy.materiality:
        return Severity.HIGH
    if amount <= policy.trivial_amount:
        return Severity.LOW
    return Severity.MEDIUM
