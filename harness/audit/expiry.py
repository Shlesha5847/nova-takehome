"""Contract expiry and notice deadline audit engine for the Nova freight audit harness.

Detects contracts whose operative deadline (end date or non-renewal notice deadline)
is within 0 to 60 days of the report as_of date (EX-01), placing them in portfolio.expiry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from harness import db


@dataclass
class ContractExpiryInfo:
    doc_id: str
    contract_id: str
    end_date: str
    auto_renews: bool
    notice_days: int
    quote: str
    page: int


class ExpiryAuditEngine:
    """Deterministic audit engine for contract expiry alerts."""

    def __init__(self, state_dir: str | Path, as_of_date: str, mode: str = "supervised"):
        self.state_dir = Path(state_dir)
        self.as_of_date = as_of_date
        self.mode = mode

        self.contracts: list[ContractExpiryInfo] = []

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

            # Parse Master Services Agreements
            if doc_type == "pdf" and "Master Services Agreement" in first_page and "Amendment" not in first_page:
                c_m = re.search(r"Agreement No\.\s+([A-Z0-9\-]+)", first_page) or re.search(r"TBX-[A-Z0-9\-]+", first_page)
                if not c_m:
                    continue
                contract_id = c_m.group(0).split()[-1]

                for p_idx, p_text in enumerate(pages, start=1):
                    if "2.2 Initial term." in p_text:
                        end_date: str | None = None
                        quote = ""
                        for l in p_text.splitlines():
                            if "2.2 Initial term." in l or "ending on" in l:
                                date_m = re.search(r"ending on\s+(\d{4}-\d{2}-\d{2})", l)
                                if date_m:
                                    end_date = date_m.group(1)
                                    quote = l.strip()
                                    break

                        auto_renews = False
                        notice_days = 0
                        for l in p_text.splitlines():
                            if "2.3 Auto-renewal." in l or "renews automatically" in l.lower():
                                if "does not renew automatically" in l.lower():
                                    auto_renews = False
                                else:
                                    auto_renews = True
                                    notice_m = re.search(r"at least\s+(\d+)\s+days", l)
                                    if notice_m:
                                        notice_days = int(notice_m.group(1))
                                break

                        if end_date and quote:
                            self.contracts.append(
                                ContractExpiryInfo(
                                    doc_id=doc_id,
                                    contract_id=contract_id,
                                    end_date=end_date,
                                    auto_renews=auto_renews,
                                    notice_days=notice_days,
                                    quote=quote,
                                    page=p_idx,
                                )
                            )
                        break

    def audit(self) -> list[dict[str, Any]]:
        self.load_data()
        alerts: list[dict[str, Any]] = []
        as_of_dt = datetime.strptime(self.as_of_date, "%Y-%m-%d")

        for c in self.contracts:
            end_dt = datetime.strptime(c.end_date, "%Y-%m-%d")

            # Determine operative deadline
            if c.auto_renews and c.notice_days > 0:
                operative_dt = end_dt - timedelta(days=c.notice_days)
            else:
                operative_dt = end_dt

            operative_date_str = operative_dt.strftime("%Y-%m-%d")
            days_diff = (operative_dt - as_of_dt).days

            # Alert window: 0 to 60 days
            if 0 <= days_diff <= 60:
                evidence = [
                    {
                        "doc": c.doc_id,
                        "page": c.page,
                        "quote": c.quote,
                    }
                ]

                alerts.append({
                    "id": f"expiry:{c.contract_id}",
                    "class": "expiry",
                    "variant": "EX-01",
                    "contract_id": c.contract_id,
                    "verdict": "finding",
                    "amount_usd": None,
                    "amount_range": None,
                    "deadline": operative_date_str,
                    "action": "internal:alert",
                    "confidence": 0.95,
                    "evidence": evidence,
                    "missing_doc": None,
                    "supersedes": [],
                    "learned_at": None,
                })

        return alerts


def audit_expiry(
    state_dir: str | Path,
    as_of_date: str,
    mode: str = "supervised",
) -> list[dict[str, Any]]:
    """Entry point for contract expiry audit. Returns portfolio.expiry items."""
    engine = ExpiryAuditEngine(state_dir, as_of_date, mode)
    return engine.audit()
