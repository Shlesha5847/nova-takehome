"""Your harness. The grader calls exactly these three commands, in a fresh process each time:

    python harness.py ingest <batch_dir> --state-dir <dir>
    python harness.py report --as-of YYYY-MM-DD [--mode supervised|autonomous] [--state-dir <dir>] > report.json
    python harness.py feedback <feedback.json> --state-dir <dir>

- ingest: read every .eml in <batch_dir> (attachments inside) and store what you learn under --state-dir. The process
  is restarted between batches, so everything you need later must be on disk.
- report: print one report.json (grader/schema/report.schema.json) to stdout. Only documents received on or before
  --as-of count. --mode defaults to supervised.
- feedback: take in a person's decisions on what you escalated (feedback.json) and let them change later reports.

The environment gives ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL. Keep the argument names as they are: grader/validate.py
checks them. Replace the NotImplementedError bodies with your code.
"""
from __future__ import annotations

import argparse
import sys

DEFAULT_STATE = ".state"


import json
import sys
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

_HARNESS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HARNESS_DIR.parent
while str(_HARNESS_DIR) in sys.path:
    sys.path.remove(str(_HARNESS_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Portable fallback to local .venv site-packages when running under stripped environment
for _site in (_REPO_ROOT / ".venv").glob("**/site-packages"):
    if _site.is_dir() and str(_site) not in sys.path:
        sys.path.append(str(_site))

from harness import db, extract


def ingest(batch_dir: str, state_dir: str) -> None:
    """Read .eml files and attachments in batch_dir and persist extracted pages under state_dir."""
    extract.ingest_batch_to_db(batch_dir, state_dir)


def report(as_of: str, mode: str, state_dir: str) -> dict:
    """Generate schema-compliant report JSON for as_of date and mode."""
    from harness.audit import (
        audit_cashback,
        audit_duplicates,
        audit_expiry,
        audit_late_fees,
        audit_overcharges,
        audit_waivers,
    )

    db.init_db(state_dir)

    # 1. Run audit engines
    overcharge_findings = audit_overcharges(state_dir, as_of, mode)
    duplicate_findings = audit_duplicates(state_dir, as_of, mode)
    waiver_findings = audit_waivers(state_dir, as_of, mode)

    # Track disputed amounts per invoice so late-fee risk is computed on undisputed principal
    disputed_per_inv: dict[str, Decimal] = {}
    for f in overcharge_findings + duplicate_findings + waiver_findings:
        if f.get("verdict") == "finding" and f.get("amount_usd") is not None:
            inv_id = f.get("invoice_id")
            if inv_id:
                try:
                    disputed_per_inv[inv_id] = disputed_per_inv.get(inv_id, Decimal("0.00")) + Decimal(str(f["amount_usd"]))
                except Exception:
                    pass

    # Late fees and late fee risks (passing disputed_per_inv)
    late_fee_findings, late_fee_risks = audit_late_fees(
        state_dir, as_of, mode, disputed_amounts_by_invoice=disputed_per_inv
    )

    # Portfolio audits
    cashback_items = audit_cashback(state_dir, as_of, mode)
    expiry_items = audit_expiry(state_dir, as_of, mode)

    # 2. Keyed deduplication of invoice findings
    # Key: (class, invoice_id)
    seen_invoice_keys: set[tuple[str, str]] = set()
    deduped_findings: list[dict[str, Any]] = []
    for f in overcharge_findings + duplicate_findings + waiver_findings + late_fee_findings:
        cls = f.get("class")
        inv_id = f.get("invoice_id")
        if cls and inv_id:
            k = (cls, inv_id)
            if k in seen_invoice_keys:
                continue
            seen_invoice_keys.add(k)
        deduped_findings.append(f)

    # 3. Apply feedback decisions
    feedback_payloads = db.get_all_feedback_payloads(state_dir)
    closed_keys: set[tuple[str, str]] = set()
    learned_tags: dict[tuple[str, str], dict[str, str]] = {}
    for fb in feedback_payloads:
        after_batch = fb.get("after_batch", "")
        for it in fb.get("items", []):
            decision = it.get("decision")
            item_id = it.get("item_id", "")
            f_key = it.get("finding_key", {})
            k_cls = f_key.get("class")
            k_inv = f_key.get("invoice_id")
            if k_cls and k_inv:
                if decision in ("close", "reject"):
                    closed_keys.add((k_cls, k_inv))
                elif item_id and decision == "confirm":
                    learned_tags[(k_cls, k_inv)] = {
                        "feedback_item_id": item_id,
                        "batch": after_batch,
                    }

    # Filter out closed keys and attach learned_at
    active_findings: list[dict[str, Any]] = []
    for f in deduped_findings:
        k = (f.get("class", ""), f.get("invoice_id", ""))
        if k in closed_keys:
            continue
        if k in learned_tags and not f.get("learned_at"):
            f["learned_at"] = learned_tags[k]
        active_findings.append(f)

    # 4. Outbound amounts threshold & Human Review gating
    # Over $5000 on one invoice (summed over outbound findings on that invoice)
    outbound_per_inv: dict[str, Decimal] = {}
    for f in active_findings:
        if f.get("verdict") == "finding" and f.get("amount_usd") is not None:
            inv_id = f.get("invoice_id")
            if inv_id:
                try:
                    outbound_per_inv[inv_id] = outbound_per_inv.get(inv_id, Decimal("0.00")) + Decimal(str(f["amount_usd"]))
                except Exception:
                    pass

    for f in active_findings:
        inv_id = f.get("invoice_id")
        if inv_id and outbound_per_inv.get(inv_id, Decimal("0.00")) >= Decimal("5000.00"):
            if f.get("action") not in ("escalate", "none", "internal:alert", "internal:payment_reminder"):
                f["action"] = "human_review"

    # 5. Autonomous mode unattended-action test
    if mode == "autonomous":
        for f in active_findings:
            if f.get("verdict") == "finding":
                act = f.get("action", "")
                if act.startswith("draft:"):
                    has_ev = bool(f.get("evidence"))
                    no_missing = f.get("missing_doc") is None
                    inv_id = f.get("invoice_id")
                    under_thresh = outbound_per_inv.get(inv_id or "", Decimal("0.00")) < Decimal("5000.00")
                    if has_ev and no_missing and under_thresh:
                        f["action"] = act.replace("draft:", "execute:")
                    else:
                        f["action"] = "human_review"
            elif f.get("verdict") == "cannot_determine":
                act = f.get("action", "")
                if act == "draft:request_document":
                    f["action"] = "execute:request_document"

        for cb in cashback_items:
            act = cb.get("action", "")
            if act.startswith("draft:"):
                cb_amt = Decimal(cb["amount_usd"]) if cb.get("amount_usd") else Decimal("0.00")
                if cb_amt < Decimal("5000.00"):
                    cb["action"] = act.replace("draft:", "execute:")
                else:
                    cb["action"] = "human_review"

    # 6. Adapt to previous report (supersedes)
    prev_report = db.get_latest_previous_report(state_dir, as_of, mode)
    if prev_report:
        prev_findings = prev_report.get("findings", [])
        prev_by_key = {
            (pf.get("class"), pf.get("invoice_id")): pf
            for pf in prev_findings
            if pf.get("class") and pf.get("invoice_id")
        }
        for f in active_findings:
            k = (f.get("class"), f.get("invoice_id"))
            if k in prev_by_key:
                old_f = prev_by_key[k]
                if old_f.get("verdict") != f.get("verdict") or old_f.get("amount_usd") != f.get("amount_usd"):
                    old_id = old_f.get("id")
                    if old_id and old_id not in f.get("supersedes", []):
                        f.setdefault("supersedes", []).append(old_id)

    report_dict = {
        "schema_version": "1.0",
        "as_of": as_of,
        "mode": mode,
        "findings": active_findings,
        "portfolio": {
            "cashback": cashback_items,
            "expiry": expiry_items,
            "late_fee_risk": late_fee_risks,
        },
    }

    # Save to DB for historical queries / supersedes
    db.save_report(state_dir, as_of, mode, report_dict)
    return report_dict


def feedback(feedback_file: str, state_dir: str) -> None:
    """Store decisions from feedback_file into state_dir."""
    fb_path = Path(feedback_file)
    if not fb_path.is_file():
        raise FileNotFoundError(f"feedback file not found: {feedback_file}")
    db.init_db(state_dir)
    payload = json.loads(fb_path.read_text(encoding="utf-8"))
    db.record_feedback(state_dir, str(fb_path), payload)



def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.py", description="Freight invoice audit harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="read one batch folder into the state dir")
    p.add_argument("batch_dir")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("report", help="print report.json for a date")
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD; only documents received by then count")
    p.add_argument("--mode", choices=["supervised", "autonomous"], default="supervised")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("feedback", help="take in a person's decisions (feedback.json)")
    p.add_argument("feedback_file")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ingest":
            ingest(a.batch_dir, a.state_dir)
        elif a.cmd == "report":
            import json
            json.dump(report(a.as_of, a.mode, a.state_dir), sys.stdout, indent=1)
            sys.stdout.write("\n")
        else:
            feedback(a.feedback_file, a.state_dir)
    except NotImplementedError as e:
        print(f"harness.py {a.cmd}: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
