"""A stand-in for a Foundry deployment that answers the agent's real prompts, so every AI code path runs offline.

It is "faithful" in that it returns what a good model would: grounded quotes copied from the document text. Tests
can subclass it to simulate a bad model (fabricated amounts, rate limits, outages).
"""
import json
import re
import threading

from vra.extract import HeuristicExtractor, find_dates
from vra.ingest import SourceDocument
from vra.source_rows import HeuristicRowReader

OCR_TEXT = "Acme Supplies Ltd\nTAX INVOICE\nInvoice No: INV-5016\nInvoice Date: 20 Mar 2026\nTotal Amount Due: USD 410.00\n"


class FakeFoundry:
    name = "foundry:fake-gpt"

    def __init__(self, *a, **k):
        self._local = threading.local()
        self.calls = []

    def pop_usage(self):
        u, self._local.usage = getattr(self._local, "usage", None), None
        return u

    # ---- dispatch on the real prompts
    def chat(self, system, user, images=(), json_mode=False):
        self.calls.append(system[:40])
        self._local.usage = (120, 30)
        if "Reply with the single word OK" in system:
            return "OK"
        if "faithful OCR engine" in system:
            return OCR_TEXT
        if system.startswith("You extract facts from ONE business document"):
            return self.extract(self._between(user))
        if "You read a document that lists financial entries" in system:
            return self.rows(self._between(user))
        if "decide whether ONE of the candidate documents" in system:
            return self.adjudicate(user)
        if "reasoning component of an audit vouching" in system:
            return self.explain(user)
        raise AssertionError("unexpected prompt: " + system[:80])

    @staticmethod
    def _between(user):
        return user.split("<untrusted_document_text>\n", 1)[1].rsplit("\n</untrusted_document_text>", 1)[0]

    @staticmethod
    def _quote(lines, needle):
        return next((l.strip() for l in lines if needle in l), needle)

    def extract(self, text):
        v = HeuristicExtractor().extract(SourceDocument("x", "x.txt", "txt", "x", text))[0]
        lines = text.splitlines()
        item = {"doc_type": v.doc_type, "confidence": 0.95, "parties": [], "references": [], "approvals": []}

        def money(field, val):
            if val is not None:
                for fmt in (f"{val:,.2f}", f"{val:.2f}"):
                    if any(fmt in l for l in lines):
                        item[field] = {"value": f"{val:.2f}", "quote": self._quote(lines, fmt)}
                        return

        money("amount", v.amount)
        money("subtotal", v.subtotal)
        money("tax", v.tax)
        if v.date:
            line = next((l for l in lines if v.date in find_dates(l)), None)
            if line:
                item["date"] = {"value": v.date.isoformat(), "quote": line.strip()}
        if v.currency:
            item["currency"] = {"value": v.currency, "quote": self._quote(lines, v.currency)}
        for p in v.parties:
            item["parties"].append({"value": p, "quote": self._quote(lines, p)})
        for r in v.references:
            item["references"].append({"value": r, "quote": self._quote(lines, r)})
        for a in v.approvals:
            item["approvals"].append({"value": a, "quote": self._quote(lines, a)})
        return json.dumps({"vouchers": [item]})

    def rows(self, text):
        rows, _ = HeuristicRowReader().rows(SourceDocument("s", "s.pdf", "pdf", "x", text))
        lines = text.splitlines()
        out = []
        for r in rows:
            line = next((l for l in lines if r.reference and r.reference in l), lines[0])
            out.append({"date": {"value": r.date.isoformat(), "quote": line.strip()},
                        "amount": {"value": f"{r.amount:.2f}", "quote": line.strip()},
                        "counterparty": {"value": r.counterparty, "quote": line.strip()} if r.counterparty and r.counterparty in line else None,
                        "reference": {"value": r.reference, "quote": r.reference} if r.reference else None,
                        "description": r.description})
        return json.dumps({"rows": out})

    def adjudicate(self, user):
        entry = json.loads(user.split("Ledger entry (trusted):\n", 1)[1].split("\n\n<untrusted", 1)[0])
        cands = json.loads(user.split("<untrusted_document_data>\n", 1)[1].rsplit("\n</untrusted_document_data>", 1)[0])
        pick = next((c["id"] for c in cands if c["amount"] and float(c["amount"]) == float(entry["amount"])), None)
        return json.dumps({"choice": pick, "reason": "same amount and supplier family" if pick else "nothing clearly matches"})

    def explain(self, user):
        findings = json.loads(user.split("\n\n<untrusted_document_data>", 1)[0].split("(trusted, from the deterministic engine):\n", 1)[1])
        return json.dumps({"classification": "other", "explanation": "The deterministic tests raised an exception: " + findings[0]["message"],
                           "claims": [], "suggested_action": "Review the supporting document.", "confidence": 0.9, "needs_human": True})
