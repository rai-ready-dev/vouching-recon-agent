"""Background execution of a vouching run, with caching of expensive model calls."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..analysts import ChatAnalyst, RuleBasedAnalyst
from ..audit_log import AuditLog
from ..evidence import EvidenceStore
from ..extract import HeuristicExtractor, LLMExtractor
from ..intake import build_vouchers
from ..ledger import load_transactions_with_problems
from ..orchestrator import DocumentVouchingWorkflow
from ..policy import Policy
from ..programmes import load_programmes
from ..records import Voucher
from .db import now
from .service import STATUS_ACTIONS, Service

log = logging.getLogger("vra.web")


class CachedExtractor:
    """Re-uses earlier extractions of byte-identical documents (model calls cost time and money)."""

    def __init__(self, inner, service: Service, tag: str) -> None:
        self.inner, self.svc, self.tag = inner, service, tag
        self.name = inner.name

    def extract(self, doc):
        key = f"{self.inner.name}|{self.tag}"
        row = self.svc.db.one("SELECT data_json FROM extraction_cache WHERE sha256=? AND extractor=?", (doc.sha256, key))
        if row:
            out = []
            for n, d in enumerate(json.loads(row["data_json"]), 1):
                d.update(id=f"{doc.id}#{n}", source_doc=doc.id)
                out.append(Voucher.model_validate(d))
            return out
        vouchers = self.inner.extract(doc)
        with self.svc.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO extraction_cache(sha256, extractor, data_json, created_at) VALUES(?,?,?,?)",
                      (doc.sha256, key, json.dumps([v.model_dump(mode="json") for v in vouchers]), now()))
        return vouchers


class CachedOcr:
    def __init__(self, inner, service: Service) -> None:
        self.inner, self.svc = inner, service
        self.name = inner.name

    def ocr(self, doc) -> str:
        row = self.svc.db.one("SELECT text FROM ocr_cache WHERE sha256=? AND engine=?", (doc.sha256, self.inner.name))
        if row:
            return row["text"]
        text = self.inner.ocr(doc)
        if text.strip():
            with self.svc.db.tx() as c:
                c.execute("INSERT OR REPLACE INTO ocr_cache(sha256, engine, text, created_at) VALUES(?,?,?,?)",
                          (doc.sha256, self.inner.name, text, now()))
        return text


def _sig(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:24]


class Runner:
    def __init__(self, service: Service, workers: int = 2) -> None:
        self.svc = service
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vra-run")

    def recover(self) -> None:
        """Runs left 'running' by a previous process can never finish; mark them failed."""
        with self.svc.db.tx() as c:
            c.execute("UPDATE runs SET status='failed', error='Interrupted because the server restarted. Start a new run.',"
                      " finished_at=? WHERE status IN ('queued','running')", (now(),))

    def submit(self, rid: int) -> None:
        self.pool.submit(self._execute, rid)

    def _set(self, rid: int, **cols) -> None:
        sets = ", ".join(f"{k}=?" for k in cols)
        with self.svc.db.tx() as c:
            c.execute(f"UPDATE runs SET {sets} WHERE id=?", (*cols.values(), rid))

    def _execute(self, rid: int) -> None:
        try:
            self._set(rid, status="running", progress="Starting")
            self._pipeline(rid)
        except Exception as exc:  # shown to the user; the traceback goes to the server log
            log.exception("run %s failed", rid)
            msg = str(exc) or type(exc).__name__
            self._set(rid, status="failed", error=f"{type(exc).__name__}: {msg}"[:1500], finished_at=now())
            self.svc.log(None, "run_failed", msg[:300], self.svc.run_row(rid)["engagement_id"], rid)

    def _pipeline(self, rid: int) -> None:
        svc = self.svc
        t0 = time.time()
        run = svc.run_row(rid)
        eid = run["engagement_id"]
        eng = svc.engagement(eid)
        opts = json.loads(run["options_json"])
        workdir = svc.settings.runs_dir / str(rid)
        docs_dir = workdir / "docs"
        if workdir.exists():
            shutil.rmtree(workdir)
        docs_dir.mkdir(parents=True)

        # snapshot the exact files this run uses
        ledger_path = None
        for f in svc.run_files(rid):
            blob = svc.blob_path(eid, f["sha256"])
            if f["kind"] == "ledger":
                ledger_path = workdir / Path(f["filename"]).name
                shutil.copyfile(blob, ledger_path)
            else:
                dest = docs_dir / f["filename"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    dest.hardlink_to(blob)
                except OSError:
                    shutil.copyfile(blob, dest)

        # model stack (one shared Foundry client if any part uses it)
        chat = None
        if "foundry" in (opts["extractor"], opts["analyst"], opts["ocr"]):
            from ..llm import FoundryChat, FoundryVisionOcr

            chat = FoundryChat()
        dayfirst = opts["dayfirst"]
        ocr, ocr_name = None, "none"
        if opts["ocr"] == "foundry":
            ocr = CachedOcr(FoundryVisionOcr(chat), svc)
            ocr_name = ocr.name
        inner = LLMExtractor(chat, dayfirst=dayfirst) if opts["extractor"] == "foundry" else HeuristicExtractor(dayfirst)
        extractor = CachedExtractor(inner, svc, f"ocr={ocr_name}|dayfirst={dayfirst}")
        analyst = ChatAnalyst(chat) if opts["analyst"] == "foundry" else RuleBasedAnalyst()

        last = [0.0]

        def progress(n, total, name):
            if time.time() - last[0] > 0.4:
                last[0] = time.time()
                self._set(rid, progress=f"Reading documents {n + 1}/{total}: {name}"[:200])

        intake = build_vouchers(docs_dir, extractor, ocr, progress)
        self._set(rid, progress="Applying reviewer corrections")

        # reviewer corrections to extracted fields
        ov = svc.overrides(eid)
        originals = {v.id: v.model_dump(mode="json") for v in intake.vouchers}
        vouchers: list[Voucher] = []
        for v in intake.vouchers:
            fields = ov["vouchers"].get(v.id)
            if fields:
                upd = {}
                for k, val in fields.items():
                    if k == "_note":
                        continue
                    if k in ("amount", "tax"):
                        upd[k] = Decimal(val) if val not in (None, "") else None
                    elif k == "date":
                        upd[k] = date.fromisoformat(val) if val else None
                    else:
                        upd[k] = val
                v = v.model_copy(update={**upd, "confidence": 1.0, "notes": [], "method": "reviewer-corrected"})
            vouchers.append(v)

        txns, ledger_problems = load_transactions_with_problems(ledger_path, eng["default_type"], dayfirst)
        issues = intake.issues + ledger_problems

        policy = dataclasses.replace(Policy(), **{k: Decimal(v) for k, v in opts["policy"].items()}) if opts["policy"] else Policy()
        programmes = None
        if opts["programmes_json"]:
            p = workdir / "programmes.json"
            p.write_text(opts["programmes_json"])
            programmes = load_programmes(p)
        period = None
        if eng["period_start"] and eng["period_end"]:
            period = (date.fromisoformat(eng["period_start"]), date.fromisoformat(eng["period_end"]))
        prefix = "Transaction:"  # the UI addresses items by evidence id; the engine by ledger id
        forced = {(t[len(prefix):] if t.startswith(prefix) else t): l["voucher_id"] for t, l in ov["links"].items()}

        store, audit = EvidenceStore(), AuditLog(workdir / "audit_log.jsonl")
        wf = DocumentVouchingWorkflow(store, policy, analyst, audit)
        last2 = [0.0]

        def on_progress(n, total):
            if time.time() - last2[0] > 0.5:
                last2[0] = time.time()
                self._set(rid, progress=f"Testing ledger entry {n + 1}/{total}")

        wf.on_progress = on_progress
        self._set(rid, progress=f"Testing {len(txns)} ledger entries")
        report = wf.run(txns, vouchers, intake.sources, period, programmes, issues, forced)
        self._set(rid, progress="Saving results")

        # ------------------------------------------------------------ persist
        by_vid = {v.id: v for v in vouchers}
        src_by_id = {s.id: s for s in intake.sources}
        cases = {c.subject_id: c for c in report.cases}
        cleared = {c.subject_id: c for c in report.cleared}
        linked_by_voucher: dict[str, list[str]] = {}
        sev_rank = {"low": 1, "medium": 2, "high": 3}
        item_rows = []
        for t in txns:
            link = wf.links[t.id]
            if link.voucher_id:
                linked_by_voucher.setdefault(link.voucher_id, []).append(t.evidence_id)
            v = by_vid.get(link.voucher_id) if link.voucher_id else None
            src = src_by_id.get(v.source_doc) if v else None
            case, clr = cases.get(t.evidence_id), cleared.get(t.evidence_id)
            findings = [f.model_dump(mode="json") for f in case.findings] if case else []
            disp = (case or clr).disposition.value
            sev = max((f["severity"] for f in findings), key=lambda s: sev_rank[s], default="none")
            exposure = float(sum(Decimal(f["amount_at_risk"]) for f in findings))
            closest = by_vid.get(link.closest_id) if link.closest_id else None
            data = {
                "txn_id": t.evidence_id, "kind": "case" if case else "cleared", "disposition": disp,
                "transaction": t.model_dump(mode="json"),
                "link": {"voucher_id": link.voucher_id, "score": link.score, "reasons": list(link.reasons),
                         "closest_id": link.closest_id, "manual": "manual link" in link.reasons,
                         "marked_no_document": "marked: no document" in link.reasons},
                "voucher": v.model_dump(mode="json") if v else None,
                "source_file": ({"filename": src.filename, "kind": src.kind, "method": src.method} if src else None),
                "closest_file": (src_by_id[closest.source_doc].filename
                                 if closest and closest.source_doc in src_by_id else None),
                "findings": findings,
                "analysis": case.analysis.model_dump(mode="json") if case and case.analysis else None,
                "problems": case.verification_problems if case else [],
            }
            sig = _sig({"t": [t.amount, t.date, t.reference, t.counterparty, t.type], "v": data["voucher"],
                        "f": sorted((f["code"], f["amount_at_risk"]) for f in findings)})
            search = " ".join([t.id, t.counterparty, t.reference, t.description, t.type,
                               src.filename if src else "", *(v.parties if v else []),
                               *(v.references if v else [])]).lower()
            item_rows.append((rid, t.evidence_id, data["kind"], disp, sev, exposure,
                              ",".join(sorted({f["code"] for f in findings})), sig, search,
                              json.dumps(data, default=str)))

        # documents view: what was read from each file, and what went wrong
        issue_by_file: dict[str, list[str]] = {}
        other_issues: list[str] = []
        known_names = {s.id for s in intake.sources} | {f["filename"] for f in svc.run_files(rid) if f["kind"] == "document"}
        for i in issues:
            head, sep, tail = i.partition(": ")
            if sep and head in known_names:
                issue_by_file.setdefault(head, []).append(tail)
            else:
                other_issues.append(i)
        docs_out = []
        for s in intake.sources:
            vs = [v for v in vouchers if v.source_doc == s.id]
            docs_out.append({
                "filename": s.filename, "kind": s.kind, "method": s.method, "pages": s.pages, "sha256": s.sha256,
                "issues": issue_by_file.get(s.id, []),
                "vouchers": [{**v.model_dump(mode="json"), "original": originals[v.id],
                              "corrected": v.method == "reviewer-corrected",
                              "linked_txns": linked_by_voucher.get(v.id, [])} for v in vs]})
        for name, msgs in issue_by_file.items():  # files that could not be read at all
            if name not in src_by_id:
                docs_out.append({"filename": name, "kind": "", "method": "unreadable", "pages": 0, "sha256": "",
                                 "issues": msgs, "vouchers": []})
        docs_out.sort(key=lambda d: d["filename"])
        (workdir / "documents.json").write_text(json.dumps(
            {"documents": docs_out, "issues": other_issues, "unlinked_documents": report.unlinked_documents}))
        (workdir / "report.json").write_text(report.model_dump_json(indent=1))
        (workdir / "manifest.json").write_text(json.dumps([r.model_dump() for r in store.manifest()], indent=1))
        (workdir / "policy.json").write_text(json.dumps(policy.to_dict(), indent=1))

        summary = {
            "population": report.population, "population_value": str(report.population_value),
            "dispositions": report.by_disposition(), "exposure": sum(r[5] for r in item_rows),
            "exceptions": len(report.cases), "documents": len(intake.sources), "vouchers": len(vouchers),
            "intake_issues": len(issues), "unlinked_documents": len(report.unlinked_documents),
            "analyst": report.analyst, "extractor": extractor.name, "ocr": ocr_name,
            "policy_version": policy.version, "seconds": round(time.time() - t0, 1)}

        prev = svc.db.one("SELECT id FROM runs WHERE engagement_id=? AND id<? AND status='completed' ORDER BY id DESC LIMIT 1",
                          (eid, rid))
        with svc.db.tx() as c:
            c.executemany("INSERT INTO items(run_id, txn_id, kind, disposition, severity, exposure, codes, signature,"
                          " search, data_json) VALUES(?,?,?,?,?,?,?,?,?,?)", item_rows)
            if prev:  # keep reviewers' work on items whose facts did not change
                for row in item_rows:
                    old = c.execute("SELECT signature FROM items WHERE run_id=? AND txn_id=?", (prev["id"], row[1])).fetchone()
                    if not old or old["signature"] != row[7]:
                        continue
                    d = c.execute("SELECT user_id, action, note FROM decisions WHERE run_id=? AND txn_id=? AND action<>'comment'"
                                  " ORDER BY id DESC LIMIT 1", (prev["id"], row[1])).fetchone()
                    if d and d["action"] in STATUS_ACTIONS:
                        c.execute("INSERT INTO decisions(run_id, txn_id, user_id, action, note, carried_from, created_at)"
                                  " VALUES(?,?,?,?,?,?,?)",
                                  (rid, row[1], d["user_id"], d["action"],
                                   (d["note"] + f" [carried forward from run {prev['id']}]")[:2000], prev["id"], now()))
            c.execute("UPDATE runs SET status='completed', progress='', finished_at=?, summary_json=? WHERE id=?",
                      (now(), json.dumps(summary), rid))
        svc.log(None, "run_completed", f"{len(report.cases)} exceptions", eid, rid)
