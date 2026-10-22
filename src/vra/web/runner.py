"""Background execution of a vouching run: parallel reading, caching, metering, benchmarking, persistence."""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..adjudicator import LinkAdjudicator
from ..analysts import ChatAnalyst, RuleBasedAnalyst
from ..audit_log import AuditLog
from ..evidence import EvidenceStore
from ..extract import HeuristicExtractor, LLMExtractor
from ..ingest import IngestError, ingest_path
from ..intake import build_vouchers
from ..ledger import LedgerError, load_transactions_with_problems
from ..llm import MeteredChat, UsageTracker
from ..orchestrator import DocumentVouchingWorkflow
from ..policy import Policy
from ..programmes import load_programmes
from ..records import Transaction, Voucher
from ..source_rows import HeuristicRowReader, LLMRowReader
from ..textutil import canon_name
from .db import now
from .service import LEDGER_EXT, STATUS_ACTIONS, Service

log = logging.getLogger("vra.web")


class _Counting:
    """Cache hit/miss counters shared by the cached wrappers."""

    def __init__(self) -> None:
        self.hits = self.misses = 0
        self._lock = threading.Lock()

    def _count(self, hit: bool) -> None:
        with self._lock:
            if hit:
                self.hits += 1
            else:
                self.misses += 1


class CachedExtractor(_Counting):
    """Re-uses earlier extractions of byte-identical documents (model calls cost time and money)."""

    def __init__(self, inner, service: Service, tag: str) -> None:
        super().__init__()
        self.inner, self.svc, self.tag = inner, service, tag
        self.name = inner.name

    def extract(self, doc):
        key = f"{self.inner.name}|{self.tag}"
        row = self.svc.db.one("SELECT data_json FROM extraction_cache WHERE sha256=? AND extractor=?", (doc.sha256, key))
        if row:
            self._count(True)
            out = []
            for n, d in enumerate(json.loads(row["data_json"]), 1):
                d.update(id=f"{doc.id}#{n}", source_doc=doc.id)
                out.append(Voucher.model_validate(d))
            return out
        self._count(False)
        vouchers = self.inner.extract(doc)
        with self.svc.db.tx() as c:
            c.execute("INSERT OR REPLACE INTO extraction_cache(sha256, extractor, data_json, created_at) VALUES(?,?,?,?)",
                      (doc.sha256, key, json.dumps([v.model_dump(mode="json") for v in vouchers]), now()))
        return vouchers


class CachedOcr(_Counting):
    def __init__(self, inner, service: Service) -> None:
        super().__init__()
        self.inner, self.svc = inner, service
        self.name = inner.name

    def ocr(self, doc) -> str:
        row = self.svc.db.one("SELECT text FROM ocr_cache WHERE sha256=? AND engine=?", (doc.sha256, self.inner.name))
        if row:
            self._count(True)
            return row["text"]
        self._count(False)
        text = self.inner.ocr(doc)
        if text.strip():
            with self.svc.db.tx() as c:
                c.execute("INSERT OR REPLACE INTO ocr_cache(sha256, engine, text, created_at) VALUES(?,?,?,?)",
                          (doc.sha256, self.inner.name, text, now()))
        return text


