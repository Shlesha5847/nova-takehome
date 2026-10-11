"""Document extraction and ingestion module for the Nova freight audit harness.

Reuses grader/extract.py for deterministic text extraction of PDFs, Excel sheets,
and emails. Uses RapidOCR for scanned image-only PDFs.
"""
from __future__ import annotations

import sys
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

# Ensure grader is importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "grader") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "grader"))

# Portable fallback to local .venv site-packages when running under stripped environment
for _site in (_REPO_ROOT / ".venv").glob("**/site-packages"):
    if _site.is_dir() and str(_site) not in sys.path:
        sys.path.append(str(_site))

try:
    from grader import extract as g_extract
except ImportError:
    import extract as g_extract  # type: ignore

from harness import db

_OCR_INSTANCE = None


def get_ocr():
    """Lazily instantiate RapidOCR engine."""
    global _OCR_INSTANCE
    if _OCR_INSTANCE is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR_INSTANCE = RapidOCR()
    return _OCR_INSTANCE


def ocr_pdf_pages(data: bytes) -> list[str]:
    """Extract and reconstruct text lines from an image-only scanned PDF using RapidOCR."""
    import pymupdf

    ocr = get_ocr()
    pages_text = []

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=150)
            img_bytes = pix.tobytes("png")
            boxes, _ = ocr(img_bytes)
            if not boxes:
                pages_text.append("")
                continue

            # Sort detected boxes by vertical center, then horizontal left
            items = []
            for box, text, _ in boxes:
                y_mid = (box[0][1] + box[2][1]) / 2.0
                x_left = box[0][0]
                items.append((y_mid, x_left, text))
            items.sort(key=lambda w: (w[0], w[1]))

            # Group into lines within 8.0 vertical point threshold
            lines = []
            for y, x, text in items:
                if lines and abs(lines[-1][0] - y) <= 8.0:
                    lines[-1][1].append((x, text))
                else:
                    lines.append([y, [(x, text)]])

            reconstructed = [
                " ".join(t for _, t in sorted(line[1], key=lambda k: k[0]))
                for line in lines
            ]
            pages_text.append("\n".join(reconstructed))

    return pages_text


def extract_document_pages(data: bytes, ext: str) -> tuple[list[str], bool]:
    """Extract page-level text for a document. Returns (pages_text_list, is_scan)."""
    ext = ext.lower().lstrip(".")
    if ext == "pdf":
        is_scan = g_extract.pdf_is_image_only(data)
        if is_scan:
            return ocr_pdf_pages(data), True
        return g_extract.pdf_pages(data), False
    if ext == "xlsx":
        return g_extract.xlsx_pages(data), False
    if ext == "eml":
        return g_extract.pages_of(data, "eml"), False
    raise ValueError(f"Unsupported file format: .{ext}")


def parse_email_date(msg: Any) -> str | None:
    """Parse email Date header into ISO YYYY-MM-DD format."""
    date_header = msg.get("Date")
    if not date_header:
        return None
    try:
        dt = parsedate_to_datetime(str(date_header))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def ingest_batch_to_db(batch_dir: str | Path, state_dir: str | Path) -> None:
    """Ingest all .eml files and attachments in batch_dir into state_dir SQLite database."""
    b_path = Path(batch_dir).resolve()
    if not b_path.is_dir():
        raise FileNotFoundError(f"batch directory not found: {batch_dir}")

    batch_name = b_path.name
    db.init_db(state_dir)
    db.record_batch(state_dir, batch_name, str(b_path))
    # Idempotent cleanup of any prior ingestion for this batch
    db.delete_batch_documents(state_dir, batch_name)

    for eml_file in sorted(b_path.glob("*.eml")):
        eml_bytes = eml_file.read_bytes()
        msg = g_extract.parse_eml(eml_bytes)
        received_date = parse_email_date(msg)

        # 1. Ingest email itself
        email_doc_id = f"{batch_name}/{eml_file.name}"
        email_pages = g_extract.pages_of(eml_bytes, "eml")
        db.save_document(
            state_dir=state_dir,
            doc_id=email_doc_id,
            batch_name=batch_name,
            file_name=eml_file.name,
            parent_doc_id=None,
            doc_type="eml",
            received_date=received_date,
            is_scan=False,
            pages=email_pages,
        )

        # 2. Ingest attachments
        for att_name, att_bytes in g_extract.attachments(eml_bytes):
            if not att_name:
                continue
            att_doc_id = f"{batch_name}/{eml_file.name}#{att_name}"
            ext = att_name.rsplit(".", 1)[-1].lower() if "." in att_name else ""
            if ext not in ("pdf", "xlsx"):
                continue

            att_pages, is_scan = extract_document_pages(att_bytes, ext)
            db.save_document(
                state_dir=state_dir,
                doc_id=att_doc_id,
                batch_name=batch_name,
                file_name=att_name,
                parent_doc_id=email_doc_id,
                doc_type=ext,
                received_date=received_date,
                is_scan=is_scan,
                pages=att_pages,
            )
