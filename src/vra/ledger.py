"""Load ledger entries (the claims to be vouched) from Excel or CSV with flexible headers."""
from __future__ import annotations

import csv
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .extract import find_dates, parse_money
from .records import Transaction

ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "txn id", "transaction id", "entry no", "journal no", "voucher no", "line"),
    "type": ("type", "txn type", "transaction type", "category", "class"),
    "date": ("date", "posting date", "transaction date", "doc date", "entry date", "invoice date"),
    "counterparty": ("counterparty", "vendor", "supplier", "customer", "payee", "name", "party", "employee",
                     "vendor name", "supplier name", "customer name"),
    "reference": ("reference", "ref", "invoice no", "invoice number", "document no", "doc no", "inv no",
                  "cheque no", "reference no"),
    "amount": ("amount", "gross amount", "total", "value", "net amount", "invoice amount"),
    "debit": ("debit", "dr"),
    "credit": ("credit", "cr"),
    "currency": ("currency", "ccy", "curr"),
    "account": ("account", "gl account", "account code", "ledger account"),
    "description": ("description", "narration", "memo", "details", "particulars", "text"),
    "approver": ("approver", "approved by", "authorised by", "authorized by", "authoriser"),
}


class LedgerError(Exception):
    pass


def _norm_header(h) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-./]", " ", str(h or "").strip().lower()))


def _map_headers(row) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for i, h in enumerate(row):
        nh = _norm_header(h)
        for field, names in ALIASES.items():
            if nh in names and field not in mapping:
                mapping[field] = i
    return mapping


def _to_date(v, dayfirst: bool) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    ds = find_dates(str(v or ""), dayfirst)
    return ds[0] if ds else None


def _to_amount(v) -> Decimal | None:
    if v is None or str(v).strip() == "":
        return None
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v))
    cleaned = re.sub(r"[^\d.,()\-]", "", str(v))
    return parse_money(cleaned) if cleaned else None


def _sheets(path: Path):
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        for ws in wb.worksheets:
            yield ws.title, [list(r) for r in ws.iter_rows(values_only=True)]
    else:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
        yield "", [list(r) for r in csv.reader(text.splitlines(), delimiter="\t" if path.suffix == ".tsv" else ",")]


def load_transactions(path: str | Path, default_type: str = "purchase", dayfirst: bool = True) -> list[Transaction]:
    path = Path(path)
    sheets = list(_sheets(path))
    out: list[Transaction] = []
    problems: list[str] = []
    for sheet, rows in sheets:
        header_at = next((i for i, r in enumerate(rows[:25])
                          if {"date", "amount"} <= set(_map_headers(r)) or
                          ({"date"} <= set(_map_headers(r)) and {"debit", "credit"} & set(_map_headers(r)))), None)
        if header_at is None:
            if len(sheets) == 1:
                raise LedgerError("could not find a header row with at least 'Date' and 'Amount' (or Debit/Credit) columns")
            continue
        cols = _map_headers(rows[header_at])
        for r_i in range(header_at + 1, len(rows)):
            row = rows[r_i]
            if not any(str(c).strip() for c in row if c is not None):
                continue

            def get(field):
                i = cols.get(field)
                return row[i] if i is not None and i < len(row) else None

            amt = _to_amount(get("amount"))
            if amt is None:
                dr, cr = _to_amount(get("debit")), _to_amount(get("credit"))
                amt = (dr or Decimal(0)) - (cr or Decimal(0)) if (dr is not None or cr is not None) else None
            d = _to_date(get("date"), dayfirst)
            rid = f"R{r_i + 1}"
            tid = str(get("id")).strip() if get("id") not in (None, "") else (f"{sheet}!{rid}" if len(sheets) > 1 else rid)
            if amt is None or d is None:
                problems.append(f"{path.name} {sheet + ' ' if sheet else ''}row {r_i + 1}: missing/unreadable "
                                f"{'date' if d is None else 'amount'}; row skipped")
                continue
            out.append(Transaction(
                id=tid, type=(str(get("type")).strip().lower().replace(" ", "_") or default_type) if get("type") else default_type,
                date=d, amount=abs(amt), currency=str(get("currency") or "").strip().upper(),
                counterparty=str(get("counterparty") or "").strip(), reference=str(get("reference") or "").strip(),
                account=str(get("account") or "").strip(), description=str(get("description") or "").strip(),
                approver=str(get("approver") or "").strip()))
    if not out:
        raise LedgerError("no usable ledger rows found" + (": " + "; ".join(problems) if problems else ""))
    load_transactions.last_problems = problems  # surfaced by the CLI / intake report
    return out


load_transactions.last_problems = []
