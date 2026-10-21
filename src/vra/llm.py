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
from typing import Callable, Protocol, Sequence

Image = tuple[str, bytes]  # (mime type, bytes)
DEFAULT_API_VERSION = "2024-10-21"


class ChatClient(Protocol):
    name: str

    def chat(self, system: str, user: str, images: Sequence[Image] = ()) -> str: ...


class FoundryChat:  # pragma: no cover - needs network and credentials
    def __init__(self, deployment: str | None = None, endpoint: str | None = None,
                 api_key: str | None = None, api_version: str | None = None,
                 base_url: str | None = None) -> None:
        self._deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("VRA_MODEL")
        if not self._deployment:
            raise RuntimeError("set AZURE_OPENAI_DEPLOYMENT to your Foundry deployment name")
        key = api_key or os.environ.get("AZURE_OPENAI_API_KEY")
        base_url = base_url or os.environ.get("AZURE_OPENAI_BASE_URL")
        endpoint = endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
        token_provider = None
        if not key:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            token_provider = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
        if base_url:
            from openai import OpenAI

            self._client = OpenAI(base_url=base_url, api_key=key or token_provider())
        else:
            if not endpoint:
                raise RuntimeError("set AZURE_OPENAI_ENDPOINT (or AZURE_OPENAI_BASE_URL)")
            from openai import AzureOpenAI

            self._client = AzureOpenAI(
                azure_endpoint=endpoint, api_key=key, azure_ad_token_provider=token_provider,
                api_version=api_version or os.environ.get("AZURE_OPENAI_API_VERSION") or DEFAULT_API_VERSION)
        self.name = f"foundry:{self._deployment}"

    def chat(self, system: str, user: str, images: Sequence[Image] = ()) -> str:
        content: list[dict] = [{"type": "text", "text": user}]
        for mime, data in images:
            b64 = base64.b64encode(data).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        resp = self._client.chat.completions.create(
            model=self._deployment,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": content if images else user}])
        return resp.choices[0].message.content or ""


class FakeChat:
    """Scripted chat client for tests: a list of replies, or a callable(system, user, images)."""

    name = "fake"

    def __init__(self, replies: Sequence[str] | Callable[[str, str, Sequence[Image]], str]) -> None:
        self._replies = replies
        self.calls: list[tuple[str, str, Sequence[Image]]] = []

    def chat(self, system: str, user: str, images: Sequence[Image] = ()) -> str:
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
