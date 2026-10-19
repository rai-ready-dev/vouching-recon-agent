"""Evidence layer: immutable, hash-stamped records addressable by evidence id.

Every number the agent reports must trace back to a record here. The store
refuses to overwrite a record with different content, and `verify_integrity`
detects any in-memory tampering after the fact.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from .records import _Record


class EvidenceConflict(Exception):
    """Same evidence id re-added with different content."""


class EvidenceNotFound(KeyError):
    pass


class EvidenceRef(BaseModel):
    id: str
    kind: str
    sha256: str
    source: str = ""


def canonical_json(record: _Record) -> str:
    return json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def digest(record: _Record) -> str:
    return hashlib.sha256(canonical_json(record).encode()).hexdigest()


class EvidenceStore:
    def __init__(self) -> None:
        self._records: dict[str, _Record] = {}
        self._refs: dict[str, EvidenceRef] = {}

    def add(self, record: _Record, source: str = "") -> EvidenceRef:
        eid = record.evidence_id
        sha = digest(record)
        existing = self._refs.get(eid)
        if existing:
            if existing.sha256 != sha:
                raise EvidenceConflict(f"{eid} already stored with different content")
            return existing
        ref = EvidenceRef(id=eid, kind=type(record).__name__, sha256=sha, source=source)
        self._records[eid] = record
        self._refs[eid] = ref
        return ref

    def __contains__(self, evidence_id: str) -> bool:
        return evidence_id in self._records

    def __len__(self) -> int:
        return len(self._records)

    def get(self, evidence_id: str) -> _Record:
        try:
            return self._records[evidence_id]
        except KeyError:
            raise EvidenceNotFound(evidence_id) from None

    def ref(self, evidence_id: str) -> EvidenceRef:
        return self._refs[evidence_id]

    def field(self, evidence_id: str, path: str) -> Any:
        """Resolve a dotted path such as 'total' or 'lines.0.qty' on a record."""
        obj: Any = self.get(evidence_id)
        for part in path.split("."):
            obj = obj[int(part)] if part.isdigit() else getattr(obj, part)
        return obj

    def payload(self, evidence_id: str) -> dict:
        return self.get(evidence_id).model_dump(mode="json")

    def manifest(self) -> list[EvidenceRef]:
        return [self._refs[k] for k in sorted(self._refs)]

    def verify_integrity(self) -> list[str]:
        """Evidence ids whose current content no longer matches the stored hash."""
        return [eid for eid, rec in self._records.items() if digest(rec) != self._refs[eid].sha256]


def numeric_leaves(payload: Any) -> list[Decimal]:
    """All numeric values inside a record payload (used to bound LLM-cited figures)."""
    out: list[Decimal] = []
    if isinstance(payload, dict):
        for v in payload.values():
            out.extend(numeric_leaves(v))
    elif isinstance(payload, list):
        for v in payload:
            out.extend(numeric_leaves(v))
    elif isinstance(payload, str):
        try:
            out.append(Decimal(payload))
        except Exception:
            pass
    return out
