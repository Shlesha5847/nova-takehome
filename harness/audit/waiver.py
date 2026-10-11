"""Waived-but-billed audit engine for the Nova freight audit harness.

Detects charges waived in writing by authorized Schedule 1 personnel that were
billed on subsequent invoices (WB-01).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from harness import db
from harness.audit.common import extract_amounts, parse_date_to_iso, round_half_up


@dataclass
class WaiverEmail:
    doc_id: str
    page: int
    sender_name: str
    sender_email: str
    sent_date: str
    awb: str | None
    charge_type: str | None
    quote: str


@dataclass
class Schedule1WaiverContact:
    name: str
    role: str
    party: str
    email: str
    quote: str
    page: int
    categories: list[str] = field(default_factory=list)


@dataclass
class WaiverContract:
    doc_id: str
    contract_id: str
    waiver_contacts: list[Schedule1WaiverContact] = field(default_factory=list)
    all_contacts: list[Schedule1WaiverContact] = field(default_factory=list)
    clause_11_1: tuple[str, int] | None = None


class WaiverAuditEngine:
    """Deterministic audit engine for waived-but-billed charges."""

    def __init__(self, state_dir: str | Path, as_of_date: str, mode: str = "supervised"):
        self.state_dir = Path(state_dir)
        self.as_of_date = as_of_date
        self.mode = mode

        self.waivers: list[WaiverEmail] = []
        self.forwarded_waivers: list[WaiverEmail] = []
        self.contracts: dict[str, WaiverContract] = {}
        self.invoices: list[dict[str, Any]] = []

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

            # 1. Contract / Schedule 1 / Clause 11.1
            if doc_type == "pdf" and "Master Services Agreement" in first_page:
                c_m = re.search(r"Agreement No\.\s+([A-Z0-9\-]+)", first_page) or re.search(r"TBX-[A-Z0-9\-]+", first_page)
                if c_m:
                    contract_id = c_m.group(0).split()[-1]
                    contract = WaiverContract(doc_id=doc_id, contract_id=contract_id)

                    for p_idx, p_text in enumerate(pages, start=1):
                        if "11.1 Waiver authority." in p_text:
                            for l in p_text.splitlines():
                                if "11.1 Waiver authority." in l:
                                    contract.clause_11_1 = (l.strip(), p_idx)
                                    break
                        if "Schedule 1: Authorised Persons" in p_text or "Schedule 1 Authorised Persons" in p_text:
                            for l in p_text.splitlines():
                                s1_m = re.search(r"^([A-Za-z\s\.\-]+),\s+([^,]+),\s+([A-Z0-9\-]+):\s+([a-z_,\s]+)\s+<([^>]+)>", l.strip())
                                if s1_m:
                                    name, role, party, cats_str, email = s1_m.groups()
                                    cats = [c.strip() for c in cats_str.split(",")]
                                    c_obj = Schedule1WaiverContact(
                                        name=name.strip(),
                                        role=role.strip(),
                                        party=party.strip(),
                                        email=email.strip(),
                                        quote=l.strip(),
                                        page=p_idx,
                                        categories=cats,
                                    )
                                    contract.all_contacts.append(c_obj)
                                    if "waivers" in cats:
                                        contract.waiver_contacts.append(c_obj)
                    self.contracts[contract_id] = contract

            # 2. Waiver emails
            elif doc_type == "eml" and "waive" in first_page.lower():
                sender_m = re.search(r"From:\s*([^<\n]+)<([^>\n]+)>", first_page)
                sender_name = sender_m.group(1).strip() if sender_m else ""
                sender_email = sender_m.group(2).strip() if sender_m else ""
                date_m = re.search(r"Date:\s*(\d{4}-\d{2}-\d{2})", first_page)
                sent_date = date_m.group(1) if date_m else d["received_date"] or self.as_of_date

                is_forward = any(marker in first_page for marker in [
                    "-----Original Message-----",
                    "---------- Forwarded message ---------",
                    "Begin forwarded message",
                    "> From:",
                    "Fwd:",
                    "FW:"
                ])

                for line in first_page.splitlines():
                    if "waive" in line.lower():
                        awbs = re.findall(r"\b([A-Z]{2}\d{8})\b", line)
                        charge_type = None
                        if "delivery fee" in line.lower() or "delivery" in line.lower():
                            charge_type = "delivery fee"
                        elif "storage" in line.lower():
                            charge_type = "storage"
                        elif "security" in line.lower():
                            charge_type = "security"

                        target_list = self.forwarded_waivers if is_forward else self.waivers
                        if awbs:
                            for awb_val in awbs:
                                target_list.append(
                                    WaiverEmail(
                                        doc_id=doc_id,
                                        page=1,
                                        sender_name=sender_name,
                                        sender_email=sender_email,
                                        sent_date=sent_date,
                                        awb=awb_val,
                                        charge_type=charge_type,
                                        quote=line.strip(),
                                    )
                                )
                        else:
                            target_list.append(
                                WaiverEmail(
                                    doc_id=doc_id,
                                    page=1,
                                    sender_name=sender_name,
                                    sender_email=sender_email,
                                    sent_date=sent_date,
                                    awb=None,
                                    charge_type=charge_type,
                                    quote=line.strip(),
                                )
                            )

            # 3. Invoices
            elif doc_type == "pdf" and any(kw in first_page for kw in ("INVOICE ", "INVOICE\n", "Charges advice", "Charges advice\n")) and not any(kw in first_page for kw in ("Master Services Agreement", "CREDIT NOTE")):
                inv_id_m = re.search(r"(?:INVOICE|Charges\s*advice)\s*([A-Z0-9/\-]+)", first_page, re.I)
                if not inv_id_m:
                    continue
                inv_id = inv_id_m.group(1).strip().upper().replace(" ", "").replace("#", "").replace(":", "")
                if inv_id.startswith("CD1-"):
                    inv_id = "CDI-" + inv_id[4:]

                date_m = re.search(r"(?:Invoice|Document)\s+date\s+(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", first_page, re.I)
                inv_date = parse_date_to_iso(date_m.group(1)) if date_m else d["received_date"] or self.as_of_date

                contract_m = re.search(r"Contract(?:\s+reference)?\s+([A-Z0-9\-]+)", first_page, re.I)
                contract_ref = contract_m.group(1) if contract_m else ""

                lines: list[dict[str, Any]] = []
                for line in first_page.splitlines():
                    awb_m = re.search(r"\b([A-Z]{2}\d{8})\b", line)
                    if awb_m:
                        amounts = extract_amounts(line)
                        line_amt = amounts[-1] if amounts else Decimal("0.00")
                        lines.append({
                            "awb": awb_m.group(1),
                            "line": line.strip(),
                            "amount": line_amt,
                        })

                self.invoices.append({
                    "doc_id": doc_id,
                    "invoice_id": inv_id,
                    "invoice_date": inv_date,
                    "contract_ref": contract_ref,
                    "lines": lines,
                })

    def audit(self) -> list[dict[str, Any]]:
        self.load_data()
        findings: list[dict[str, Any]] = []

        for inv in self.invoices:
            contract = self.contracts.get(inv["contract_ref"])
            if not contract:
                for c in self.contracts.values():
                    if inv["contract_ref"].startswith(c.contract_id) or c.contract_id.startswith(inv["contract_ref"]):
                        contract = c
                        break
            if not contract:
                continue

            for w in self.waivers:
                if not w.awb:
                    continue

                # Check waiver authority in Schedule 1
                auth_contact: Schedule1WaiverContact | None = None
                for contact in contract.waiver_contacts:
                    if contact.email.lower() == w.sender_email.lower() or contact.name.lower() in w.sender_name.lower():
                        auth_contact = contact
                        break

                if not auth_contact:
                    # Unauthorized waiver sender -> invalid
                    continue

                # Check if invoice is dated after the waiver
                if inv["invoice_date"] < w.sent_date:
                    continue

                # Check if invoice bills the waived charge on this AWB
                for item in inv["lines"]:
                    if item["awb"] == w.awb:
                        line_text = item["line"].lower()
                        is_match = False
                        if w.charge_type and w.charge_type in line_text:
                            is_match = True
                        elif "delivery fee" in line_text or "delivery" in line_text:
                            is_match = True

                        if is_match:
                            cl_quote, cl_page = contract.clause_11_1 or (
                                "11.1 Waiver authority. A charge may be waived only in writing by a person listed in Schedule 1 for waivers. Schedule 1 is exhaustive.",
                                4,
                            )
                            evidence = [
                                {"doc": w.doc_id, "page": w.page, "quote": w.quote},
                                {"doc": contract.doc_id, "page": auth_contact.page, "quote": auth_contact.quote},
                                {"doc": inv["doc_id"], "page": 1, "quote": item["line"]},
                                {"doc": contract.doc_id, "page": cl_page, "quote": cl_quote},
                            ]

                            action = "draft:dispute" if self.mode == "supervised" else "execute:dispute"
                            findings.append({
                                "id": f"waived_but_billed:{inv['invoice_id']}",
                                "class": "waived_but_billed",
                                "variant": "WB-01",
                                "invoice_id": inv["invoice_id"],
                                "verdict": "finding",
                                "amount_usd": f"{item['amount']:.2f}",
                                "amount_range": None,
                                "deadline": None,
                                "action": action,
                                "confidence": 0.95,
                                "evidence": evidence,
                                "missing_doc": None,
                                "supersedes": [],
                                "learned_at": None,
                            })

        # Check forwarded waivers (cannot_determine asking Key Account Manager for original)
        for inv in self.invoices:
            contract = self.contracts.get(inv["contract_ref"])
            if not contract:
                for c in self.contracts.values():
                    if inv["contract_ref"].startswith(c.contract_id) or c.contract_id.startswith(inv["contract_ref"]):
                        contract = c
                        break
            if not contract:
                continue

            for fw in self.forwarded_waivers:
                if not fw.awb:
                    continue
                if any(f["invoice_id"] == inv["invoice_id"] for f in findings):
                    continue

                for item in inv["lines"]:
                    if item["awb"] == fw.awb:
                        kam_contact = None
                        for c in contract.all_contacts:
                            if any(k in c.role.lower() for k in ["key account", "account manager", "kam"]):
                                kam_contact = c
                                break
                        if not kam_contact and contract.all_contacts:
                            kam_contact = contract.all_contacts[0]

                        cl_quote, cl_page = contract.clause_11_1 or (
                            "11.1 Waiver authority. A charge may be waived only in writing by a person listed in Schedule 1 for waivers. Schedule 1 is exhaustive.",
                            4,
                        )
                        evidence = [
                            {"doc": fw.doc_id, "page": fw.page, "quote": fw.quote},
                            {"doc": contract.doc_id, "page": cl_page, "quote": cl_quote},
                        ]
                        action = "draft:request_document" if self.mode == "supervised" else "execute:request_document"
                        findings.append({
                            "id": f"waived_but_billed:{inv['invoice_id']}",
                            "class": "waived_but_billed",
                            "variant": "WB-01",
                            "invoice_id": inv["invoice_id"],
                            "verdict": "cannot_determine",
                            "amount_usd": None,
                            "amount_range": None,
                            "deadline": None,
                            "action": action,
                            "confidence": 0.90,
                            "evidence": evidence,
                            "missing_doc": {
                                "name": f"original waiver email for {fw.awb}",
                                "holder": {
                                    "party": kam_contact.party if kam_contact else "Carrier",
                                    "person": kam_contact.name if kam_contact else "Key Account Manager",
                                    "role": kam_contact.role if kam_contact else "Key Account Manager",
                                }
                            },
                            "supersedes": [],
                            "learned_at": None,
                        })

        return findings


def audit_waivers(
    state_dir: str | Path,
    as_of_date: str,
    mode: str = "supervised",
) -> list[dict[str, Any]]:
    """Entry point for waived-but-billed audit."""
    engine = WaiverAuditEngine(state_dir, as_of_date, mode)
    return engine.audit()
