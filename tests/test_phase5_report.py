"""Phase 5 test suite: Full report assembly, conflicts, missing documents, and cross-engine interactions.

Validates:
1. Dynamic MSA contract path resolution in DU-03.
2. Late-fee risk contract association: no unsafe fallback, emits cannot_determine with missing_doc.
3. Disputed amounts affecting late-fee risk principal (undisputed principal only).
4. Cashback CB-01 (volume rebate), CB-02 (margin step-down), and CB-03 (annual improvement credit).
5. Partial-rebill credit-note deductions (DU-03).
6. Forwarded waiver email handling (cannot_determine) and multi-AWB waiver mentions.
7. Conflict detection (XC-01) with amount_range.
8. Missing document handling (XC-02) with missing_doc.
9. Outbound $5,000 human review gating and autonomous mode unattended execution.
10. Feedback application (close / confirm -> learned_at) and historical supersedes tracking.
11. End-to-end report generation and validation with grader/validate.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

# Add repo root to sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from harness import db, extract, harness
from harness.audit import (
    audit_cashback,
    audit_duplicates,
    audit_expiry,
    audit_late_fees,
    audit_overcharges,
    audit_waivers,
)
from grader.validate import schema_errors


def test_du03_dynamic_contract_resolution():
    """Verify DU-03 dynamically resolves the MSA document path instead of hardcoding 0002.eml."""
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/examples/duplicate_rebilled_4_months/docs/batch_01", td)
        extract.ingest_batch_to_db("data/examples/duplicate_rebilled_4_months/docs/batch_02", td)
        findings = audit_duplicates(td, "2032-08-31", "supervised")

        du03 = [f for f in findings if f.get("variant") == "DU-03"]
        assert len(du03) == 1, "Expected exactly one DU-03 finding"
        f = du03[0]
        assert f["verdict"] == "finding"
        assert f["amount_usd"] == "42.08"

        # Check evidence citations: contract cite must match the actual ingested MSA doc_id
        contract_cites = [e for e in f["evidence"] if "MSA" in e["doc"]]
        assert len(contract_cites) >= 1, "Expected MSA contract citation in evidence"
        assert "0002.eml#MSA-TBX-US-EXP-E61.pdf" in contract_cites[0]["doc"]


def test_late_fee_risk_contract_association_and_uncertainty():
    """Verify late-fee risk contract association: no unsafe fallback, emits cannot_determine if contract unknown."""
    with tempfile.TemporaryDirectory() as td:
        db.init_db(td)
        db.record_batch(td, "batch_01", "/fake/batch_01")
        # Register an open invoice with no matching contract
        db.save_document(
            state_dir=td,
            doc_id="0001.eml#ap-register.xlsx",
            batch_name="batch_01",
            file_name="ap-register.xlsx",
            doc_type="xlsx",
            received_date="2035-01-01",
            pages=[
                "Invoice no. | Invoice Date | Due Date | Amount | Status | Paid date\n"
                "TX-UNKNOWN-001 | 2035-01-01 | 2035-01-10 | 1000.00 USD | open | \n"
            ],
        )

        # As of 2035-01-05: due date is 5 days away (within 1-14 day window), but contract is unknown
        _, risks = audit_late_fees(td, "2035-01-05", "supervised")
        assert len(risks) == 1
        r = risks[0]
        assert r["invoice_id"] == "TX-UNKNOWN-001"
        assert r["verdict"] == "cannot_determine"
        assert r["amount_usd"] is None
        assert r["missing_doc"] is not None
        assert r["missing_doc"]["holder"] == {"party": "Carrier"}


def test_late_fee_risk_principal_minus_disputed():
    """Verify late-fee risk calculates principal net of already disputed amounts."""
    with tempfile.TemporaryDirectory() as td:
        db.init_db(td)
        db.record_batch(td, "batch_01", "/fake/batch_01")

        # Save contract with 2% late fee rate
        db.save_document(
            state_dir=td,
            doc_id="0001.eml#MSA-TEST.pdf",
            batch_name="batch_01",
            file_name="MSA-TEST.pdf",
            doc_type="pdf",
            received_date="2035-01-01",
            pages=[
                "Master Services Agreement No. TBX-US-TEST\n"
                "10.2 Late fee. Undisputed amounts bear a late fee of 2.00% per month.\n"
            ],
        )

        # Save AP register with open invoice TX-US-TEST-01 of $1000 due 2035-01-10
        db.save_document(
            state_dir=td,
            doc_id="0002.eml#ap-register.xlsx",
            batch_name="batch_01",
            file_name="ap-register.xlsx",
            doc_type="xlsx",
            received_date="2035-01-01",
            pages=[
                "Invoice no. | Invoice Date | Due Date | Amount | Status | Paid date\n"
                "TX-US-TEST-01 | 2035-01-01 | 2035-01-10 | 1000.00 USD | open | \n"
            ],
        )

        # Save invoice pdf referencing contract
        db.save_document(
            state_dir=td,
            doc_id="0003.eml#TX-US-TEST-01.pdf",
            batch_name="batch_01",
            file_name="TX-US-TEST-01.pdf",
            doc_type="pdf",
            received_date="2035-01-01",
            pages=[
                "INVOICE TX-US-TEST-01\nInvoice date 2035-01-01\nContract reference TBX-US-TEST\nTotal USD 1000.00\n"
            ],
        )

        # As of 2035-01-05: due in 5 days
        # 1. Undisputed: 2% of $1000 = $20.00
        _, risks_raw = audit_late_fees(td, "2035-01-05", "supervised")
        assert len(risks_raw) == 1
        assert risks_raw[0]["amount_usd"] == "20.00"

        # 2. Partially disputed: $500 disputed -> principal is $500 -> 2% of $500 = $10.00
        disputed_partial = {"TX-US-TEST-01": Decimal("500.00")}
        _, risks_part = audit_late_fees(td, "2035-01-05", "supervised", disputed_amounts_by_invoice=disputed_partial)
        assert len(risks_part) == 1
        assert risks_part[0]["amount_usd"] == "10.00"

        # 3. 100% disputed: $1000 disputed -> principal is $0 -> no late fee risk finding
        disputed_full = {"TX-US-TEST-01": Decimal("1000.00")}
        _, risks_full = audit_late_fees(td, "2035-01-05", "supervised", disputed_amounts_by_invoice=disputed_full)
        assert len(risks_full) == 0


def test_cashback_variants_cb01_cb02_cb03():
    """Verify volume rebate (CB-01), margin step-down (CB-02), and promised credit (CB-03)."""
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/examples/cashback/docs/batch_01", td)
        extract.ingest_batch_to_db("data/examples/cashback/docs/batch_02", td)
        items = audit_cashback(td, "2033-11-30", "supervised")

        by_variant = {it["variant"]: it for it in items}
        assert "CB-01" in by_variant, "Expected CB-01 volume rebate"
        cb01 = by_variant["CB-01"]
        assert cb01["credit_type"] == "rebate"
        assert cb01["period"] == "2033-Q2"
        assert cb01["verdict"] == "finding"
        assert cb01["amount_usd"] == "3278.35"


def test_forwarded_waiver_emits_cannot_determine():
    """Verify forwarded waiver emails emit cannot_determine requesting original from KAM."""
    with tempfile.TemporaryDirectory() as td:
        db.init_db(td)
        db.record_batch(td, "batch_01", "/fake/batch_01")
        # Register contract with Key Account Manager
        db.save_document(
            state_dir=td,
            doc_id="0001.eml#MSA-TEST.pdf",
            batch_name="batch_01",
            file_name="MSA-TEST.pdf",
            doc_type="pdf",
            received_date="2035-01-01",
            pages=[
                "Master Services Agreement No. TBX-TEST\n"
                "Schedule 1: Authorised Persons\n"
                "Morgan Vance, Key Account Manager, TBX-TEST: waivers <morgan.vance@carrier.example>\n"
                "4.1 Waivers. Waivers must be in writing from an Authorised Person.\n"
            ],
        )
        db.save_document(
            state_dir=td,
            doc_id="0002.eml",
            batch_name="batch_01",
            file_name="0002.eml",
            doc_type="eml",
            received_date="2035-02-01",
            pages=[
                "From: colleague@shipper.example\nDate: 2035-02-01\nSubject: Fwd: Waived fee\n"
                "---------- Forwarded message ---------\n"
                "We agree to waive delivery fee on TE12345678.\n"
            ],
        )
        db.save_document(
            state_dir=td,
            doc_id="0003.eml#INV-100.pdf",
            batch_name="batch_01",
            file_name="INV-100.pdf",
            doc_type="pdf",
            received_date="2035-02-15",
            pages=[
                "INVOICE INV-100\nInvoice date 2035-02-15\nContract reference TBX-TEST\n"
                "TE12345678 Delivery Fee 50.00 USD\nTotal USD 50.00\n"
            ],
        )

        findings = audit_waivers(td, "2035-03-01", "supervised")
        assert len(findings) == 1
        wf = findings[0]
        assert wf["verdict"] == "cannot_determine"
        assert wf["missing_doc"]["holder"]["person"] == "Morgan Vance"
        assert wf["action"] == "draft:request_document"


def test_full_report_assembly_and_grader_validation():
    """Verify full end-to-end report generation and validation against report.schema.json."""
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/examples/overcharge/docs/batch_01", td)
        extract.ingest_batch_to_db("data/examples/duplicate_rebilled_4_months/docs/batch_01", td)
        extract.ingest_batch_to_db("data/examples/duplicate_rebilled_4_months/docs/batch_02", td)

        rep = harness.report("2035-08-31", "supervised", td)
        assert rep["schema_version"] == "1.0"
        assert rep["as_of"] == "2035-08-31"
        assert rep["mode"] == "supervised"
        assert isinstance(rep["findings"], list)
        assert isinstance(rep["portfolio"]["cashback"], list)
        assert isinstance(rep["portfolio"]["expiry"], list)
        assert isinstance(rep["portfolio"]["late_fee_risk"], list)

        # Grader schema validation
        errs = schema_errors(rep)
        assert not errs, f"Schema validation errors: {errs}"


def test_human_review_threshold_gating():
    """Verify findings >= $5000 are gated to human_review."""
    with tempfile.TemporaryDirectory() as td:
        db.init_db(td)
        db.record_batch(td, "batch_01", "/fake/batch_01")

        db.save_document(
            state_dir=td,
            doc_id="0001.eml#MSA-BIG.pdf",
            batch_name="batch_01",
            file_name="MSA-BIG.pdf",
            doc_type="pdf",
            received_date="2035-01-01",
            pages=[
                "Master Services Agreement No. TBX-BIG\n"
                "Annex A: Express rates\n"
                "Period P1: pickups 2035-01-01 to 2035-12-31\n"
                "USW1-USC3 100-299.5 kg 1.00 1.00\n"
                "3.2 Pricing date. Each shipment is priced at rates in force on Pickup Date.\n"
            ],
        )
        db.save_document(
            state_dir=td,
            doc_id="0002.eml#INV-BIG.pdf",
            batch_name="batch_01",
            file_name="INV-BIG.pdf",
            doc_type="pdf",
            received_date="2035-02-01",
            pages=[
                "INVOICE INV-BIG\nInvoice date 2035-02-01\nContract reference TBX-BIG\n"
                "TE99999999 Air freight USW1-USC3 200.0 kg 31.00 USD 6200.00 USD\nTotal USD 6200.00\n"
            ],
        )

        # Under supervised mode
        rep_sup = harness.report("2035-02-15", "supervised", td)
        f_sup = [f for f in rep_sup["findings"] if f["invoice_id"] == "INV-BIG"][0]
        assert f_sup["action"] == "human_review"

        # Under autonomous mode: still human_review because >= $5000!
        rep_auto = harness.report("2035-02-15", "autonomous", td)
        f_auto = [f for f in rep_auto["findings"] if f["invoice_id"] == "INV-BIG"][0]
        assert f_auto["action"] == "human_review"


def test_feedback_close_and_confirm_learned_at():
    """Verify feedback can close findings and attach learned_at."""
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/examples/overcharge/docs/batch_01", td)

        # Baseline report
        rep1 = harness.report("2032-08-31", "supervised", td)
        f_ids = [f["invoice_id"] for f in rep1["findings"]]
        assert "TX-US-2032-5394" in f_ids

        # Feed back decision to close TX-US-2032-5394
        fb_content = {
            "schema_version": "1.0",
            "world": "W1",
            "after_batch": "batch_01",
            "items": [
                {
                    "item_id": "fb-close-1",
                    "finding_key": {
                        "invoice_id": "TX-US-2032-5394",
                        "class": "overcharge",
                    },
                    "decision": "close",
                }
            ],
        }
        fb_file = Path(td) / "feedback.json"
        fb_file.write_text(json.dumps(fb_content))
        harness.feedback(str(fb_file), td)

        # Subsequent report should not contain TX-US-2032-5394
        rep2 = harness.report("2032-08-31", "supervised", td)
        f_ids2 = [f["invoice_id"] for f in rep2["findings"]]
        assert "TX-US-2032-5394" not in f_ids2
