"""Web app settings (environment-driven)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def foundry_configured() -> bool:
    return bool(os.environ.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("VRA_MODEL")) and bool(
        os.environ.get("AZURE_OPENAI_ENDPOINT") or os.environ.get("AZURE_OPENAI_BASE_URL"))


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("VRA_DATA_DIR", "vra_data")))
    host: str = "127.0.0.1"
    port: int = 8000
    solo: bool = False                   # one person testing: allow signing off one's own run
    cookie_secure: bool = False          # set true when served over HTTPS
    max_upload_mb: int = 100             # per file
    max_zip_mb: int = 500                # total uncompressed size per zip
    max_zip_files: int = 5000
    session_hours: int = 12
    min_password_len: int = 10

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).resolve()
        self.cookie_secure = self.cookie_secure or os.environ.get("VRA_COOKIE_SECURE") == "1"
        self.solo = self.solo or os.environ.get("VRA_SOLO") == "1"
        self.max_upload_mb = int(os.environ.get("VRA_MAX_UPLOAD_MB", self.max_upload_mb))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vra.sqlite3"

    @property
    def engagements_dir(self) -> Path:
        return self.data_dir / "engagements"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.engagements_dir, self.runs_dir):
            d.mkdir(parents=True, exist_ok=True)
