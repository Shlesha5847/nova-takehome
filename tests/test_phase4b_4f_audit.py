"""Phase 4B-4F tests: Verification of remaining 5 audit categories.

Tests duplicate invoices (DU-01, DU-03), waived-but-billed charges (WB-01),
late fees not owed (LF-01) and late fee risk alerts (LF-03),
cashback quarterly volume rebates (CB-01), and contract expiry alerts (EX-01).
Validates official schema compliance and citation resolution via grader/validate.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

# Add repo root to path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from harness import extract
from harness.audit import (
    audit_cashback,
    audit_duplicates,
    audit_expiry,
    audit_late_fees,
    audit_waivers,
)


def test_duplicate_exact_resend():
    """Verify DU-01 duplicate resend of paid invoice on data/examples/duplicate."""
    print("Running test_duplicate_exact_resend...")
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/examples/duplicate/docs/batch_01", td)
        extract.ingest_batch_to_db("data/examples/duplicate/docs/batch_02", td)
        findings = audit_duplicates(td, "2033-08-31", "supervised")

        assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}"
        f = findings[0]
        assert f["id"] == "duplicate:TX-US-2033-5511"
        assert f["class"] == "duplicate"
        assert f["variant"] == "DU-01"
        assert f["verdict"] == "finding"
        assert f["amount_usd"] == "1259.23", f"Expected 1259.23, got {f['amount_usd']}"
        assert f["action"] == "draft:dispute"
        assert len(f["evidence"]) == 2

        # Validate with grader/validate.py
        rep = {
            "as_of": "2033-08-31",
            "mode": "supervised",
            "findings": findings,
            "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []},
        }
        rp = Path(td) / "rep.json"
        rp.write_text(json.dumps(rep, indent=2))
        res = subprocess.run(
            [sys.executable, "grader/validate.py", "--report", str(rp), "--mailbox", "data/examples/duplicate/docs"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        assert res.returncode == 0, f"Validation failed:\n{res.stdout}\n{res.stderr}"
        assert "PASS schema: 0 problem(s)" in res.stdout
        assert "PASS citations: 2 of 2 resolve" in res.stdout

    print("[PASS] test_duplicate_exact_resend")


def test_duplicate_rebilled_annex_e():
    """Verify DU-03 duplicate rebilled charge on data/examples/duplicate_rebilled_4_months."""
    print("Running test_duplicate_rebilled_annex_e...")
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/examples/duplicate_rebilled_4_months/docs/batch_01", td)
        extract.ingest_batch_to_db("data/examples/duplicate_rebilled_4_months/docs/batch_02", td)
        findings = audit_duplicates(td, "2032-08-31", "supervised")

        assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}"
        f = findings[0]
        assert f["id"] == "duplicate:TX-US-2032-5441"
        assert f["class"] == "duplicate"
        assert f["variant"] == "DU-03"
        assert f["verdict"] == "finding"
        assert f["amount_usd"] == "42.08", f"Expected 42.08, got {f['amount_usd']}"
        assert f["action"] == "draft:dispute"
        assert len(f["evidence"]) == 3

        # Validate with grader/validate.py
        rep = {
            "as_of": "2032-08-31",
            "mode": "supervised",
            "findings": findings,
            "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []},
        }
        rp = Path(td) / "rep.json"
        rp.write_text(json.dumps(rep, indent=2))
        res = subprocess.run(
            [sys.executable, "grader/validate.py", "--report", str(rp), "--mailbox", "data/examples/duplicate_rebilled_4_months/docs"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        assert res.returncode == 0, f"Validation failed:\n{res.stdout}\n{res.stderr}"
        assert "PASS schema: 0 problem(s)" in res.stdout
        assert "PASS citations: 3 of 3 resolve" in res.stdout

    print("[PASS] test_duplicate_rebilled_annex_e")


def test_waived_but_billed():
    """Verify WB-01 waived charges on data/examples/waived_but_billed."""
    print("Running test_waived_but_billed...")
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/examples/waived_but_billed/docs/batch_01", td)
        extract.ingest_batch_to_db("data/examples/waived_but_billed/docs/batch_02", td)
        findings = audit_waivers(td, "2034-02-28", "supervised")

        assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}"
        f = findings[0]
        assert f["id"] == "waived_but_billed:TX-US-2033-5337"
        assert f["class"] == "waived_but_billed"
        assert f["variant"] == "WB-01"
        assert f["verdict"] == "finding"
        assert f["amount_usd"] == "133.44", f"Expected 133.44, got {f['amount_usd']}"
        assert f["action"] == "draft:dispute"
        assert len(f["evidence"]) == 4

        # Validate with grader/validate.py
        rep = {
            "as_of": "2034-02-28",
            "mode": "supervised",
            "findings": findings,
            "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []},
        }
        rp = Path(td) / "rep.json"
        rp.write_text(json.dumps(rep, indent=2))
        res = subprocess.run(
            [sys.executable, "grader/validate.py", "--report", str(rp), "--mailbox", "data/examples/waived_but_billed/docs"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        assert res.returncode == 0, f"Validation failed:\n{res.stdout}\n{res.stderr}"
        assert "PASS schema: 0 problem(s)" in res.stdout
        assert "PASS citations: 4 of 4 resolve" in res.stdout

    print("[PASS] test_waived_but_billed")


def test_late_fees_and_risks():
    """Verify LF-01 late fee not owed and LF-03 late fee risk alerts."""
    print("Running test_late_fees_and_risks...")
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/examples/late_fees/docs/batch_01", td)
        extract.ingest_batch_to_db("data/examples/late_fees/docs/batch_02", td)
        findings, risks = audit_late_fees(td, "2033-02-28", "supervised")

        # 1. Finding: TX-US-2032-5496
        assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}"
        f = findings[0]
        assert f["id"] == "late_fee_not_owed:TX-US-2032-5496"
        assert f["class"] == "late_fee_not_owed"
        assert f["variant"] == "LF-01"
        assert f["verdict"] == "finding"
        assert f["amount_usd"] == "12.65", f"Expected 12.65, got {f['amount_usd']}"
        assert f["action"] == "draft:dispute"
        assert len(f["evidence"]) == 4

        # Validate with grader/validate.py
        rep = {
            "as_of": "2033-02-28",
            "mode": "supervised",
            "findings": findings,
            "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": risks},
        }
        rp = Path(td) / "rep.json"
        rp.write_text(json.dumps(rep, indent=2))
        res = subprocess.run(
            [sys.executable, "grader/validate.py", "--report", str(rp), "--mailbox", "data/examples/late_fees/docs"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        assert res.returncode == 0, f"Validation failed:\n{res.stdout}\n{res.stderr}"
        assert "PASS schema: 0 problem(s)" in res.stdout
        assert "PASS citations: 4 of 4 resolve" in res.stdout

    print("[PASS] test_late_fees_and_risks")


def test_cashback_rebate():
    """Verify CB-01 volume rebate on data/examples/cashback."""
    print("Running test_cashback_rebate...")
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/examples/cashback/docs/batch_01", td)
        extract.ingest_batch_to_db("data/examples/cashback/docs/batch_02", td)
        items = audit_cashback(td, "2033-11-30", "supervised")

        assert len(items) == 1, f"Expected 1 cashback finding, got {len(items)}"
        item = items[0]
        assert item["id"] == "cashback:TBX-US-EXP-E58/2033-Q2/rebate"
        assert item["class"] == "cashback"
        assert item["variant"] == "CB-01"
        assert item["credit_type"] == "rebate"
        assert item["period"] == "2033-Q2"
        assert item["verdict"] == "finding"
        assert item["amount_usd"] == "3278.35", f"Expected 3278.35, got {item['amount_usd']}"
        assert item["action"] == "draft:claim_credit"
        assert len(item["evidence"]) == 2

        # Validate with grader/validate.py
        rep = {
            "as_of": "2033-11-30",
            "mode": "supervised",
            "findings": [],
            "portfolio": {"cashback": items, "expiry": [], "late_fee_risk": []},
        }
        rp = Path(td) / "rep.json"
        rp.write_text(json.dumps(rep, indent=2))
        res = subprocess.run(
            [sys.executable, "grader/validate.py", "--report", str(rp), "--mailbox", "data/examples/cashback/docs"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        assert res.returncode == 0, f"Validation failed:\n{res.stdout}\n{res.stderr}"
        assert "PASS schema: 0 problem(s)" in res.stdout
        assert "PASS citations: 2 of 2 resolve" in res.stdout

    print("[PASS] test_cashback_rebate")


def test_contract_expiry():
    """Verify EX-01 contract expiry alert on data/examples/expiry."""
    print("Running test_contract_expiry...")
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/examples/expiry/docs/batch_01", td)

        # 1. Alert window: as of 2034-02-28, deadline 2034-03-20 (20 days left) -> finding
        alerts = audit_expiry(td, "2034-02-28", "supervised")
        assert len(alerts) == 1, f"Expected 1 expiry alert, got {len(alerts)}"
        a = alerts[0]
        assert a["id"] == "expiry:TBX-US-EXP-E24"
        assert a["class"] == "expiry"
        assert a["variant"] == "EX-01"
        assert a["deadline"] == "2034-03-20"
        assert a["amount_usd"] is None
        assert a["action"] == "internal:alert"
        assert len(a["evidence"]) == 1

        # Validate with grader/validate.py
        rep = {
            "as_of": "2034-02-28",
            "mode": "supervised",
            "findings": [],
            "portfolio": {"cashback": [], "expiry": alerts, "late_fee_risk": []},
        }
        rp = Path(td) / "rep.json"
        rp.write_text(json.dumps(rep, indent=2))
        res = subprocess.run(
            [sys.executable, "grader/validate.py", "--report", str(rp), "--mailbox", "data/examples/expiry/docs"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        assert res.returncode == 0, f"Validation failed:\n{res.stdout}\n{res.stderr}"
        assert "PASS schema: 0 problem(s)" in res.stdout
        assert "PASS citations: 1 of 1 resolve" in res.stdout

        # 2. Decoy: as of 2033-08-31 (> 60 days left) -> no alert
        decoy_alerts = audit_expiry(td, "2033-08-31", "supervised")
        assert len(decoy_alerts) == 0, f"Expected 0 alerts for decoy date, got {len(decoy_alerts)}"

    print("[PASS] test_contract_expiry")


def run_all():
    print("Starting Phase 4B-4F verification suite...")
    test_duplicate_exact_resend()
    test_duplicate_rebilled_annex_e()
    test_waived_but_billed()
    test_late_fees_and_risks()
    test_cashback_rebate()
    test_contract_expiry()
    print("\nALL PHASE 4B-4F TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    run_all()
