"""Workflows: deterministic engine -> reasoning layer -> verifier -> router.

The orchestrator owns control flow. The analyst never calls the shots: it is
handed a case, returns a proposal, and the verifier and router decide what
happens to it.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from .analysis import Analysis, CaseBundle
from .analysts import Analyst, RuleBasedAnalyst
from .audit_log import AuditLog
from .engine.bank_rec import reconcile_bank_to_gl
from .engine.duplicates import find_duplicates
from .engine.recompute import recompute_invoice
from .engine.three_way import three_way_match
from .evidence import EvidenceStore
from .findings import Finding
from .money import q2
from .policy import Policy
from .records import BankLine, GLEntry, GoodsReceipt, Invoice, PurchaseOrder
from .reports import (CaseResult, ClearedItem, Disposition, MatchRecord, RunReport,
                      route_case, route_clean, route_match)
from .verifier import verify_analysis


class _Base:
    def __init__(self, store: EvidenceStore, policy: Policy, analyst: Analyst, log: AuditLog) -> None:
        self.store, self.policy, self.analyst, self.log = store, policy, analyst, log
        self._fallback = RuleBasedAnalyst()

    def _case(self, domain: str, subject_id: str, findings: list[Finding]) -> CaseResult:
        ev_ids = sorted({e for f in findings for e in f.evidence_ids})
        bundle = CaseBundle(case_id=f"case:{subject_id}", subject_id=subject_id, domain=domain,
                            findings=findings, evidence={e: self.store.payload(e) for e in ev_ids})
        problems: list[str] = []
        try:
            analysis: Analysis | None = self.analyst.analyze(bundle)
            analysis.analyst = analysis.analyst or self.analyst.name
        except Exception as exc:  # model/network/parse failure -> never fail silently
            self.log.append("analyst_error", {"case": bundle.case_id, "analyst": self.analyst.name,
                                              "error": f"{type(exc).__name__}: {exc}"})
            analysis = self._fallback.analyze(bundle)
            problems.append(f"analyst '{self.analyst.name}' failed; baseline used: {type(exc).__name__}")
        problems += verify_analysis(analysis, bundle, self.store)
        disposition = route_case(findings, analysis, problems, self.policy)
        self.log.append("case_decided", {
            "case": bundle.case_id, "codes": [f.code.value for f in findings],
            "analyst": analysis.analyst, "classification": analysis.classification.value,
            "problems": problems, "disposition": disposition.value})
        return CaseResult(case_id=bundle.case_id, subject_id=subject_id, domain=domain,
                          findings=findings, analysis=analysis, verification_problems=problems,
                          disposition=disposition)

    def _start(self, kind: str, extra: dict) -> None:
        self.log.append("run_started", {"workflow": kind, "policy": self.policy.to_dict(),
                                        "analyst": self.analyst.name, **extra})
        self.log.append("evidence_loaded", {
            "records": len(self.store),
            "manifest": [r.model_dump() for r in self.store.manifest()]})

    def _finish(self, report: RunReport) -> RunReport:
        tampered = self.store.verify_integrity()
        self.log.append("run_completed", {"dispositions": report.by_disposition(),
                                          "integrity_failures": tampered})
        if tampered:
            raise RuntimeError(f"evidence integrity failure: {tampered}")
        return report


class VouchingWorkflow(_Base):
    def run(self, invoices: list[Invoice], pos: list[PurchaseOrder],
            grns: list[GoodsReceipt]) -> RunReport:
        for rec in (*pos, *grns, *invoices):
            self.store.add(rec, source=type(rec).__name__)
        self._start("vouching", {"invoices": len(invoices)})

        po_by_id = {p.id: p for p in pos}
        grns_by_po: dict[str, list[GoodsReceipt]] = defaultdict(list)
        for g in grns:
            grns_by_po[g.po_ref].append(g)

        findings: dict[str, list[Finding]] = defaultdict(list)
        for f in find_duplicates(invoices, self.policy):
            findings[f.subject_id].append(f)

        billed: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for inv in sorted(invoices, key=lambda i: (i.date, i.id)):
            po = po_by_id.get(inv.po_ref) if inv.po_ref else None
            fs = recompute_invoice(inv, self.policy)
            fs += three_way_match(inv, po, grns_by_po.get(po.id, []) if po else [],
                                  billed[po.id] if po else {}, self.policy)
            if po:
                for line in inv.lines:
                    billed[po.id][line.sku] += line.qty
            findings[inv.evidence_id].extend(fs)

        report = RunReport(domain="vouching", policy_version=self.policy.version,
                           analyst=self.analyst.name, population=len(invoices),
                           population_value=q2(sum((i.total for i in invoices), Decimal(0))))
        for inv in invoices:
            fs = findings.get(inv.evidence_id)
            if fs:
                report.cases.append(self._case("vouching", inv.evidence_id, fs))
            else:
                d = route_clean(inv.total, self.policy)
                report.cleared.append(ClearedItem(subject_id=inv.evidence_id, amount=inv.total, disposition=d))
                self.log.append("item_cleared", {"subject": inv.evidence_id, "disposition": d.value})
        return self._finish(report)


class ReconciliationWorkflow(_Base):
    def run(self, bank: list[BankLine], gl: list[GLEntry], as_of: date,
            bank_balance: Decimal | None = None, gl_balance: Decimal | None = None) -> RunReport:
        for rec in (*bank, *gl):
            self.store.add(rec, source=type(rec).__name__)
        self._start("bank_reconciliation", {"as_of": as_of.isoformat(), "bank": len(bank), "gl": len(gl)})

        result = reconcile_bank_to_gl(bank, gl, self.policy, as_of, bank_balance, gl_balance)
        report = RunReport(domain="bank_rec", policy_version=self.policy.version,
                           analyst=self.analyst.name, population=len(bank) + len(gl),
                           population_value=q2(sum((abs(b.amount) for b in bank), Decimal(0))),
                           aging=result.aging, proof=result.proof)
        for m in result.recon.matches:
            d = route_match(m.confidence, self.policy)
            report.matches.append(MatchRecord(left=list(m.left), right=list(m.right), rule=m.rule,
                                              confidence=m.confidence, difference=m.difference, disposition=d))
            self.log.append("match", {"left": m.left, "right": m.right, "rule": m.rule,
                                      "confidence": m.confidence, "disposition": d.value})
        by_subject: dict[str, list[Finding]] = defaultdict(list)
        for f in result.findings:
            by_subject[f.subject_id].append(f)
        for subject, fs in by_subject.items():
            report.cases.append(self._case("bank_rec", subject, fs))
        return self._finish(report)
