"""Business logic for the web app. No HTTP in here, so it is easy to test."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from datetime import date
from pathlib import Path
from typing import BinaryIO

from ..ingest import SUPPORTED_EXT
from ..programmes import TESTS, TYPE_ALIASES, PROGRAMMES
from .auth import Auth
from .config import Settings, foundry_configured
from .db import Database, now


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


LEDGER_EXT = {".xlsx", ".xlsm", ".csv", ".tsv"}   # spreadsheets: columns are mapped
SOURCE_EXT = LEDGER_EXT | {".pdf", ".docx", ".txt", ".eml", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
DECISION_ACTIONS = {"resolved", "confirmed", "followup", "reopen", "comment"}
STATUS_ACTIONS = ("resolved", "confirmed", "followup")
SEV_RANK = "CASE i.severity WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END"
POLICY_KEYS = {  # option -> (min, max)
    "materiality": (0, 1e12), "approval_limit": (0, 1e12), "abs_tolerance": (0, 1e6),
    "trivial_amount": (0, 1e9), "auto_clear_limit": (0, 1e12), "pct_tolerance": (0, 1),
}


def safe_relpath(name: str) -> str | None:
    """Sanitise an uploaded path. Returns None for junk files, raises for traversal attempts."""
    name = name.replace("\\", "/")
    name = re.sub(r"^[A-Za-z]:", "", name)
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ApiError(400, f"Illegal path in upload: {name!r}")
    if not parts or parts[-1].startswith(("~$", ".")) or any(p.startswith(".") and p != parts[-1] for p in parts):
        return None
    parts = [re.sub(r"[\x00-\x1f<>:\"|?*]", "_", p)[:150] for p in parts]
    return "/".join(parts)


class Service:
    def __init__(self, db: Database, settings: Settings, auth: Auth) -> None:
        self.db, self.settings, self.auth = db, settings, auth

    # ------------------------------------------------------------------ helpers
    def log(self, user: dict | None, action: str, detail: str = "", eid: int | None = None, rid: int | None = None) -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO activity(ts, user_id, engagement_id, run_id, action, detail) VALUES(?,?,?,?,?,?)",
                      (now(), user["id"] if user else None, eid, rid, action, detail[:1000]))

    def blob_path(self, eid: int, sha: str) -> Path:
        return self.settings.engagements_dir / str(eid) / "blobs" / sha

    def engagement(self, eid: int) -> dict:
        row = self.db.one("SELECT * FROM engagements WHERE id = ?", (eid,))
        if not row:
            raise ApiError(404, "Engagement not found.")
        return dict(row)

    def run_row(self, rid: int) -> dict:
        row = self.db.one("SELECT * FROM runs WHERE id = ?", (rid,))
        if not row:
            raise ApiError(404, "Run not found.")
        return dict(row)

    # ------------------------------------------------------------------ users
    def list_users(self) -> list[dict]:
        return [self.auth.public(r) | {"created_at": r["created_at"]}
                for r in self.db.query("SELECT * FROM users ORDER BY username")]

    def update_user(self, uid: int, actor: dict, role: str | None, active: bool | None, password: str | None) -> None:
        row = self.db.one("SELECT * FROM users WHERE id = ?", (uid,))
        if not row:
            raise ApiError(404, "User not found.")
        if uid == actor["id"] and (active is False or (role and role != "admin")):
            raise ApiError(400, "You cannot deactivate or demote your own account.")
        with self.db.tx() as c:
            if role:
                if role not in ("admin", "reviewer", "preparer"):
                    raise ApiError(400, "Invalid role.")
                c.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
            if active is not None:
                c.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, uid))
                if not active:
                    c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
            if password:
                from .auth import AuthError, hash_password

                try:
                    self.auth.check_password_policy(password)
                except AuthError as e:
                    raise ApiError(400, str(e))
                c.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(password), uid))
                c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        self.log(actor, "user_updated", f"{row['username']}")

    # ------------------------------------------------------------------ engagements
    @staticmethod
    def _check_date(v: str | None, label: str) -> str | None:
        if not v:
            return None
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError:
            raise ApiError(400, f"{label} must be a date like 2026-03-31.")

    def create_engagement(self, user: dict, name: str, client: str, period_start: str | None,
                          period_end: str | None, default_type: str) -> int:
        name = name.strip()
        if not name:
            raise ApiError(400, "Engagement name is required.")
        ps, pe = self._check_date(period_start, "Period start"), self._check_date(period_end, "Period end")
        if ps and pe and ps > pe:
            raise ApiError(400, "Period start is after period end.")
        with self.db.tx() as c:
            cur = c.execute("INSERT INTO engagements(name, client, period_start, period_end, default_type, created_by, created_at)"
                            " VALUES(?,?,?,?,?,?,?)", (name[:200], client.strip()[:200], ps, pe,
                                                       (default_type or "purchase").strip().lower(), user["id"], now()))
            eid = cur.lastrowid
        self.log(user, "engagement_created", name, eid)
        return eid

    def update_engagement(self, user: dict, eid: int, fields: dict) -> None:
        self.engagement(eid)
        sets, args = [], []
        for k in ("name", "client", "default_type", "status"):
            if k in fields and fields[k] is not None:
                if k == "status" and fields[k] not in ("active", "archived"):
                    raise ApiError(400, "Status must be active or archived.")
                if k == "name" and not str(fields[k]).strip():
                    raise ApiError(400, "Engagement name is required.")
                sets.append(f"{k} = ?")
                args.append(str(fields[k]).strip().lower() if k == "default_type" else str(fields[k]).strip())
        for k, label in (("period_start", "Period start"), ("period_end", "Period end")):
            if k in fields:
                sets.append(f"{k} = ?")
                args.append(self._check_date(fields[k], label))
        if sets:
            with self.db.tx() as c:
                c.execute(f"UPDATE engagements SET {', '.join(sets)} WHERE id = ?", (*args, eid))
            self.log(user, "engagement_updated", ", ".join(fields), eid)

    def list_engagements(self, include_archived: bool = False) -> list[dict]:
        rows = self.db.query(
            "SELECT e.*, u.display_name AS created_by_name,"
            " (SELECT COUNT(*) FROM files f WHERE f.engagement_id=e.id AND f.active=1 AND f.kind='document') AS n_docs,"
            " (SELECT COUNT(*) FROM files f WHERE f.engagement_id=e.id AND f.active=1 AND f.kind='ledger') AS n_ledger,"
            " (SELECT id FROM runs r WHERE r.engagement_id=e.id ORDER BY r.id DESC LIMIT 1) AS last_run_id"
            " FROM engagements e LEFT JOIN users u ON u.id = e.created_by "
            + ("" if include_archived else "WHERE e.status='active' ") + "ORDER BY e.id DESC")
        out = []
        for r in rows:
            d = dict(r)
            d["last_run"] = self.run_public(d["last_run_id"]) if d["last_run_id"] else None
            out.append(d)
        return out

    # ------------------------------------------------------------------ files
    def list_files(self, eid: int) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT f.id, f.kind, f.filename, f.sha256, f.size, f.uploaded_at, u.display_name AS uploaded_by"
            " FROM files f LEFT JOIN users u ON u.id=f.uploaded_by"
            " WHERE f.engagement_id=? AND f.active=1 ORDER BY f.kind DESC, f.filename", (eid,))]

    def _store_blob(self, eid: int, src: BinaryIO, max_bytes: int) -> tuple[str, int]:
        d = self.settings.engagements_dir / str(eid) / "blobs"
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".tmp-{os.getpid()}-{id(src)}"
        h, size = hashlib.sha256(), 0
        try:
            with open(tmp, "wb") as out:
                while chunk := src.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ApiError(413, f"File is larger than the {max_bytes // (1024 * 1024)} MB limit.")
                    h.update(chunk)
                    out.write(chunk)
            sha = h.hexdigest()
            final = d / sha
            if final.exists():
                tmp.unlink()
            else:
                tmp.replace(final)
            return sha, size
        finally:
            if tmp.exists():
                tmp.unlink()

    def add_file(self, user: dict, eid: int, kind: str, filename: str, src: BinaryIO) -> list[dict]:
        """Store one uploaded file (a .zip of documents is expanded). Returns what was stored."""
        eng = self.engagement(eid)
        if eng["status"] != "active":
            raise ApiError(409, "This engagement is archived.")
        if kind not in ("ledger", "document"):
            raise ApiError(400, "kind must be 'ledger' or 'document'.")
        rel = safe_relpath(filename)
        if rel is None:
            return []
        ext = Path(rel).suffix.lower()
        limit = self.settings.max_upload_mb * 1024 * 1024
        if kind == "ledger":
            if ext not in SOURCE_EXT:
                raise ApiError(400, "The source of truth must be an Excel/CSV file, or a document (PDF, Word, text, email, "
                                    f"scan). Got '{ext or rel}'.")
            rel = Path(rel).name
        elif ext == ".zip":
            return self._add_zip(user, eid, src, limit)
        elif ext not in SUPPORTED_EXT:
            raise ApiError(400, f"Unsupported document type '{ext or rel}'. Supported: "
                                f"{', '.join(sorted(SUPPORTED_EXT))} and .zip")
        sha, size = self._store_blob(eid, src, limit)
        self._register_file(user, eid, kind, rel, sha, size)
        return [{"filename": rel, "size": size}]

    def _register_file(self, user: dict, eid: int, kind: str, rel: str, sha: str, size: int) -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO files(engagement_id, kind, filename, sha256, size, uploaded_by, uploaded_at)"
                      " VALUES(?,?,?,?,?,?,?) ON CONFLICT(engagement_id, kind, filename) DO UPDATE SET"
                      " sha256=excluded.sha256, size=excluded.size, uploaded_by=excluded.uploaded_by,"
                      " uploaded_at=excluded.uploaded_at, active=1", (eid, kind, rel, sha, size, user["id"], now()))
        self.log(user, "file_uploaded", f"{kind}: {rel}", eid)

    def _add_zip(self, user: dict, eid: int, src: BinaryIO, limit: int) -> list[dict]:
        tmp = self.settings.data_dir / f".upload-{os.getpid()}-{id(src)}.zip"
        try:
            with open(tmp, "wb") as out:
                size = 0
                while chunk := src.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit * 5:
                        raise ApiError(413, "Zip file is too large.")
                    out.write(chunk)
            try:
                zf = zipfile.ZipFile(tmp)
            except zipfile.BadZipFile:
                raise ApiError(400, "That zip file is corrupt or not a zip file.")
            try:
                return self._store_zip_members(zf, user, eid, limit)
            finally:
                zf.close()
        finally:
            if tmp.exists():
                tmp.unlink()

    def _store_zip_members(self, zf: zipfile.ZipFile, user: dict, eid: int, limit: int) -> list[dict]:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > self.settings.max_zip_files:
            raise ApiError(413, f"Zip contains more than {self.settings.max_zip_files} files.")
        if sum(i.file_size for i in infos) > self.settings.max_zip_mb * 1024 * 1024:
            raise ApiError(413, f"Zip expands to more than {self.settings.max_zip_mb} MB.")
        stored, total_written = [], 0
        for info in infos:
            if info.flag_bits & 0x1:
                raise ApiError(400, "Password-protected zip files are not supported; remove the password first.")
            rel = safe_relpath(info.filename)
            if rel is None or Path(rel).suffix.lower() not in SUPPORTED_EXT:
                continue
            with zf.open(info) as member:
                sha, fsize = self._store_blob(eid, member, limit)
            total_written += fsize
            if total_written > self.settings.max_zip_mb * 1024 * 1024:
                raise ApiError(413, f"Zip expands to more than {self.settings.max_zip_mb} MB.")
            self._register_file(user, eid, "document", rel, sha, fsize)
            stored.append({"filename": rel, "size": fsize})
        return stored

    def remove_file(self, user: dict, fid: int) -> None:
        row = self.db.one("SELECT * FROM files WHERE id = ?", (fid,))
        if not row:
            raise ApiError(404, "File not found.")
        with self.db.tx() as c:
            c.execute("UPDATE files SET active = 0 WHERE id = ?", (fid,))
        self.log(user, "file_removed", f"{row['kind']}: {row['filename']}", row["engagement_id"])

    # ------------------------------------------------------------------ options
    def normalize_options(self, raw: dict) -> dict:
        o = {"extractor": raw.get("extractor", "heuristic"), "analyst": raw.get("analyst", "rule"),
             "ocr": raw.get("ocr", "none"), "dayfirst": bool(raw.get("dayfirst", True)), "policy": {},
             "programmes_json": (raw.get("programmes_json") or "").strip(),
             "risk_signals": bool(raw.get("risk_signals", False)), "benchmark": bool(raw.get("benchmark", False)),
             "ai_linking": bool(raw.get("ai_linking", False)), "sample_interval": None, "sample_seed": 1}
        for key, allowed in (("extractor", ("heuristic", "foundry")), ("analyst", ("rule", "foundry")),
                             ("ocr", ("none", "foundry"))):
            if o[key] not in allowed:
                raise ApiError(400, f"{key} must be one of {', '.join(allowed)}.")
        wants_ai = "foundry" in (o["extractor"], o["analyst"], o["ocr"]) or o["benchmark"] or o["ai_linking"]
        if wants_ai and not foundry_configured():
            raise ApiError(400, "Microsoft Foundry is not configured on the server. Set AZURE_OPENAI_ENDPOINT and "
                                "AZURE_OPENAI_DEPLOYMENT in .env and restart the app, or choose the offline options.")
        si = raw.get("sample_interval")
        if si not in (None, ""):
            try:
                if float(si) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise ApiError(400, "The sampling interval must be a positive number.")
            o["sample_interval"] = str(si)
        try:
            o["sample_seed"] = int(raw.get("sample_seed") or 1)
        except (TypeError, ValueError):
            raise ApiError(400, "The sampling seed must be a whole number.")
        for k, v in (raw.get("policy") or {}).items():
            if k not in POLICY_KEYS:
                raise ApiError(400, f"Unknown policy setting '{k}'.")
            if v in (None, ""):
                continue
            try:
                num = float(v)
            except (TypeError, ValueError):
                raise ApiError(400, f"{k} must be a number.")
            lo, hi = POLICY_KEYS[k]
            if not lo <= num <= hi:
                raise ApiError(400, f"{k} is out of range.")
            o["policy"][k] = str(v)
        if o["programmes_json"]:
            try:
                data = json.loads(o["programmes_json"])
                assert isinstance(data, dict)
                for steps in data.values():
                    for s in steps:
                        if s["test"] not in TESTS:
                            raise ApiError(400, f"Unknown test '{s['test']}'. Known tests: {', '.join(sorted(TESTS))}")
            except ApiError:
                raise
            except Exception:
                raise ApiError(400, "Programmes must be valid JSON like {\"lease\": [{\"test\": \"amount_agrees\"}]}.")
        return o

    # ------------------------------------------------------------------ runs
    def create_run(self, user: dict, eid: int, raw_options: dict) -> int:
        eng = self.engagement(eid)
        if eng["status"] != "active":
            raise ApiError(409, "This engagement is archived.")
        opts = self.normalize_options(raw_options)
        files = self.db.query("SELECT kind, filename, sha256 FROM files WHERE engagement_id=? AND active=1", (eid,))
        if not any(f["kind"] == "ledger" for f in files):
            raise ApiError(400, "Upload the source of truth (the ledger or listing to vouch) first.")
        if not any(f["kind"] == "document" for f in files):
            raise ApiError(400, "Upload the supporting documents first.")
        if self.db.one("SELECT 1 FROM runs WHERE engagement_id=? AND status IN ('queued','running')", (eid,)):
            raise ApiError(409, "A run is already in progress for this engagement.")
        with self.db.tx() as c:
            cur = c.execute("INSERT INTO runs(engagement_id, status, options_json, started_by, started_at)"
                            " VALUES(?,?,?,?,?)", (eid, "queued", json.dumps(opts), user["id"], now()))
            rid = cur.lastrowid
            c.executemany("INSERT INTO run_files(run_id, kind, filename, sha256) VALUES(?,?,?,?)",
                          [(rid, f["kind"], f["filename"], f["sha256"]) for f in files])
        self.log(user, "run_started", f"extractor={opts['extractor']} analyst={opts['analyst']} ocr={opts['ocr']}", eid, rid)
        return rid

    def run_public(self, rid: int) -> dict:
        r = self.run_row(rid)
        r["options"] = json.loads(r.pop("options_json"))
        r["summary"] = json.loads(r["summary_json"]) if r.get("summary_json") else None
        r.pop("summary_json", None)
        for k, label in (("started_by", "started_by_name"), ("signoff_by", "signoff_by_name")):
            if r.get(k):
                row = self.db.one("SELECT display_name FROM users WHERE id=?", (r[k],))
                r[label] = row["display_name"] if row else ""
        r["locked"] = bool(r["signoff_at"])
        return r

    def list_runs(self, eid: int) -> list[dict]:
        return [self.run_public(r["id"]) for r in self.db.query(
            "SELECT id FROM runs WHERE engagement_id=? ORDER BY id DESC", (eid,))]

    def run_files(self, rid: int) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT kind, filename, sha256 FROM run_files WHERE run_id=? ORDER BY kind, filename", (rid,))]

    # ------------------------------------------------------------------ items & decisions
    _STATUS_SQL = ("(SELECT d.action FROM decisions d WHERE d.run_id = i.run_id AND d.txn_id = i.txn_id "
                   "AND d.action <> 'comment' ORDER BY d.id DESC LIMIT 1)")

    @staticmethod
    def review_status(last_action: str | None) -> str:
        return last_action if last_action in STATUS_ACTIONS else "pending"

    def list_items(self, rid: int, disposition: str | None = None, status: str | None = None,
                   code: str | None = None, severity: str | None = None, q: str | None = None,
                   limit: int = 50, offset: int = 0) -> dict:
        where, args = ["i.run_id = ?"], [rid]
        if disposition:
            where.append("i.disposition = ?"); args.append(disposition)
        if severity:
            where.append("i.severity = ?"); args.append(severity)
        if code:
            where.append("(',' || i.codes || ',') LIKE ?"); args.append(f"%,{code},%")
        if q:
            where.append("i.search LIKE ?"); args.append("%" + q.lower().replace("%", "") + "%")
        if status == "needs_decision":
            where.append(f"i.disposition IN ('review','escalate') AND COALESCE({self._STATUS_SQL},'') NOT IN ('resolved','confirmed')")
        elif status == "pending":
            where.append(f"COALESCE({self._STATUS_SQL},'') NOT IN ('resolved','confirmed','followup')")
        elif status in STATUS_ACTIONS:
            where.append(f"{self._STATUS_SQL} = ?"); args.append(status)
        w = " AND ".join(where)
        total = self.db.one(f"SELECT COUNT(*) n FROM items i WHERE {w}", args)["n"]
        rows = self.db.query(
            f"SELECT i.txn_id, i.kind, i.disposition, i.severity, i.exposure, i.codes, i.data_json,"
            f" {self._STATUS_SQL} AS last_action FROM items i WHERE {w}"
            f" ORDER BY CASE i.disposition WHEN 'escalate' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,"
            f" {SEV_RANK} DESC, i.exposure DESC, i.txn_id LIMIT ? OFFSET ?", (*args, min(max(limit, 1), 500), max(offset, 0)))
        items = []
        for r in rows:
            data = json.loads(r["data_json"])
            t, v = data["transaction"], data.get("voucher")
            items.append({
                "txn_id": r["txn_id"], "kind": r["kind"], "disposition": r["disposition"], "severity": r["severity"],
                "exposure": r["exposure"], "codes": [c for c in r["codes"].split(",") if c],
                "status": self.review_status(r["last_action"]),
                "date": t["date"], "type": t["type"], "counterparty": t["counterparty"], "reference": t["reference"],
                "amount": t["amount"], "currency": t["currency"],
                "document": data["source_file"]["filename"] if data.get("source_file") else None,
                "summary": (data["findings"][0]["message"] if data["findings"] else "No exceptions")})
        return {"total": total, "items": items}

    def item_detail(self, rid: int, txn_id: str) -> dict:
        row = self.db.one("SELECT data_json FROM items WHERE run_id=? AND txn_id=?", (rid, txn_id))
        if not row:
            raise ApiError(404, "Item not found.")
        data = json.loads(row["data_json"])
        data["decisions"] = [dict(r) for r in self.db.query(
            "SELECT d.id, d.action, d.note, d.created_at, d.carried_from, u.display_name AS user"
            " FROM decisions d LEFT JOIN users u ON u.id=d.user_id WHERE d.run_id=? AND d.txn_id=? ORDER BY d.id",
            (rid, txn_id))]
        last = [d for d in data["decisions"] if d["action"] != "comment"]
        data["status"] = self.review_status(last[-1]["action"] if last else None)
        return data

    def add_decision(self, user: dict, rid: int, txn_id: str, action: str, note: str) -> None:
        run = self.run_row(rid)
        if run["signoff_at"]:
            raise ApiError(409, "This run has been signed off and is locked. Start a new run to make changes.")
        if run["status"] != "completed":
            raise ApiError(409, "The run has not completed.")
        if action not in DECISION_ACTIONS:
            raise ApiError(400, "Invalid action.")
        if action != "comment" and user["role"] == "preparer":
            raise ApiError(403, "Only a reviewer can resolve or confirm items. You can add comments.")
        item = self.db.one("SELECT disposition FROM items WHERE run_id=? AND txn_id=?", (rid, txn_id))
        if not item:
            raise ApiError(404, "Item not found.")
        note = (note or "").strip()
        if action in ("resolved", "confirmed", "followup", "comment") and not note and item["disposition"] != "auto_cleared":
            raise ApiError(400, "Please add a short note explaining your decision.")
        with self.db.tx() as c:
            c.execute("INSERT INTO decisions(run_id, txn_id, user_id, action, note, created_at) VALUES(?,?,?,?,?,?)",
                      (rid, txn_id, user["id"], action, note[:2000], now()))
        self.log(user, "decision", f"{txn_id}: {action}", run["engagement_id"], rid)

    def outstanding(self, rid: int) -> int:
        return self.db.one(
            f"SELECT COUNT(*) n FROM items i WHERE i.run_id=? AND i.disposition IN ('review','escalate')"
            f" AND COALESCE({self._STATUS_SQL},'') NOT IN ('resolved','confirmed')", (rid,))["n"]

    def sign_off(self, user: dict, rid: int, note: str, acknowledge_issues: bool) -> None:
        run = self.run_public(rid)
        if run["status"] != "completed":
            raise ApiError(409, "Only a completed run can be signed off.")
        if run["locked"]:
            raise ApiError(409, "This run is already signed off.")
        if user["role"] not in ("admin", "reviewer"):
            raise ApiError(403, "Only a reviewer can sign off.")
        if run["started_by"] == user["id"] and not self.settings.solo:
            raise ApiError(403, "You prepared this run, so someone else must sign it off "
                                "(segregation of duties).")
        n = self.outstanding(rid)
        if n:
            raise ApiError(409, f"{n} item(s) still need a decision (resolved or confirmed) before sign-off.")
        s = run["summary"] or {}
        if (s.get("intake_issues") or s.get("unlinked_documents")) and not acknowledge_issues:
            raise ApiError(409, "This run has unreadable or unlinked documents. Tick the acknowledgement box to "
                                "confirm you have considered them.")
        with self.db.tx() as c:
            c.execute("UPDATE runs SET signoff_by=?, signoff_at=?, signoff_note=? WHERE id=?",
                      (user["id"], now(), (note or "").strip()[:2000], rid))
        self.log(user, "signed_off", note or "", run["engagement_id"], rid)

    # ------------------------------------------------------------------ corrections
    def voucher_documents(self, rid: int) -> dict:
        path = self.settings.runs_dir / str(rid) / "documents.json"
        if not path.is_file():
            raise ApiError(404, "No document data for this run.")
        return json.loads(path.read_text(encoding="utf-8"))

    EDITABLE = {"doc_type", "date", "amount", "subtotal", "tax", "currency", "parties", "references", "approvals"}

    def set_voucher_override(self, user: dict, eid: int, voucher_id: str, fields: dict, note: str) -> None:
        self.engagement(eid)
        clean = {}
        for k, v in fields.items():
            if k not in self.EDITABLE:
                raise ApiError(400, f"Field '{k}' cannot be corrected.")
            if k in ("parties", "references", "approvals"):
                v = [str(x).strip() for x in (v if isinstance(v, list) else str(v).split(";")) if str(x).strip()]
            elif k == "date":
                v = self._check_date(v, "Date")
            elif k in ("amount", "subtotal", "tax"):
                if v in (None, ""):
                    v = None
                else:
                    try:
                        v = str(abs(float(str(v).replace(",", ""))))
                    except ValueError:
                        raise ApiError(400, f"{k} must be a number.")
            elif k == "currency":
                v = (str(v).strip().upper() or None) if v else None
            elif k == "doc_type":
                v = str(v).strip().lower().replace(" ", "_") or "unknown"
            clean[k] = v
        if not clean:
            raise ApiError(400, "Nothing to correct.")
        with self.db.tx() as c:
            c.execute("INSERT INTO voucher_overrides(engagement_id, voucher_id, fields_json, user_id, note, created_at)"
                      " VALUES(?,?,?,?,?,?) ON CONFLICT(engagement_id, voucher_id) DO UPDATE SET"
                      " fields_json=excluded.fields_json, user_id=excluded.user_id, note=excluded.note,"
                      " created_at=excluded.created_at", (eid, voucher_id, json.dumps(clean), user["id"], note[:500], now()))
        self.log(user, "extraction_corrected", f"{voucher_id}: {', '.join(clean)}", eid)

    def clear_voucher_override(self, user: dict, eid: int, voucher_id: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM voucher_overrides WHERE engagement_id=? AND voucher_id=?", (eid, voucher_id))
        self.log(user, "extraction_correction_removed", voucher_id, eid)

    def set_link_override(self, user: dict, eid: int, txn_id: str, voucher_id: str | None, note: str) -> None:
        self.engagement(eid)
        with self.db.tx() as c:
            c.execute("INSERT INTO link_overrides(engagement_id, txn_id, voucher_id, user_id, note, created_at)"
                      " VALUES(?,?,?,?,?,?) ON CONFLICT(engagement_id, txn_id) DO UPDATE SET"
                      " voucher_id=excluded.voucher_id, user_id=excluded.user_id, note=excluded.note,"
                      " created_at=excluded.created_at", (eid, txn_id, voucher_id, user["id"], note[:500], now()))
        self.log(user, "link_overridden", f"{txn_id} -> {voucher_id or 'no document'}", eid)

    def clear_link_override(self, user: dict, eid: int, txn_id: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM link_overrides WHERE engagement_id=? AND txn_id=?", (eid, txn_id))
        self.log(user, "link_override_removed", txn_id, eid)

    def overrides(self, eid: int) -> dict:
        return {
            "vouchers": {r["voucher_id"]: json.loads(r["fields_json"]) | {"_note": r["note"]}
                         for r in self.db.query("SELECT * FROM voucher_overrides WHERE engagement_id=?", (eid,))},
            "links": {r["txn_id"]: {"voucher_id": r["voucher_id"], "note": r["note"]}
                      for r in self.db.query("SELECT * FROM link_overrides WHERE engagement_id=?", (eid,))}}

    def activity(self, eid: int, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT a.ts, a.action, a.detail, a.run_id, u.display_name AS user FROM activity a"
            " LEFT JOIN users u ON u.id=a.user_id WHERE a.engagement_id=? ORDER BY a.id DESC LIMIT ?", (eid, limit))]

    # ------------------------------------------------------------------ source of truth setup
    def _file(self, eid: int, fid: int) -> dict:
        row = self.db.one("SELECT * FROM files WHERE id=? AND engagement_id=? AND active=1", (fid, eid))
        if not row:
            raise ApiError(404, "File not found.")
        return dict(row)

    def source_preview(self, eid: int, fid: int, header_row: int | None = None) -> dict:
        from ..ledger import LedgerError, preview_source

        f = self._file(eid, fid)
        ext = Path(f["filename"]).suffix.lower()
        saved = self.source_settings(eid).get(f["filename"], {})
        if ext not in LEDGER_EXT:
            return {"kind": "document", "filename": f["filename"], "settings": saved,
                    "note": "Entries will be read from the text of this document (a date, some text and an amount on each line, "
                            "or with the AI reader for harder layouts). There are no columns to map."}
        blob = self.blob_path(eid, f["sha256"])
        tmp = self.settings.data_dir / f".preview-{os.getpid()}-{fid}{ext}"
        try:
            tmp.write_bytes(blob.read_bytes())
            return {"kind": "spreadsheet", "filename": f["filename"], "settings": saved, **preview_source(tmp, header_row=header_row)}
        except LedgerError as e:
            raise ApiError(400, str(e))
        finally:
            tmp.unlink(missing_ok=True)

    def source_settings(self, eid: int) -> dict:
        return {r["filename"]: json.loads(r["settings_json"]) for r in
                self.db.query("SELECT filename, settings_json FROM source_settings WHERE engagement_id=?", (eid,))}

    def set_source_settings(self, user: dict, eid: int, filename: str, settings: dict) -> None:
        from ..ledger import FIELDS

        self.engagement(eid)
        if not self.db.one("SELECT 1 FROM files WHERE engagement_id=? AND kind='ledger' AND filename=? AND active=1", (eid, filename)):
            raise ApiError(404, "That source file is not part of this engagement.")
        clean: dict = {}
        mapping = {k: str(v).strip() for k, v in (settings.get("mapping") or {}).items() if str(v or "").strip()}
        bad = [k for k in mapping if k not in FIELDS]
        if bad:
            raise ApiError(400, f"Unknown field(s): {', '.join(bad)}")
        if mapping:
            if "date" not in mapping or not ("amount" in mapping or "debit" in mapping or "credit" in mapping):
                raise ApiError(400, "Choose at least a Date column and an Amount (or Debit/Credit) column.")
            clean["mapping"] = mapping
        if settings.get("sheet"):
            clean["sheet"] = str(settings["sheet"])
        if settings.get("header_row"):
            try:
                clean["header_row"] = max(1, int(settings["header_row"]))
            except (TypeError, ValueError):
                raise ApiError(400, "The header row must be a whole number.")
        if settings.get("default_type"):
            clean["default_type"] = str(settings["default_type"]).strip().lower()
        with self.db.tx() as c:
            if clean:
                c.execute("INSERT INTO source_settings(engagement_id, filename, settings_json, user_id, created_at) VALUES(?,?,?,?,?)"
                          " ON CONFLICT(engagement_id, filename) DO UPDATE SET settings_json=excluded.settings_json,"
                          " user_id=excluded.user_id, created_at=excluded.created_at",
                          (eid, filename, json.dumps(clean), user["id"], now()))
            else:
                c.execute("DELETE FROM source_settings WHERE engagement_id=? AND filename=?", (eid, filename))
        self.log(user, "source_mapping_saved", filename, eid)

    # ------------------------------------------------------------------ vendor aliases (learned from reviewers)
    def alias_map(self, eid: int) -> dict[str, str]:
        return {r["alias"]: r["canonical"] for r in self.db.query("SELECT alias, canonical FROM aliases WHERE engagement_id=?", (eid,))}

    def list_aliases(self, eid: int) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT a.alias, a.canonical, a.created_at, u.display_name AS user FROM aliases a LEFT JOIN users u ON u.id=a.user_id"
            " WHERE a.engagement_id=? ORDER BY a.alias", (eid,))]

    def add_alias(self, user: dict, eid: int, name_a: str, name_b: str) -> None:
        from ..textutil import normalize_name

        self.engagement(eid)
        a, b = normalize_name(name_a), normalize_name(name_b)
        if not a or not b:
            raise ApiError(400, "Both names are required.")
        if a == b:
            raise ApiError(400, "Those names are already treated as the same.")
        with self.db.tx() as c:  # b (the ledger's name) is the canonical one; a maps onto it
            c.execute("INSERT INTO aliases(engagement_id, alias, canonical, user_id, created_at) VALUES(?,?,?,?,?)"
                      " ON CONFLICT(engagement_id, alias) DO UPDATE SET canonical=excluded.canonical,"
                      " user_id=excluded.user_id, created_at=excluded.created_at", (eid, a, b, user["id"], now()))
        self.log(user, "alias_added", f"{name_a} = {name_b}", eid)

    def remove_alias(self, user: dict, eid: int, alias: str) -> None:
        with self.db.tx() as c:
            c.execute("DELETE FROM aliases WHERE engagement_id=? AND alias=?", (eid, alias))
        self.log(user, "alias_removed", alias, eid)

    # ------------------------------------------------------------------ performance and quality
    def run_performance(self, rid: int) -> dict:
        run = self.run_public(rid)
        if run["status"] != "completed":
            raise ApiError(409, "The run has not completed.")
        wd = self.settings.runs_dir / str(rid)
        perf = json.loads((wd / "perf.json").read_text(encoding="utf-8")) if (wd / "perf.json").is_file() else {}
        bench = json.loads((wd / "benchmark.json").read_text(encoding="utf-8")) if (wd / "benchmark.json").is_file() else None
        report = json.loads((wd / "report.json").read_text(encoding="utf-8")) if (wd / "report.json").is_file() else {}
        return {"run": {"id": rid, "started_at": run["started_at"], "options": run["options"]}, "perf": perf,
                "benchmark": bench, "review": self.review_metrics(rid), "insights": report.get("insights", {}),
                "sampling": report.get("sampling", {}), "linking": report.get("linking", {})}

    def review_metrics(self, rid: int) -> dict:
        """How the agent's flags held up against what reviewers decided: the real-world precision measure."""
        rows = self.db.query(
            f"SELECT i.txn_id, i.disposition, i.codes, {self._STATUS_SQL} AS last_action FROM items i WHERE i.run_id=?", (rid,))
        by_code: dict[str, dict] = {}
        tot = {"flagged": 0, "confirmed": 0, "resolved": 0, "followup": 0, "pending": 0}
        for r in rows:
            codes = [c for c in r["codes"].split(",") if c]
            if not codes or r["disposition"] == "auto_cleared":  # signal-only items never needed a decision
                continue
            st = self.review_status(r["last_action"])
            tot["flagged"] += 1
            tot[st] += 1
            for c in codes:
                d = by_code.setdefault(c, {"flagged": 0, "confirmed": 0, "resolved": 0, "followup": 0, "pending": 0})
                d["flagged"] += 1
                d[st] += 1
        for d in list(by_code.values()) + [tot]:
            decided = d["confirmed"] + d["resolved"]
            d["precision"] = round(d["confirmed"] / decided, 3) if decided else None  # confirmed / (confirmed + dismissed)
        run = self.run_row(rid)
        n_docs = (json.loads(run["summary_json"]) or {}).get("vouchers", 0) if run.get("summary_json") else 0
        ov = self.overrides(run["engagement_id"])
        return {"totals": tot, "by_finding": dict(sorted(by_code.items())),
                "corrections": {"extraction_corrections": len(ov["vouchers"]), "documents": n_docs,
                                "correction_rate": round(len(ov["vouchers"]) / n_docs, 3) if n_docs else None,
                                "manual_links": len(ov["links"]), "aliases_learned": len(self.alias_map(run["engagement_id"]))}}

    def evaluate_run(self, rid: int, key_text: str) -> dict:
        from ..evaluation import evaluate_items, parse_answer_key

        run = self.run_row(rid)
        if run["status"] != "completed":
            raise ApiError(409, "The run has not completed.")
        key = parse_answer_key(key_text)
        if not key:
            raise ApiError(400, "The answer key is empty. Use lines like: R12,AMOUNT_MISMATCH  or  R13,CLEAN")
        predicted = {r["txn_id"]: {c for c in r["codes"].split(",") if c}
                     for r in self.db.query("SELECT txn_id, codes FROM items WHERE run_id=?", (rid,))}
        return evaluate_items(key, predicted)

    def engagement_quality(self, eid: int) -> list[dict]:
        out = []
        for r in self.db.query("SELECT id FROM runs WHERE engagement_id=? AND status='completed' ORDER BY id", (eid,)):
            run = self.run_public(r["id"])
            m = self.review_metrics(r["id"])["totals"]
            s = run["summary"] or {}
            out.append({"run": run["id"], "started_at": run["started_at"], "exceptions": s.get("exceptions"), "seconds": s.get("seconds"),
                        "tokens": s.get("tokens"), "llm_calls": s.get("llm_calls"), "extractor": s.get("extractor"),
                        "confirmed": m["confirmed"], "resolved": m["resolved"], "pending": m["pending"], "precision": m["precision"]})
        return out

    def programmes(self) -> dict:
        return {"programmes": PROGRAMMES, "tests": sorted(TESTS), "type_aliases": TYPE_ALIASES}
