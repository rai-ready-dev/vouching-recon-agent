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


# ---------------------------------------------------------------- answer keys for any run
CLEAN_WORDS = {"", "clean", "ok", "none", "no", "pass", "passed", "cleared", "-"}


def parse_answer_key(text: str) -> dict[str, set[str]]:
    """CSV/plain text, one entry per line: `R12,AMOUNT_MISMATCH;DOC_ARITHMETIC` or `R13,CLEAN`.

    The first column is the ledger entry id (as shown in the app, e.g. R12); the second lists the findings
    that SHOULD be raised (separated by ; | or spaces), or CLEAN if nothing should be. A header row is ignored.
    """
    import csv
    import io
    import re

    key: dict[str, set[str]] = {}
    for row in csv.reader(io.StringIO(text.strip("\ufeff\n ") + "\n")):
        if not row or not row[0].strip() or row[0].strip().lower() in ("item", "id", "entry", "txn", "transaction"):
            continue
        item = row[0].strip()
        item = item if item.startswith("Transaction:") else f"Transaction:{item}"
        raw = ",".join(row[1:]).strip()
        codes = {c.upper() for c in re.split(r"[;|\s,]+", raw) if c} - {w.upper() for w in CLEAN_WORDS}
        key[item] = codes
    return key


def evaluate_items(expected: dict[str, set[str]], predicted: dict[str, set[str]]) -> dict:
    """Score predicted findings per entry against an answer key. Entries not in the key are ignored."""
    judged = {k: predicted.get(k) for k in expected}
    missing_entries = sorted(k for k, v in judged.items() if v is None)
    judged = {k: v for k, v in judged.items() if v is not None}
    tp = fp = fn = tn = 0
    per_code: dict[str, dict] = {}
    missed, false_alarms, wrong_code = [], [], []
    for k, got in judged.items():
        want = expected[k]
        positive, flagged = bool(want), bool(got)
        tp += positive and flagged
        fn += positive and not flagged
        fp += (not positive) and flagged
        tn += (not positive) and not flagged
        if positive and not flagged:
            missed.append({"item": k, "expected": sorted(want), "found": []})
        elif (not positive) and flagged:
            false_alarms.append({"item": k, "found": sorted(got)})
        elif positive and flagged and not want <= got:
            wrong_code.append({"item": k, "expected": sorted(want), "found": sorted(got)})
        for c in want | got:
            d = per_code.setdefault(c, {"tp": 0, "fp": 0, "fn": 0})
            d["tp"] += c in want and c in got
            d["fp"] += c in got and c not in want
            d["fn"] += c in want and c not in got

    def rate(a, b):
        return round(a / b, 3) if b else None

    for d in per_code.values():
        d["precision"], d["recall"] = rate(d["tp"], d["tp"] + d["fp"]), rate(d["tp"], d["tp"] + d["fn"])
    prec, rec = rate(tp, tp + fp), rate(tp, tp + fn)
    return {"entries_in_key": len(expected), "entries_judged": len(judged), "entries_not_in_run": missing_entries,
            "exception_detection": {"true_positive": tp, "false_negative": fn, "false_positive": fp, "true_negative": tn,
                                    "recall": rec, "precision": prec, "specificity": rate(tn, tn + fp),
                                    "f1": round(2 * prec * rec / (prec + rec), 3) if prec and rec else None},
            "by_finding": dict(sorted(per_code.items())), "missed": missed, "false_alarms": false_alarms,
            "found_but_different_finding": wrong_code}
