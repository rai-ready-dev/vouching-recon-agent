"""Connect Microsoft Foundry from the UI: stored in the data folder, applied immediately, never sent back."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from urllib.parse import urlparse

from ..envfile import load_env_file, parse_env
from .config import Settings

KEYS = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_BASE_URL", "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_VERSION",
        "VRA_PRICE_IN_PER_M", "VRA_PRICE_OUT_PER_M")


def path(settings: Settings) -> Path:
    return settings.data_dir / "foundry.env"


def load(settings: Settings) -> None:
    """Called at start-up: settings saved from the UI win over the .env file."""
    load_env_file(path(settings), override=True)


def current() -> dict:
    """Non-secret view of the live configuration."""
    key = bool(os.environ.get("AZURE_OPENAI_API_KEY"))
    raw_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    return {"endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT", ""), "base_url": os.environ.get("AZURE_OPENAI_BASE_URL", ""),
            "key_hint": ("…" + raw_key[-4:]) if len(raw_key) >= 12 else "", "deployment": os.environ.get("AZURE_OPENAI_DEPLOYMENT", ""),
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
    base = (values.get("base_url") or "").strip()
    if base:
        bu = urlparse(base)
        if bu.scheme != "https" or not bu.netloc:
            raise ValueError("The OpenAI-compatible base URL must start with https:// and usually ends with /openai/v1/")
        out["AZURE_OPENAI_BASE_URL"] = base if base.endswith("/") else base + "/"
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


def save(settings: Settings, values: dict, clear_key: bool = False, clear_base_url: bool = False) -> None:
    new = validate(values)
    p = path(settings)
    existing = parse_env(p.read_text(encoding="utf-8-sig")) if p.is_file() else {}
    existing.update(new)
    if clear_key:
        existing.pop("AZURE_OPENAI_API_KEY", None)
        os.environ.pop("AZURE_OPENAI_API_KEY", None)
    if clear_base_url and "AZURE_OPENAI_BASE_URL" not in new:
        existing.pop("AZURE_OPENAI_BASE_URL", None)
        os.environ.pop("AZURE_OPENAI_BASE_URL", None)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# Written by the Settings page. Contains a secret: do not share or commit this file.\n"
                 + "".join(f"{k}={v}\n" for k, v in existing.items() if k in KEYS), encoding="utf-8")
    try:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)  # best effort; Windows ignores most of this
    except OSError:
        pass
    os.environ.update({k: v for k, v in new.items()})


def candidate_styles(endpoint: str, base_url: str) -> list[dict]:
    """Address styles worth trying for a resource: classic and OpenAI-compatible (v1), on each host a Foundry/Azure
    OpenAI resource answers on."""
    src = urlparse(endpoint or base_url or "")
    if not src.netloc:
        return []
    resource = src.netloc.split(".")[0]
    styles: list[dict] = []
    for host in dict.fromkeys([src.netloc, f"{resource}.openai.azure.com", f"{resource}.cognitiveservices.azure.com",
                               f"{resource}.services.ai.azure.com"]):
        origin = f"https://{host}"
        styles.append({"label": f"Classic Azure OpenAI at {host}", "endpoint": origin, "base_url": ""})
        styles.append({"label": f"OpenAI-compatible (v1) at {host}", "endpoint": "", "base_url": f"{origin}/openai/v1/"})
    return styles


def probe(values: dict, make=None, per_try_seconds: float = 20.0, styles: list[dict] | None = None) -> list[dict]:
    """Try each address style with the given (or saved) key and report which one answers."""
    if make is None:
        from ..llm import FoundryChat as make  # noqa: N813
    cur = current()
    endpoint = (values.get("endpoint") or cur["endpoint"] or "").strip()
    base = (values.get("base_url") or cur["base_url"] or "").strip()
    dep = (values.get("deployment") or cur["deployment"] or "").strip()
    key = (values.get("api_key") or "").strip() or os.environ.get("AZURE_OPENAI_API_KEY") or None
    ver = (values.get("api_version") or cur["api_version"] or "").strip() or None
    results = []
    for s in (styles if styles is not None else candidate_styles(endpoint, base))[:8]:
        row = {**s, "ok": False, "status": None, "error": ""}
        try:
            chat = make(deployment=dep, endpoint=s["endpoint"], base_url=s["base_url"], api_key=key, api_version=ver, timeout=per_try_seconds)
            reply = chat.chat("Reply with the single word OK.", "ping")
            row.update(ok=True, reply=str(reply)[:40])
        except Exception as e:  # each failure is information
            row.update(status=getattr(e, "status_code", None), error=f"{type(e).__name__}: {str(e)[:160]}")
        results.append(row)
        if row["ok"]:
            break  # the first style that works is the one to use
    return results


def clear(settings: Settings) -> None:
    p = path(settings)
    if p.is_file():
        for k in parse_env(p.read_text(encoding="utf-8-sig")):
            os.environ.pop(k, None)
        p.unlink()
