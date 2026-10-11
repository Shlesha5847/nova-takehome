"""Common data models, parsers, and utilities for the Nova freight audit harness."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def round_half_up(val: Decimal, places: int = 2) -> Decimal:
    """Round a Decimal half up (ties away from zero) to the given decimal places."""
    q = Decimal("10") ** -places
    return val.quantize(q, rounding=ROUND_HALF_UP)


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
class APRegisterEntry:
    invoice_id: str
    invoice_date: str
    due_date: str
    amount_usd: Decimal
    status: str  # 'paid', 'open', 'scheduled'
    paid_date: str | None
    quote: str
    doc_id: str
    page: int


def parse_ap_register(pages: list[str], doc_id: str) -> list[APRegisterEntry]:
    """Parse AP register rows from Excel sheet page text."""
    entries: list[APRegisterEntry] = []
    for page_idx, page_text in enumerate(pages, start=1):
        for line in page_text.splitlines():
            line_str = line.strip()
            if not line_str or "Invoice no." in line_str:
                continue
            parts = [p.strip() for p in line_str.split("|")]
            if len(parts) >= 5:
                raw_inv_id = parts[0]
                inv_id = raw_inv_id.upper().replace(" ", "").replace("#", "").replace(":", "")
                inv_date = parts[1]
                # Due date often prefixed with 'due '
                due_str = parts[2].replace("due ", "").strip()
                amt_str = parts[3].replace("USD", "").replace("INR", "").strip()
                status = parts[4].lower()
                paid_date = parts[5].strip() if len(parts) > 5 and parts[5].strip() else None

                try:
                    amt = Decimal(amt_str)
                except Exception:
                    amt = Decimal("0.00")

                entries.append(
                    APRegisterEntry(
                        invoice_id=inv_id,
                        invoice_date=inv_date,
                        due_date=due_str,
                        amount_usd=amt,
                        status=status,
                        paid_date=paid_date,
                        quote=line_str,
                        doc_id=doc_id,
                        page=page_idx,
                    )
                )
    return entries


def parse_annex_e_mapping(pages: list[str]) -> dict[str, tuple[str, str, int]]:
    """Map charge labels to (charge_code, quote_line, page_number) using Annex E."""
    label_to_code: dict[str, tuple[str, str, int]] = {}
    for page_idx, page_text in enumerate(pages, start=1):
        if "Annex E: Charge codes" in page_text or "Annex E Charge codes" in page_text:
            for line in page_text.splitlines():
                line_str = line.strip()
                m = re.search(r"Code\s+([A-Z]+):\s*([^;]+)(?:;\s*also billed as:\s*(.*))?", line_str)
                if m:
                    code = m.group(1).strip()
                    primary = m.group(2).strip()
                    aliases_str = m.group(3) or ""
                    aliases = [a.strip() for a in aliases_str.split(",") if a.strip()]

                    label_to_code[primary.lower()] = (code, line_str, page_idx)
                    for a in aliases:
                        label_to_code[a.lower()] = (code, line_str, page_idx)
    return label_to_code
