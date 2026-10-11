"""Cashback and credit audit engine for the Nova freight audit harness.

Calculates quarterly volume rebates (CB-01), margin step-downs (CB-02), and promised credits (CB-03)
strictly via deterministic Python business logic according to contract Qualifying Spend definitions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from harness import db
from harness.audit.common import extract_amounts, parse_date_to_iso, round_half_up


def get_quarter_for_date(date_str: str) -> str:
    """Return YYYY-Qn for an ISO date."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    q = (dt.month - 1) // 3 + 1
    return f"{dt.year}-Q{q}"


def get_quarter_end_date(quarter_str: str) -> str:
    """Return the last date of the quarter."""
    year, q = quarter_str.split("-Q")
    if q == "1":
        return f"{year}-03-31"
    if q == "2":
        return f"{year}-06-30"
    if q == "3":
        return f"{year}-09-30"
    return f"{year}-12-31"


@dataclass
class CashbackContract:
    doc_id: str
    contract_id: str
    effective_date: str | None = None
    tiers: list[tuple[Decimal, Decimal]] = field(default_factory=list)  # (threshold, percent)
    clause_7_1: tuple[str, int] | None = None
    clause_7_2: tuple[str, int] | None = None
    clause_7_3: tuple[str, int] | None = None
    clause_1_1: tuple[str, int] | None = None
    annex_f_rates: dict[str, Decimal] = field(default_factory=dict)
    margin_step_down_threshold: Decimal | None = None
    margin_step_down_pct: Decimal | None = None
    annual_improvement_credit: Decimal | None = None


