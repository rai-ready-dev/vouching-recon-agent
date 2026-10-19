"""Score a run against an answer key. Recall on true exceptions matters most."""
from __future__ import annotations

from .reports import RunReport


def score(expected: dict[str, set[str]], report: RunReport) -> dict:
    flagged = {c.subject_id: {f.code.value for f in c.findings} for c in report.cases}
    expected_pairs = [(s, code) for s, codes in expected.items() for code in sorted(codes)]
    missed = [(s, code) for s, code in expected_pairs if code not in flagged.get(s, set())]
    unexpected = sorted(s for s in flagged if s not in expected)
    detected = len(expected_pairs) - len(missed)
    return {
        "expected_findings": len(expected_pairs),
        "detected": detected,
        "recall": detected / len(expected_pairs) if expected_pairs else 1.0,
        "missed": missed,
        "unexpected_flagged_subjects": unexpected,
        "subject_precision": (len(flagged) - len(unexpected)) / len(flagged) if flagged else 1.0,
    }
