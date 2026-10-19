"""Verifier: the gate between the reasoning layer and the report.

An analysis is rejected (and the case escalated) if it cites evidence that does
not exist or is outside the case, quotes a figure that does not match the
record, introduces money figures found nowhere in the evidence or engine
output, or tries to relax a high-severity case.
"""
from __future__ import annotations

import re
from decimal import Decimal

from .analysis import Analysis, CaseBundle
from .evidence import EvidenceNotFound, EvidenceStore, numeric_leaves
from .findings import Severity
from .money import D, q2

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONEY = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+)")


def money_figures(text: str) -> set[Decimal]:
    """Decimal / thousands-separated figures in free text (bare integers are ignored)."""
    text = _ISO_DATE.sub(" ", text)
    return {abs(q2(Decimal(m.replace(",", "")))) for m in _MONEY.findall(text)}


def verify_analysis(analysis: Analysis, bundle: CaseBundle, store: EvidenceStore) -> list[str]:
    problems: list[str] = []
    case_evidence = set(bundle.evidence)

    if analysis.case_id != bundle.case_id:
        problems.append(f"analysis is for case '{analysis.case_id}', expected '{bundle.case_id}'")

    allowed: set[Decimal] = set()
    for f in bundle.findings:
        allowed.add(abs(q2(f.amount_at_risk)))
        allowed |= money_figures(f.message)
        allowed |= {abs(q2(n.value)) for n in f.numbers}
    for payload in bundle.evidence.values():
        allowed |= {abs(q2(v)) for v in numeric_leaves(payload)}

    for i, claim in enumerate(analysis.claims):
        if not claim.evidence_ids:
            problems.append(f"claim {i} cites no evidence")
        for eid in claim.evidence_ids:
            if eid not in store:
                problems.append(f"claim {i} cites unknown evidence '{eid}'")
            elif eid not in case_evidence:
                problems.append(f"claim {i} cites evidence '{eid}' outside this case")
        for ref in claim.numbers:
            try:
                actual = D(store.field(ref.evidence_id, ref.path))
            except (EvidenceNotFound, AttributeError, IndexError, ValueError):
                problems.append(f"claim {i} cites unresolvable {ref.evidence_id}.{ref.path}")
                continue
            if actual != ref.value:
                problems.append(
                    f"claim {i}: {ref.evidence_id}.{ref.path} is {actual}, analysis says {ref.value}")
            allowed.add(abs(q2(actual)))
        allowed |= money_figures(claim.text)

    for fig in money_figures(analysis.explanation + " " + analysis.suggested_action):
        if fig not in allowed:
            problems.append(f"figure {fig:,.2f} in the explanation is not supported by any evidence")

    worst = max((f.severity for f in bundle.findings),
                key=lambda s: {"low": 0, "medium": 1, "high": 2}[s.value])
    if worst != Severity.LOW and not analysis.needs_human:
        problems.append("analysis tries to clear a medium/high severity case without human review")
    return problems