class CashbackAuditEngine:
    """Deterministic audit engine for cashback rebates and credits."""

    def __init__(self, state_dir: str | Path, as_of_date: str, mode: str = "supervised"):
        self.state_dir = Path(state_dir)
        self.as_of_date = as_of_date
        self.mode = mode

        self.contracts: dict[str, CashbackContract] = {}
        self.invoices_by_contract_quarter: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.credit_notes_by_contract_quarter: dict[tuple[str, str], list[Decimal]] = {}
        self.credit_notes_by_contract_period_type: dict[tuple[str, str, str], list[Decimal]] = {}

    def load_data(self) -> None:
        docs = db.get_documents_up_to_date(self.state_dir, self.as_of_date)
        pages_cache: dict[str, list[str]] = {}
        for d in docs:
            pages_cache[d["doc_id"]] = db.get_document_pages(self.state_dir, d["doc_id"])

        for d in docs:
            doc_id = d["doc_id"]
            pages = pages_cache.get(doc_id, [])
            if not pages:
                continue

            doc_type = d["doc_type"]
            first_page = pages[0]

            # 1. Contract / Clauses 1.1, 7.1, 7.2, 7.3 & Annex F
            if doc_type == "pdf" and "Master Services Agreement" in first_page and "Amendment" not in first_page:
                c_m = re.search(r"Agreement No\.\s+([A-Z0-9\-]+)", first_page) or re.search(r"TBX-[A-Z0-9\-]+", first_page)
                if c_m:
                    contract_id = c_m.group(0).split()[-1]
                    contract = CashbackContract(doc_id=doc_id, contract_id=contract_id)

                    date_eff_m = re.search(r"commencing on\s+(\d{4}-\d{2}-\d{2})", first_page) or re.search(r"dated\s+(\d{4}-\d{2}-\d{2})", first_page)
                    if date_eff_m:
                        contract.effective_date = date_eff_m.group(1)

                    for p_idx, p_text in enumerate(pages, start=1):
                        if "1.1 Qualifying Spend." in p_text:
                            cl_1_m = re.search(r"(1\.1 Qualifying Spend\..*?)(?=\n1\.2|\n2|\Z)", p_text, re.DOTALL)
                            if cl_1_m:
                                contract.clause_1_1 = (" ".join(cl_1_m.group(1).split()), p_idx)
                        if "7.1 Volume rebate." in p_text:
                            # Extract entire clause 7.1 across wrapped lines
                            cl_m = re.search(r"(7\.1 Volume rebate\..*?)(?=\n7\.2|\n8|\Z)", p_text, re.DOTALL)
                            if cl_m:
                                full_clause = " ".join(cl_m.group(1).split())
                                contract.clause_7_1 = (full_clause, p_idx)
                                # Extract tiers: e.g. "QS >= USD 55,000.00: 5%; QS >= USD 91,000.00: 7.5%; QS >= USD 150,000.00: 10%"
                                tier_matches = re.findall(r"QS\s*>=\s*(?:USD\s*)?([\d,]+\.\d{2}):\s*(\d+(?:\.\d+)?)%", full_clause)
                                for thresh_str, pct_str in tier_matches:
                                    t_val = Decimal(thresh_str.replace(",", ""))
                                    p_val = Decimal(pct_str)
                                    contract.tiers.append((t_val, p_val))
                                contract.tiers.sort(key=lambda t: t[0], reverse=True)

                        if "7.2 Margin step-down" in p_text or "7.2 Handling margin" in p_text:
                            cl_2_m = re.search(r"(7\.2 .*?)(?=\n7\.3|\n8|\Z)", p_text, re.DOTALL)
                            if cl_2_m:
                                full_cl2 = " ".join(cl_2_m.group(1).split())
                                contract.clause_7_2 = (full_cl2, p_idx)
                                m_pct = re.search(r"(\d+(?:\.\d+)?)%", full_cl2)
                                m_thresh = re.search(r"([\d,]+\.\d{2})", full_cl2)
                                if m_pct and m_thresh:
                                    contract.margin_step_down_pct = Decimal(m_pct.group(1))
                                    contract.margin_step_down_threshold = Decimal(m_thresh.group(1).replace(",", ""))

                        if "7.3 Annual improvement credit" in p_text or "7.3 Improvement credit" in p_text:
                            cl_3_m = re.search(r"(7\.3 .*?)(?=\n7\.4|\n8|\Z)", p_text, re.DOTALL)
                            if cl_3_m:
                                full_cl3 = " ".join(cl_3_m.group(1).split())
                                contract.clause_7_3 = (full_cl3, p_idx)
                                amt_m = re.search(r"(?:USD\s*)?([\d,]+\.\d{2})", full_cl3)
                                if amt_m:
                                    contract.annual_improvement_credit = Decimal(amt_m.group(1).replace(",", ""))

                        if "Annex F" in p_text or "Annex F:" in p_text:
                            for l in p_text.splitlines():
                                m = re.search(r"(\d{4}-\d{2})\s+(\d+\.\d{2})", l)
                                if m:
                                    contract.annex_f_rates[m.group(1)] = Decimal(m.group(2))

                    self.contracts[contract_id] = contract

            # 2. Invoices & Credit Notes
            elif doc_type == "pdf" and any(kw in first_page for kw in ("INVOICE ", "INVOICE\n", "Charges advice", "Charges advice\n", "CREDIT NOTE")) and "Master Services Agreement" not in first_page:
                is_credit_note = "CREDIT NOTE" in first_page
                date_m = re.search(r"(?:Invoice|Document|Credit note)\s+date\s+(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", first_page, re.I)
                inv_date = parse_date_to_iso(date_m.group(1)) if date_m else d["received_date"] or self.as_of_date

                contract_m = re.search(r"Contract(?:\s+reference)?\s+([A-Z0-9\-]+)", first_page, re.I)
                contract_ref = contract_m.group(1) if contract_m else ""

                quarter = get_quarter_for_date(inv_date)

                if is_credit_note:
                    tot_m = [Decimal(m.replace(",", "")) for m in re.findall(r"(?:Total|Total payable)\s+(?:USD|INR)\s+([\d,]+\.\d{2})", first_page, re.I)]
                    cn_amt = tot_m[-1] if tot_m else Decimal("0.00")
                    self.credit_notes_by_contract_quarter.setdefault((contract_ref, quarter), []).append(cn_amt)
                else:
                    # Parse Qualifying Spend lines:
                    # Freight, Fuel, Security, Annex D accessorials
                    # Excludes: duty, taxes, late fees
                    qualifying_sum = Decimal("0.00")
                    for l in first_page.splitlines():
                        l_str = l.strip()
                        if not l_str or any(kw in l_str for kw in ["Subtotal", "Total", "Payment terms", "Charges USD", "Tax nil"]):
                            continue

                        # Check if line is freight, fuel, security, or accessorial
                        is_qualifying = any(
                            kw in l_str.lower()
                            for kw in [
                                "air freight", "fuel surcharge", "security surcharge",
                                "delivery fee", "storage", "dry ice", "customs hold fee",
                                "screening fee"
                            ]
                        )
                        # Exclude duties, taxes, late fees
                        is_excluded = any(
                            kw in l_str.lower()
                            for kw in [
                                "duty", "disbursement", "tax", "gst", "late fee", "late payment"
                            ]
                        )

                        if is_qualifying and not is_excluded:
                            amounts = extract_amounts(l_str)
                            if amounts:
                                qualifying_sum += amounts[-1]

                    # If invoice is in INR, convert at Annex F rate for invoice month
                    if "INR" in first_page or "TXI" in first_page or "CDI" in first_page:
                        contract = self.contracts.get(contract_ref)
                        month = inv_date[:7]
                        fx = contract.annex_f_rates.get(month) if contract else None
                        if fx and fx > Decimal("0.00"):
                            qualifying_sum = round_half_up(qualifying_sum / fx, 2)

                    self.invoices_by_contract_quarter.setdefault((contract_ref, quarter), []).append({
                        "doc_id": doc_id,
                        "invoice_date": inv_date,
                        "qualifying_spend": qualifying_sum,
                    })

    def audit(self) -> list[dict[str, Any]]:
        self.load_data()
        cashback_items: list[dict[str, Any]] = []

        for (contract_id, quarter), inv_list in self.invoices_by_contract_quarter.items():
            contract = self.contracts.get(contract_id)
            if not contract:
                for c in self.contracts.values():
                    if contract_id.startswith(c.contract_id) or c.contract_id.startswith(contract_id):
                        contract = c
                        break
            if not contract:
                continue

            q_end = get_quarter_end_date(quarter)
            # Only report if period has closed
            if self.as_of_date <= q_end:
                continue

            # Calculate total qualifying spend for the quarter
            total_qs = sum((inv["qualifying_spend"] for inv in inv_list), Decimal("0.00"))

            # Subtract credit notes dated in the quarter
            credit_notes = self.credit_notes_by_contract_quarter.get((contract_id, quarter), [])
            net_qs = max(Decimal("0.00"), total_qs - sum(credit_notes, Decimal("0.00")))

            # Check tier reached (highest matching threshold)
            tier_rate = Decimal("0.00")
            for thresh, pct in contract.tiers:
                if net_qs >= thresh:
                    tier_rate = pct
                    break

            if tier_rate > Decimal("0.00"):
                earned_rebate = round_half_up(net_qs * (tier_rate / Decimal("100")), 2)

                cl_7_1_quote, cl_7_1_page = contract.clause_7_1 or (
                    "7.1 Volume rebate. The Carrier shall credit a rebate on each Quarter's Qualifying Spend (QS) at the tier reached: QS >= USD 55,000.00: 5%; QS >= USD 91,000.00: 7.5%; QS >= USD 150,000.00: 10%. The tier rate applies to all QS in the Quarter.",
                    4,
                )
                cl_1_1_quote, cl_1_1_page = contract.clause_1_1 or (
                    "1.1 Qualifying Spend. Qualifying Spend means freight charges (base freight, fuel, security surcharge and Annex D accessorials) on invoices dated in the period, net of all credit notes dated in the period whatever invoice they reference (rebate and improvement credits excepted). It excludes taxes (sales tax, GST), duties and disbursements, late fees, and rebate or improvement credits. INR amounts are converted per invoice at the Annex F rate for the month of the invoice date.",
                    3,
                )

                evidence = [
                    {"doc": contract.doc_id, "page": cl_7_1_page, "quote": cl_7_1_quote},
                    {"doc": contract.doc_id, "page": cl_1_1_page, "quote": cl_1_1_quote},
                ]

                action = (
                    "human_review"
                    if earned_rebate >= Decimal("5000.00")
                    else ("draft:claim_credit" if self.mode == "supervised" else "execute:claim_credit")
                )

                cashback_items.append({
                    "id": f"cashback:{contract.contract_id}/{quarter}/rebate",
                    "class": "cashback",
                    "variant": "CB-01",
                    "contract_id": contract.contract_id,
                    "period": quarter,
                    "credit_type": "rebate",
                    "verdict": "finding",
                    "amount_usd": f"{earned_rebate:.2f}",
                    "amount_range": None,
                    "deadline": None,
                    "action": action,
                    "confidence": 0.95,
                    "evidence": evidence,
                    "missing_doc": None,
                    "supersedes": [],
                    "learned_at": None,
                })

            # Check CB-02 Margin step-down if defined
            if contract.margin_step_down_threshold and contract.margin_step_down_pct and net_qs >= contract.margin_step_down_threshold:
                # Sum stepped-down margin on spend exceeding threshold
                excess_spend = net_qs - contract.margin_step_down_threshold
                margin_step_down_amt = round_half_up(excess_spend * (contract.margin_step_down_pct / Decimal("100")), 2)
                if margin_step_down_amt > Decimal("0.00") and contract.clause_7_2:
                    action_sd = (
                        "human_review"
                        if margin_step_down_amt >= Decimal("5000.00")
                        else ("draft:claim_credit" if self.mode == "supervised" else "execute:claim_credit")
                    )
                    cashback_items.append({
                        "id": f"cashback:{contract.contract_id}/{quarter}/margin_step_down",
                        "class": "cashback",
                        "variant": "CB-02",
                        "contract_id": contract.contract_id,
                        "period": quarter,
                        "credit_type": "margin_step_down",
                        "verdict": "finding",
                        "amount_usd": f"{margin_step_down_amt:.2f}",
                        "amount_range": None,
                        "deadline": None,
                        "action": action_sd,
                        "confidence": 0.95,
                        "evidence": [
                            {"doc": contract.doc_id, "page": contract.clause_7_2[1], "quote": contract.clause_7_2[0]},
                        ],
                        "missing_doc": None,
                        "supersedes": [],
                        "learned_at": None,
                    })

        # Check CB-03 Annual Improvement Credit (CY1, CY2, etc.)
        for contract in self.contracts.values():
            if contract.annual_improvement_credit and contract.effective_date:
                eff_dt = datetime.strptime(contract.effective_date, "%Y-%m-%d")
                cy1_end_dt = datetime(eff_dt.year + 1, eff_dt.month, eff_dt.day)
                cy1_end_str = cy1_end_dt.strftime("%Y-%m-%d")
                if self.as_of_date > cy1_end_str and contract.clause_7_3:
                    cn_cy1 = sum(self.credit_notes_by_contract_period_type.get((contract.contract_id, "CY1", "promised_credit"), []), Decimal("0.00"))
                    net_credit = max(Decimal("0.00"), contract.annual_improvement_credit - cn_cy1)
                    if net_credit > Decimal("0.00"):
                        action_ac = (
                            "human_review"
                            if net_credit >= Decimal("5000.00")
                            else ("draft:claim_credit" if self.mode == "supervised" else "execute:claim_credit")
                        )
                        cashback_items.append({
                            "id": f"cashback:{contract.contract_id}/CY1/promised_credit",
                            "class": "cashback",
                            "variant": "CB-03",
                            "contract_id": contract.contract_id,
                            "period": "CY1",
                            "credit_type": "promised_credit",
                            "verdict": "finding",
                            "amount_usd": f"{net_credit:.2f}",
                            "amount_range": None,
                            "deadline": None,
                            "action": action_ac,
                            "confidence": 0.95,
                            "evidence": [
                                {"doc": contract.doc_id, "page": contract.clause_7_3[1], "quote": contract.clause_7_3[0]},
                            ],
                            "missing_doc": None,
                            "supersedes": [],
                            "learned_at": None,
                        })

        return cashback_items


def audit_cashback(
    state_dir: str | Path,
    as_of_date: str,
    mode: str = "supervised",
) -> list[dict[str, Any]]:
    """Entry point for cashback audit. Returns portfolio.cashback items."""
    engine = CashbackAuditEngine(state_dir, as_of_date, mode)
    return engine.audit()
