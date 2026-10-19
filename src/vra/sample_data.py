"""Deterministic demo data with seeded errors and answer keys (a tiny golden dataset)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from .money import q2
from .records import BankLine, GLEntry, GoodsReceipt, Invoice, Line, PurchaseOrder

D = Decimal
VENDORS = [("Acme Supplies Ltd", "ACM"), ("Globex Industrial LLC", "GLX"), ("Initech Office Co", "INI")]


def _doc_set(po_id, inv_id, vendor, number, po_date, lines, *, grn_qty=None, inv_lines=None,
             inv_date=None, tax_rate=D("0.05"), po_ref="same", grns=1):
    """Build a PO + GRN(s) + Invoice. `lines` = [(sku, qty, price)]."""
    po = PurchaseOrder(id=po_id, vendor=vendor, date=po_date,
                       lines=[Line(sku=s, qty=D(q), unit_price=D(p)) for s, q, p in lines])
    received = grn_qty or [(s, q) for s, q, _ in lines]
    per_grn = [
        GoodsReceipt(id=f"GRN-{po_id[3:]}{'' if grns == 1 else '-' + str(k + 1)}", po_ref=po_id,
                     date=po_date + timedelta(days=3 + k),
                     lines=[Line(sku=s, qty=D(q) / grns) for s, q in received])
        for k in range(grns)
    ]
    il = inv_lines or lines
    inv_lines_m = [Line(sku=s, qty=D(q), unit_price=D(p)) for s, q, p in il]
    subtotal = q2(sum((l.amount for l in inv_lines_m), D(0)))
    tax = q2(subtotal * tax_rate)
    inv = Invoice(id=inv_id, vendor=vendor, number=number, date=inv_date or po_date + timedelta(days=6),
                  lines=inv_lines_m, subtotal=subtotal, tax=tax, total=subtotal + tax,
                  po_ref=po_id if po_ref == "same" else po_ref)
    return po, per_grn, inv


def vouching_dataset():
    invoices, pos, grns = [], [], []
    expected: dict[str, set[str]] = {}

    def add(po, gr, inv, codes=()):
        if po:
            pos.append(po)
        grns.extend(gr)
        invoices.append(inv)
        if codes:
            expected[inv.evidence_id] = set(codes)

    # 20 clean three-way matches
    for i in range(20):
        vendor, tag = VENDORS[i % 3]
        lines = [(f"SKU-A{i % 5}", 10 + i, D(20 + (i * 3) % 17) + D("0.50"))]
        if i % 2:
            lines.append((f"SKU-B{i % 4}", 5 + i % 6, D(12 + (i * 5) % 11) + D("0.25")))
        add(*_doc_set(f"PO-{1000 + i}", f"INV-{2000 + i}", vendor, f"{tag}-{40000 + i * 137}",
                      date(2026, 1, 5) + timedelta(days=i), lines))

    mar = date(2026, 3, 2)
    # E1 price variance (+8%)
    add(*_doc_set("PO-1100", "INV-2100", "Acme Supplies Ltd", "ACM-70011", mar, [("SKU-P1", 100, D("50.00"))],
                  inv_lines=[("SKU-P1", 100, D("54.00"))]), {"PRICE_VARIANCE"})
    # E2 billed more than received
    add(*_doc_set("PO-1101", "INV-2101", "Globex Industrial LLC", "GLX-70022", mar, [("SKU-Q1", 50, D("40.00"))],
                  grn_qty=[("SKU-Q1", 30)]), {"QTY_OVER_RECEIVED"})
    # E3 no PO
    po, gr, inv = _doc_set("PO-X", "INV-2102", "Initech Office Co", "INI-70033", mar, [("SKU-N1", 15, D("100.00"))],
                           po_ref=None)
    add(None, [], inv, {"NO_PO"})
    # E4 exact duplicate of a clean invoice, re-entered 10 days later
    orig = next(i for i in invoices if i.id == "INV-2005")
    add(None, [], orig.model_copy(update={"id": "INV-2103", "date": orig.date + timedelta(days=10)}),
        {"DUPLICATE_EXACT", "QTY_OVER_PO", "QTY_OVER_RECEIVED"})
    # E5 near-duplicate (transposed digits, same amount) on two identical POs
    add(*_doc_set("PO-1104", "INV-2104", "Acme Supplies Ltd", "ACM-48213", mar, [("SKU-D1", 20, D("100.00"))]))
    add(*_doc_set("PO-1105", "INV-2105", "Acme Supplies Ltd", "ACM-48231", mar + timedelta(days=1),
                  [("SKU-D1", 20, D("100.00"))]), {"DUPLICATE_FUZZY"})
    # E6 arithmetic error in the stated total
    po, gr, inv = _doc_set("PO-1106", "INV-2106", "Globex Industrial LLC", "GLX-70066", mar, [("SKU-M1", 30, D("50.00"))])
    add(po, gr, inv.model_copy(update={"total": inv.total + D("180.00")}), {"INVOICE_ARITHMETIC"})
    # E7 split invoice: 12,000 PO billed as 2 x 6,000 inside two days (limit 10,000)
    po, gr, inv1 = _doc_set("PO-1107", "INV-2107", "Globex Industrial LLC", "GLX-70077", mar + timedelta(days=20),
                            [("SKU-S1", 200, D("60.00"))], grns=2, tax_rate=D(0),
                            inv_lines=[("SKU-S1", 100, D("60.00"))])
    inv2 = inv1.model_copy(update={"id": "INV-2108", "number": "GLX-70078", "date": inv1.date + timedelta(days=2)})
    add(po, gr, inv1)
    add(None, [], inv2, {"SPLIT_INVOICE"})
    # E8 invoice vendor differs from PO vendor
    add(*_doc_set("PO-1108", "INV-2109", "Initech Office Co", "UMB-1", mar, [("SKU-V1", 9, D("100.00"))],
                  tax_rate=D(0)), None)
    invoices[-1] = invoices[-1].model_copy(update={"vendor": "Umbrella Trading FZE"})
    expected[invoices[-1].evidence_id] = {"PO_VENDOR_MISMATCH"}
    # E9 item billed that is not on the PO
    add(*_doc_set("PO-1109", "INV-2110", "Acme Supplies Ltd", "ACM-70099", mar, [("SKU-R1", 10, D("30.00"))],
                  inv_lines=[("SKU-R1", 10, D("30.00")), ("SKU-X9", 2, D("150.00"))]), {"ITEM_NOT_ON_PO"})
    return invoices, pos, grns, expected


AS_OF = date(2026, 3, 31)


def bank_dataset():
    bank, gl, exp = [], [], {}
    n = [0]

    def b(d, amt, ref="", desc=""):
        n[0] += 1
        rec = BankLine(id=f"B{n[0]:03d}", date=d, amount=D(amt), reference=ref, description=desc)
        bank.append(rec)
        return rec

    def g(d, amt, ref="", desc=""):
        rec = GLEntry(id=f"G{len(gl) + 1:03d}", account="1000-Cash", date=d, amount=D(amt),
                      reference=ref, description=desc)
        gl.append(rec)
        return rec

    for i in range(15):  # exact reference + amount
        d = date(2026, 3, 1) + timedelta(days=i)
        amt = -(D(1000) + D(137) * i + D("0.25") * i) if i % 4 else D(2500) + D(311) * i
        g(d, amt, f"REF{1001 + i}", "payment/receipt")
        b(d + timedelta(days=1), amt, f"REF{1001 + i}", "bank clearing")
    for k, amt in enumerate(["1234.56", "-987.65", "4321.09"]):  # no reference, date lag
        d = date(2026, 3, 10 + k)
        g(d, amt, "", "transfer")
        b(d + timedelta(days=2), amt, "", "TRF")
    g(date(2026, 3, 12), "1000.00", "", "customer receipt")  # tolerance
    tol = b(date(2026, 3, 13), "1000.40", "", "customer receipt")
    exp[tol.evidence_id] = {"TOLERANCE_DIFFERENCE"}
    for d, amt in ((24, "1500.00"), (25, "1800.00"), (26, "1200.00")):  # one bank deposit, three receipts
        g(date(2026, 3, d), amt, "", "receipt")
    b(date(2026, 3, 27), "4500.00", "DEP-BATCH", "batched deposit")
    exp[b(date(2026, 3, 31), "-25.00", "", "Monthly bank charges").evidence_id] = {"BANK_ITEM_NOT_IN_GL"}
    exp[b(date(2026, 3, 31), "12.34", "", "Interest credit").evidence_id] = {"BANK_ITEM_NOT_IN_GL"}
    exp[g(date(2026, 3, 21), "-2300.00", "CHQ9001", "cheque").evidence_id] = {"GL_ITEM_NOT_CLEARED"}
    exp[g(date(2025, 11, 1), "-800.00", "CHQ8001", "old cheque").evidence_id] = {"STALE_GL_ITEM"}
    exp[g(date(2026, 3, 29), "5000.00", "", "deposit in transit").evidence_id] = {"GL_ITEM_NOT_CLEARED"}
    return bank, gl, exp
