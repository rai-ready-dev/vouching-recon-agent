"""Command line: `vra demo`, `vra vouch`, `vra reconcile`."""
from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from .analysts import ChatAnalyst, build_analyst
from .audit_log import AuditLog
from .envfile import load_env_file
from .evaluation import score
from .evidence import EvidenceStore
from .extract import HeuristicExtractor, LLMExtractor
from .intake import build_vouchers
from .ledger import load_transactions
from .loaders import load_bank_csv, load_gl_csv, load_vouching_data
from .orchestrator import DocumentVouchingWorkflow, ReconciliationWorkflow, VouchingWorkflow
from .programmes import load_programmes
from .policy import Policy
from .sample_data import AS_OF, bank_dataset, vouching_dataset
from .sample_docs import PERIOD, generate_demo
from .workpaper import render_workpaper


def _write(out: Path, name: str, report, store, log) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}_workpaper.md").write_text(render_workpaper(report, store))
    (out / f"{name}_report.json").write_text(report.model_dump_json(indent=2))
    (out / f"{name}_evidence_manifest.json").write_text(
        json.dumps([r.model_dump() for r in store.manifest()], indent=2))
    (out / f"{name}_audit_log.jsonl").write_text("".join(json.dumps(e, default=str) + "\n" for e in log.entries))


def _summary(label: str, report, extra: dict | None = None) -> None:
    print(f"\n== {label} ==  analyst={report.analyst}  population={report.population}")
    print("   dispositions:", report.by_disposition())
    for k, v in (extra or {}).items():
        print(f"   {k}: {v}")


def _run_vouching(data, policy, analyst, out: Path, expected=None):
    store, log = EvidenceStore(), AuditLog()
    report = VouchingWorkflow(store, policy, analyst, log).run(*data)
    _write(out, "vouching", report, store, log)
    _summary("Vouching", report, {"audit chain intact": AuditLog.verify(log.entries),
                                  **({"eval": score(expected, report)} if expected else {})})


def _run_recon(bank, gl, as_of, policy, analyst, out: Path, expected=None):
    store, log = EvidenceStore(), AuditLog()
    bal = (sum((b.amount for b in bank), Decimal(0)), sum((g.amount for g in gl), Decimal(0)))
    report = ReconciliationWorkflow(store, policy, analyst, log).run(bank, gl, as_of, *bal)
    _write(out, "bank_rec", report, store, log)
    _summary("Bank reconciliation", report, {"proof": report.proof,
                                             "audit chain intact": AuditLog.verify(log.entries),
                                             **({"eval": score(expected, report)} if expected else {})})


def _build_stack(args):
    """Analyst, extractor and OCR engine from the CLI flags; one Foundry client is shared."""
    chat = None
    if "foundry" in (args.analyst, args.extractor, args.ocr):
        from .llm import FoundryChat, FoundryVisionOcr

        chat = FoundryChat()
    analyst = ChatAnalyst(chat) if args.analyst == "foundry" else build_analyst(args.analyst)
    extractor = LLMExtractor(chat) if args.extractor == "foundry" else HeuristicExtractor()
    ocr = FoundryVisionOcr(chat) if args.ocr == "foundry" else None
    return analyst, extractor, ocr


def _run_docs(ledger, docs, period, policy, analyst, extractor, ocr, out: Path, programmes=None,
              default_type="purchase", expected=None):
    intake = build_vouchers(docs, extractor, ocr)
    txns = load_transactions(ledger, default_type)
    issues = intake.issues + list(load_transactions.last_problems)
    store, log = EvidenceStore(), AuditLog()
    report = DocumentVouchingWorkflow(store, policy, analyst, log).run(
        txns, intake.vouchers, intake.sources, period, load_programmes(programmes), issues)
    _write(out, "document_vouching", report, store, log)
    _summary("Document vouching", report, {
        "documents read": len(intake.sources), "vouchers extracted": len(intake.vouchers),
        "intake issues": len(issues), "unlinked documents": report.unlinked_documents,
        "audit chain intact": AuditLog.verify(log.entries),
        **({"eval": score(expected, report)} if expected else {})})


def _run_extract(docs, extractor, ocr, out: Path) -> None:
    intake = build_vouchers(docs, extractor, ocr)
    out.mkdir(parents=True, exist_ok=True)
    (out / "extracted.json").write_text(json.dumps(
        {"vouchers": [v.model_dump(mode="json") for v in intake.vouchers], "issues": intake.issues}, indent=2))
    print(f"\n{'document':34} {'type':14} {'date':11} {'amount':>12}  conf  refs / parties")
    for v in intake.vouchers:
        print(f"{v.source_doc[:33]:34} {v.doc_type:14} {str(v.date or '-'):11} "
              f"{(f'{v.amount:,.2f}' if v.amount is not None else '-'):>12}  {v.confidence:.2f}  "
              f"{', '.join(v.references)} / {', '.join(v.parties)}")
    for i in intake.issues:
        print("  ! " + i)
    print(f"\nWrote {out / 'extracted.json'}")


