"""Minimal .env loader (no dependency). Real environment variables always win."""
from __future__ import annotations

import os
from pathlib import Path


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:  # inline comment on an unquoted value
            val = val.split(" #", 1)[0].rstrip()
        if key:
            out[key] = val
    return out


def load_env_file(path: str | Path = ".env", override: bool = False) -> list[str]:
    """Load KEY=VALUE pairs into os.environ (without overriding variables already set, unless override=True).

    Returns the names of the variables that were loaded (never their values).
    """
    p = Path(path)
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    loaded = []
    for key, val in parse_env(text).items():
        if val != "" and (override or key not in os.environ):
            os.environ[key] = val
            loaded.append(key)
    return loaded
