import io
import sys
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import openpyxl
import pytest
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).parent))
from test_web import PW, do_run, make_client, new_engagement  # noqa: E402

from vra.analysts import RuleBasedAnalyst  # noqa: E402
from vra.audit_log import AuditLog  # noqa: E402
from vra.evaluation import score  # noqa: E402
from vra.evidence import EvidenceStore  # noqa: E402
from vra.extract import HeuristicExtractor  # noqa: E402
from vra.ingest import ingest_path  # noqa: E402
from vra.intake import build_vouchers  # noqa: E402
from vra.ledger import load_transactions  # noqa: E402
from vra.linking import link_all  # noqa: E402
from vra.orchestrator import DocumentVouchingWorkflow  # noqa: E402
from vra.policy import Policy  # noqa: E402
from vra.records import Transaction, Voucher  # noqa: E402
from vra.sample_bank import PERIOD, generate_bank_demo  # noqa: E402
from vra.sample_docs import make_pdf  # noqa: E402


@pytest.fixture(scope="module")
def bank(tmp_path_factory):
    return generate_bank_demo(tmp_path_factory.mktemp("bank"))


def run_bank(bank):
    ledger, ev, expected = bank
    intake = build_vouchers(ev, HeuristicExtractor())
    wf = DocumentVouchingWorkflow(EvidenceStore(), Policy(), RuleBasedAnalyst(), AuditLog())
    rep = wf.run(load_transactions(ledger, "bank_payment"), intake.vouchers, intake.sources, PERIOD, None, intake.issues)
    return rep, wf, intake, expected


def test_every_seeded_bank_problem_is_found_and_nothing_else(bank):
    rep, wf, intake, expected = run_bank(bank)
    s = score(expected, rep)
    assert s["recall"] == 1.0 and s["unexpected_flagged_subjects"] == [], s
    assert {c for v in expected.values() for c in v} == {"AMOUNT_MISMATCH", "COUNTERPARTY_MISMATCH", "VOUCHER_REUSED", "NO_VOUCHER", "CUTOFF_ISSUE"}
    assert not intake.issues


def test_each_statement_line_is_its_own_evidence_across_pdf_and_excel(bank):
    _, _, intake, _ = run_bank(bank)
    assert len(intake.sources) == 2 and len(intake.vouchers) == 17
    assert {v.doc_type for v in intake.vouchers} == {"statement"} and all(v.currency == "USD" for v in intake.vouchers)
    amounts = sorted(v.amount for v in intake.vouchers)
    assert D("4500.00") in amounts and D("12.34") in amounts and D("21500.00") in amounts      # whole-number Excel amounts read as money
    assert not any(v.amount in (D("26802.20"),) for v in intake.vouchers)                        # a running balance is never taken as the amount
    line = next(v for v in intake.vouchers if "CHQ-1043" in v.references)
    assert line.amount == D("4050.00") and line.date == date(2026, 3, 10) and "UMBRELLA TRADING FZE" in line.parties[0].upper()


def test_a_batched_deposit_covers_three_receipts_and_is_clean(bank):
    rep, wf, _, _ = run_bank(bank)
    batch = [wf.links[f"R{i}"] for i in (12, 13, 14)]
    assert len({l.voucher_id for l in batch}) == 1 and all(l.reasons[0].startswith("batch deposit") for l in batch)
    assert not {f"Transaction:R{i}" for i in (12, 13, 14)} & {c.subject_id for c in rep.cases}


def test_statement_lines_missing_from_the_cash_book_are_listed_one_by_one(bank):
    rep, *_ = run_bank(bank)
    assert len(rep.unlinked_documents) == 2
    assert any("BANK CHARGES MARCH" in x and "25.00" in x for x in rep.unlinked_documents)
    assert any("INTEREST" in x and "12.34" in x for x in rep.unlinked_documents)


def test_findings_say_what_the_auditor_needs_to_know(bank):
    rep, *_ = run_bank(bank)
    msgs = {c.subject_id: " ".join(f.message for f in c.findings) for c in rep.cases}
    assert "4,500.00" in msgs["Transaction:R8"] and "4,050.00" in msgs["Transaction:R8"] and "450.00" in msgs["Transaction:R8"]
    assert "HOOLI SERVICES" in msgs["Transaction:R9"].upper()
    assert "CHQ-1047" not in msgs["Transaction:R19"] and "1,100.00" in msgs["Transaction:R19"]
    assert "2026-04-02" in msgs["Transaction:R20"] and "cut-off" in msgs["Transaction:R20"]


def test_multi_page_pdf_statements_are_read_completely(tmp_path):
    lines = ["BANK STATEMENT", "Account number: 123", "Currency: USD", "Date  Description  Debit  Balance"]
    lines += [f"{1 + i % 28:02d} Mar 2026   PAYMENT SUPPLIER{i} CHQ-{2000 + i}   {10 + i}.00   {9000 - i}.00" for i in range(120)]
    (tmp_path / "statement.pdf").write_bytes(make_pdf(lines))
    assert len(PdfReader(tmp_path / "statement.pdf").pages) == 3
    doc = ingest_path(tmp_path / "statement.pdf")
    assert doc.pages == 3 and not doc.needs_ocr
    vs = HeuristicExtractor().extract(doc)
    assert len(vs) == 120 and vs[0].amount == D("10.00") and vs[-1].references == ["CHQ-2119"]


