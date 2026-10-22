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


# Arabic-Indic (U+0660..) and Eastern/Persian (U+06F0..) digits and Arabic separators -> ASCII
_DIGITS = str.maketrans("\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"
                        "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9", "01234567890123456789")


def to_ascii_digits(text: str | None) -> str:
    """Western digits and separators, so amounts, dates and numbers in Arabic-script documents can be read."""
    return (text or "").translate(_DIGITS).replace("\u066b", ".").replace("\u066c", ",")


def normalize_ref(value: str | None) -> str:
    """Upper-case letters and digits of any script only: 'inv-001' -> 'INV001'."""
    return re.sub(r"[\W_]+", "", to_ascii_digits(value).upper())


def normalize_number(value: str | None) -> str:
    """Like normalize_ref but also ignores padding zeros: INV-00123 == INV123."""
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", normalize_ref(value))


def normalize_name(value: str | None) -> str:
    text = to_ascii_digits(value).lower().replace("&", " and ")
    tokens = [t for t in re.sub(r"[\W_]+", " ", text).split() if t not in _CORP_SUFFIXES]
    return " ".join(tokens)


def canon_name(name: str | None, aliases: dict[str, str] | None = None) -> str:
    """Normalised name with reviewer-approved aliases applied (alias -> canonical)."""
    n = normalize_name(name)
    return (aliases or {}).get(n, n)


def similarity(a: str, b: str) -> float:
    """0..1 similarity of two already-normalised strings."""
    if not a or not b:
        return 0.0
    if _fuzz is not None:
        return _fuzz.ratio(a, b) / 100.0
    return difflib.SequenceMatcher(None, a, b).ratio()
