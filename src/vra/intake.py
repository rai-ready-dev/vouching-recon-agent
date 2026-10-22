"""Intake: a folder of mixed documents -> SourceFile + Voucher records, with every problem reported."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .extract import Extractor
from .ingest import SUPPORTED_EXT, IngestError, OcrEngine, ingest_path
from .records import SourceFile, Voucher

OCR_CONFIDENCE_CAP = 0.75  # text read by OCR is inherently less certain than a text layer


@dataclass
class IntakeResult:
    sources: list[SourceFile] = field(default_factory=list)
    vouchers: list[Voucher] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def collect_files(folder: str | Path) -> list[Path]:
    p = Path(folder)
    files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file())
    return [f for f in files if not f.name.startswith(("~$", ".")) and f.suffix.lower() in SUPPORTED_EXT]


def _read_one(path: Path, root: Path, extractor: Extractor, ocr: OcrEngine | None) -> IntakeResult:
    res = IntakeResult()
    rel = path.relative_to(root).as_posix()
    try:
        doc = ingest_path(path, root)
    except IngestError as exc:
        res.issues.append(f"{rel}: {exc}")
        return res
    for w in doc.warnings:
        res.issues.append(f"{rel}: {w}")
    if doc.needs_ocr:
        if ocr is None:
            res.issues.append(f"{rel}: scanned/image document needs OCR but no OCR engine is configured "
                              f"(use --ocr foundry); document not vouched")
            res.sources.append(SourceFile(id=doc.id, filename=doc.filename, sha256=doc.sha256,
                                          kind=doc.kind, pages=doc.pages, method="unread"))
            return res
        try:
            doc.text, doc.method = ocr.ocr(doc), "ocr"
        except Exception as exc:
            res.issues.append(f"{rel}: OCR failed ({type(exc).__name__}: {exc})")
            return res
    if not doc.text.strip():
        res.issues.append(f"{rel}: no readable text")
        return res
    res.sources.append(SourceFile(id=doc.id, filename=doc.filename, sha256=doc.sha256, kind=doc.kind,
                                  pages=doc.pages, method=doc.method, flags=doc.flags))
    try:
        found = extractor.extract(doc)
    except Exception as exc:
        res.issues.append(f"{rel}: extraction failed ({type(exc).__name__}: {exc})")
        return res
    if not found:
        res.issues.append(f"{rel}: no voucher could be extracted")
    for v in found:
        if doc.method == "ocr":
            v = v.model_copy(update={"confidence": min(v.confidence, OCR_CONFIDENCE_CAP)})
        res.vouchers.append(v)
    return res


def build_vouchers(folder: str | Path, extractor: Extractor, ocr: OcrEngine | None = None,
                   progress=None, workers: int = 1) -> IntakeResult:
    """`progress(done, total, filename)` is called as files finish. `workers` > 1 reads files in parallel
    (worthwhile when each file costs a model call); results always come back in file order."""
    res = IntakeResult()
    root = Path(folder) if Path(folder).is_dir() else Path(folder).parent
    if Path(folder).is_dir():
        for f in sorted(Path(folder).rglob("*")):
            if f.is_file() and f.suffix.lower() not in SUPPORTED_EXT and not f.name.startswith(("~$", ".")):
                res.issues.append(f"{f.name}: unsupported file type, not read")

    files = collect_files(folder)
    done, lock = [0], threading.Lock()

    def work(path: Path) -> IntakeResult:
        out = _read_one(path, root, extractor, ocr)
        with lock:
            done[0] += 1
            if progress:
                progress(done[0] - 1, len(files), path.relative_to(root).as_posix())
        return out

    if workers > 1 and len(files) > 1:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vra-read") as pool:
            parts = list(pool.map(work, files))
    else:
        parts = [work(p) for p in files]
    for p in parts:
        res.sources += p.sources
        res.vouchers += p.vouchers
        res.issues += p.issues
    return res
