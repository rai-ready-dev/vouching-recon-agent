from datetime import date
from decimal import Decimal as D

import pytest

from vra.analysis import Analysis, CaseBundle, Claim, Classification
from vra.analysts import RuleBasedAnalyst
from vra.audit_log import AuditLog
from vra.engine.aging import age_items
from vra.engine.duplicates import find_duplicates
from vra.engine.matching import Item, reconcile
from vra.engine.recompute import recompute_invoice
from vra.evidence import EvidenceConflict, EvidenceStore
from vra.findings import FindingCode, NumberRef
from vra.policy import Policy
from vra.records import GoodsReceipt, Invoice, Line, PurchaseOrder
from vra.textutil import normalize_number
from vra.verifier import money_figures, verify_analysis

P = Policy()


def inv(id="I1", vendor="Acme Ltd", number="INV-1", d=date(2026, 1, 10), total="105.00", sub="100.00",
        tax="5.00", qty="10", price="10.00", po="PO1"):
    return Invoice(id=id, vendor=vendor, number=number, date=d, subtotal=D(sub), tax=D(tax), total=D(total),
                   lines=[Line(sku="S", qty=D(qty), unit_price=D(price))], po_ref=po)


# ---- evidence ----------------------------------------------------------
def test_evidence_is_immutable_and_tamper_evident():
    s = EvidenceStore()
    i = inv()
    s.add(i)
    s.add(i)  # idempotent
    with pytest.raises(EvidenceConflict):
        s.add(i.model_copy(update={"total": D("999")}))
    assert s.verify_integrity() == []
    i.lines.append(Line(sku="X", qty=D(1)))  # in-place tamper (list is mutable inside frozen model)
    assert s.verify_integrity() == [i.evidence_id]


def test_field_lookup():
    s = EvidenceStore()
    s.add(inv())
    assert s.field("Invoice:I1", "lines.0.qty") == D("10")
    assert s.field("Invoice:I1", "lines.0.amount") == D("100.00")


# ---- recompute / duplicates ---------------------------------------------
def test_recompute_flags_bad_total_only():
    assert recompute_invoice(inv(), P) == []
    bad = recompute_invoice(inv(total="150.00"), P)
    assert [f.code for f in bad] == [FindingCode.INVOICE_ARITHMETIC]


def test_number_normalisation_ignores_padding():
    assert normalize_number("INV-00123") == normalize_number("inv 123")


def test_duplicate_exact_and_not_for_distinct_numbers():
    dup = find_duplicates([inv("I1"), inv("I2", number="inv 0001", d=date(2026, 1, 20))], P)
    assert [f.code for f in dup] == [FindingCode.DUPLICATE_EXACT] and dup[0].subject_id == "Invoice:I2"
    assert find_duplicates([inv("I1"), inv("I2", number="ZZZ-9", d=date(2026, 3, 1))], P) == []


def test_monthly_recurring_invoice_not_flagged():
    a = inv("I1", number="RENT-0101", d=date(2026, 1, 1))
    b = inv("I2", number="RENT-0102", d=date(2026, 2, 1))
    assert find_duplicates([a, b], P) == []


def test_split_invoices_flagged():
    a = inv("I1", number="A-1", total="6000.00", d=date(2026, 1, 1))
    b = inv("I2", number="B-7", total="6000.00", d=date(2026, 1, 3))
    codes = {f.code for f in find_duplicates([a, b], P)}
    assert FindingCode.SPLIT_INVOICE in codes


# ---- matching -----------------------------------------------------------
def it(i, amt, d=1, ref=""):
    return Item(i, D(amt), date(2026, 3, d), ref)


def test_exact_ref_beats_amount_only():
    r = reconcile([it("b1", "100", 1, "X1"), it("b2", "100", 1, "X2")],
                  [it("g2", "100", 1, "X2"), it("g1", "100", 1, "X1")], P)
    pairs = {(m.left[0], m.right[0]) for m in r.matches}
    assert pairs == {("b1", "g1"), ("b2", "g2")}


