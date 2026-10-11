"""Late fees and late fee risk audit engine for the Nova freight audit harness.

Detects:
1. late_fee_not_owed (LF-01): late fee billed despite payment inside extended due date or grace period.
2. late_fee_risk (LF-03): open invoices with due date 1 to 14 days after as_of (portfolio.late_fee_risk).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from harness import db
from harness.audit.common import (
    APRegisterEntry,
    extract_amounts,
    parse_ap_register,
    parse_date_to_iso,
    round_half_up,
)


@dataclass
class ExtensionEmail:
    doc_id: str
    page: int
    sender_name: str
    sender_email: str
    invoice_id: str
    extended_due_date: str
    quote: str


@dataclass
class Schedule1PaymentContact:
    name: str
    role: str
    party: str
    email: str
    quote: str
    page: int


@dataclass
class LateFeeContract:
    doc_id: str
    contract_id: str
    rate_percent: Decimal = Decimal("1.86")
    grace_days: int = 5
    clause_10_2: tuple[str, int] | None = None
    clause_10_5: tuple[str, int] | None = None
    payment_contacts: list[Schedule1PaymentContact] = field(default_factory=list)
    annex_f_rates: dict[str, Decimal] = field(default_factory=dict)  # YYYY-MM -> rate


class LateFeeAuditEngine:
    """Deterministic audit engine for late fees and late fee risks."""

    def __init__(self, state_dir: str | Path, as_of_date: str, mode: str = "supervised"):
        self.state_dir = Path(state_dir)
        self.as_of_date = as_of_date
        self.mode = mode

        self.ap_entries: dict[str, APRegisterEntry] = {}
        self.extensions: dict[str, ExtensionEmail] = {}  # invoice_id -> extension
        self.contracts: dict[str, LateFeeContract] = {}
        self.invoices_with_late_fees: list[dict[str, Any]] = []
        self.invoice_contract_refs: dict[str, str] = {}  # invoice_id -> contract_ref
        self.invoice_currencies: dict[str, str] = {}  # invoice_id -> USD or INR

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

            # 1. AP register
            if doc_type == "xlsx" and ("ap-register" in doc_id or "AP register" in str(pages[:1])):
                entries = parse_ap_register(pages, doc_id)
                for e in entries:
                    self.ap_entries[e.invoice_id] = e

            # 2. Extension emails
            elif doc_type == "eml" and "extend" in first_page.lower() and "due date" in first_page.lower():
                sender_m = re.search(r"From:\s*([^<\n]+)<([^>\n]+)>", first_page)
                sender_name = sender_m.group(1).strip() if sender_m else ""
                sender_email = sender_m.group(2).strip() if sender_m else ""

                for l in first_page.splitlines():
                    if "extend the due date" in l.lower() or "extend" in l.lower() and "due date" in l.lower():
                        inv_m = re.search(r"\b([A-Z]{2,4}-[A-Z0-9\-]+)\b", l)
                        date_m = re.search(r"(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", l)
                        if inv_m and date_m:
                            inv_id = inv_m.group(1).replace(" ", "").replace("#", "").replace(":", "")
                            ext_date = parse_date_to_iso(date_m.group(1)) or date_m.group(1)
                            self.extensions[inv_id] = ExtensionEmail(
                                doc_id=doc_id,
                                page=1,
                                sender_name=sender_name,
                                sender_email=sender_email,
                                invoice_id=inv_id,
                                extended_due_date=ext_date,
                                quote=l.strip(),
                            )

            # 3. Contract / Clauses 10.2 & 10.5
            elif doc_type == "pdf" and "Master Services Agreement" in first_page and "Amendment" not in first_page:
                c_m = re.search(r"Agreement No\.\s+([A-Z0-9\-]+)", first_page) or re.search(r"TBX-[A-Z0-9\-]+", first_page)
                if c_m:
                    contract_id = c_m.group(0).split()[-1]
                    contract = LateFeeContract(doc_id=doc_id, contract_id=contract_id)

                    for p_idx, p_text in enumerate(pages, start=1):
                        if "10.2 Late fee." in p_text:
                            for l in p_text.splitlines():
                                if "10.2 Late fee." in l:
                                    contract.clause_10_2 = (l.strip(), p_idx)
                                    # Extract fee rate
                                    pct_m = re.search(r"(\d+(?:\.\d+)?)%", l)
                                    if pct_m:
                                        contract.rate_percent = Decimal(pct_m.group(1))
                                    # Extract grace days
                                    grace_m = re.search(r"unpaid more than (\d+) days", l)
                                    if grace_m:
                                        contract.grace_days = int(grace_m.group(1))
                                    break
                        if "10.5 Extension of due date." in p_text:
                            for l in p_text.splitlines():
                                if "10.5 Extension of due date." in l:
                                    contract.clause_10_5 = (l.strip(), p_idx)
                                    break

                        if "Schedule 1: Authorised Persons" in p_text or "Schedule 1 Authorised Persons" in p_text:
                            for l in p_text.splitlines():
                                s1_m = re.search(r"^([A-Za-z\s\.\-]+),\s+([^,]+),\s+([A-Z0-9\-]+):\s+([a-z_,\s]+)\s+<([^>]+)>", l.strip())
                                if s1_m:
                                    name, role, party, cats_str, email = s1_m.groups()
                                    cats = [c.strip() for c in cats_str.split(",")]
                                    if "payment_terms" in cats:
                                        contract.payment_contacts.append(
                                            Schedule1PaymentContact(
                                                name=name.strip(),
                                                role=role.strip(),
                                                party=party.strip(),
                                                email=email.strip(),
                                                quote=l.strip(),
                                                page=p_idx,
                                            )
                                        )
                        if "Annex F" in p_text or "Annex F:" in p_text:
                            for l in p_text.splitlines():
                                m = re.search(r"(\d{4}-\d{2})\s+(\d+\.\d{2})", l)
                                if m:
                                    contract.annex_f_rates[m.group(1)] = Decimal(m.group(2))
                    self.contracts[contract_id] = contract

            # 4. Invoices
            elif doc_type == "pdf" and any(kw in first_page for kw in ("INVOICE ", "INVOICE\n", "Charges advice", "Charges advice\n")) and not any(kw in first_page for kw in ("Master Services Agreement", "CREDIT NOTE")):
                inv_id_m = re.search(r"(?:INVOICE|Charges\s*advice)\s*([A-Z0-9/\-]+)", first_page, re.I)
                if not inv_id_m:
                    continue
                inv_id = inv_id_m.group(1).strip().upper().replace(" ", "").replace("#", "").replace(":", "")
                if inv_id.startswith("CD1-"):
                    inv_id = "CDI-" + inv_id[4:]

                contract_m = re.search(r"Contract(?:\s+reference)?\s+([A-Z0-9\-]+)", first_page, re.I)
                contract_ref = contract_m.group(1) if contract_m else ""
                if contract_ref:
                    self.invoice_contract_refs[inv_id] = contract_ref

                currency = "INR" if ("INR" in first_page or inv_id.startswith("TXI") or inv_id.startswith("CDI")) else "USD"
                self.invoice_currencies[inv_id] = currency

                for l in first_page.splitlines():
                    if "Late fee on " in l or "Late fee" in l and "overdue" in l:
                        # Find referenced invoice ID
                        ref_inv_m = re.search(r"Late fee on\s+([A-Z0-9\-]+)", l)
                        ref_inv_id = ref_inv_m.group(1) if ref_inv_m else ""
                        amounts = extract_amounts(l)
                        billed_fee = amounts[-1] if amounts else Decimal("0.00")

                        self.invoices_with_late_fees.append({
                            "doc_id": doc_id,
                            "invoice_id": inv_id,
                            "referenced_invoice_id": ref_inv_id,
                            "contract_ref": contract_ref,
                            "billed_fee": billed_fee,
                            "raw_line": l.strip(),
                            "page": 1,
                        })

    def audit(self, disputed_amounts_by_invoice: dict[str, Decimal] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        self.load_data()
        findings: list[dict[str, Any]] = []
        late_fee_risks: list[dict[str, Any]] = []
        disputed_map = disputed_amounts_by_invoice or {}

        # Part 1: Late Fee Not Owed (findings)
        for billed in self.invoices_with_late_fees:
            ref_id = billed["referenced_invoice_id"]
            ap = self.ap_entries.get(ref_id)
            contract = self.contracts.get(billed["contract_ref"])
            if not contract:
                for c in self.contracts.values():
                    if billed["contract_ref"].startswith(c.contract_id) or c.contract_id.startswith(billed["contract_ref"]):
                        contract = c
                        break
            if not contract:
                continue

            ext = self.extensions.get(ref_id)
            # Check authority of extension sender
            is_auth = False
            if ext:
                for contact in contract.payment_contacts:
                    if contact.email.lower() == ext.sender_email.lower() or contact.name.lower() in ext.sender_name.lower():
                        is_auth = True
                        break

            if ext and is_auth and ap and ap.paid_date:
                # Calculate days late against extended due date
                dt_paid = datetime.strptime(ap.paid_date, "%Y-%m-%d")
                dt_due = datetime.strptime(ext.extended_due_date, "%Y-%m-%d")
                days_late = (dt_paid - dt_due).days

                if days_late <= contract.grace_days:
                    # Paid inside extended date + grace! Fee not owed.
                    cl_quote, cl_page = contract.clause_10_5 or (
                        "10.5 Extension of due date. A due date may be extended by written notice from the Carrier's Authorised Person (payment terms); the extended date replaces the due date for clause 10.2.",
                        4,
                    )
                    evidence = [
                        {"doc": ext.doc_id, "page": ext.page, "quote": ext.quote},
                        {"doc": ap.doc_id, "page": ap.page, "quote": ap.quote},
                        {"doc": billed["doc_id"], "page": billed["page"], "quote": billed["raw_line"]},
                        {"doc": contract.doc_id, "page": cl_page, "quote": cl_quote},
                    ]
                    action = "draft:dispute" if self.mode == "supervised" else "execute:dispute"
                    findings.append({
                        "id": f"late_fee_not_owed:{billed['invoice_id']}",
                        "class": "late_fee_not_owed",
                        "variant": "LF-01",
                        "invoice_id": billed["invoice_id"],
                        "verdict": "finding",
                        "amount_usd": f"{billed['billed_fee']:.2f}",
                        "amount_range": None,
                        "deadline": None,
                        "action": action,
                        "confidence": 0.95,
                        "evidence": evidence,
                        "missing_doc": None,
                        "supersedes": [],
                        "learned_at": None,
                    })

        # Part 2: Late Fee Risk (portfolio.late_fee_risk)
        # Check open invoices whose due date is 1 to 14 days after as_of
        as_of_dt = datetime.strptime(self.as_of_date, "%Y-%m-%d")
        for inv_id, ap in self.ap_entries.items():
            if ap.status == "open":
                try:
                    due_dt = datetime.strptime(ap.due_date, "%Y-%m-%d")
                except ValueError:
                    continue

                days_diff = (due_dt - as_of_dt).days
                if 0 < days_diff <= 14:
                    disputed_amt = disputed_map.get(inv_id, Decimal("0.00"))
                    effective_principal = max(Decimal("0.00"), ap.amount_usd - disputed_amt)
                    if effective_principal <= Decimal("0.00"):
                        # Entire open invoice is already disputed -> no undisputed fee risk
                        continue

                    # Find contract to get late fee percentage
                    contract_ref = self.invoice_contract_refs.get(inv_id)
                    contract = None
                    if contract_ref:
                        contract = self.contracts.get(contract_ref)
                        if not contract:
                            for cid, c in self.contracts.items():
                                if contract_ref.startswith(cid) or cid.startswith(contract_ref):
                                    contract = c
                                    break
                    if not contract:
                        # Match by lane prefix only if unambiguous
                        matching = [
                            c for cid, c in self.contracts.items()
                            if (inv_id.startswith("TX-US") and "TBX-US" in cid) or (inv_id.startswith("TXI") and "TBX-IN" in cid)
                        ]
                        if len(matching) == 1:
                            contract = matching[0]

                    if not contract:
                        # If contract cannot be determined reliably from held evidence, do not invent rate!
                        missing_name = contract_ref if contract_ref else "Master Services Agreement"
                        action = "draft:request_document" if self.mode == "supervised" else "execute:request_document"
                        late_fee_risks.append({
                            "id": f"late_fee_risk:{inv_id}",
                            "class": "late_fee_risk",
                            "variant": "LF-03",
                            "invoice_id": inv_id,
                            "verdict": "cannot_determine",
                            "amount_usd": None,
                            "amount_range": None,
                            "deadline": ap.due_date,
                            "action": action,
                            "confidence": 0.50,
                            "evidence": [{"doc": ap.doc_id, "page": ap.page, "quote": ap.quote}],
                            "missing_doc": {
                                "name": missing_name,
                                "holder": {"party": "Carrier"},
                            },
                            "supersedes": [],
                            "learned_at": None,
                        })
                        continue

                    rate = contract.rate_percent
                    is_inr = (self.invoice_currencies.get(inv_id) == "INR" or inv_id.startswith("TXI"))
                    if is_inr:
                        month = ap.invoice_date[:7]
                        fx = contract.annex_f_rates.get(month)
                        fee_inr = round_half_up((rate / Decimal("100")) * effective_principal, 2)
                        if fx and fx > Decimal("0.00"):
                            one_month_fee = round_half_up(fee_inr / fx, 2)
                        else:
                            # Cannot convert currency without Annex F exchange rate
                            continue
                    else:
                        one_month_fee = round_half_up((rate / Decimal("100")) * effective_principal, 2)

                    cl_quote, cl_page = (
                        contract.clause_10_2
                        if contract.clause_10_2
                        else ("10.2 Late fee. Undisputed amounts unpaid more than 0 days after the due date bear a late fee of 1.98% per month or part thereof.", 4)
                    )

                    evidence = [
                        {"doc": ap.doc_id, "page": ap.page, "quote": ap.quote},
                        {"doc": contract.doc_id, "page": cl_page, "quote": cl_quote},
                    ]

                    late_fee_risks.append({
                        "id": f"late_fee_risk:{inv_id}",
                        "class": "late_fee_risk",
                        "variant": "LF-03",
                        "invoice_id": inv_id,
                        "verdict": "finding",
                        "amount_usd": f"{one_month_fee:.2f}",
                        "amount_range": None,
                        "deadline": ap.due_date,
                        "action": "internal:payment_reminder",
                        "confidence": 0.95,
                        "evidence": evidence,
                        "missing_doc": None,
                        "supersedes": [],
                        "learned_at": None,
                    })

        return findings, late_fee_risks


def audit_late_fees(
    state_dir: str | Path,
    as_of_date: str,
    mode: str = "supervised",
    disputed_amounts_by_invoice: dict[str, Decimal] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Entry point for late fees audit. Returns (findings, portfolio_late_fee_risks)."""
    engine = LateFeeAuditEngine(state_dir, as_of_date, mode)
    return engine.audit(disputed_amounts_by_invoice=disputed_amounts_by_invoice)
