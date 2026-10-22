"""Load the source of truth (the entries to be vouched) from Excel or CSV with flexible headers.

Header detection is automatic; `preview_source` shows what was detected so a person can correct the column
mapping, and `mapping` / `sheet` / `header_row` then pin it down for any unusual layout.
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .extract import find_dates, parse_money
from .records import Transaction
from .textutil import to_ascii_digits

ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "txn id", "transaction id", "entry no", "journal no", "voucher no", "line"),
    "type": ("type", "txn type", "transaction type", "category", "class"),
    "date": ("date", "dt", "posting date", "posting dt", "transaction date", "doc date", "entry date", "invoice date", "value date"),
    "counterparty": ("counterparty", "vendor", "supplier", "customer", "payee", "name", "party", "employee",
                     "vendor name", "supplier name", "customer name", "beneficiary"),
    "reference": ("reference", "ref", "invoice no", "invoice number", "document no", "doc no", "inv no",
                  "cheque no", "reference no", "document number", "invoice"),
    "amount": ("amount", "gross amount", "total", "value", "net amount", "invoice amount"),
    "debit": ("debit", "dr", "debit amount", "withdrawal"),
    "credit": ("credit", "cr", "credit amount", "deposit"),
    "currency": ("currency", "ccy", "curr"),
    "account": ("account", "gl account", "account code", "ledger account"),
    "description": ("description", "narration", "memo", "details", "particulars", "text", "narrative"),
    "approver": ("approver", "approved by", "authorised by", "authorized by", "authoriser"),
}
FIELDS = tuple(ALIASES)
# a loose match only applies to these distinctive words, to keep wrong guesses rare
LOOSE = {"date": ("date", "dt"), "amount": ("amount", "total", "value"), "counterparty": ("vendor", "supplier", "customer", "payee", "party"),
         "reference": ("reference", "invoice", "ref"), "description": ("description", "narration", "memo", "details", "particulars"),
         "currency": ("currency",), "debit": ("debit",), "credit": ("credit",), "approver": ("approver", "approved"),
         "account": ("account",), "type": ("type",)}


class LedgerError(Exception):
    pass


def _norm_header(h) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-./()]", " ", str(h or "").strip().lower())).strip()


def _map_headers(row) -> dict[str, int]:
    mapping: dict[str, int] = {}
    heads = [_norm_header(h) for h in row]
    for i, nh in enumerate(heads):  # exact alias first
        for field, names in ALIASES.items():
            if nh in names and field not in mapping:
                mapping[field] = i
                break
    for i, nh in enumerate(heads):  # then distinctive words inside longer headings ("Vendor Name (Supplier)")
        if i in mapping.values() or not nh:
            continue
        words = set(nh.split())
        for field, keys in LOOSE.items():
            if field not in mapping and words & set(keys):
                mapping[field] = i
                break
    return mapping


def _to_date(v, dayfirst: bool) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    ds = find_dates(to_ascii_digits(str(v or "")), dayfirst)
    return ds[0] if ds else None


def _to_amount(v) -> Decimal | None:
    if v is None or str(v).strip() == "":
        return None
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v))
    cleaned = re.sub(r"[^\d.,()\-]", "", to_ascii_digits(str(v)))
    return parse_money(cleaned) if cleaned else None


def _sheets(path: Path):
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        try:
            wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        except Exception as exc:
            raise LedgerError(f"cannot open the workbook ({type(exc).__name__}); it may be corrupt or password-protected") from exc
        try:
            for ws in wb.worksheets:
                yield ws.title, [list(r) for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
    else:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")
        yield "", [list(r) for r in csv.reader(text.splitlines(), delimiter="\t" if path.suffix == ".tsv" else ",")]


def _find_header(rows: list[list], limit: int = 40) -> int | None:
    for i, r in enumerate(rows[:limit]):
        m = set(_map_headers(r))
        if "date" in m and ("amount" in m or {"debit", "credit"} & m):
            return i
    return None


def preview_source(path: str | Path, sample: int = 6, header_row: int | None = None) -> dict:
    """What was detected in each sheet: header row, headers, suggested column mapping, sample rows."""
    path = Path(path)
    out = []
    for name, rows in _sheets(path):
        hi = _find_header(rows)
        guess = (header_row - 1) if header_row and 1 <= header_row <= max(1, len(rows)) else hi
        if guess is None:  # no clear header: offer the first row with several text cells
            guess = next((i for i, r in enumerate(rows[:40]) if sum(1 for c in r if isinstance(c, str) and c.strip()) >= 3), 0)
        headers = [str(c).strip() if c is not None else "" for c in (rows[guess] if rows else [])]
        sug = {f: headers[i] for f, i in _map_headers(rows[guess]).items()} if rows else {}
        body = [[("" if c is None else (c.date().isoformat() if isinstance(c, datetime) else str(c))) for c in r]
                for r in rows[guess + 1: guess + 1 + sample]]
        out.append({"name": name, "header_row": guess + 1, "auto_detected": hi is not None and not header_row, "headers": headers,
                    "suggested": sug, "sample": body, "rows": max(0, len(rows) - guess - 1)})
    return {"sheets": out, "fields": list(FIELDS)}


def load_transactions_with_problems(path: str | Path, default_type: str = "purchase", dayfirst: bool = True,
                                    mapping: dict[str, str] | None = None, sheet: str | None = None,
                                    header_row: int | None = None) -> tuple[list[Transaction], list[str]]:
    """Rows of the source as Transactions, plus the problems met (skipped rows etc.).

    `mapping` is {field: header text}; with it the header row is found by those texts (or `header_row`, 1-based).
    """
    path = Path(path)
    sheets = [(n, r) for n, r in _sheets(path) if not sheet or n == sheet]
    if sheet and not sheets:
        raise LedgerError(f"sheet '{sheet}' was not found in {path.name}")
    out: list[Transaction] = []
    problems: list[str] = []
    for sname, rows in sheets:
        if mapping:
            want = {f: _norm_header(h) for f, h in mapping.items() if h}
            if header_row:
                header_at = header_row - 1
            else:
                header_at = next((i for i, r in enumerate(rows[:60]) if set(want.values()) <= {_norm_header(c) for c in r}), None)
            if header_at is None or header_at >= len(rows):
                if len(sheets) == 1:
                    raise LedgerError("the chosen column headings were not found; check the header row number and mapping")
                continue
            heads = [_norm_header(c) for c in rows[header_at]]
            cols = {}
            for f, nh in want.items():
                if nh not in heads:
                    raise LedgerError(f"column '{mapping[f]}' (for {f}) was not found in the header row")
                cols[f] = heads.index(nh)
        else:
            header_at = _find_header(rows)
            if header_at is None:
                if len(sheets) == 1:
                    raise LedgerError("could not find a header row with at least 'Date' and 'Amount' (or Debit/Credit) columns. "
                                      "Use 'Preview & map columns' on the file to choose the columns yourself.")
                continue
            cols = _map_headers(rows[header_at])
        if "date" not in cols or not ("amount" in cols or {"debit", "credit"} & set(cols)):
            raise LedgerError("the mapping needs a Date column and an Amount (or Debit/Credit) column")
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
            tid = str(get("id")).strip() if get("id") not in (None, "") else (f"{sname}!{rid}" if len(sheets) > 1 else rid)
            if amt is None or d is None:
                problems.append(f"{path.name} {sname + ' ' if sname else ''}row {r_i + 1}: missing/unreadable "
                                f"{'date' if d is None else 'amount'}; row skipped")
                continue
            out.append(Transaction(
                id=tid, type=(str(get("type")).strip().lower().replace(" ", "_") or default_type) if get("type") else default_type,
                date=d, amount=abs(amt), currency=str(get("currency") or "").strip().upper(),
                counterparty=str(get("counterparty") or "").strip(), reference=str(get("reference") or "").strip(),
                account=str(get("account") or "").strip(), description=str(get("description") or "").strip(),
                approver=str(get("approver") or "").strip(), source=path.name))
    if not out:
        raise LedgerError("no usable rows found" + (": " + "; ".join(problems[:5]) if problems else ""))
    return out, problems


def load_transactions(path: str | Path, default_type: str = "purchase", dayfirst: bool = True,
                      **kw) -> list[Transaction]:
    out, problems = load_transactions_with_problems(path, default_type, dayfirst, **kw)
    load_transactions.last_problems = problems  # convenience for the CLI; the web app uses the function above
    return out


load_transactions.last_problems = []