def _check_foundry() -> None:
    import os
    import time

    names = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_VERSION")
    for n in names:
        v = os.environ.get(n, "")
        print(f"  {n:28} {'(set, hidden)' if v and 'KEY' in n else (v or '(not set)')}")
    if not os.environ.get("AZURE_OPENAI_API_KEY"):
        print("  No API key: using Microsoft Entra ID sign-in (run `az login` first).")
    try:
        from .llm import FoundryChat

        t0 = time.time()
        chat = FoundryChat()
        reply = chat.chat("Reply with the single word OK.", "ping")
        usage = chat.pop_usage()
        print(f"\nConnected to {chat.name} in {time.time() - t0:.1f}s; the model replied {reply!r}"
              + (f" ({usage[0]}+{usage[1]} tokens)" if usage else ""))
    except Exception as exc:
        print(f"\nCould not connect: {type(exc).__name__}: {str(exc)[:400]}")
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="vra", description=__doc__)
    ap.add_argument("--analyst", choices=["rule", "foundry", "anthropic", "openai"], default="rule")
    ap.add_argument("--extractor", choices=["heuristic", "foundry"], default="heuristic",
                    help="how documents are read (foundry = LLM with verbatim-quote checking)")
    ap.add_argument("--ocr", choices=["none", "foundry"], default="none", help="engine for scans/images")
    ap.add_argument("--policy", help="policy JSON file (defaults to built-in policy)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="run both workflows on built-in seeded data")
    d.add_argument("--out", default="demo_output")
    v = sub.add_parser("vouch", help="vouch invoices in a folder of JSON files")
    v.add_argument("--data", required=True)
    v.add_argument("--out", default="out")
    r = sub.add_parser("reconcile", help="reconcile a bank CSV to a GL CSV")
    r.add_argument("--bank", required=True)
    r.add_argument("--gl", required=True)
    r.add_argument("--as-of", required=True, type=date.fromisoformat)
    r.add_argument("--out", default="out")
    dd = sub.add_parser("demo-docs", help="mixed-format documents + ledger demo (PDF/Word/Excel/text/email/scan)")
    dd.add_argument("--out", default="demo_docs_output")
    vd = sub.add_parser("vouch-docs", help="vouch a ledger (xlsx/csv) to a folder of supporting documents")
    vd.add_argument("--ledger", required=True)
    vd.add_argument("--docs", required=True)
    vd.add_argument("--type", default="purchase", help="default transaction type if the ledger has no Type column")
    vd.add_argument("--period-start", type=date.fromisoformat)
    vd.add_argument("--period-end", type=date.fromisoformat)
    vd.add_argument("--programmes", help="JSON file overriding/adding vouching programmes")
    vd.add_argument("--out", default="out")
    sub.add_parser("check-foundry", help="test the Microsoft Foundry connection and show what is configured")
    ex = sub.add_parser("extract", help="read documents only and show what was extracted (no ledger needed)")
    ex.add_argument("--docs", required=True)
    ex.add_argument("--out", default="out")
    args = ap.parse_args(argv)

    load_env_file(".env")
    if args.cmd == "check-foundry":
        return _check_foundry()
    policy = Policy.from_file(args.policy) if args.policy else Policy()
    analyst, extractor, ocr = _build_stack(args)

    if args.cmd == "demo":
        out = Path(args.out)
        invoices, pos, grns, exp_v = vouching_dataset()
        data_dir = out / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        for name, rows in (("invoices", invoices), ("purchase_orders", pos), ("goods_receipts", grns)):
            (data_dir / f"{name}.json").write_text(json.dumps([r.model_dump(mode="json") for r in rows], indent=1))
        _run_vouching((invoices, pos, grns), policy, analyst, out, exp_v)
        bank, gl, exp_b = bank_dataset()
        _run_recon(bank, gl, AS_OF, policy, analyst, out, exp_b)
        print(f"\nWorkpapers and audit logs written to {out.resolve()}")
    elif args.cmd == "demo-docs":
        out = Path(args.out)
        ledger, docs, exp = generate_demo(out)
        _run_docs(ledger, docs, PERIOD, policy, analyst, extractor, ocr, out, expected=exp)
        print(f"\nDemo inputs: {ledger} and {docs}\nOutputs written to {out.resolve()}")
    elif args.cmd == "vouch-docs":
        period = (args.period_start, args.period_end) if args.period_start and args.period_end else None
        _run_docs(args.ledger, args.docs, period, policy, analyst, extractor, ocr, Path(args.out),
                  args.programmes, args.type)
    elif args.cmd == "extract":
        _run_extract(args.docs, extractor, ocr, Path(args.out))
    elif args.cmd == "vouch":
        _run_vouching(load_vouching_data(args.data), policy, analyst, Path(args.out))
    else:
        _run_recon(load_bank_csv(args.bank), load_gl_csv(args.gl), args.as_of, policy, analyst, Path(args.out))


if __name__ == "__main__":
    main()
