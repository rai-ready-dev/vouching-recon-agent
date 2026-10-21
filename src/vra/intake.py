"""Intake: a folder of mixed documents -> SourceFile + Voucher records, with every problem reported."""
from __future__ import annotations

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


def build_vouchers(folder: str | Path, extractor: Extractor, ocr: OcrEngine | None = None,
                   progress=None) -> IntakeResult:
    """`progress(done, total, filename)` is called before each file if given."""
    res = IntakeResult()
    root = Path(folder) if Path(folder).is_dir() else Path(folder).parent
    skipped = [f.name for f in Path(folder).rglob("*") if f.is_file() and f.suffix.lower() not in SUPPORTED_EXT
               and not f.name.startswith(("~$", "."))] if Path(folder).is_dir() else []
    for name in skipped:
        res.issues.append(f"{name}: unsupported file type, not read")

    all_files = collect_files(folder)
    for n, path in enumerate(all_files):
        rel = path.relative_to(root).as_posix()
        if progress:
            progress(n, len(all_files), rel)
        try:
            doc = ingest_path(path, root)
        except IngestError as exc:
            res.issues.append(f"{rel}: {exc}")
            continue
        for w in doc.warnings:
            res.issues.append(f"{rel}: {w}")
        if doc.needs_ocr:
            if ocr is None:
                res.issues.append(f"{rel}: scanned/image document needs OCR but no OCR engine is configured "
                                  f"(use --ocr foundry); document not vouched")
                res.sources.append(SourceFile(id=doc.id, filename=doc.filename, sha256=doc.sha256,
                                              kind=doc.kind, pages=doc.pages, method="unread"))
                continue
            try:
                doc.text, doc.method = ocr.ocr(doc), "ocr"
            except Exception as exc:
                res.issues.append(f"{rel}: OCR failed ({type(exc).__name__}: {exc})")
                continue
        if not doc.text.strip():
            res.issues.append(f"{rel}: no readable text")
            continue
        res.sources.append(SourceFile(id=doc.id, filename=doc.filename, sha256=doc.sha256,
                                      kind=doc.kind, pages=doc.pages, method=doc.method))
        try:
            found = extractor.extract(doc)
        except Exception as exc:
            res.issues.append(f"{rel}: extraction failed ({type(exc).__name__}: {exc})")
            continue
        if not found:
            res.issues.append(f"{rel}: no voucher could be extracted")
        for v in found:
            if doc.method == "ocr":
                v = v.model_copy(update={"confidence": min(v.confidence, OCR_CONFIDENCE_CAP)})
            res.vouchers.append(v)
    return res
