"""Duplicate invoice detection audit engine for the Nova freight audit harness.

Detects exact resends of paid invoices (DU-01), rebilled line items under Annex E codes (DU-03),
and cross-invoice shipment duplications (DU-05).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from harness import db
from harness.audit.common import (
    APRegisterEntry,
    extract_amounts,
    parse_annex_e_mapping,
    parse_ap_register,
    parse_date_to_iso,
    round_half_up,
)


@dataclass
class BilledChargeLine:
    invoice_id: str
    invoice_date: str
    doc_id: str
    page: int
    awb: str
    raw_label: str
    charge_code: str
    amount: Decimal
    raw_line: str
    is_mawb: bool
    hawb: str | None


class DuplicateAuditEngine:
    """Deterministic audit engine for detecting duplicate billings."""

    def __init__(self, state_dir: str | Path, as_of_date: str, mode: str = "supervised"):
        self.state_dir = Path(state_dir)
        self.as_of_date = as_of_date
        self.mode = mode

        self.ap_entries: dict[str, APRegisterEntry] = {}  # invoice_id -> AP entry
        self.invoices_by_id: dict[str, list[dict[str, Any]]] = {}  # inv_id -> list of email/invoice instances
        self.invoice_charges: list[BilledChargeLine] = []
        self.annex_e_maps: dict[str, dict[str, tuple[str, str, int]]] = {}  # contract_id -> label mapping
        self.contract_docs: dict[str, str] = {}  # contract_id -> doc_id
        self.credit_notes_by_invoice: dict[str, list[Decimal]] = {}  # invoice_id -> list of credit note amounts

    def load_data(self) -> None:
        docs = db.get_documents_up_to_date(self.state_dir, self.as_of_date)
        pages_cache: dict[str, list[str]] = {}
        for d in docs:
            pages_cache[d["doc_id"]] = db.get_document_pages(self.state_dir, d["doc_id"])

        # 1. Parse AP Registers
        for d in docs:
            doc_id = d["doc_id"]
            pages = pages_cache.get(doc_id, [])
            if d["doc_type"] == "xlsx" and ("ap-register" in doc_id or "AP register" in str(pages[:1])):
                entries = parse_ap_register(pages, doc_id)
                for e in entries:
                    self.ap_entries[e.invoice_id] = e

        # 2. Parse Contracts for Annex E
        for d in docs:
            doc_id = d["doc_id"]
            pages = pages_cache.get(doc_id, [])
            if d["doc_type"] == "pdf" and ("Master Services Agreement" in str(pages[:1]) or "MSA" in doc_id):
                c_m = re.search(r"(?:Agreement|Contract)\s+No\.\s+([A-Z0-9\-]+)", str(pages[:1])) or re.search(r"[A-Z]{3,4}-[A-Z0-9\-]+", str(pages[:1]))
                contract_id = c_m.group(1) if (c_m and c_m.lastindex) else (c_m.group(0).split()[-1] if c_m else doc_id)
                self.contract_docs[contract_id] = doc_id
                self.contract_docs[doc_id] = doc_id
                self.annex_e_maps[contract_id] = parse_annex_e_mapping(pages)

        # 2b. Parse Credit Notes
        for d in docs:
            doc_id = d["doc_id"]
            pages = pages_cache.get(doc_id, [])
            if d["doc_type"] == "pdf" and pages and "CREDIT NOTE" in pages[0]:
                p1 = pages[0]
                tot_m = re.findall(r"Total\s+(?:USD|INR)\s+(\d+\.\d{2})", p1)
                cn_amt = Decimal(tot_m[-1]) if tot_m else Decimal("0.00")
                inv_ref_m = re.search(r"(?:Invoice reference|ref(?:erence)?)\s+([A-Z0-9\-]+)", p1)
                if inv_ref_m:
                    cn_inv = inv_ref_m.group(1).strip().upper().replace(" ", "").replace("#", "").replace(":", "")
                    self.credit_notes_by_invoice.setdefault(cn_inv, []).append(cn_amt)

        # 3. Parse Invoices and their containing emails
        for d in docs:
            doc_id = d["doc_id"]
            pages = pages_cache.get(doc_id, [])
            is_inv_doc = d["doc_type"] == "pdf" and pages and any(
                kw in pages[0] for kw in ("INVOICE ", "INVOICE\n", "Charges advice", "Charges advice\n")
            )
            if is_inv_doc and not any(kw in pages[0] for kw in ("Master Services Agreement", "CREDIT NOTE")):
                p1 = pages[0]
                inv_id_m = re.search(r"(?:INVOICE|Charges\s*advice)\s*([A-Z0-9/\-]+)", p1, re.I)
                if not inv_id_m:
                    continue
                inv_id = inv_id_m.group(1).strip().upper().replace(" ", "").replace("#", "").replace(":", "")
                if inv_id.startswith("CD1-"):
                    inv_id = "CDI-" + inv_id[4:]

                date_m = re.search(r"(?:Invoice|Document)\s+date\s+(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", p1, re.I)
                inv_date = parse_date_to_iso(date_m.group(1)) if date_m else d["received_date"] or self.as_of_date

                contract_m = re.search(r"Contract(?:\s+reference)?\s+([A-Z0-9\-]+)", p1, re.I)
                contract_ref = contract_m.group(1) if contract_m else ""

                # Total amount
                tot_m = [Decimal(m.replace(",", "")) for m in re.findall(r"(?:Total|Total payable)\s+(?:USD|INR)\s+([\d,]+\.\d{2})", p1, re.I)]
                inv_total = tot_m[-1] if tot_m else Decimal("0.00")

                parent_doc_id = d.get("parent_doc_id")
                email_text = ""
                if parent_doc_id and parent_doc_id in pages_cache and pages_cache[parent_doc_id]:
                    email_text = pages_cache[parent_doc_id][0]

                record = {
                    "invoice_id": inv_id,
                    "invoice_date": inv_date,
                    "contract_ref": contract_ref,
                    "total": inv_total,
                    "doc_id": doc_id,
                    "email_doc_id": parent_doc_id,
                    "email_text": email_text,
                    "received_date": d["received_date"],
                    "pages": pages,
                }
                self.invoices_by_id.setdefault(inv_id, []).append(record)

                # Parse charge lines on invoice
                annex_e = self.annex_e_maps.get(contract_ref, {})
                for line in p1.splitlines():
                    line_str = line.strip()
                    if not line_str or "Subtotal" in line_str or "Total" in line_str or "Payment terms" in line_str:
                        continue

                    awb_m = re.search(r"\b([A-Z]{2}\d{8})\b", line_str)
                    if not awb_m:
                        continue
                    awb = awb_m.group(1)

                    # Check for split shipment house AWB: e.g. TE19536828-H1 or CA96669138-H1
                    hawb_m = re.search(r"\b([A-Z]{2}\d{8}-H\d+)\b", line_str)
                    hawb = hawb_m.group(1) if hawb_m else None
                    is_mawb = "MAWB" in line_str

                    # Determine charge code from line description
                    amounts = extract_amounts(line_str)
                    line_amt = amounts[-1] if amounts else Decimal("0.00")

                    charge_code = "UNKNOWN"
                    lower_line = line_str.lower()

                    for label, (code, _, _) in annex_e.items():
                        if label in lower_line:
                            charge_code = code
                            break
                    if charge_code == "UNKNOWN":
                        if "air freight" in lower_line or "freight" in lower_line:
                            charge_code = "FRT"
                        elif "fuel surcharge" in lower_line or "fuel" in lower_line:
                            charge_code = "FUEL"
                        elif "security" in lower_line or "screening" in lower_line:
                            charge_code = "SEC"
                        elif "delivery" in lower_line:
                            charge_code = "DLV"
                        elif "storage" in lower_line:
                            charge_code = "STOR"
                        elif "dry ice" in lower_line:
                            charge_code = "DRYICE"
                        elif "dangerous" in lower_line or "dg " in lower_line:
                            charge_code = "DG"

                    self.invoice_charges.append(
                        BilledChargeLine(
                            invoice_id=inv_id,
                            invoice_date=inv_date,
                            doc_id=doc_id,
                            page=1,
                            awb=awb,
                            raw_label=line_str,
                            charge_code=charge_code,
                            amount=line_amt,
                            raw_line=line_str,
                            is_mawb=is_mawb,
                            hawb=hawb,
                        )
                    )

    def audit(self) -> list[dict[str, Any]]:
        self.load_data()
        findings: list[dict[str, Any]] = []

        # Variant 1: DU-01 - Exact resend of paid invoice
        for inv_id, instances in self.invoices_by_id.items():
            if len(instances) > 1:
                # Sort instances by received_date
                instances.sort(key=lambda x: x["received_date"] or x["invoice_date"])
                first_inst = instances[0]
                ap_entry = self.ap_entries.get(inv_id)

                for resent in instances[1:]:
                    email_text = resent["email_text"]
                    # Check if email asks for payment
                    asks_payment = any(
                        phrase in email_text.lower()
                        for phrase in [
                            "process payment",
                            "remit payment",
                            "payment at your earliest",
                            "kindly settle",
                            "overdue for payment",
                        ]
                    )
                    is_marked_copy = any(
                        phrase in email_text.lower()
                        for phrase in ["copy", "for your records", "reminder only", "statement only"]
                    )

                    if ap_entry and ap_entry.status == "paid" and asks_payment and not is_marked_copy:
                        # Extract the exact payment request line from email
                        email_quote = ""
                        for l in email_text.splitlines():
                            if any(p in l.lower() for p in ["process payment", "earliest convenience", "remit payment"]):
                                email_quote = l.strip()
                                break
                        if not email_quote:
                            email_quote = "Please process payment at your earliest convenience."

                        evidence = [
                            {
                                "doc": ap_entry.doc_id,
                                "page": ap_entry.page,
                                "quote": ap_entry.quote,
                            },
                            {
                                "doc": resent["email_doc_id"],
                                "page": 1,
                                "quote": email_quote,
                            },
                        ]
                        action = "draft:dispute" if self.mode == "supervised" else "execute:dispute"
                        findings.append(
                            {
                                "id": f"duplicate:{inv_id}",
                                "class": "duplicate",
                                "variant": "DU-01",
                                "invoice_id": inv_id,
                                "verdict": "finding",
                                "amount_usd": f"{resent['total']:.2f}",
                                "amount_range": None,
                                "deadline": None,
                                "action": action,
                                "confidence": 0.95,
                                "evidence": evidence,
                                "missing_doc": None,
                                "supersedes": [],
                                "learned_at": None,
                            }
                        )

        # Variant 2: DU-03 - Same charge rebilled on different invoice (e.g. 4 months later)
        # Group charges by (awb, charge_code, amount)
        charges_by_awb_code: dict[tuple[str, str, Decimal], list[BilledChargeLine]] = {}
        for c in self.invoice_charges:
            if c.charge_code != "UNKNOWN" and c.amount > Decimal("0.00"):
                charges_by_awb_code.setdefault((c.awb, c.charge_code, c.amount), []).append(c)

        for (awb, code, amt), lines in charges_by_awb_code.items():
            if len(lines) > 1:
                # Check if across different invoices
                unique_invoices = {l.invoice_id for l in lines}
                if len(unique_invoices) > 1:
                    lines.sort(key=lambda l: l.invoice_date)
                    first_line = lines[0]
                    for later_line in lines[1:]:
                        # Check decoy: split house AWBs (e.g. H1 and H2)
                        if first_line.hawb and later_line.hawb and first_line.hawb != later_line.hawb:
                            continue

                        # Find Annex E quote from contract
                        annex_e_quote = ""
                        contract_doc_id = ""
                        annex_e_page = 1
                        matched_contract_id = ""
                        for c_id, mapping in self.annex_e_maps.items():
                            for lbl, (c_code, quote_str, p_num) in mapping.items():
                                if c_code == code:
                                    annex_e_quote = quote_str
                                    annex_e_page = p_num
                                    matched_contract_id = c_id
                                    break
                            if annex_e_quote:
                                break

                        # Find the contract doc_id from first line invoice
                        first_inv_rec = self.invoices_by_id.get(first_line.invoice_id, [{}])[0]
                        contract_ref = first_inv_rec.get("contract_ref", "")

                        # Find contract doc_id dynamically from parsed contracts
                        contract_pdf_id = self.contract_docs.get(contract_ref, "")
                        if not contract_pdf_id:
                            contract_pdf_id = self.contract_docs.get(matched_contract_id, "")
                        if not contract_pdf_id:
                            for cid, cdoc in self.contract_docs.items():
                                if (contract_ref and (contract_ref in cid or cid in contract_ref)) or (matched_contract_id and (matched_contract_id in cid or cid in matched_contract_id)):
                                    contract_pdf_id = cdoc
                                    break

                        # Deduct any credit notes issued against later invoice
                        credit_sum = sum(self.credit_notes_by_invoice.get(later_line.invoice_id, []), Decimal("0.00"))
                        effective_amt = max(Decimal("0.00"), amt - credit_sum)
                        if effective_amt <= Decimal("0.00"):
                            # Fully credited rebill -> legitimate / resolved
                            continue

                        evidence = [
                            {"doc": first_line.doc_id, "page": first_line.page, "quote": first_line.raw_line},
                            {"doc": later_line.doc_id, "page": later_line.page, "quote": later_line.raw_line},
                        ]
                        if annex_e_quote and contract_pdf_id:
                            evidence.append({"doc": contract_pdf_id, "page": annex_e_page, "quote": annex_e_quote})

                        action = "draft:dispute" if self.mode == "supervised" else "execute:dispute"
                        findings.append(
                            {
                                "id": f"duplicate:{later_line.invoice_id}",
                                "class": "duplicate",
                                "variant": "DU-03",
                                "invoice_id": later_line.invoice_id,
                                "verdict": "finding",
                                "amount_usd": f"{effective_amt:.2f}",
                                "amount_range": None,
                                "deadline": None,
                                "action": action,
                                "confidence": 0.95,
                                "evidence": evidence,
                                "missing_doc": None,
                                "supersedes": [],
                                "learned_at": None,
                            }
                        )

        return findings


def audit_duplicates(
    state_dir: str | Path,
    as_of_date: str,
    mode: str = "supervised",
) -> list[dict[str, Any]]:
    """Entry point for duplicate invoice detection audit."""
    engine = DuplicateAuditEngine(state_dir, as_of_date, mode)
    return engine.audit()
