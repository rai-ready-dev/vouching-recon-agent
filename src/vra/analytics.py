"""Population-level analytics: patterns that no single entry shows (shown as insights, not exceptions)."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from decimal import Decimal

from .policy import Policy
from .records import Transaction
from .textutil import canon_name

BENFORD_CRITICAL_8DF_5PCT = 15.507  # chi-square critical value, 8 degrees of freedom, 5% level
MIN_BENFORD_N = 50


def benford(amounts: list[Decimal]) -> dict:
    digits = []
    for a in amounts:
        s = str(abs(a)).lstrip("0.").replace(".", "")
        if s and s[0].isdigit() and s[0] != "0":
            digits.append(int(s[0]))
    n = len(digits)
    expected = {d: math.log10(1 + 1 / d) for d in range(1, 10)}
    counts = Counter(digits)
    observed = {d: (counts[d] / n if n else 0.0) for d in range(1, 10)}
    chi2 = sum((counts[d] - expected[d] * n) ** 2 / (expected[d] * n) for d in range(1, 10)) if n else 0.0
    return {"n": n, "chi2": round(chi2, 2), "observed": {str(d): round(v, 3) for d, v in observed.items()},
            "expected": {str(d): round(v, 3) for d, v in expected.items()},
            "deviates": (chi2 > BENFORD_CRITICAL_8DF_5PCT) if n >= MIN_BENFORD_N else None,
            "note": ("First-digit pattern deviates from Benford's law (a prompt to look closer, not proof of anything)."
                     if n >= MIN_BENFORD_N and chi2 > BENFORD_CRITICAL_8DF_5PCT else
                     "First-digit pattern is consistent with Benford's law." if n >= MIN_BENFORD_N else
                     f"Too few entries ({n}) for a meaningful Benford test; at least {MIN_BENFORD_N} are needed.")}


def reference_gaps(refs: list[str], max_range: int = 5000, show: int = 25) -> list[dict]:
    """Missing numbers inside runs of sequentially numbered references (INV-1001, INV-1002, ...)."""
    groups: dict[str, list[int]] = defaultdict(list)
    for r in refs:
        m = re.fullmatch(r"\s*([A-Za-z\-_/ ]*?)[\-_/ ]?(\d{2,})\s*", r or "")
        if m:
            groups[m.group(1).upper().strip("-_/ ")].append(int(m.group(2)))
    out = []
    for prefix, nums in sorted(groups.items()):
        uniq = sorted(set(nums))
        if len(uniq) < 5 or uniq[-1] - uniq[0] > max_range:
            continue
        missing = sorted(set(range(uniq[0], uniq[-1] + 1)) - set(uniq))
        if missing:
            out.append({"prefix": prefix or "(numbers only)", "range": [uniq[0], uniq[-1]], "missing_count": len(missing),
                        "missing": missing[:show]})
    return out


def population_insights(txns: list[Transaction], policy: Policy) -> dict:
    n = len(txns)
    total = sum((t.amount for t in txns), Decimal(0))
    out: dict = {"entries": n, "benford": benford([t.amount for t in txns]), "reference_gaps": reference_gaps([t.reference for t in txns])}
    if not n:
        return out
    by_party: dict[str, Decimal] = defaultdict(Decimal)
    for t in txns:
        by_party[canon_name(t.counterparty) or "(no counterparty)"] += t.amount
    top = sorted(by_party.items(), key=lambda kv: -kv[1])[:5]
    out["concentration"] = [{"counterparty": k, "value": str(v), "share": round(float(v / total), 3) if total else 0.0} for k, v in top]
    out["round_amount_share"] = round(sum(1 for t in txns if t.amount >= 1000 and t.amount % 100 == 0) / n, 3)
    out["weekend_share"] = round(sum(1 for t in txns if t.date.weekday() >= 5) / n, 3)
    out["near_approval_limit"] = sum(1 for t in txns if policy.approval_limit * Decimal("0.9") <= t.amount < policy.approval_limit)
    clusters: dict[tuple[str, Decimal], list[str]] = defaultdict(list)
    for t in txns:
        clusters[(canon_name(t.counterparty), t.amount)].append(t.id)
    out["repeated_amounts"] = [{"counterparty": k[0], "amount": str(k[1]), "entries": ids}
                               for k, ids in clusters.items() if len(ids) >= 2 and k[0]][:20]
    return out
