"""Canonical evidence record types (Invoice / PO / GRN / GL / Bank).

Every source system is mapped into these schemas on ingestion. They are frozen
so a record cannot be edited after it enters the evidence layer.
"""
from __future__ import annotations

from datetime import date
from datetime import date as Date  # alias: fields named `date` shadow the type inside class bodies
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Line(_Frozen):
    sku: str
    description: str = ""
    qty: Decimal
    unit_price: Decimal = Decimal(0)

    @property
    def amount(self) -> Decimal:
        return self.qty * self.unit_price


class _Record(_Frozen):
    id: str

    @property
    def evidence_id(self) -> str:
        return f"{type(self).__name__}:{self.id}"


class Invoice(_Record):
    vendor: str
    number: str
    date: date
    currency: str = "USD"
    lines: list[Line] = Field(default_factory=list)
    subtotal: Decimal
    tax: Decimal = Decimal(0)
    total: Decimal
    po_ref: str | None = None


class PurchaseOrder(_Record):
    vendor: str
    date: date
    currency: str = "USD"
    lines: list[Line] = Field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((l.amount for l in self.lines), Decimal(0))


class GoodsReceipt(_Record):
    po_ref: str
    date: date
    lines: list[Line] = Field(default_factory=list)


class GLEntry(_Record):
    account: str
    date: date
    amount: Decimal  # signed: receipts/debits positive, payments/credits negative
    reference: str = ""
    description: str = ""


class SourceFile(_Record):
    """A supporting document as received (identified by content hash)."""

    filename: str
    sha256: str
    kind: str          # pdf | image | xlsx | docx | txt | csv | eml
    pages: int = 1
    method: str = "text"  # text | ocr
    flags: list[str] = Field(default_factory=list)  # integrity observations (edited PDF, etc.)


class Transaction(_Record):
    """A ledger entry being vouched: the claim that needs supporting evidence."""

    type: str = "other"   # purchase | sales | expense | payroll | fixed_asset | bank_payment | other
    date: date
    amount: Decimal       # absolute recorded amount
    currency: str = ""
    counterparty: str = ""
    reference: str = ""
    account: str = ""
    description: str = ""
    approver: str = ""
    source: str = ""      # which source-of-truth file this entry came from


class Voucher(_Record):
    """Structured facts extracted from one supporting document."""

    source_doc: str       # SourceFile id
    doc_type: str = "unknown"
    date: Date | None = None
    amount: Decimal | None = None
    subtotal: Decimal | None = None   # amount before tax, when the document shows it
    tax: Decimal | None = None
    currency: str | None = None
    parties: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    approvals: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    method: str = "text"
    notes: list[str] = Field(default_factory=list)  # fields dropped as ungrounded, etc.


class BankLine(_Record):
    date: date
    amount: Decimal  # signed: deposits positive, withdrawals negative
    reference: str = ""
    description: str = ""
