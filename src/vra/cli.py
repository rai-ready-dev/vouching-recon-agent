"""Command line: `vra demo`, `vra vouch`, `vra reconcile`."""
from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from .analysts import build_analyst
from .audit_log import AuditLog
from .evaluation import score
from .evidence import EvidenceStore
from .loaders import load_bank_csv, load_gl_csv, load_vouching_data
from .orchestrator import ReconciliationWorkflow, VouchingWorkflow
from .policy import Policy
from .sample_data import AS_OF, bank_dataset, vouching_dataset
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


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="vra", description=__doc__)
    ap.add_argument("--analyst", choices=["rule", "anthropic", "openai"], default="rule")
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
    args = ap.parse_args(argv)

    policy = Policy.from_file(args.policy) if args.policy else Policy()
    analyst = build_analyst(args.analyst)

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
    elif args.cmd == "vouch":
        _run_vouching(load_vouching_data(args.data), policy, analyst, Path(args.out))
    else:
        _run_recon(load_bank_csv(args.bank), load_gl_csv(args.gl), args.as_of, policy, analyst, Path(args.out))


if __name__ == "__main__":
    main()