def test_excel_money_columns_read_as_money_but_ids_stay_ids(tmp_path):
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Invoice No", "Qty", "Amount"]); ws.append([1001, 3, 4500]); ws.append([1002, 5, 12.5])
    wb.save(tmp_path / "t.xlsx")
    text = ingest_path(tmp_path / "t.xlsx").text
    assert "1001\t3\t4500.00" in text and "1002\t5\t12.50" in text


def mk(i, typ, d, amt, party=""):
    return Transaction(id=i, type=typ, date=d, amount=D(amt), counterparty=party)


def test_batch_matching_stays_narrow():
    v = Voucher(id="s#1", source_doc="s", doc_type="statement", date=date(2026, 3, 17), amount=D("4500.00"), confidence=0.85)
    p = Policy()
    # works: same kind, close dates
    ok = link_all([mk("a", "bank_receipt", date(2026, 3, 16), "1500"), mk("b", "bank_receipt", date(2026, 3, 16), "1800"), mk("c", "bank_receipt", date(2026, 3, 17), "1200")], [v], {}, p)
    assert {l.voucher_id for l in ok.links.values()} == {"s#1"}
    # different kinds of entry are never combined
    mixed = link_all([mk("a", "bank_receipt", date(2026, 3, 16), "1500"), mk("b", "bank_payment", date(2026, 3, 16), "1800"), mk("c", "bank_receipt", date(2026, 3, 17), "1200")], [v], {}, p)
    assert all(l.voucher_id is None for l in mixed.links.values())
    # entries far from the document date are not combined
    far = link_all([mk("a", "bank_receipt", date(2026, 1, 2), "1500"), mk("b", "bank_receipt", date(2026, 1, 3), "1800"), mk("c", "bank_receipt", date(2026, 1, 4), "1200")], [v], {}, p)
    assert all(l.voucher_id is None for l in far.links.values())
    # a sum that is off by more than the tolerance is not a match
    off = link_all([mk("a", "bank_receipt", date(2026, 3, 16), "1500"), mk("b", "bank_receipt", date(2026, 3, 16), "1800"), mk("c", "bank_receipt", date(2026, 3, 17), "1100")], [v], {}, p)
    assert all(l.voucher_id is None for l in off.links.values())


def test_bank_description_words_are_enough_for_the_counterparty_test():
    from vra.programmes import counterparty_agrees, Ctx
    from vra.linking import Link

    t = mk("a", "bank_payment", date(2026, 3, 1), "100", "Acme Supplies")
    v = Voucher(id="s#1", source_doc="s", doc_type="statement", amount=D("100"), parties=["ACME SUPPLIES LIMITED OPERATIONS DUBAI"], confidence=0.85)
    assert counterparty_agrees(t, v, Link("a", "s#1", 1, ()), Ctx(Policy())) == []
    other = Voucher(id="s#2", source_doc="s", doc_type="statement", amount=D("100"), parties=["HOOLI SERVICES"], confidence=0.85)
    assert counterparty_agrees(t, other, Link("a", "s#2", 1, ()), Ctx(Policy()))


def test_web_flow_with_the_bank_demo_and_its_answer_key(tmp_path, bank):
    ledger, ev, expected = bank
    app, c = make_client(tmp_path / "web")
    c.post("/api/setup", json={"username": "admin", "display_name": "A", "password": PW})
    eid = new_engagement(c, name="Bank statement vouching", default_type="bank_payment")
    c.post(f"/api/engagements/{eid}/files", data={"kind": "ledger"}, files=[("files", ("cash_book_march_2026.xlsx", ledger.read_bytes()))])
    c.post(f"/api/engagements/{eid}/files", data={"kind": "document"}, files=[("files", (p.name, p.read_bytes())) for p in sorted(ev.iterdir())])
    run = do_run(c, eid)
    assert run["summary"]["population"] == 19 and run["summary"]["vouchers"] == 17 and run["summary"]["exceptions"] == 5
    key = (ledger.parent / "answer_key.csv").read_text(encoding="utf-8")
    r = c.post(f"/api/runs/{run['id']}/evaluate", json={"answer_key": key}).json()
    assert r["exception_detection"]["recall"] == 1.0 and r["exception_detection"]["precision"] == 1.0 and r["exception_detection"]["true_negative"] == 14
    assert not r["missed"] and not r["false_alarms"] and not r["found_but_different_finding"]
    docs = c.get(f"/api/runs/{run['id']}/documents").json()
    assert sum(len(d["vouchers"]) for d in docs["documents"]) == 17 and len(docs["unlinked_documents"]) == 2
