"""Canonical evidence record types (Invoice / PO / GRN / GL / Bank).

Every source system is mapped into these schemas on ingestion. They are frozen
so a record cannot be edited after it enters the evidence layer.
"""
from __future__ import annotations

from datetime import date
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


class BankLine(_Record):
    date: date
    amount: Decimal  # signed: deposits positive, withdrawals negative
    reference: str = ""
    description: str = ""
