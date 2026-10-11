"""Phase 4A tests: Overcharge detection engine, pricing formulas, date rules, currency conversion, and evidence citation validation.

Tests overcharge detection against worked examples, conflict resolution, INR conversion via Annex F,
Dry Ice vs DG handling (OC-02), and checks citation resolution via grader/validate.py.
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

from harness import extract, db
from harness.audit import overcharge


def test_overcharge_worked_example():
    """Verify overcharge detection on data/examples/overcharge."""
    print("Running test_overcharge_worked_example...")
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/examples/overcharge/docs/batch_01", td)
        findings = overcharge.audit_overcharges(td, "2032-08-31", "supervised")

        by_id = {f["invoice_id"]: f for f in findings}

        # 1. Finding case: TX-US-2032-5394
        assert "TX-US-2032-5394" in by_id, "Missing TX-US-2032-5394"
        f1 = by_id["TX-US-2032-5394"]
        assert f1["id"] == "overcharge:TX-US-2032-5394"
        assert f1["class"] == "overcharge"
        assert f1["variant"] == "OC-01"
        assert f1["verdict"] == "finding"
        assert f1["amount_usd"] == "196.92", f"Expected 196.92, got {f1['amount_usd']}"
        assert f1["action"] == "draft:dispute"
        assert len(f1["evidence"]) == 4

        # 2. Decoy case: TX-US-2032-5397
        assert "TX-US-2032-5397" in by_id, "Missing TX-US-2032-5397"
        f2 = by_id["TX-US-2032-5397"]
        assert f2["verdict"] == "no_finding"
        assert f2["amount_usd"] is None
        assert f2["action"] == "none"

        # 3. Check official grader validation on the generated report
        report_data = {
            "as_of": "2032-08-31",
            "mode": "supervised",
            "findings": findings,
            "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []},
        }
        report_path = Path(td) / "report.json"
        report_path.write_text(json.dumps(report_data, indent=2))

        proc = subprocess.run(
            [
                sys.executable,
                "grader/validate.py",
                "--report",
                str(report_path),
                "--mailbox",
                "data/examples/overcharge/docs",
            ],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        assert proc.returncode == 0, f"validate.py failed:\n{proc.stdout}\n{proc.stderr}"
        assert "PASS schema: 0 problem(s)" in proc.stdout
        assert "PASS citations: 8 of 8 resolve" in proc.stdout

    print("[PASS] test_overcharge_worked_example")


def test_conflict_worked_example():
    """Verify conflict handling on data/examples/conflict."""
    print("Running test_conflict_worked_example...")
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/examples/conflict/docs/batch_01", td)
        extract.ingest_batch_to_db("data/examples/conflict/docs/batch_02", td)
        findings = overcharge.audit_overcharges(td, "2032-11-30", "supervised")

        by_id = {f["invoice_id"]: f for f in findings}
        assert "TX-US-2032-5166" in by_id, "Missing TX-US-2032-5166"
        f = by_id["TX-US-2032-5166"]

        assert f["id"] == "overcharge:TX-US-2032-5166"
        assert f["class"] == "overcharge"
        assert f["variant"] == "XC-01"
        assert f["verdict"] == "conflict"
        assert f["amount_usd"] is None
        assert f["amount_range"] == ["0.00", "20.87"], f"Expected ['0.00', '20.87'], got {f['amount_range']}"
        assert f["action"] == "escalate"
        assert len(f["evidence"]) == 3

        # Validate with grader/validate.py
        report_data = {
            "as_of": "2032-11-30",
            "mode": "supervised",
            "findings": findings,
            "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []},
        }
        report_path = Path(td) / "report.json"
        report_path.write_text(json.dumps(report_data, indent=2))

        proc = subprocess.run(
            [
                sys.executable,
                "grader/validate.py",
                "--report",
                str(report_path),
                "--mailbox",
                "data/examples/conflict/docs",
            ],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        assert proc.returncode == 0, f"validate.py failed:\n{proc.stdout}\n{proc.stderr}"
        assert "PASS schema: 0 problem(s)" in proc.stdout
        assert "PASS citations: 3 of 3 resolve" in proc.stdout

    print("[PASS] test_conflict_worked_example")


def test_inr_currency_conversion():
    """Verify Annex F INR conversion on scenario invoice TXI/35-36/0030."""
    print("Running test_inr_currency_conversion...")
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/scenarios/batch_01", td)
        extract.ingest_batch_to_db("data/scenarios/batch_02", td)
        findings = overcharge.audit_overcharges(td, "2035-09-30", "supervised")

        by_id = {f["invoice_id"]: f for f in findings}
        assert "TXI/35-36/0030" in by_id, "Missing TXI/35-36/0030"
        f = by_id["TXI/35-36/0030"]

        assert f["verdict"] == "finding"
        assert f["amount_usd"] == "383.13", f"Expected 383.13 USD, got {f['amount_usd']}"
        assert f["action"] == "draft:dispute"
        assert len(f["evidence"]) == 4

    print("[PASS] test_inr_currency_conversion")


def test_dry_ice_dg_handling():
    """Verify OC-02 Dry ice vs DG fee on invoice TX-US-2035-0236."""
    print("Running test_dry_ice_dg_handling...")
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/scenarios/batch_01", td)
        extract.ingest_batch_to_db("data/scenarios/batch_02", td)
        findings = overcharge.audit_overcharges(td, "2035-09-30", "supervised")

        by_id = {f["invoice_id"]: f for f in findings}
        assert "TX-US-2035-0236" in by_id, "Missing TX-US-2035-0236"
        f = by_id["TX-US-2035-0236"]

        assert f["variant"] == "OC-02"
        assert f["verdict"] == "finding"
        assert f["amount_usd"] == "124.02", f"Expected 124.02 USD, got {f['amount_usd']}"
        assert f["action"] == "draft:dispute"

    print("[PASS] test_dry_ice_dg_handling")


def test_action_gating_and_modes():
    """Verify action gating between supervised and autonomous modes."""
    print("Running test_action_gating_and_modes...")
    with tempfile.TemporaryDirectory() as td:
        extract.ingest_batch_to_db("data/examples/overcharge/docs/batch_01", td)

        # Supervised: outbound dispute is draft:dispute
        sup_findings = overcharge.audit_overcharges(td, "2032-08-31", "supervised")
        f_sup = [f for f in sup_findings if f["invoice_id"] == "TX-US-2032-5394"][0]
        assert f_sup["action"] == "draft:dispute"

        # Autonomous: outbound dispute is execute:dispute (under $5000, passes unattended test)
        auto_findings = overcharge.audit_overcharges(td, "2032-08-31", "autonomous")
        f_auto = [f for f in auto_findings if f["invoice_id"] == "TX-US-2032-5394"][0]
        assert f_auto["action"] == "execute:dispute"

    print("[PASS] test_action_gating_and_modes")


def run_all():
    print("Starting Phase 4A verification suite...")
    test_overcharge_worked_example()
    test_conflict_worked_example()
    test_inr_currency_conversion()
    test_dry_ice_dg_handling()
    test_action_gating_and_modes()
    print("\nALL PHASE 4A TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    run_all()
