"""Connect Microsoft Foundry from the UI: stored in the data folder, applied immediately, never sent back."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from urllib.parse import urlparse

from ..envfile import load_env_file, parse_env
from .config import Settings

KEYS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_VERSION",
        "VRA_PRICE_IN_PER_M", "VRA_PRICE_OUT_PER_M")


def path(settings: Settings) -> Path:
    return settings.data_dir / "foundry.env"


def load(settings: Settings) -> None:
    """Called at start-up: settings saved from the UI win over the .env file."""
    load_env_file(path(settings), override=True)


def current() -> dict:
    """Non-secret view of the live configuration."""
    key = bool(os.environ.get("AZURE_OPENAI_API_KEY"))
    return {"endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT", ""), "deployment": os.environ.get("AZURE_OPENAI_DEPLOYMENT", ""),
            "api_version": os.environ.get("AZURE_OPENAI_API_VERSION", ""), "key_set": key,
            "auth": "API key" if key else "Microsoft Entra ID (az login)",
            "price_in_per_m": os.environ.get("VRA_PRICE_IN_PER_M", ""), "price_out_per_m": os.environ.get("VRA_PRICE_OUT_PER_M", "")}


def validate(values: dict) -> dict:
    out = {}
    ep = (values.get("endpoint") or "").strip()
    if ep:
        u = urlparse(ep)
        if u.scheme != "https" or not u.netloc:
            raise ValueError("The endpoint must start with https:// and look like https://<resource>.openai.azure.com")
        out["AZURE_OPENAI_ENDPOINT"] = f"{u.scheme}://{u.netloc}"  # a pasted Target URI or project URL: keep the resource address only
    dep = (values.get("deployment") or "").strip().strip("\"'")
    if dep:
        if not re.fullmatch(r"[A-Za-z0-9._\-]{1,64}", dep):
            raise ValueError("The deployment name may only contain letters, digits, '.', '_' and '-'.")
        out["AZURE_OPENAI_DEPLOYMENT"] = dep
    ver = (values.get("api_version") or "").strip()
    if ver:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(-preview)?", ver):
            raise ValueError("The API version looks like 2024-10-21.")
        out["AZURE_OPENAI_API_VERSION"] = ver
    key = (values.get("api_key") or "").strip().strip("\"'").strip()
    if key:
        if re.search(r"\s", key) or len(key) < 16:
            raise ValueError("That does not look like an API key (too short or contains spaces).")
        out["AZURE_OPENAI_API_KEY"] = key
    for field, env in (("price_in_per_m", "VRA_PRICE_IN_PER_M"), ("price_out_per_m", "VRA_PRICE_OUT_PER_M")):
        v = str(values.get(field) or "").strip()
        if v:
            try:
                if float(v) < 0:
                    raise ValueError
            except ValueError:
                raise ValueError("Prices must be numbers (cost per million tokens).")
            out[env] = v
    return out


def save(settings: Settings, values: dict, clear_key: bool = False) -> None:
    new = validate(values)
    p = path(settings)
    existing = parse_env(p.read_text(encoding="utf-8-sig")) if p.is_file() else {}
    existing.update(new)
    if clear_key:
        existing.pop("AZURE_OPENAI_API_KEY", None)
        os.environ.pop("AZURE_OPENAI_API_KEY", None)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# Written by the Settings page. Contains a secret: do not share or commit this file.\n"
                 + "".join(f"{k}={v}\n" for k, v in existing.items() if k in KEYS), encoding="utf-8")
    try:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)  # best effort; Windows ignores most of this
    except OSError:
        pass
    os.environ.update({k: v for k, v in new.items()})


def clear(settings: Settings) -> None:
    p = path(settings)
    if p.is_file():
        for k in parse_env(p.read_text(encoding="utf-8-sig")):
            os.environ.pop(k, None)
        p.unlink()
