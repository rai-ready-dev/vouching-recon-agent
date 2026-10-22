"""Chat-model access. One tiny interface so the model is swappable.

`FoundryChat` talks to a model deployed in Microsoft Foundry / Azure OpenAI.
Authentication uses an API key if one is configured, otherwise Microsoft Entra
ID (`DefaultAzureCredential`), which is what most firms require.

Environment variables
    AZURE_OPENAI_ENDPOINT      https://<resource>.openai.azure.com   (or .services.ai.azure.com)
    AZURE_OPENAI_DEPLOYMENT    the *deployment name* you gave the model in Foundry
    AZURE_OPENAI_API_KEY       optional; omit to use Entra ID sign-in
    AZURE_OPENAI_API_VERSION   optional, default below
    AZURE_OPENAI_BASE_URL      optional alternative: the "v1" style endpoint (.../openai/v1/)
"""
from __future__ import annotations

import base64
import os
import random
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

Image = tuple[str, bytes]  # (mime type, bytes)
DEFAULT_API_VERSION = "2024-10-21"


class ChatClient(Protocol):
    name: str

    def chat(self, system: str, user: str, images: Sequence[Image] = (), json_mode: bool = False) -> str: ...


class FoundryChat:  # pragma: no cover - needs network and credentials
    """Chat client for a model deployed in Microsoft Foundry / Azure OpenAI.

    Two address styles are supported: the classic Azure OpenAI one (`endpoint`) and the OpenAI-compatible "v1" one
    (`base_url`, usually ending in /openai/v1/). When either is passed explicitly, the environment is not consulted
    for the other, so alternatives can be tried side by side.
    """

    def __init__(self, deployment: str | None = None, endpoint: str | None = None,
                 api_key: str | None = None, api_version: str | None = None,
                 base_url: str | None = None, timeout: float = 120.0) -> None:
        self._deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("VRA_MODEL")
        if not self._deployment:
            raise RuntimeError("set AZURE_OPENAI_DEPLOYMENT to your Foundry deployment name")
        key = api_key or os.environ.get("AZURE_OPENAI_API_KEY")
        if endpoint is not None or base_url is not None:  # explicit style: do not mix in the other one from the environment
            endpoint, base_url = endpoint or None, base_url or None
        else:
            base_url, endpoint = os.environ.get("AZURE_OPENAI_BASE_URL") or None, os.environ.get("AZURE_OPENAI_ENDPOINT") or None
        token_provider = None
        if not key:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            token_provider = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
        if base_url:
            from openai import OpenAI

            self._client = OpenAI(base_url=base_url, api_key=key or token_provider(), timeout=timeout,
                                  default_headers={"api-key": key} if key else None)
            self.style = "v1"
        else:
            if not endpoint:
                raise RuntimeError("set AZURE_OPENAI_ENDPOINT (or AZURE_OPENAI_BASE_URL)")
            from openai import AzureOpenAI

            self._client = AzureOpenAI(
                azure_endpoint=endpoint, api_key=key, azure_ad_token_provider=token_provider, timeout=timeout,
                api_version=api_version or os.environ.get("AZURE_OPENAI_API_VERSION") or DEFAULT_API_VERSION)
            self.style = "classic"
        self.name = f"foundry:{self._deployment}"
        self._json_ok = True
        self._local = threading.local()

    def pop_usage(self):
        u, self._local.usage = getattr(self._local, "usage", None), None
        return u

    def chat(self, system: str, user: str, images: Sequence[Image] = (), json_mode: bool = False) -> str:
        content: list[dict] = [{"type": "text", "text": user}]
        for mime, data in images:
            b64 = base64.b64encode(data).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        kwargs = dict(model=self._deployment, messages=[{"role": "system", "content": system},
                                                       {"role": "user", "content": content if images else user}])
        if json_mode and self._json_ok:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            if "response_format" in kwargs and getattr(exc, "status_code", None) == 400:
                self._json_ok = False  # this deployment does not support JSON mode; fall back to prompt-only JSON
                kwargs.pop("response_format")
                resp = self._client.chat.completions.create(**kwargs)
            else:
                raise
        u = getattr(resp, "usage", None)
        self._local.usage = (getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0) if u else None
        return resp.choices[0].message.content or ""


class FakeChat:
    """Scripted chat client for tests: a list of replies, or a callable(system, user, images)."""

    name = "fake"

    def __init__(self, replies: Sequence[str] | Callable[[str, str, Sequence[Image]], str],
                 usage: tuple[int, int] | None = None) -> None:
        self._replies = replies
        self.calls: list[tuple[str, str, Sequence[Image]]] = []
        self._usage = usage
        self._local = threading.local()

    def pop_usage(self):
        u, self._local.usage = getattr(self._local, "usage", None), None
        return u

    def chat(self, system: str, user: str, images: Sequence[Image] = (), json_mode: bool = False) -> str:
        self._local.usage = self._usage
        self.calls.append((system, user, images))
        if callable(self._replies):
            return self._replies(system, user, images)
        return self._replies[min(len(self.calls), len(self._replies)) - 1]


MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".tif": "image/tiff",
        ".tiff": "image/tiff", ".bmp": "image/bmp", ".gif": "image/gif", ".webp": "image/webp"}


class FoundryVisionOcr:  # pragma: no cover - needs network and credentials
    """Transcribes scans and images with a vision-capable Foundry deployment."""

    name = "foundry-vision-ocr"
    PROMPT = ("Transcribe all text in this document image exactly as printed, preserving line breaks and "
              "table rows (use tabs between columns). Do not summarise, correct or add anything. "
              "If a value is illegible write [illegible].")

    def __init__(self, chat: ChatClient, max_pages: int = 20) -> None:
        self._chat, self._max_pages = chat, max_pages

    def ocr(self, doc) -> str:
        images: list[Image] = []
        if doc.kind == "pdf":
            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(doc.raw)
            for i in range(min(len(pdf), self._max_pages)):
                import io

                buf = io.BytesIO()
                pdf[i].render(scale=2).to_pil().save(buf, format="PNG")
                images.append(("image/png", buf.getvalue()))
        else:
            ext = "." + doc.filename.rsplit(".", 1)[-1].lower()
            images.append((MIME.get(ext, "image/png"), doc.raw))
        return "\n\n".join(
            self._chat.chat("You are a faithful OCR engine.", self.PROMPT, [img]) for img in images)


# ---------------------------------------------------------------- reliability + measurement
RETRYABLE_NAMES = {"RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError", "ServiceUnavailableError"}
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def is_retryable(exc: Exception) -> bool:
    return type(exc).__name__ in RETRYABLE_NAMES or getattr(exc, "status_code", None) in RETRYABLE_STATUS


@dataclass
class CallRecord:
    purpose: str
    seconds: float
    tokens_in: int
    tokens_out: int
    ok: bool
    retries: int


class UsageTracker:
    """Thread-safe record of every model call: latency, tokens, retries, failures."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.records: list[CallRecord] = []

    def add(self, rec: CallRecord) -> None:
        with self._lock:
            self.records.append(rec)

    @staticmethod
    def _block(recs: list[CallRecord]) -> dict:
        lat = sorted(r.seconds for r in recs)
        return {"calls": len(recs), "tokens_in": sum(r.tokens_in for r in recs), "tokens_out": sum(r.tokens_out for r in recs),
                "errors": sum(1 for r in recs if not r.ok), "retries": sum(r.retries for r in recs),
                "avg_seconds": round(statistics.fmean(lat), 2) if lat else 0.0,
                "p95_seconds": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 2) if lat else 0.0}

    def summary(self) -> dict:
        with self._lock:
            recs = list(self.records)
        out = self._block(recs)
        out["by_purpose"] = {p: self._block([r for r in recs if r.purpose == p]) for p in sorted({r.purpose for r in recs})}
        try:
            pin, pout = float(os.environ.get("VRA_PRICE_IN_PER_M", "")), float(os.environ.get("VRA_PRICE_OUT_PER_M", ""))
            out["estimated_cost"] = round(out["tokens_in"] / 1e6 * pin + out["tokens_out"] / 1e6 * pout, 4)
        except ValueError:
            out["estimated_cost"] = None  # set VRA_PRICE_IN_PER_M / VRA_PRICE_OUT_PER_M to see an estimate
        return out


class MeteredChat:
    """Wraps any ChatClient with retry/backoff and usage tracking, labelled by purpose."""

    def __init__(self, inner: ChatClient, tracker: UsageTracker, purpose: str = "other", attempts: int = 4,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.inner, self.tracker, self.purpose, self.attempts, self._sleep = inner, tracker, purpose, attempts, sleep
        self.name = inner.name

    def for_purpose(self, purpose: str) -> "MeteredChat":
        return MeteredChat(self.inner, self.tracker, purpose, self.attempts, self._sleep)

    def chat(self, system: str, user: str, images: Sequence[Image] = (), json_mode: bool = False) -> str:
        t0, retries = time.time(), 0
        while True:
            try:
                reply = self.inner.chat(system, user, images, json_mode)
                usage = getattr(self.inner, "pop_usage", lambda: None)() or (0, 0)
                self.tracker.add(CallRecord(self.purpose, time.time() - t0, usage[0], usage[1], True, retries))
                return reply
            except Exception as exc:
                if retries + 1 < self.attempts and is_retryable(exc):
                    retries += 1
                    self._sleep(min(30.0, 2 ** retries) + random.random())
                    continue
                self.tracker.add(CallRecord(self.purpose, time.time() - t0, 0, 0, False, retries))
                raise
