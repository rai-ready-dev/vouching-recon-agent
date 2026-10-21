"""Result models and routing."""
from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

from .analysis import Analysis
from .findings import Finding, Severity
from .policy import Policy


class Disposition(str, Enum):
    AUTO_CLEARED = "auto_cleared"
    REVIEW = "review"
    ESCALATE = "escalate"


class CaseResult(BaseModel):
    case_id: str
    subject_id: str
    domain: str
    findings: list[Finding]
    analysis: Analysis | None = None
    verification_problems: list[str] = Field(default_factory=list)
    disposition: Disposition


class ClearedItem(BaseModel):
    subject_id: str
    amount: Decimal
    disposition: Disposition


class MatchRecord(BaseModel):
    left: list[str]
    right: list[str]
    rule: str
    confidence: float
    difference: Decimal
    disposition: Disposition


class RunReport(BaseModel):
    domain: str
    policy_version: str
    analyst: str
    population: int
    population_value: Decimal
    cleared: list[ClearedItem] = Field(default_factory=list)
    matches: list[MatchRecord] = Field(default_factory=list)
    cases: list[CaseResult] = Field(default_factory=list)
    aging: dict = Field(default_factory=dict)
    proof: dict | None = None
    # document vouching
    period: str = ""
    programmes: dict[str, list[str]] = Field(default_factory=dict)
    intake_issues: list[str] = Field(default_factory=list)
    unlinked_documents: list[str] = Field(default_factory=list)
    documents: list[dict] = Field(default_factory=list)

    def by_disposition(self) -> dict[str, int]:
        counts = {d.value: 0 for d in Disposition}
        for c in self.cleared:
            counts[c.disposition.value] += 1
        for c in self.cases:
            counts[c.disposition.value] += 1
        return counts


def route_case(findings: list[Finding], analysis: Analysis | None,
               problems: list[str], policy: Policy) -> Disposition:
    if problems or analysis is None:
        return Disposition.ESCALATE
    if any(f.severity == Severity.HIGH for f in findings):
        return Disposition.ESCALATE
    if analysis.classification.value == "possible_fraud_risk":
        return Disposition.ESCALATE
    exposure = sum((f.amount_at_risk for f in findings), Decimal(0))
    if all(f.severity == Severity.LOW for f in findings) and exposure <= policy.trivial_amount:
        return Disposition.AUTO_CLEARED
    return Disposition.REVIEW


def route_clean(amount: Decimal, policy: Policy) -> Disposition:
    """No exceptions: clear small items, still sample large ones."""
    return Disposition.AUTO_CLEARED if abs(amount) <= policy.auto_clear_limit else Disposition.REVIEW


def route_match(confidence: float, policy: Policy) -> Disposition:
    return Disposition.AUTO_CLEARED if confidence >= policy.auto_clear_confidence else Disposition.REVIEW
