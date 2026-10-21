"""Structured output the reasoning layer must produce."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .findings import Finding, NumberRef


class Classification(str, Enum):
    TIMING = "timing"
    PRICING = "pricing"
    QUANTITY = "quantity"
    MISSING_DOCUMENT = "missing_document"
    DUPLICATE = "duplicate"
    ARITHMETIC = "arithmetic"
    UNAUTHORISED_VENDOR_OR_ITEM = "unauthorised_vendor_or_item"
    BANK_ITEM_UNRECORDED = "bank_item_unrecorded"
    OUTSTANDING_ITEM = "outstanding_item"
    ROUNDING_OR_TOLERANCE = "rounding_or_tolerance"
    AMOUNT_DIFFERENCE = "amount_difference"
    CUTOFF = "cutoff"
    AUTHORISATION = "authorisation"
    DATA_QUALITY = "data_quality"
    POSSIBLE_FRAUD_RISK = "possible_fraud_risk"
    OTHER = "other"


class Claim(BaseModel):
    """One assertion in the explanation, tied to evidence and checkable figures."""

    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    numbers: list[NumberRef] = Field(default_factory=list)


class Analysis(BaseModel):
    case_id: str = ""
    classification: Classification
    explanation: str
    claims: list[Claim] = Field(default_factory=list)
    suggested_action: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    needs_human: bool = True
    analyst: str = ""  # set by the orchestrator, not by the model


class CaseBundle(BaseModel):
    """Everything the reasoning layer is allowed to see about one case."""

    case_id: str
    subject_id: str
    domain: str  # "vouching" | "bank_rec"
    findings: list[Finding]
    evidence: dict[str, dict]  # evidence_id -> record payload (UNTRUSTED document data)