def test_one_to_many_and_no_reuse():
    r = reconcile([it("b", "4500")], [it("a", "1500"), it("c", "1800"), it("d", "1200"), it("e", "999")], P)
    m = r.matches[0]
    assert m.rule == "one_to_many" and set(m.right) == {"a", "c", "d"} and m.difference == 0
    assert [x.id for x in r.unmatched_right] == ["e"]


def test_tolerance_match_reports_difference():
    r = reconcile([it("b", "1000.40")], [it("g", "1000.00")], P)
    assert r.matches[0].rule == "amount_tolerance" and r.matches[0].difference == D("0.40")


def test_outside_tolerance_or_window_stays_unmatched():
    r = reconcile([it("b", "100.00", 1)], [it("g", "100.00", 20)], P)
    assert not r.matches and len(r.unmatched_left) == 1


def test_opposite_signs_never_group_match():
    r = reconcile([it("b", "300")], [it("a", "-100"), it("c", "-200")], P)
    assert not r.matches


def test_aging_buckets():
    out = age_items([it("a", "10", 1), it("b", "20", 1)], date(2026, 6, 30))
    assert out["90+"]["count"] == 2 and out["90+"]["total"] == "30.00"


# ---- verifier -----------------------------------------------------------
def _case(store=None):
    from vra.engine.three_way import three_way_match
    store = store or EvidenceStore()
    i = inv(price="12.00", sub="120.00", total="126.00", tax="6.00")
    po = PurchaseOrder(id="PO1", vendor="Acme Ltd", date=date(2026, 1, 1), lines=[Line(sku="S", qty=D(10), unit_price=D(10))])
    g = GoodsReceipt(id="G1", po_ref="PO1", date=date(2026, 1, 5), lines=[Line(sku="S", qty=D(10))])
    for r in (i, po, g):
        store.add(r)
    fs = three_way_match(i, po, [g], {}, P)
    ids = sorted({e for f in fs for e in f.evidence_ids})
    return store, CaseBundle(case_id="c", subject_id=i.evidence_id, domain="vouching", findings=fs,
                             evidence={e: store.payload(e) for e in ids})


def test_baseline_analysis_passes_verifier():
    store, b = _case()
    assert verify_analysis(RuleBasedAnalyst().analyze(b), b, store) == []


def test_verifier_rejects_fabricated_figure_and_evidence():
    store, b = _case()
    a = RuleBasedAnalyst().analyze(b)
    a.explanation += " The vendor overcharged by 7,777.77 in total."
    a.claims.append(Claim(text="x", evidence_ids=["Invoice:NOPE"]))
    a.claims.append(Claim(text="y", evidence_ids=["Invoice:I1"],
                          numbers=[NumberRef(evidence_id="Invoice:I1", path="total", value=D("1.00"))]))
    problems = " | ".join(verify_analysis(a, b, store))
    assert "7,777.77" in problems and "unknown evidence" in problems and "analysis says 1.00" in problems


def test_verifier_blocks_clearing_a_flagged_case_and_foreign_evidence():
    store, b = _case()
    store.add(inv("OTHER", number="Z-9"))
    a = RuleBasedAnalyst().analyze(b)
    a.needs_human = False
    a.claims.append(Claim(text="z", evidence_ids=["Invoice:OTHER"]))
    problems = " | ".join(verify_analysis(a, b, store))
    assert "without human review" in problems and "outside this case" in problems


def test_money_figure_extraction_ignores_dates_and_ids():
    assert money_figures("INV-1001 dated 2026-01-05 qty 12 owes 1,234.50 and 7.5") == {D("1234.50"), D("7.50")}


# ---- audit log ------------------------------------------------------------
def test_audit_chain_detects_tampering():
    log = AuditLog(clock=lambda: "t")
    for e in ("a", "b", "c"):
        log.append(e, {"k": e})
    assert AuditLog.verify(log.entries)
    log.entries[1]["payload"]["k"] = "changed"
    assert not AuditLog.verify(log.entries)