def _sig(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------- source of truth
def load_sources(files: list[dict], dest: Path, svc: Service, eid: int, default_type: str, dayfirst: bool,
                 ocr, row_reader) -> tuple[list[Transaction], list[str]]:
    """Every source-of-truth file (spreadsheet, CSV, PDF, Word, text, email, scan) -> entries to vouch."""
    settings = svc.source_settings(eid)
    dest.mkdir(parents=True, exist_ok=True)
    txns: list[Transaction] = []
    problems: list[str] = []
    multi = len(files) > 1
    for f in files:
        path = dest / Path(f["filename"]).name
        shutil.copyfile(svc.blob_path(eid, f["sha256"]), path)
        st = settings.get(f["filename"], {})
        dtype = st.get("default_type") or default_type
        try:
            if path.suffix.lower() in LEDGER_EXT:
                rows, probs = load_transactions_with_problems(path, dtype, dayfirst, st.get("mapping"), st.get("sheet"), st.get("header_row"))
            else:
                doc = ingest_path(path)
                if doc.needs_ocr:
                    if ocr is None:
                        raise LedgerError("it is a scan and OCR is not enabled (choose the AI reader for scans in the run options)")
                    doc.text, doc.method = ocr.ocr(doc), "ocr"
                rows, probs = row_reader.rows(doc, dtype, dayfirst)
                if not rows:
                    raise LedgerError("; ".join(probs) or "no entries could be read")
        except (LedgerError, IngestError) as e:
            raise LedgerError(f"{f['filename']}: {e}") from e
        stem = Path(f["filename"]).stem
        for t in rows:
            txns.append(t.model_copy(update={"id": f"{stem}!{t.id}"}) if multi and not t.id.startswith(stem + "!") else t)
        problems += probs
    ids = [t.id for t in txns]
    if len(ids) != len(set(ids)):
        raise LedgerError("entry ids are not unique across the source files; give the ID column unique values")
    return txns, problems


# ---------------------------------------------------------------------------- reader comparison
def compare_readers(a: list[Voucher], b: list[Voucher], name_a: str, name_b: str) -> dict:
    """Field-by-field agreement of two readers over the same documents."""
    bb = {v.id: v for v in b}
    fields = ("doc_type", "date", "amount", "subtotal", "tax", "currency", "references", "parties")
    stats = {f: {"compared": 0, "agree": 0} for f in fields}
    diffs: list[dict] = []

    def norm(f, v):
        if f == "references":
            return {"".join(ch for ch in r.upper() if ch.isalnum()) for r in v}
        if f == "parties":
            return {canon_name(p) for p in v}
        return v

    for va in a:
        vb = bb.get(va.id)
        if vb is None:
            continue
        for f in fields:
            x, y = getattr(va, f), getattr(vb, f)
            if f in ("references", "parties"):
                nx, ny = norm(f, x), norm(f, y)
                if not nx and not ny:
                    continue
                ok = nx <= ny or ny <= nx  # one reader may list fewer
            else:
                if x is None and y is None:
                    continue
                if f == "doc_type" and "unknown" in (x, y):
                    ok = x == y
                else:
                    ok = x == y
            stats[f]["compared"] += 1
            stats[f]["agree"] += ok
            if not ok and len(diffs) < 200:
                diffs.append({"document": va.source_doc, "field": f, "reader_a": str(x), "reader_b": str(y)})
    for f, s in stats.items():
        s["rate"] = round(s["agree"] / s["compared"], 3) if s["compared"] else None
    return {"reader_a": name_a, "reader_b": name_b, "documents": len(set(v.source_doc for v in a)), "fields": stats, "disagreements": diffs}


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

    # ------------------------------------------------------------------
    def _pipeline(self, rid: int) -> None:
        svc = self.svc
        t_start = time.time()
        stages: dict[str, float] = {}

        @contextlib.contextmanager
        def stage(name):
            t = time.time()
            try:
                yield
            finally:
                stages[name] = round(stages.get(name, 0) + time.time() - t, 2)

        run = svc.run_row(rid)
        eid = run["engagement_id"]
        eng = svc.engagement(eid)
        opts = json.loads(run["options_json"])
        workdir = svc.settings.runs_dir / str(rid)
        docs_dir = workdir / "docs"
        if workdir.exists():
            shutil.rmtree(workdir)
        docs_dir.mkdir(parents=True)

        with stage("snapshot"):  # the exact files this run uses
            source_files = []
            for f in svc.run_files(rid):
                if f["kind"] == "ledger":
                    source_files.append(f)
                    continue
                dest = docs_dir / f["filename"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    dest.hardlink_to(svc.blob_path(eid, f["sha256"]))
                except OSError:
                    shutil.copyfile(svc.blob_path(eid, f["sha256"]), dest)

        # model stack: one Foundry client, metered per purpose (calls, tokens, latency, retries)
        tracker = UsageTracker()
        chat = None
        if "foundry" in (opts["extractor"], opts["analyst"], opts["ocr"]) or opts.get("benchmark") or opts.get("ai_linking"):
            from ..llm import FoundryChat

            chat = MeteredChat(FoundryChat(), tracker)
        dayfirst = opts["dayfirst"]
        ocr, ocr_name = None, "none"
        if opts["ocr"] == "foundry":
            from ..llm import FoundryVisionOcr

            ocr = CachedOcr(FoundryVisionOcr(chat.for_purpose("ocr")), svc)
            ocr_name = ocr.name

        def make_extractor(kind: str) -> CachedExtractor:
            inner = LLMExtractor(chat.for_purpose("extract"), dayfirst=dayfirst) if kind == "foundry" else HeuristicExtractor(dayfirst)
            return CachedExtractor(inner, svc, f"ocr={ocr_name}|dayfirst={dayfirst}")

        extractor = make_extractor(opts["extractor"])
        analyst = ChatAnalyst(chat.for_purpose("explain")) if opts["analyst"] == "foundry" else RuleBasedAnalyst()
        row_reader = LLMRowReader(chat.for_purpose("source")) if opts["extractor"] == "foundry" else HeuristicRowReader()
        workers = int(os.environ.get("VRA_WORKERS", 4 if opts["extractor"] == "foundry" or opts["ocr"] == "foundry" else 1))
        last = [0.0]

        def progress(n, total, name):
            if time.time() - last[0] > 0.4:
                last[0] = time.time()
                self._set(rid, progress=f"Reading documents {n + 1}/{total}: {name}"[:200])

        with stage("source of truth"):
            self._set(rid, progress="Reading the source of truth")
            txns, source_problems = load_sources(source_files, workdir / "sources", svc, eid, eng["default_type"], dayfirst, ocr, row_reader)
        with stage("reading documents"):
            intake = build_vouchers(docs_dir, extractor, ocr, progress, workers)
        issues = intake.issues + source_problems

        bench = None
        if opts.get("benchmark"):
            with stage("benchmark"):
                self._set(rid, progress="Reading again with the other reader to compare")
                other_kind = "heuristic" if opts["extractor"] == "foundry" else "foundry"
                other = make_extractor(other_kind)
                other_intake = build_vouchers(docs_dir, other, ocr, None, workers if other_kind == "foundry" else 1)
                bench = compare_readers(intake.vouchers, other_intake.vouchers, extractor.name, other.name)
                (workdir / "benchmark.json").write_text(json.dumps(bench), encoding="utf-8")

        self._set(rid, progress="Applying reviewer corrections")
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
                    if k in ("amount", "subtotal", "tax"):
                        upd[k] = Decimal(val) if val not in (None, "") else None
                    elif k == "date":
                        upd[k] = date.fromisoformat(val) if val else None
                    else:
                        upd[k] = val
                v = v.model_copy(update={**upd, "confidence": 1.0, "notes": [], "method": "reviewer-corrected"})
            vouchers.append(v)

        pol = {k: Decimal(v) for k, v in opts["policy"].items()}
        policy = dataclasses.replace(Policy(), risk_signals=bool(opts.get("risk_signals")), **pol)
        programmes = None
        if opts["programmes_json"]:
            p = workdir / "programmes.json"
            p.write_text(opts["programmes_json"], encoding="utf-8")
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
        adjudicator = LinkAdjudicator(chat.for_purpose("link")) if opts.get("ai_linking") else None
        interval = Decimal(opts["sample_interval"]) if opts.get("sample_interval") else None
        with stage("linking and testing"):
            self._set(rid, progress=f"Linking and testing {len(txns)} ledger entries")
            report = wf.run(txns, vouchers, intake.sources, period, programmes, issues + [], forced,
                            svc.alias_map(eid), adjudicator, interval, int(opts.get("sample_seed") or 1))
        self._set(rid, progress="Saving results")

        # ------------------------------------------------------------ persist
        t_persist = time.time()
        lr = wf.link_result
        by_vid = {v.id: v for v in (*vouchers, *lr.synthetic)}
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
                for part in lr.combined.get(link.voucher_id, []):
                    linked_by_voucher.setdefault(part, []).append(t.evidence_id)
            v = by_vid.get(link.voucher_id) if link.voucher_id else None
            src = src_by_id.get(v.source_doc) if v else None
            case, clr = cases.get(t.evidence_id), cleared.get(t.evidence_id)
            findings = [f.model_dump(mode="json") for f in case.findings] if case else []
            disp = (case or clr).disposition.value
            sev = max((f["severity"] for f in findings), key=lambda s: sev_rank[s], default="none")
            exposure = float(sum(Decimal(f["amount_at_risk"]) for f in findings))
            closest = by_vid.get(link.closest_id) if link.closest_id else None
            parts = [by_vid[p] for p in lr.combined.get(link.voucher_id or "", []) if p in by_vid]
            data = {
                "txn_id": t.evidence_id, "kind": "case" if case else "cleared", "disposition": disp,
                "transaction": t.model_dump(mode="json"),
                "link": {"voucher_id": link.voucher_id, "score": link.score, "reasons": list(link.reasons), "note": link.note,
                         "closest_id": link.closest_id, "manual": "manual link" in link.reasons,
                         "ai": "ai-suggested" in link.reasons, "marked_no_document": "marked: no document" in link.reasons},
                "voucher": v.model_dump(mode="json") if v else None,
                "source_file": ({"filename": src.filename, "kind": src.kind, "method": src.method, "flags": src.flags} if src else None),
                "combined_from": [src_by_id[p.source_doc].filename for p in parts if p.source_doc in src_by_id],
                "closest_file": (src_by_id[closest.source_doc].filename if closest and closest.source_doc in src_by_id else None),
                "findings": findings,
                "analysis": case.analysis.model_dump(mode="json") if case and case.analysis else None,
                "problems": case.verification_problems if case else [],
            }
            sig = _sig({"t": [t.amount, t.date, t.reference, t.counterparty, t.type], "v": data["voucher"],
                        "f": sorted((f["code"], f["amount_at_risk"]) for f in findings)})
            search = " ".join([t.id, t.counterparty, t.reference, t.description, t.type, src.filename if src else "",
                               *(v.parties if v else []), *(v.references if v else [])]).lower()
            item_rows.append((rid, t.evidence_id, data["kind"], disp, sev, exposure,
                              ",".join(sorted({f["code"] for f in findings})), sig, search, json.dumps(data, default=str)))

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
                "flags": s.flags, "issues": issue_by_file.get(s.id, []),
                "vouchers": [{**v.model_dump(mode="json"), "original": originals[v.id], "corrected": v.method == "reviewer-corrected",
                              "linked_txns": linked_by_voucher.get(v.id, [])} for v in vs]})
        for name, msgs in issue_by_file.items():  # files that could not be read at all
            if name not in src_by_id:
                docs_out.append({"filename": name, "kind": "", "method": "unreadable", "pages": 0, "sha256": "", "flags": [],
                                 "issues": msgs, "vouchers": []})
        docs_out.sort(key=lambda d: d["filename"])
        (workdir / "documents.json").write_text(json.dumps(
            {"documents": docs_out, "issues": other_issues, "unlinked_documents": report.unlinked_documents}), encoding="utf-8")
        (workdir / "report.json").write_text(report.model_dump_json(indent=1), encoding="utf-8")
        (workdir / "manifest.json").write_text(json.dumps([r.model_dump() for r in store.manifest()], indent=1), encoding="utf-8")
        (workdir / "policy.json").write_text(json.dumps(policy.to_dict(), indent=1), encoding="utf-8")

        grounding_drops = sum(1 for v in intake.vouchers for n in v.notes if "not found in the document" in n)
        analyst_errors = sum(1 for e in audit.entries if e["event"] == "analyst_error")
        rejected = sum(1 for c in report.cases for p in c.verification_problems if not p.startswith("analyst '"))
        stages["saving"] = round(time.time() - t_persist, 2)
        stages["total"] = round(time.time() - t_start, 2)
        perf = {
            "stages": stages,
            "documents": {"files": len(intake.sources) + sum(1 for i in issues if "unsupported" in i), "read": len(intake.sources),
                          "vouchers": len(intake.vouchers), "ledger_entries": len(txns),
                          "per_second": round(len(intake.sources) / stages["reading documents"], 1) if stages.get("reading documents") else None,
                          "workers": workers, "cache_hits": extractor.hits + (ocr.hits if ocr else 0),
                          "cache_misses": extractor.misses + (ocr.misses if ocr else 0)},
            "llm": tracker.summary(),
            "guards": {"fields_dropped_as_not_in_document": grounding_drops, "assessments_rejected_by_verifier": rejected,
                       "model_failures_fallback_used": analyst_errors,
                       "low_confidence_documents": sum(1 for v in intake.vouchers if v.confidence < policy.min_extraction_confidence or v.notes),
                       "ai_links_suggested": lr.ai_links, "ai_link_errors": lr.ai_errors, "split_payments": len(lr.synthetic)},
            "readers": {"documents": extractor.name, "explanations": analyst.name, "ocr": ocr_name, "source_rows": row_reader.name},
            "benchmark": bench["fields"] if bench else None,
        }
        (workdir / "perf.json").write_text(json.dumps(perf, default=str), encoding="utf-8")

        summary = {
            "population": report.population, "population_value": str(report.population_value),
            "dispositions": report.by_disposition(), "exposure": sum(r[5] for r in item_rows),
            "exceptions": len(report.cases), "documents": len(intake.sources), "vouchers": len(vouchers),
            "intake_issues": len(issues), "unlinked_documents": len(report.unlinked_documents),
            "analyst": report.analyst, "extractor": extractor.name, "ocr": ocr_name,
            "policy_version": policy.version, "seconds": stages["total"], "benchmarked": bool(bench),
            "llm_calls": perf["llm"]["calls"], "tokens": perf["llm"]["tokens_in"] + perf["llm"]["tokens_out"]}

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
