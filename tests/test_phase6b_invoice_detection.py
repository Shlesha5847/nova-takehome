"""Regression tests for Phase 6B: Exam-style Charges advice and invoice detection."""
from decimal import Decimal
from pathlib import Path
import pytest

from harness.audit.common import extract_amounts, parse_date_to_iso
from harness.audit.overcharge import OverchargeAuditEngine
from harness.audit.duplicate import DuplicateAuditEngine
from harness.audit.waiver import WaiverAuditEngine
from harness.audit.late_fee import LateFeeAuditEngine
from harness.audit.cashback import CashbackAuditEngine
from harness import db


def test_parse_date_to_iso():
    # ISO date unchanged
    assert parse_date_to_iso("2030-06-03") == "2030-06-03"
    # Exam-style DD/MM/YYYY
    assert parse_date_to_iso("03/06/2030") == "2030-06-03"
    assert parse_date_to_iso("19/05/2030") == "2030-05-19"
    # Textual dates
    assert parse_date_to_iso("22 Aug 2035") == "2035-08-22"
    assert parse_date_to_iso("invalid date") is None


def test_extract_amounts():
    text = "USE2-INS2 CA51959332 Air freight 299 kg 5.69 1,701.31 19/05/2030"
    amts = extract_amounts(text)
    assert Decimal("5.69") in amts
    assert Decimal("1701.31") in amts

    text2 = "Charges INR 28,200.00 Total payable USD 2,493.71"
    amts2 = extract_amounts(text2)
    assert Decimal("28200.00") in amts2
    assert Decimal("2493.71") in amts2


def test_invoice_formats_in_temp_db(tmp_path: Path):
    state_dir = tmp_path / "state"
    db.init_db(state_dir)
    db.record_batch(state_dir, "batch_01", str(tmp_path))

    # Document 1: Scenario style INVOICE <ID>
    doc1 = "batch_01/0001.eml#TX-US-2034-0379.pdf"
    p1_text = (
        "Tarrowby Express Inc.\n"
        "INVOICE TX-US-2034-0379\n"
        "Invoice date 2034-11-26\n"
        "Contract reference TBX-US-EXP-01\n"
        "1 TX46602456 2034-11-17 Air freight USS2-INW2 299 kg 2.84 849.16\n"
        "Total USD 849.16\n"
    )
    db.save_document(
        state_dir, doc1, "batch_01", "TX-US-2034-0379.pdf", None, "pdf", "2034-11-26", False, [p1_text]
    )

    # Document 2: Exam style Charges advice <ID>
    doc2 = "batch_01/0002.eml#CDU-3051254.pdf"
    p2_text = (
        "Caldrith Air Logistics Inc.\n"
        "Charges advice CDU-3051254\n"
        "Document date 03/06/2030 Payable by 03/07/2030\n"
        "Contract CDA-US-EXP-E91 Currency USD\n"
        "USE2-INS2 CA51959332 Air freight 299 kg 5.69 1,701.31 19/05/2030\n"
        "Total payable USD 1,701.31\n"
    )
    db.save_document(
        state_dir, doc2, "batch_01", "CDU-3051254.pdf", None, "pdf", "2030-06-04", False, [p2_text]
    )

    # Document 3: Non-invoice document (Payment advice / email / AP register / MSA)
    doc3 = "batch_01/0003.eml"
    p3_text = (
        "From: billing@carrier.example\n"
        "Subject: Payment Advice Notification\n"
        "Please note payment advice for remittance 98213 is attached.\n"
    )
    db.save_document(
        state_dir, doc3, "batch_01", "0003.eml", None, "eml", "2030-06-04", False, [p3_text]
    )

    doc4 = "batch_01/0004.eml#MSA-CDA.pdf"
    p4_text = (
        "Master Services Agreement\n"
        "Agreement No. CDA-US-EXP-E91\n"
        "Clause 1 Charges advice procedure\n"
    )
    db.save_document(
        state_dir, doc4, "batch_01", "MSA-CDA.pdf", None, "pdf", "2030-06-04", False, [p4_text]
    )

    # Test OverchargeAuditEngine loader
    oc_engine = OverchargeAuditEngine(state_dir, "2035-01-01", "supervised")
    oc_engine.load_data()
    inv_ids = {inv.invoice_id for inv in oc_engine.invoices}

    assert "TX-US-2034-0379" in inv_ids, "Should match scenario style INVOICE <ID>"
    assert "CDU-3051254" in inv_ids, "Should match exam style Charges advice <ID>"
    assert len(oc_engine.invoices) == 2, "Must NOT match non-invoice email or MSA"

    # Check line items on exam invoice
    cdu_inv = next(inv for inv in oc_engine.invoices if inv.invoice_id == "CDU-3051254")
    assert len(cdu_inv.freight_lines) == 1
    fl = cdu_inv.freight_lines[0]
    assert fl.awb == "CA51959332"
    assert fl.weight == Decimal("299")
    assert fl.rate == Decimal("5.69")
    assert fl.amount == Decimal("1701.31")
    assert fl.printed_date == "2030-05-19"
    assert cdu_inv.invoice_date == "2030-06-03"

    # Test DuplicateAuditEngine loader
    dup_engine = DuplicateAuditEngine(state_dir, "2035-01-01", "supervised")
    dup_engine.load_data()
    assert "TX-US-2034-0379" in dup_engine.invoices_by_id
    assert "CDU-3051254" in dup_engine.invoices_by_id
    assert len(dup_engine.invoices_by_id) == 2

    # Test WaiverAuditEngine loader
    wv_engine = WaiverAuditEngine(state_dir, "2035-01-01", "supervised")
    wv_engine.load_data()
    wv_inv_ids = {inv["invoice_id"] for inv in wv_engine.invoices}
    assert "TX-US-2034-0379" in wv_inv_ids
    assert "CDU-3051254" in wv_inv_ids
    assert len(wv_engine.invoices) == 2
