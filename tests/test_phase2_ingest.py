"""Phase 2 tests: document extraction, provenance, OCR, and persistence.

Validates email parsing, text PDF extraction, XLSX extraction, scanned-document
OCR with RapidOCR, page-level SQLite persistence, idempotent re-ingestion, and
cross-subprocess reads against real repository fixtures.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# Add repo root to path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from grader import extract as g_extract
from harness import db, extract


def test_email_parsing():
    """Test reading email headers, body lines, and attachment listing."""
    eml_path = _REPO_ROOT / "data" / "examples" / "overcharge" / "docs" / "batch_01" / "0002.eml"
    data = eml_path.read_bytes()
    msg = g_extract.parse_eml(data)

    date_str = extract.parse_email_date(msg)
    assert date_str == "2031-11-25", f"Expected 2031-11-25, got {date_str}"

    lines = g_extract.email_lines(msg)
    assert any("Executed agreement TBX-US-EXP-E60" in l for l in lines)
    assert any("MSA-TBX-US-EXP-E60.pdf" in l for l in lines)

    atts = g_extract.attachments(data)
    assert len(atts) == 1
    assert atts[0][0] == "MSA-TBX-US-EXP-E60.pdf"
    print("[PASS] test_email_parsing")



def test_pdf_text_extraction():
    """Test extracting PDF pages matching grader.extract exact line clustering."""
    eml_path = _REPO_ROOT / "data" / "examples" / "overcharge" / "docs" / "batch_01" / "0002.eml"
    atts = g_extract.attachments(eml_path.read_bytes())
    msa_bytes = atts[0][1]

    pages, is_scan = extract.extract_document_pages(msa_bytes, "pdf")
    assert not is_scan, "MSA PDF should not be classified as scan"
    assert len(pages) == 11, f"Expected 11 pages in MSA, got {len(pages)}"

    # Check key quote on page 3
    quote = "3.2 Pricing date. Each shipment is priced at the rates, surcharges and fees in force on its Pickup Date."
    assert g_extract.found(pages, 3, quote), f"Quote not found in page 3: {pages[2][:200]}"
    print("[PASS] test_pdf_text_extraction")


def test_xlsx_extraction():
    """Test extracting XLSX sheets with rows joined by ' | '."""
    eml_path = _REPO_ROOT / "data" / "examples" / "overcharge" / "docs" / "batch_01" / "0005.eml"
    atts = g_extract.attachments(eml_path.read_bytes())
    xlsx_bytes = atts[0][1]

    pages, is_scan = extract.extract_document_pages(xlsx_bytes, "xlsx")
    assert not is_scan
    assert len(pages) >= 1

    sheet1 = pages[0]
    expected_event = "TE56915687 | PICKED_UP | 2031-12-25 | USE2 | -"
    assert expected_event in sheet1, f"Expected event not found in XLSX: {sheet1[:200]}"
    print("[PASS] test_xlsx_extraction")


def test_scanned_pdf_ocr():
    """Test RapidOCR extraction on exam scanned invoice without text layer."""
    eml_path = _REPO_ROOT / "data" / "exam" / "batch_02" / "0016.eml"
    atts = g_extract.attachments(eml_path.read_bytes())
    scan_bytes = next(b for name, b in atts if name == "CDI-3031-0306.pdf")

    assert g_extract.pdf_is_image_only(scan_bytes), "Document should be identified as image-only scan"

    pages, is_scan = extract.extract_document_pages(scan_bytes, "pdf")
    assert is_scan
    assert len(pages) == 1

    ocr_text = pages[0]
    assert "CD1-3031-0306" in ocr_text or "CDI-3031-0306" in ocr_text
    assert "CA68214550" in ocr_text
    assert "CDA-IN-EXP-E91" in ocr_text
    print("[PASS] test_scanned_pdf_ocr")


def test_persistence_and_cross_subprocess():
    """Test storing document pages in SQLite and reading across subprocesses."""
    test_state = _REPO_ROOT / "out" / "test_p2_state"
    if test_state.exists():
        shutil.rmtree(test_state)

    # 1. Ingest via CLI subprocess
    batch_dir = _REPO_ROOT / "data" / "examples" / "overcharge" / "docs" / "batch_01"
    cmd = [sys.executable, str(_REPO_ROOT / "harness" / "harness.py"), "ingest", str(batch_dir), "--state-dir", str(test_state)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"ingest failed: {r.stderr}"
    assert r.stdout == "", "ingest should not print to stdout"

    # 2. Query in fresh process connection
    docs = db.list_documents(test_state)
    assert len(docs) == 8, f"Expected 8 documents (4 emails + 4 attachments), got {len(docs)}"

    # 3. Verify page text retrieval
    msa_doc = "batch_01/0002.eml#MSA-TBX-US-EXP-E60.pdf"
    pages = db.get_document_pages(test_state, msa_doc)
    assert len(pages) == 11, f"Expected 11 pages, got {len(pages)}"

    p3 = db.get_page_text(test_state, msa_doc, 3)
    assert p3 is not None
    assert "3.2 Pricing date" in p3

    # 4. Idempotent re-ingestion check
    r2 = subprocess.run(cmd, capture_output=True, text=True)
    assert r2.returncode == 0
    docs2 = db.list_documents(test_state)
    assert len(docs2) == 8, "Re-ingesting batch must not duplicate documents"

    pages2 = db.get_document_pages(test_state, msa_doc)
    assert len(pages2) == 11, "Re-ingesting batch must not duplicate pages"

    # Cleanup
    shutil.rmtree(test_state)
    print("[PASS] test_persistence_and_cross_subprocess")


def run_all():
    print("Starting Phase 2 verification suite...")
    test_email_parsing()
    test_pdf_text_extraction()
    test_xlsx_extraction()
    test_scanned_pdf_ocr()
    test_persistence_and_cross_subprocess()
    print("\nALL PHASE 2 TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    run_all()
