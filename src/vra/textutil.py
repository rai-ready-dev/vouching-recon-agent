"""Normalisation and fuzzy-similarity helpers."""
from __future__ import annotations

import difflib
import re

try:  # optional, faster and better calibrated
    from rapidfuzz import fuzz as _fuzz
except ImportError:  # pragma: no cover - exercised only without rapidfuzz
    _fuzz = None

_CORP_SUFFIXES = {
    "ltd", "limited", "llc", "inc", "incorporated", "co", "corp", "corporation",
    "plc", "pjsc", "fze", "llp", "gmbh", "company", "the",
}


def normalize_ref(value: str | None) -> str:
    """Upper-case alphanumerics only: 'inv-001' -> 'INV001'."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def normalize_number(value: str | None) -> str:
    """Like normalize_ref but also ignores padding zeros: INV-00123 == INV123."""
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", normalize_ref(value))


def normalize_name(value: str | None) -> str:
    text = (value or "").lower().replace("&", " and ")
    tokens = [t for t in re.sub(r"[^a-z0-9 ]", " ", text).split() if t not in _CORP_SUFFIXES]
    return " ".join(tokens)


def similarity(a: str, b: str) -> float:
    """0..1 similarity of two already-normalised strings."""
    if not a or not b:
        return 0.0
    if _fuzz is not None:
        return _fuzz.ratio(a, b) / 100.0
    return difflib.SequenceMatcher(None, a, b).ratio()
