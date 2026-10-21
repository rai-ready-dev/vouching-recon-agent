"""Accounts, password hashing and server-side sessions (stdlib only)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from .config import Settings
from .db import Database, now

ITERATIONS = 310_000
ROLES = ("admin", "reviewer", "preparer")


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    MAX_FAILS, WINDOW = 5, 15 * 60

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db, self.settings = db, settings
        self._fails: dict[str, list[float]] = {}

    # ---- users
    def user_count(self) -> int:
        return self.db.one("SELECT COUNT(*) n FROM users")["n"]

    def check_password_policy(self, password: str) -> None:
        if len(password) < self.settings.min_password_len:
            raise AuthError(f"Password must be at least {self.settings.min_password_len} characters.", 400)

    def create_user(self, username: str, display_name: str, password: str, role: str,
                    only_if_empty: bool = False) -> int:
        username = username.strip()
        if not username or len(username) > 64:
            raise AuthError("Username is required (max 64 characters).", 400)
        if role not in ROLES:
            raise AuthError("Role must be admin, reviewer or preparer.", 400)
        self.check_password_policy(password)
        with self.db.tx() as c:
            if only_if_empty and c.execute("SELECT 1 FROM users").fetchone():
                raise AuthError("Setup has already been completed.", 409)
            if c.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise AuthError("That username already exists.", 409)
            cur = c.execute("INSERT INTO users(username, display_name, pw_hash, role, created_at) VALUES(?,?,?,?,?)",
                            (username, display_name.strip() or username, hash_password(password), role, now()))
            return cur.lastrowid

    # ---- login / sessions
    def _throttled(self, key: str) -> bool:
        t = time.time()
        recent = [x for x in self._fails.get(key, []) if t - x < self.WINDOW]
        self._fails[key] = recent
        return len(recent) >= self.MAX_FAILS

    def login(self, username: str, password: str) -> tuple[str, dict]:
        key = username.strip().lower()
        if self._throttled(key):
            raise AuthError("Too many failed attempts. Try again in 15 minutes.", 429)
        row = self.db.one("SELECT * FROM users WHERE username = ?", (username.strip(),))
        # always run a hash so unknown users cost the same as wrong passwords
        ok = verify_password(password, row["pw_hash"] if row else hash_password("x", 1000))
        if not row or not ok or not row["active"]:
            self._fails.setdefault(key, []).append(time.time())
            raise AuthError("Wrong username or password.", 401)
        self._fails.pop(key, None)
        token = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(hours=self.settings.session_hours)).isoformat(timespec="seconds")
        with self.db.tx() as c:
            c.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
            c.execute("INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?,?,?)",
                      (_token_hash(token), row["id"], exp))
        return token, self.public(row)

    def logout(self, token: str | None) -> None:
        if token:
            with self.db.tx() as c:
                c.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def user_for_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        row = self.db.one(
            "SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (_token_hash(token),))
        if not row or not row["active"] or row["expires_at"] < now():
            return None
        return self.public(row)

    @staticmethod
    def public(row) -> dict:
        return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
                "role": row["role"], "active": bool(row["active"])}
