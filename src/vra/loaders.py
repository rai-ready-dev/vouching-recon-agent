"""Load source data. JSON for documents, CSV for bank/GL extracts."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .records import BankLine, GLEntry, GoodsReceipt, Invoice, PurchaseOrder


def _json_list(path: Path, model) -> list:
    return [model.model_validate(r) for r in json.loads(path.read_text(encoding="utf-8"))]


def load_vouching_data(folder: str | Path):
    d = Path(folder)
    return (_json_list(d / "invoices.json", Invoice),
            _json_list(d / "purchase_orders.json", PurchaseOrder),
            _json_list(d / "goods_receipts.json", GoodsReceipt))


def _csv_rows(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as fh:
        yield from csv.DictReader(fh)


def load_bank_csv(path: str | Path) -> list[BankLine]:
    return [BankLine.model_validate(r) for r in _csv_rows(Path(path))]


def load_gl_csv(path: str | Path) -> list[GLEntry]:
    return [GLEntry.model_validate(r) for r in _csv_rows(Path(path))]
