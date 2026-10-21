"""SQLite storage (single file, WAL). Swap for Postgres when the team outgrows one server."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, display_name TEXT NOT NULL,
  pw_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','reviewer','preparer')),
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS engagements(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, client TEXT NOT NULL DEFAULT '',
  period_start TEXT, period_end TEXT, default_type TEXT NOT NULL DEFAULT 'purchase',
  status TEXT NOT NULL DEFAULT 'active', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, engagement_id INTEGER NOT NULL REFERENCES engagements(id),
  kind TEXT NOT NULL CHECK(kind IN ('ledger','document')), filename TEXT NOT NULL, sha256 TEXT NOT NULL,
  size INTEGER NOT NULL, uploaded_by INTEGER REFERENCES users(id), uploaded_at TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1, UNIQUE(engagement_id, kind, filename));
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, engagement_id INTEGER NOT NULL REFERENCES engagements(id), status TEXT NOT NULL,
  options_json TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '', started_by INTEGER REFERENCES users(id),
  started_at TEXT NOT NULL, finished_at TEXT, error TEXT, summary_json TEXT,
  signoff_by INTEGER REFERENCES users(id), signoff_at TEXT, signoff_note TEXT);
CREATE TABLE IF NOT EXISTS run_files(
  run_id INTEGER NOT NULL REFERENCES runs(id), kind TEXT NOT NULL, filename TEXT NOT NULL,
  sha256 TEXT NOT NULL, PRIMARY KEY(run_id, kind, filename));
CREATE TABLE IF NOT EXISTS items(
  run_id INTEGER NOT NULL REFERENCES runs(id), txn_id TEXT NOT NULL, kind TEXT NOT NULL,
  disposition TEXT NOT NULL, severity TEXT NOT NULL, exposure REAL NOT NULL, codes TEXT NOT NULL,
  signature TEXT NOT NULL, search TEXT NOT NULL, data_json TEXT NOT NULL, PRIMARY KEY(run_id, txn_id));
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), txn_id TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id), action TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  carried_from INTEGER, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_decisions ON decisions(run_id, txn_id);
CREATE TABLE IF NOT EXISTS extraction_cache(
  sha256 TEXT NOT NULL, extractor TEXT NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(sha256, extractor));
CREATE TABLE IF NOT EXISTS ocr_cache(
  sha256 TEXT NOT NULL, engine TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(sha256, engine));
CREATE TABLE IF NOT EXISTS voucher_overrides(
  engagement_id INTEGER NOT NULL REFERENCES engagements(id), voucher_id TEXT NOT NULL,
  fields_json TEXT NOT NULL, user_id INTEGER, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  PRIMARY KEY(engagement_id, voucher_id));
CREATE TABLE IF NOT EXISTS link_overrides(
  engagement_id INTEGER NOT NULL REFERENCES engagements(id), txn_id TEXT NOT NULL, voucher_id TEXT,
  user_id INTEGER, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, PRIMARY KEY(engagement_id, txn_id));
CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, user_id INTEGER, engagement_id INTEGER, run_id INTEGER,
  action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def tx(self):
        """One connection per unit of work; commits on success, rolls back on error."""
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def query(self, sql: str, args=()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def one(self, sql: str, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None
