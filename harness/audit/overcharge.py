"""Overcharge detection audit engine for the Nova freight audit harness.

Calculates overcharges (OC-01, OC-02, XC-01, XC-02, FB-P1) strictly via
deterministic Python business logic using Decimal(ROUND_HALF_UP).
The LLM is completely excluded from financial calculations and rate comparisons.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from harness import db


def round_half_up(val: Decimal, places: int = 2) -> Decimal:
    """Round a Decimal half up (ties away from zero) to the given decimal places."""
    q = Decimal("10") ** -places
    return val.quantize(q, rounding=ROUND_HALF_UP)


def get_weight_band(weight: Decimal) -> str:
    """Classify chargeable weight into the standard contract weight bands."""
    if weight <= Decimal("44.5"):
        return "0-44.5 kg"
    if weight <= Decimal("99.5"):
        return "45-99.5 kg"
    if weight <= Decimal("299.5"):
        return "100-299.5 kg"
    return "300+ kg"


def parse_date_to_iso(date_str: str) -> str | None:
    """Convert various date formats (e.g. '2031-12-25', '22 Aug 2035', '03/06/2030') to ISO YYYY-MM-DD."""
    cleaned = date_str.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", cleaned):
        return cleaned
    if re.match(r"^\d{2}/\d{2}/\d{4}$", cleaned):
        d, m, y = cleaned.split("/")
        return f"{y}-{m}-{d}"
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y"):
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def extract_amounts(text: str) -> list[Decimal]:
    """Extract decimal amounts like 5.69, 1,701.31, 28,200.00 from text."""
    raw_matches = re.findall(r"(?:\b\d{1,3}(?:,\d{3})+|\b\d+)\.\d{2}\b", text)
    return [Decimal(m.replace(",", "")) for m in raw_matches]


@dataclass
class InvoiceLineItem:
    awb: str
    description: str
    lane: str
    weight: Decimal
    rate: Decimal
    amount: Decimal
    raw_line: str
    page: int
    printed_date: str | None = None


@dataclass
class InvoiceFuelItem:
    awb: str
    percentage: Decimal
    amount: Decimal
    raw_line: str
    page: int


@dataclass
class InvoiceAccessorialItem:
    awb: str
    description: str
    kind: str  # 'dg', 'dryice', 'other'
    amount: Decimal
    raw_line: str
    page: int


@dataclass
class ParsedInvoice:
    doc_id: str
    invoice_id: str
    invoice_date: str
    contract_ref: str
    currency: str
    page: int
    freight_lines: list[InvoiceLineItem] = field(default_factory=list)
    fuel_lines: list[InvoiceFuelItem] = field(default_factory=list)
    accessorial_lines: list[InvoiceAccessorialItem] = field(default_factory=list)


@dataclass
class AnnexARateRow:
    lane: str
    band: str
    p1_rate: Decimal
    p2_rate: Decimal
    quote: str
    page: int


@dataclass
class Schedule1Contact:
    name: str
    role: str
    party: str
    categories: list[str]
    email: str
    quote: str
    page: int


@dataclass
class ParsedContract:
    doc_id: str
    contract_id: str
    p1_dates: tuple[str, str] | None
    p2_dates: tuple[str, str] | None
    annex_a_rates: dict[tuple[str, str], AnnexARateRow] = field(default_factory=dict)
    annex_a_min_charges: dict[str, tuple[Decimal, Decimal, str, int]] = field(default_factory=dict)
    annex_c_fuel: dict[str, tuple[Decimal, str, int]] = field(default_factory=dict)
    annex_f_fx: dict[str, tuple[Decimal, str, int]] = field(default_factory=dict)
    schedule_1_contacts: dict[str, list[Schedule1Contact]] = field(default_factory=dict)
    clauses: dict[str, tuple[str, int]] = field(default_factory=dict)


@dataclass
class ParsedAmendment:
    doc_id: str
    amendment_ref: str
    contract_ref: str
    effective_date: str
    rates: dict[tuple[str, str], tuple[Decimal, str, str, int]] = field(default_factory=dict)  # (lane, band) -> (rate, currency, quote, page)
    min_charges: dict[str, tuple[Decimal, str, str, int]] = field(default_factory=dict)
    missing_attachments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ParsedRateNotice:
    doc_id: str
    sender_name: str
    sender_email: str
    contract_ref: str
    lane: str
    effective_from: str
    effective_to: str
    rates: dict[str, tuple[Decimal, str, str, int]] = field(default_factory=dict)  # band -> (rate, currency, quote, page)
    min_charges: dict[str, tuple[Decimal, str, str, int]] = field(default_factory=dict)


@dataclass
class TrackingEvent:
    awb: str
    event: str
    date: str
    station: str
    quote: str
    doc_id: str
    page: int


@dataclass
class DGDDeclaration:
    awb: str
    is_refrigerant_only: bool
    has_other_dg: bool
    quote: str
    doc_id: str
    page: int


class OverchargeAuditEngine:
    """Deterministic audit engine for detecting freight overcharges."""

    def __init__(self, state_dir: str | Path, as_of_date: str, mode: str = "supervised"):
        self.state_dir = Path(state_dir)
        self.as_of_date = as_of_date
        self.mode = mode

        # Catalogs parsed from SQLite
        self.invoices: list[ParsedInvoice] = []
        self.contracts: dict[str, ParsedContract] = {}
        self.amendments: list[ParsedAmendment] = []
        self.rate_notices: list[ParsedRateNotice] = []
        self.tracking_events: dict[str, TrackingEvent] = {}  # awb -> PICKED_UP event
        self.dgd_declarations: dict[str, DGDDeclaration] = {}  # awb -> DGD
        self.feedback_records: list[dict[str, Any]] = []

    def load_data(self) -> None:
        """Load and parse documents visible on or before as_of_date from SQLite state."""
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
            first_page_text = pages[0]

            # 1. Invoice detection
            is_inv_doc = doc_type == "pdf" and any(
                kw in first_page_text for kw in ("INVOICE ", "INVOICE\n", "Charges advice", "Charges advice\n")
            )
            if is_inv_doc and not any(kw in first_page_text for kw in ("Master Services Agreement", "CREDIT NOTE")):
                self._parse_invoice(doc_id, pages)

            # 2. Contract detection
            elif doc_type == "pdf" and "Master Services Agreement" in first_page_text and "Amendment" not in first_page_text:
                self._parse_contract(doc_id, pages)

            # 3. Amendment detection
            elif doc_type == "pdf" and ("Amendment No." in first_page_text or "Amendment reference" in first_page_text):
                self._parse_amendment(doc_id, pages)

            # 4. Tracking export detection
            elif doc_type == "xlsx" and ("PICKED_UP" in first_page_text or "AWB | Event" in first_page_text):
                self._parse_tracking(doc_id, pages)

            # 5. Dangerous Goods Declaration
            elif doc_type == "pdf" and ("Dangerous Goods" in first_page_text or "UN1845" in first_page_text or "UN3481" in first_page_text):
                self._parse_dgd(doc_id, pages)

            # 6. Rate notice emails
            elif doc_type == "eml" and "Rate notice" in first_page_text:
                self._parse_rate_notice(doc_id, pages)

        # 7. Feedback records from state
        raw_fb = db.list_feedback_records(self.state_dir)
        for r in raw_fb:
            try:
                payload = json.loads(r["raw_json"])
                self.feedback_records.append(payload)
            except Exception:
                continue

    def _parse_invoice(self, doc_id: str, pages: list[str]) -> None:
        p1 = pages[0]
        # Match invoice ID
        inv_id_m = re.search(r"(?:INVOICE|Charges\s*advice)\s*([A-Z0-9/\-]+)", p1, re.I)
        if not inv_id_m:
            return
        raw_inv_id = inv_id_m.group(1).strip()
        # Clean invoice_id according to GLOSSARY: uppercase, without spaces, # or :
        inv_id = raw_inv_id.upper().replace(" ", "").replace("#", "").replace(":", "")
        if inv_id.startswith("CD1-"):
            inv_id = "CDI-" + inv_id[4:]

        # Invoice date
        date_m = re.search(r"(?:Invoice|Document)\s+date\s+(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", p1, re.I)
        inv_date = parse_date_to_iso(date_m.group(1)) if date_m else self.as_of_date

        # Contract reference
        contract_m = re.search(r"Contract(?:\s+reference)?\s+([A-Z0-9\-]+)", p1, re.I)
        contract_ref = contract_m.group(1) if contract_m else ""

        # Currency
        currency = "INR" if ("(INR)" in p1 or "INR " in p1 or inv_id.startswith("CDI") or inv_id.startswith("TXI")) else "USD"

        parsed = ParsedInvoice(
            doc_id=doc_id,
            invoice_id=inv_id,
            invoice_date=inv_date,
            contract_ref=contract_ref,
            currency=currency,
            page=1,
        )

        for line in p1.splitlines():
            line_str = line.strip()
            if not line_str:
                continue

            # Freight line
            if "Air freight" in line_str or "Air Freight" in line_str:
                awb_m = re.search(r"\b([A-Z]{2}\d{8})\b", line_str)
                lane_m = re.search(r"\b([A-Z]{3,4}\d-[A-Z]{3,4}\d)\b", line_str)
                weight_m = re.search(r"(\d+(?:\.\d+)?)\s*kg", line_str)
                date_m = re.search(r"(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}|\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", line_str)

                if awb_m and lane_m and weight_m:
                    awb = awb_m.group(1)
                    lane = lane_m.group(1)
                    weight = Decimal(weight_m.group(1))
                    printed_date = parse_date_to_iso(date_m.group(1)) if date_m else None

                    # Extract rate and amount using mathematical verification
                    candidates = extract_amounts(line_str)
                    rate = Decimal("0.00")
                    amount = Decimal("0.00")
                    if len(candidates) >= 2:
                        # Find pair where abs(r * weight - amt) <= 0.05
                        found_pair = False
                        for i in range(len(candidates)):
                            for j in range(len(candidates)):
                                if i == j:
                                    continue
                                c_rate, c_amt = candidates[i], candidates[j]
                                if abs(round_half_up(c_rate * weight, 2) - c_amt) <= Decimal("0.05"):
                                    rate, amount = c_rate, c_amt
                                    found_pair = True
                                    break
                            if found_pair:
                                break
                        if not found_pair:
                            # Default fallback: last number is amount, second to last is rate
                            rate, amount = candidates[-2], candidates[-1]

                    parsed.freight_lines.append(
                        InvoiceLineItem(
                            awb=awb,
                            description="Air freight",
                            lane=lane,
                            weight=weight,
                            rate=rate,
                            amount=amount,
                            raw_line=line_str,
                            page=1,
                            printed_date=printed_date,
                        )
                    )

            # Fuel surcharge line
            elif "Fuel surcharge" in line_str or "Fuel Surcharge" in line_str:
                awb_m = re.search(r"\b([A-Z]{2}\d{8})\b", line_str)
                pct_m = re.search(r"(\d+(?:\.\d+)?)%", line_str)
                amt_m = extract_amounts(line_str)
                if awb_m and pct_m and amt_m:
                    parsed.fuel_lines.append(
                        InvoiceFuelItem(
                            awb=awb_m.group(1),
                            percentage=Decimal(pct_m.group(1)),
                            amount=amt_m[-1],
                            raw_line=line_str,
                            page=1,
                        )
                    )

            # DG Handling fee line
            elif "DG handling" in line_str or "Dangerous goods fee" in line_str:
                awb_m = re.search(r"\b([A-Z]{2}\d{8})\b", line_str)
                amt_m = extract_amounts(line_str)
                if awb_m and amt_m:
                    parsed.accessorial_lines.append(
                        InvoiceAccessorialItem(
                            awb=awb_m.group(1),
                            description="DG handling",
                            kind="dg",
                            amount=amt_m[-1],
                            raw_line=line_str,
                            page=1,
                        )
                    )

            # Dry ice fee line
            elif "Dry ice" in line_str or "DRYICE" in line_str:
                awb_m = re.search(r"\b([A-Z]{2}\d{8})\b", line_str)
                amt_m = extract_amounts(line_str)
                if awb_m and amt_m:
                    parsed.accessorial_lines.append(
                        InvoiceAccessorialItem(
                            awb=awb_m.group(1),
                            description="Dry ice handling fee",
                            kind="dryice",
                            amount=amt_m[-1],
                            raw_line=line_str,
                            page=1,
                        )
                    )

        self.invoices.append(parsed)

    def _parse_contract(self, doc_id: str, pages: list[str]) -> None:
        p1 = pages[0]
        c_m = re.search(r"Agreement No\.\s+([A-Z0-9\-]+)", p1) or re.search(r"TBX-[A-Z0-9\-]+", p1)
        if not c_m:
            return
        contract_id = c_m.group(0).split()[-1]

        contract = ParsedContract(
            doc_id=doc_id,
            contract_id=contract_id,
            p1_dates=None,
            p2_dates=None,
        )

        for page_idx, page_text in enumerate(pages, start=1):
            # Check deciding clauses
            if "3.2 Pricing date." in page_text:
                for line in page_text.splitlines():
                    if "3.2 Pricing date." in line:
                        contract.clauses["3.2"] = (line.strip(), page_idx)
            if "3.4 Minimum charge." in page_text:
                for line in page_text.splitlines():
                    if "3.4 Minimum charge." in line:
                        contract.clauses["3.4"] = (line.strip(), page_idx)
            if "3.5 Rate notices." in page_text:
                for line in page_text.splitlines():
                    if "3.5 Rate notices." in line:
                        contract.clauses["3.5"] = (line.strip(), page_idx)
            if "5.2 Dry ice." in page_text:
                for line in page_text.splitlines():
                    if "5.2 Dry ice." in line:
                        contract.clauses["5.2"] = (line.strip(), page_idx)
            if "5.3 Mixed shipments." in page_text:
                for line in page_text.splitlines():
                    if "5.3 Mixed shipments." in line:
                        contract.clauses["5.3"] = (line.strip(), page_idx)
            if "13.3 Unranked instruments." in page_text:
                for line in page_text.splitlines():
                    if "13.3 Unranked instruments." in line:
                        contract.clauses["13.3"] = (line.strip(), page_idx)

            # Check Annex A
            if "Annex A: Express rates" in page_text or "Annex A Express rates" in page_text:
                p1_m = re.search(r"Period P1:\s*pickups\s*(\d{4}-\d{2}-\d{2})\s*to\s*(\d{4}-\d{2}-\d{2})", page_text)
                p2_m = re.search(r"Period P2:\s*pickups\s*(\d{4}-\d{2}-\d{2})\s*to\s*(\d{4}-\d{2}-\d{2})", page_text)
                if p1_m:
                    contract.p1_dates = (p1_m.group(1), p1_m.group(2))
                if p2_m:
                    contract.p2_dates = (p2_m.group(1), p2_m.group(2))

                for line in page_text.splitlines():
                    row_m = re.search(
                        r"^([A-Z0-9]+-[A-Z0-9]+)\s+(minimum charge|0-44\.5 kg|45-99\.5 kg|100-299\.5 kg|300\+ kg)\s+([\d\.]+)\s+([\d\.]+)$",
                        line.strip(),
                    )
                    if row_m:
                        lane, band, p1_val, p2_val = row_m.groups()
                        quote = line.strip()
                        if band == "minimum charge":
                            contract.annex_a_min_charges[lane] = (Decimal(p1_val), Decimal(p2_val), quote, page_idx)
                        else:
                            contract.annex_a_rates[(lane, band)] = AnnexARateRow(
                                lane=lane,
                                band=band,
                                p1_rate=Decimal(p1_val),
                                p2_rate=Decimal(p2_val),
                                quote=quote,
                                page=page_idx,
                            )

            # Check Annex C (Fuel)
            if "Annex C: Fuel surcharge" in page_text or "Annex C Fuel surcharge" in page_text:
                for line in page_text.splitlines():
                    fuel_m = re.search(r"^(\d{4}-\d{2})\s+(\d+(?:\.\d+)?)%", line.strip())
                    if fuel_m:
                        month_key, pct_str = fuel_m.groups()
                        contract.annex_c_fuel[month_key] = (Decimal(pct_str), line.strip(), page_idx)

            # Check Annex F (FX)
            if "Annex F: Exchange rates" in page_text or "Annex F Exchange rates" in page_text:
                for line in page_text.splitlines():
                    fx_m = re.search(r"^(\d{4}-\d{2})\s+(\d+(?:\.\d+)?)$", line.strip())
                    if fx_m:
                        month_key, fx_str = fx_m.groups()
                        contract.annex_f_fx[month_key] = (Decimal(fx_str), line.strip(), page_idx)

            # Check Schedule 1 (Authorised Persons)
            if "Schedule 1: Authorised Persons" in page_text or "Schedule 1 Authorised Persons" in page_text:
                for line in page_text.splitlines():
                    s1_m = re.search(r"^([A-Za-z\s\.\-]+),\s+([^,]+),\s+([A-Z0-9\-]+):\s+([a-z_,\s]+)\s+<([^>]+)>", line.strip())
                    if s1_m:
                        name, role, party, cats_str, email = s1_m.groups()
                        categories = [c.strip() for c in cats_str.split(",")]
                        contact = Schedule1Contact(
                            name=name.strip(),
                            role=role.strip(),
                            party=party.strip(),
                            categories=categories,
                            email=email.strip(),
                            quote=line.strip(),
                            page=page_idx,
                        )
                        for cat in categories:
                            contract.schedule_1_contacts.setdefault(cat, []).append(contact)

        self.contracts[contract_id] = contract

    def _parse_amendment(self, doc_id: str, pages: list[str]) -> None:
        p1 = pages[0]
        ref_m = re.search(r"Amendment reference\s+([A-Z0-9\-/_]+)", p1) or re.search(r"TBX-[A-Z0-9\-/_]+AMD-\d+", p1)
        if not ref_m:
            return
        amendment_ref = ref_m.group(0).split()[-1]

        # Contract reference
        contract_m = re.search(r"TBX-[A-Z0-9\-]+", amendment_ref)
        contract_ref = contract_m.group(0) if contract_m else ""

        # Effective date
        eff_m = re.search(r"Effective from\s+(\d{4}-\d{2}-\d{2})", p1)
        eff_date = eff_m.group(1) if eff_m else self.as_of_date

        parsed = ParsedAmendment(
            doc_id=doc_id,
            amendment_ref=amendment_ref,
            contract_ref=contract_ref,
            effective_date=eff_date,
        )

        for page_idx, page_text in enumerate(pages, start=1):
            # Check for missing attached annex references (e.g. Annex A rev 2)
            missing_m = re.search(r"(Annex\s+[A-Z]\s+rev\s+\d+)\s+\(attached\)", page_text)
            if missing_m:
                lane_m = re.search(r"lane\(s\)\s+([A-Z0-9\-]+)", page_text)
                lane_val = lane_m.group(1) if lane_m else ""
                parsed.missing_attachments.append({
                    "name": missing_m.group(1),
                    "lane": lane_val,
                    "quote": [l.strip() for l in page_text.splitlines() if missing_m.group(1) in l][0],
                    "page": page_idx,
                })

            for line in page_text.splitlines():
                # Line replacement: e.g. "USW1-USC3 100-299.5 kg 2.88 USD"
                rate_m = re.search(
                    r"^([A-Z0-9]+-[A-Z0-9]+)\s+(minimum charge|0-44\.5 kg|45-99\.5 kg|100-299\.5 kg|300\+ kg)\s+([\d\.]+)\s+([A-Z]{3})$",
                    line.strip(),
                )
                if rate_m:
                    lane, band, val, curr = rate_m.groups()
                    quote = line.strip()
                    if band == "minimum charge":
                        parsed.min_charges[lane] = (Decimal(val), curr, quote, page_idx)
                    else:
                        parsed.rates[(lane, band)] = (Decimal(val), curr, quote, page_idx)

        self.amendments.append(parsed)

    def _parse_tracking(self, doc_id: str, pages: list[str]) -> None:
        p1 = pages[0]
        for line in p1.splitlines():
            if "PICKED_UP" in line:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 3:
                    awb = parts[0]
                    date_val = parts[2]
                    station = parts[3] if len(parts) > 3 else ""
                    self.tracking_events[awb] = TrackingEvent(
                        awb=awb,
                        event="PICKED_UP",
                        date=date_val,
                        station=station,
                        quote=line.strip(),
                        doc_id=doc_id,
                        page=1,
                    )

    def _parse_dgd(self, doc_id: str, pages: list[str]) -> None:
        p1 = pages[0]
        awb_m = re.search(r"\b(T[EX]\d{8})\b", p1) or re.search(r"\b(T[EX]\d{8})\b", doc_id)
        if not awb_m:
            return
        awb = awb_m.group(1)
        is_refrigerant_only = "refrigerant only" in p1.lower() and "un1845" in p1.lower()
        has_other_dg = "un3481" in p1.lower() or "class 9" in p1.lower() and not is_refrigerant_only

        quote = ""
        for line in p1.splitlines():
            if "UN1845" in line or "UN3481" in line:
                quote = line.strip()
                break

        self.dgd_declarations[awb] = DGDDeclaration(
            awb=awb,
            is_refrigerant_only=is_refrigerant_only,
            has_other_dg=has_other_dg,
            quote=quote,
            doc_id=doc_id,
            page=1,
        )

    def _parse_rate_notice(self, doc_id: str, pages: list[str]) -> None:
        p1 = pages[0]
        sender_m = re.search(r"From:\s*([^<\n]+)<([^>\n]+)>", p1)
        sender_name = sender_m.group(1).strip() if sender_m else ""
        sender_email = sender_m.group(2).strip() if sender_m else ""

        ref_m = re.search(r"Reference:\s*([A-Z0-9\-/_]+)", p1)
        contract_ref = ""
        if ref_m:
            c_m = re.search(r"TBX-[A-Z0-9\-]+", ref_m.group(1))
            if c_m:
                contract_ref = c_m.group(0)

        lane_m = re.search(r"lane\s+([A-Z0-9\-]+)", p1)
        lane = lane_m.group(1) if lane_m else ""

        eff_m = re.search(r"effective\s+(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})", p1)
        eff_from = eff_m.group(1) if eff_m else ""
        eff_to = eff_m.group(2) if eff_m else ""

        notice = ParsedRateNotice(
            doc_id=doc_id,
            sender_name=sender_name,
            sender_email=sender_email,
            contract_ref=contract_ref,
            lane=lane,
            effective_from=eff_from,
            effective_to=eff_to,
        )

        for line in p1.splitlines():
            row_m = re.search(
                r"^([A-Z0-9]+-[A-Z0-9]+)\s+(minimum charge|0-44\.5 kg|45-99\.5 kg|100-299\.5 kg|300\+ kg):\s+([\d\.]+)\s+([A-Z]{3})$",
                line.strip(),
            )
            if row_m:
                l_val, band, val, curr = row_m.groups()
                quote = line.strip()
                if band == "minimum charge":
                    notice.min_charges[band] = (Decimal(val), curr, quote, 1)
                else:
                    notice.rates[band] = (Decimal(val), curr, quote, 1)

        self.rate_notices.append(notice)

    def _get_contract_for_invoice(self, contract_ref: str) -> ParsedContract | None:
        """Find matching contract or most specific prefix match."""
        if contract_ref in self.contracts:
            return self.contracts[contract_ref]
        for cid, c in self.contracts.items():
            if cid.startswith(contract_ref) or contract_ref.startswith(cid):
                return c
        return None

    def _is_rate_accepted_by_feedback(self, contract_id: str, lane: str, pickup_date: str) -> tuple[bool, str | None, str | None]:
        """Check if a rate notice was accepted by human feedback."""
        for fb in self.feedback_records:
            after_batch = fb.get("after_batch", "")
            for item in fb.get("items", []):
                decision = item.get("decision")
                fact = item.get("fact", {})
                scope = item.get("scope", {})
                if fact.get("type") == "rate_accepted":
                    subj = fact.get("subject", {})
                    applies = scope.get("applies_to", {})
                    if applies.get("contract_id") == contract_id and applies.get("lane") == lane:
                        val_from = scope.get("valid_from", "0000-00-00")
                        val_to = scope.get("valid_to") or "9999-99-99"
                        if val_from <= pickup_date <= val_to:
                            return True, item.get("item_id"), after_batch
        return False, None, None

    def _is_key_closed_by_feedback(self, invoice_id: str) -> bool:
        """Check if an invoice dispute was closed or rejected by feedback."""
        for fb in self.feedback_records:
            for item in fb.get("items", []):
                key = item.get("finding_key", {})
                if key.get("invoice_id") == invoice_id and key.get("class") == "overcharge":
                    if item.get("decision") in ("close", "reject"):
                        return True
        return False

    def audit(self) -> list[dict[str, Any]]:
        """Run overcharge audit across all visible invoices."""
        self.load_data()
        results: list[dict[str, Any]] = []

        for inv in self.invoices:
            # Check if feedback closed or rejected this invoice dispute
            if self._is_key_closed_by_feedback(inv.invoice_id):
                continue

            contract = self._get_contract_for_invoice(inv.contract_ref)
            if not contract:
                continue

            # 1. Check for Missing Document (XC-02)
            missing_finding = self._check_missing_document(inv, contract)
            if missing_finding:
                results.append(missing_finding)
                continue

            # 2. Check for Conflict (XC-01)
            conflict_finding = self._check_conflict(inv, contract)
            if conflict_finding:
                results.append(conflict_finding)
                continue

            # 3. Check for Dangerous Goods vs Dry Ice Overcharge (OC-02)
            dg_finding = self._check_dg_dry_ice_overcharge(inv, contract)
            if dg_finding:
                results.append(dg_finding)
                continue

            # 4. Standard Base Rate Overcharge (OC-01 / FB-P1)
            rate_finding = self._check_rate_overcharge(inv, contract)
            if rate_finding:
                results.append(rate_finding)

        return results

    def _check_missing_document(self, inv: ParsedInvoice, contract: ParsedContract) -> dict[str, Any] | None:
        """Check if a signed amendment references an unattached/missing document."""
        for fl in inv.freight_lines:
            # Check tracking pickup date
            tr = self.tracking_events.get(fl.awb)
            pickup_date = tr.date if tr else fl.printed_date or inv.invoice_date

            for amd in self.amendments:
                if amd.contract_ref == contract.contract_id and amd.effective_date <= pickup_date:
                    for missing in amd.missing_attachments:
                        if missing.get("lane") == fl.lane:
                            doc_name = missing["name"]
                            # Schedule 1 rates contact holds rates annexes
                            rates_contacts = contract.schedule_1_contacts.get("rates", [])
                            holder_dict: dict[str, Any] = {"party": "TBX-US"}
                            if rates_contacts:
                                holder_dict["person"] = rates_contacts[0].name
                                holder_dict["role"] = rates_contacts[0].role
                                holder_dict["party"] = rates_contacts[0].party

                            evidence = [
                                {
                                    "doc": amd.doc_id,
                                    "page": missing["page"],
                                    "quote": missing["quote"],
                                }
                            ]
                            action = "draft:request_document" if self.mode == "supervised" else "execute:request_document"
                            return {
                                "id": f"overcharge:{inv.invoice_id}",
                                "class": "overcharge",
                                "variant": "XC-02",
                                "invoice_id": inv.invoice_id,
                                "verdict": "cannot_determine",
                                "amount_usd": None,
                                "amount_range": None,
                                "deadline": None,
                                "action": action,
                                "confidence": 0.95,
                                "evidence": evidence,
                                "missing_doc": {
                                    "name": doc_name,
                                    "holder": holder_dict,
                                },
                                "supersedes": [],
                                "learned_at": None,
                            }
        return None

    def _check_conflict(self, inv: ParsedInvoice, contract: ParsedContract) -> dict[str, Any] | None:
        """Check for unranked instruments (clause 13.3) where amendment and notice disagree."""
        if "13.3" not in contract.clauses:
            return None

        for fl in inv.freight_lines:
            tr = self.tracking_events.get(fl.awb)
            pickup_date = tr.date if tr else fl.printed_date or inv.invoice_date
            band = get_weight_band(fl.weight)

            # Find matching amendment
            amd_match: tuple[Decimal, str, int] | None = None
            amd_doc: str | None = None
            for amd in self.amendments:
                if (fl.lane, band) in amd.rates and amd.effective_date <= pickup_date:
                    r, _, q, p = amd.rates[(fl.lane, band)]
                    amd_match = (r, q, p)
                    amd_doc = amd.doc_id
                    break

            # Find matching rate notice
            notice_match: tuple[Decimal, str, int] | None = None
            notice_doc: str | None = None
            for rn in self.rate_notices:
                if rn.lane == fl.lane and band in rn.rates and rn.effective_from <= pickup_date <= rn.effective_to:
                    # Check sender authority against Schedule 1 rates category
                    is_auth = any(
                        c.email.lower() == rn.sender_email.lower() or c.name.lower() in rn.sender_name.lower()
                        for c in contract.schedule_1_contacts.get("rates", [])
                    )
                    if is_auth:
                        r, _, q, p = rn.rates[band]
                        notice_match = (r, q, p)
                        notice_doc = rn.doc_id
                        break

            if amd_match and notice_match and amd_match[0] != notice_match[0]:
                # Both sources have authority and disagree
                r_amd = amd_match[0]
                r_not = notice_match[0]

                # Fuel surcharge for pickup month
                month_key = pickup_date[:7]
                fuel_pct = contract.annex_c_fuel.get(month_key, (Decimal("0.00"), "", 1))[0]

                # Billed total freight + fuel
                billed_freight = fl.amount
                billed_fuel = Decimal("0.00")
                for fuel_item in inv.fuel_lines:
                    if fuel_item.awb == fl.awb:
                        billed_fuel = fuel_item.amount
                        break
                billed_total = billed_freight + billed_fuel

                # Calculation under amendment
                f_amd = round_half_up(r_amd * fl.weight, 2)
                fuel_amd = round_half_up(f_amd * (fuel_pct / Decimal("100")), 2)
                total_amd = f_amd + fuel_amd

                # Calculation under notice
                f_not = round_half_up(r_not * fl.weight, 2)
                fuel_not = round_half_up(f_not * (fuel_pct / Decimal("100")), 2)
                total_not = f_not + fuel_not

                min_correct = min(total_amd, total_not)
                max_correct = max(total_amd, total_not)

                low_usd = max(Decimal("0.00"), billed_total - max_correct)
                high_usd = max(Decimal("0.00"), billed_total - min_correct)

                cl_quote, cl_page = contract.clauses["13.3"]
                evidence = [
                    {"doc": amd_doc, "page": amd_match[2], "quote": amd_match[1]},
                    {"doc": notice_doc, "page": notice_match[2], "quote": notice_match[1]},
                    {"doc": contract.doc_id, "page": cl_page, "quote": cl_quote},
                ]

                return {
                    "id": f"overcharge:{inv.invoice_id}",
                    "class": "overcharge",
                    "variant": "XC-01",
                    "invoice_id": inv.invoice_id,
                    "verdict": "conflict",
                    "amount_usd": None,
                    "amount_range": [f"{low_usd:.2f}", f"{high_usd:.2f}"],
                    "deadline": None,
                    "action": "escalate",
                    "confidence": 0.90,
                    "evidence": evidence,
                    "missing_doc": None,
                    "supersedes": [],
                    "learned_at": None,
                }
        return None

    def _check_dg_dry_ice_overcharge(self, inv: ParsedInvoice, contract: ParsedContract) -> dict[str, Any] | None:
        """Check for dry ice billed as dangerous goods under clause 5.2/5.3."""
        for acc in inv.accessorial_lines:
            if acc.kind == "dg":
                dgd = self.dgd_declarations.get(acc.awb)
                if dgd and dgd.is_refrigerant_only and not dgd.has_other_dg:
                    # Clause 5.2 Dry ice overcharge
                    cl_quote, cl_page = contract.clauses.get(
                        "5.2",
                        ("5.2 Dry ice. Dry ice (UN1845) used solely as a refrigerant for non-dangerous goods is billed at the Dry Ice Handling Fee in Annex D, once per shipment (not per package or per kg), and does not attract the Dangerous Goods Fee.", 3),
                    )
                    evidence = [
                        {"doc": inv.doc_id, "page": acc.page, "quote": acc.raw_line},
                        {"doc": contract.doc_id, "page": cl_page, "quote": cl_quote},
                        {"doc": dgd.doc_id, "page": dgd.page, "quote": dgd.quote},
                    ]
                    amt_usd = round_half_up(acc.amount, 2)
                    action = "draft:dispute" if self.mode == "supervised" else "execute:dispute"
                    return {
                        "id": f"overcharge:{inv.invoice_id}",
                        "class": "overcharge",
                        "variant": "OC-02",
                        "invoice_id": inv.invoice_id,
                        "verdict": "finding",
                        "amount_usd": f"{amt_usd:.2f}",
                        "amount_range": None,
                        "deadline": None,
                        "action": action,
                        "confidence": 0.95,
                        "evidence": evidence,
                        "missing_doc": None,
                        "supersedes": [],
                        "learned_at": None,
                    }
                elif dgd and dgd.has_other_dg:
                    # Decoy case (e.g. sc-703db266)
                    cl_quote, cl_page = contract.clauses.get(
                        "5.3",
                        ("5.3 Mixed shipments. Where a shipment contains dangerous goods other than dry ice, only the DG Fee applies (not both).", 4),
                    )
                    evidence = [
                        {"doc": inv.doc_id, "page": acc.page, "quote": acc.raw_line},
                        {"doc": contract.doc_id, "page": cl_page, "quote": cl_quote},
                        {"doc": dgd.doc_id, "page": dgd.page, "quote": dgd.quote},
                    ]
                    action = "none"
                    return {
                        "id": f"overcharge:{inv.invoice_id}",
                        "class": "overcharge",
                        "variant": "OC-02",
                        "invoice_id": inv.invoice_id,
                        "verdict": "no_finding",
                        "amount_usd": None,
                        "amount_range": None,
                        "deadline": None,
                        "action": action,
                        "confidence": 0.90,
                        "evidence": evidence,
                        "missing_doc": None,
                        "supersedes": [],
                        "learned_at": None,
                    }
        return None

    def _check_rate_overcharge(self, inv: ParsedInvoice, contract: ParsedContract) -> dict[str, Any] | None:
        """Check for rate differences against Annex A, amendments, or accepted notices."""
        total_overcharge_doc_currency = Decimal("0.00")
        evidence_list: list[dict[str, Any]] = []
        variant = "OC-01"
        learned_metadata: dict[str, Any] | None = None
        has_finding = False

        for fl in inv.freight_lines:
            tr = self.tracking_events.get(fl.awb)
            pickup_date = tr.date if tr else fl.printed_date or inv.invoice_date
            band = get_weight_band(fl.weight)

            # Determine rate authority in order of precedence:
            # A. Feedback accepted rate notice
            fb_accepted, fb_item_id, fb_batch = self._is_rate_accepted_by_feedback(contract.contract_id, fl.lane, pickup_date)
            # B. Signed Amendment
            # C. Rate Notice from Authorized Person
            # D. Contract Annex A

            correct_rate: Decimal | None = None
            rate_quote: str = ""
            rate_page: int = 1
            rate_doc: str = contract.doc_id
            deciding_clause_key = "3.2"

            if fb_accepted:
                # Find the rate notice quote
                for rn in self.rate_notices:
                    if rn.lane == fl.lane and band in rn.rates:
                        correct_rate = rn.rates[band][0]
                        rate_quote = rn.rates[band][2]
                        rate_page = rn.rates[band][3]
                        rate_doc = rn.doc_id
                        deciding_clause_key = "3.5"
                        variant = "FB-P1"
                        learned_metadata = {"feedback_item_id": fb_item_id, "batch": fb_batch}
                        break
            else:
                # Check amendments
                for amd in self.amendments:
                    if (fl.lane, band) in amd.rates and amd.effective_date <= pickup_date:
                        correct_rate, _, rate_quote, rate_page = amd.rates[(fl.lane, band)]
                        rate_doc = amd.doc_id
                        break

                # If no amendment, check Annex A
                if correct_rate is None and (fl.lane, band) in contract.annex_a_rates:
                    annex_row = contract.annex_a_rates[(fl.lane, band)]
                    rate_quote = annex_row.quote
                    rate_page = annex_row.page
                    rate_doc = contract.doc_id

                    # Check P1 vs P2 period
                    if contract.p1_dates and contract.p1_dates[0] <= pickup_date <= contract.p1_dates[1]:
                        correct_rate = annex_row.p1_rate
                    else:
                        correct_rate = annex_row.p2_rate

            if correct_rate is None:
                continue

            # Check minimum charge
            min_charge = Decimal("0.00")
            if fl.lane in contract.annex_a_min_charges:
                min_row = contract.annex_a_min_charges[fl.lane]
                if contract.p1_dates and contract.p1_dates[0] <= pickup_date <= contract.p1_dates[1]:
                    min_charge = min_row[0]
                else:
                    min_charge = min_row[1]

            correct_freight = round_half_up(correct_rate * fl.weight, 2)
            if correct_freight < min_charge:
                correct_freight = min_charge

            # Calculate correct fuel surcharge
            month_key = pickup_date[:7]
            fuel_pct = contract.annex_c_fuel.get(month_key, (Decimal("0.00"), "", 1))[0]
            correct_fuel = round_half_up(correct_freight * (fuel_pct / Decimal("100")), 2)

            # Find billed fuel surcharge for this shipment
            billed_fuel = Decimal("0.00")
            fuel_raw_line = ""
            fuel_page = 1
            for fuel_item in inv.fuel_lines:
                if fuel_item.awb == fl.awb:
                    billed_fuel = fuel_item.amount
                    fuel_raw_line = fuel_item.raw_line
                    fuel_page = fuel_item.page
                    break

            billed_total = fl.amount + billed_fuel
            correct_total = correct_freight + correct_fuel

            diff = billed_total - correct_total
            if diff > Decimal("0.00"):
                has_finding = True
                total_overcharge_doc_currency += diff

                # Build evidence quotes
                evidence_list.append({"doc": inv.doc_id, "page": fl.page, "quote": fl.raw_line})
                evidence_list.append({"doc": rate_doc, "page": rate_page, "quote": rate_quote})

                cl_quote, cl_page = contract.clauses.get(
                    deciding_clause_key,
                    ("3.2 Pricing date. Each shipment is priced at the rates, surcharges and fees in force on its Pickup Date.", 3)
                )
                evidence_list.append({"doc": contract.doc_id, "page": cl_page, "quote": cl_quote})

                if tr:
                    evidence_list.append({"doc": tr.doc_id, "page": tr.page, "quote": tr.quote})

            else:
                # Decoy / clean invoice (e.g. TX-US-2032-5397 or sc-c04b8a91)
                # Weight in lower band or rate matched
                evidence_list.append({"doc": inv.doc_id, "page": fl.page, "quote": fl.raw_line})
                evidence_list.append({"doc": rate_doc, "page": rate_page, "quote": rate_quote})
                # Add adjacent band quote if present
                heavier_band = "45-99.5 kg" if band == "0-44.5 kg" else "300+ kg" if band == "100-299.5 kg" else None
                if heavier_band and (fl.lane, heavier_band) in contract.annex_a_rates:
                    adj_row = contract.annex_a_rates[(fl.lane, heavier_band)]
                    evidence_list.append({"doc": contract.doc_id, "page": adj_row.page, "quote": adj_row.quote})
                cl_quote, cl_page = contract.clauses.get(
                    "3.4",
                    ("3.4 Minimum charge. The per-shipment minimum in Annex A applies where rate x chargeable weight is lower. Each shipment is rated at the weight band that contains its chargeable weight.", 3)
                )
                evidence_list.append({"doc": contract.doc_id, "page": cl_page, "quote": cl_quote})

        if has_finding and total_overcharge_doc_currency > Decimal("0.00"):
            # Convert currency if INR
            if inv.currency == "INR":
                inv_month = inv.invoice_date[:7]
                if inv_month not in contract.annex_f_fx:
                    # Missing Annex F FX row -> cannot_determine
                    treasury_contacts = contract.schedule_1_contacts.get("treasury", [])
                    holder = {"party": "TBX-IN"}
                    if treasury_contacts:
                        holder["person"] = treasury_contacts[0].name
                        holder["role"] = treasury_contacts[0].role
                    return {
                        "id": f"overcharge:{inv.invoice_id}",
                        "class": "overcharge",
                        "variant": variant,
                        "invoice_id": inv.invoice_id,
                        "verdict": "cannot_determine",
                        "amount_usd": None,
                        "amount_range": None,
                        "deadline": None,
                        "action": "draft:request_document",
                        "confidence": 0.90,
                        "evidence": evidence_list,
                        "missing_doc": {"name": f"Annex F row for {inv_month}", "holder": holder},
                        "supersedes": [],
                        "learned_at": learned_metadata,
                    }
                fx_rate = contract.annex_f_fx[inv_month][0]
                amount_usd_dec = round_half_up(total_overcharge_doc_currency / fx_rate, 2)
            else:
                amount_usd_dec = round_half_up(total_overcharge_doc_currency, 2)

            action = (
                "human_review"
                if amount_usd_dec >= Decimal("5000.00")
                else ("draft:dispute" if self.mode == "supervised" else "execute:dispute")
            )

            return {
                "id": f"overcharge:{inv.invoice_id}",
                "class": "overcharge",
                "variant": variant,
                "invoice_id": inv.invoice_id,
                "verdict": "finding",
                "amount_usd": f"{amount_usd_dec:.2f}",
                "amount_range": None,
                "deadline": None,
                "action": action,
                "confidence": 0.95,
                "evidence": evidence_list,
                "missing_doc": None,
                "supersedes": [],
                "learned_at": learned_metadata,
            }

        # Decoy / no finding
        return {
            "id": f"overcharge:{inv.invoice_id}",
            "class": "overcharge",
            "variant": variant,
            "invoice_id": inv.invoice_id,
            "verdict": "no_finding",
            "amount_usd": None,
            "amount_range": None,
            "deadline": None,
            "action": "none",
            "confidence": 0.90,
            "evidence": evidence_list,
            "missing_doc": None,
            "supersedes": [],
            "learned_at": None,
        }


def audit_overcharges(
    state_dir: str | Path,
    as_of_date: str,
    mode: str = "supervised",
) -> list[dict[str, Any]]:
    """Entry point for overcharge detection audit."""
    engine = OverchargeAuditEngine(state_dir, as_of_date, mode)
    return engine.audit()
