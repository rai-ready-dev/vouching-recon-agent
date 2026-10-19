"""Append-only, hash-chained audit log: any edit or deletion breaks the chain."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuditLog:
    def __init__(self, path: str | Path | None = None, clock: Callable[[], str] = _now) -> None:
        self.entries: list[dict] = []
        self._path = Path(path) if path else None
        self._clock = clock
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text("")

    @staticmethod
    def _hash(entry: dict) -> str:
        body = {k: v for k, v in entry.items() if k != "hash"}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def append(self, event: str, payload: dict | None = None) -> dict:
        entry = {
            "seq": len(self.entries), "ts": self._clock(), "event": event,
            "payload": payload or {},
            "prev": self.entries[-1]["hash"] if self.entries else "0" * 64,
        }
        entry["hash"] = self._hash(entry)
        self.entries.append(entry)
        if self._path:
            with self._path.open("a") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        return entry

    @classmethod
    def verify(cls, entries: list[dict]) -> bool:
        prev = "0" * 64
        for i, e in enumerate(entries):
            if e["seq"] != i or e["prev"] != prev or e["hash"] != cls._hash(e):
                return False
            prev = e["hash"]
        return True
