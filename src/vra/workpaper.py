"""Draft audit workpaper (Markdown). Always a draft until a reviewer signs off."""
from __future__ import annotations

from .evidence import EvidenceStore
from .reports import Disposition, RunReport

_TITLE = {"vouching": "Purchase invoice vouching (three-way match)",
          "document_vouching": "Vouching of ledger entries to supporting documents",
          "bank_rec": "Bank reconciliation (bank statement to general ledger)"}
_PROCEDURE = {
    "vouching": [
        "Recomputed line extensions, subtotal and total for every invoice.",
        "Matched each invoice to its purchase order and goods receipts (price, quantity, vendor, item), "
        "tracking cumulative billing against each PO.",
        "Tested the full population for exact, near and split-invoice duplicates.",
    ],
    "document_vouching": [
        "Read every supporting document (PDF, scan, Excel, Word, text, email) and extracted key fields; "
        "each extracted value was verified to appear in the document.",
        "Linked each ledger entry to its supporting document by reference, amount, counterparty and date.",
        "Applied the vouching programme for each transaction type (existence, accuracy, counterparty, "
        "cut-off, authorisation, reuse of documents).",
        "Listed documents that matched no ledger entry (possible unrecorded items).",
    ],
    "bank_rec": [
        "Matched bank lines to GL cash entries: exact reference, amount and date window, tolerance, "
        "then one-to-many groupings.",
        "Aged unmatched GL items and classified unmatched bank items.",
        "Proved the reconciliation arithmetically where balances were supplied.",
    ],
}


def render_workpaper(report: RunReport, store: EvidenceStore | None = None, manifest=None) -> str:
    counts = report.by_disposition()
    out = [
        f"# Workpaper (DRAFT): {_TITLE[report.domain]}",
        "",
        f"- **Policy version:** {report.policy_version}",
        f"- **Reasoning layer:** {report.analyst}",
        f"- **Population:** {report.population} items, value {report.population_value:,.2f}",
        "- **Prepared by:** agent  |  **Reviewed by:** ____________  |  **Date:** ________",
        "",
        "## Procedure performed",
        *[f"{i}. {s}" for i, s in enumerate(_PROCEDURE[report.domain], 1)],
        "",
        "## Result summary",
        f"- Auto-cleared: **{counts['auto_cleared']}**",
        f"- For reviewer: **{counts['review']}**",
        f"- Escalated: **{counts['escalate']}**",
        "",
    ]
    if report.period:
        out[3:3] = [f"- **Audit period:** {report.period}"]
    if report.programmes:
        out += ["## Programmes applied", ""]
        out += [f"- **{k.replace('_', ' ')}:** {', '.join(v)}" for k, v in sorted(report.programmes.items())]
        out.append("")
    if report.documents:
        out += ["## Documents read", "", "| File | Type | Read by | SHA-256 |", "|---|---|---|---|"]
        out += [f"| {d['file']} | {d['kind']} | {d['method']} | `{d['sha256']}…` |" for d in report.documents]
        out.append("")
    if report.intake_issues:
        out += ["## Documents that could not be fully read (reviewer action required)", ""]
        out += [f"- {i}" for i in report.intake_issues]
        out.append("")
    if report.unlinked_documents:
        out += ["## Documents not linked to any ledger entry (possible unrecorded items)", ""]
        out += [f"- {d}" for d in report.unlinked_documents]
        out.append("")
    if report.proof:
        p = report.proof
        out += ["## Reconciliation proof", "",
                f"- Bank balance {p['bank_balance']}, GL balance {p['gl_balance']}",
                f"- Unmatched bank items {p['unmatched_bank_total']}, unmatched GL items {p['unmatched_gl_total']}",
                f"- Unexplained difference **{p['unexplained_difference']}** "
                f"({'reconciled' if p['reconciled'] else 'NOT reconciled'})", ""]
    if report.matches:
        out += ["## Matches", "", "| Bank | GL | Rule | Confidence | Difference | Disposition |",
                "|---|---|---|---|---|---|"]
        for m in report.matches:
            out.append(f"| {', '.join(m.left)} | {', '.join(m.right)} | {m.rule} | {m.confidence:.2f} "
                       f"| {m.difference:,.2f} | {m.disposition.value} |")
        out.append("")
    if report.aging:
        out += ["## Ageing of uncleared GL items", "", "| Bucket | Count | Total |", "|---|---|---|"]
        out += [f"| {k} | {v['count']} | {v['total']} |" for k, v in report.aging.items()]
        out.append("")

    out.append("## Exceptions")
    order = {Disposition.ESCALATE: 0, Disposition.REVIEW: 1, Disposition.AUTO_CLEARED: 2}
    for c in sorted(report.cases, key=lambda c: (order[c.disposition], c.subject_id)):
        out += ["", f"### {c.subject_id}  `{c.disposition.value.upper()}`"]
        for f in c.findings:
            out.append(f"- **{f.code.value}** ({f.severity.value}): {f.message}")
        if c.analysis:
            out += ["", f"> *{c.analysis.classification.value}* (confidence {c.analysis.confidence:.2f}, "
                        f"{c.analysis.analyst}): {c.analysis.suggested_action}"]
        for prob in c.verification_problems:
            out.append(f"- VERIFIER: {prob}")
        out.append(f"- Evidence: {', '.join(sorted({e for f in c.findings for e in f.evidence_ids}))}")
    if not report.cases:
        out.append("\nNo exceptions noted.")

    out += ["", "## Conclusion (draft)", "",
            (f"{counts['escalate'] + counts['review']} of {report.population} items require "
             "reviewer attention; remaining items were cleared by deterministic tests. "
             "This conclusion is a draft and is not final until signed off by a reviewer."),
            "", "## Evidence manifest", "", "| Evidence id | SHA-256 |", "|---|---|"]
    out += [f"| {r.id} | `{r.sha256[:16]}…` |" for r in (manifest if manifest is not None else store.manifest())]
    return "\n".join(out) + "\n"
