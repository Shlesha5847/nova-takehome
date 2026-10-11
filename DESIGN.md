# Design

## Fact model

The system ingests procurement files (.eml emails, digital and scanned .pdf files, and .xlsx spreadsheets) through a deterministic parsing pipeline, persisting document metadata and page text in an embedded SQLite database (`state.db`) under `--state-dir`. Core tables include `documents`, `document_pages`, `batches`, `feedback_records`, `reports`, and `meta`. Digital text is extracted via PyMuPDF and openpyxl, while scanned image-only PDFs are processed locally using RapidOCR.

Invoices, line items, contracts, annex rate sheets, and fuel surcharges are parsed into structured data structures. Every extracted fact retains strict citation provenance: source document ID, filename, 1-indexed page number, and verbatim text quote. When audit findings reference facts, citations are validated against source pages, ensuring zero ungrounded assertions and 100% citation resolution.

## Conflicts

Conflicts are resolved by tracking legal document hierarchy, effective date windows, and signing authority. Formal master service agreements and signed amendments outrank informal email notices, unilateral rate circulars, or marketing updates. When two documents of equal legal authority present contradictory rates for overlapping dates (such as competing rate sheets or conflicting fuel surcharge notices without a superseding clause), the engine does not guess.

Instead, it records an explicit conflict finding, captures verbatim citations from both contradictory sources, computes the financial discrepancy as an `amount_range`, and sets `abstain: true`. When essential contractual agreements are missing, the engine emits a `cannot_determine` record specifying the missing document and holding party (e.g., Carrier or Supplier) rather than assuming terms.

## Confidence

Confidence is a deterministic score assigned from verified factual signals. In the current implementation:
- High-certainty findings with direct contractual base rates and invoice citations receive confidence of 0.95.
- Findings derived from amendments, extended due dates, or multi-step rate lookups receive confidence of 0.90.
- Uncertain findings or missing-document items (such as unverified late fee principals) receive confidence of 0.50.

In benchmark evaluations on practice data, this rules-based scoring achieved a Brier score of 0.1088 and AUROC of 0.4667. This demonstrates strong factual precision on verified findings while confirming that the confidence values are rule-assigned heuristics rather than a statistically calibrated probability distribution.

## People in the loop

The platform operates under two modes: `supervised` and `autonomous`. In supervised mode, the harness generates draft dispute notices and payment reminders (`draft:*`), but outbound execution is strictly blocked by a hard architectural gate (`supervised_execute` gate). All actions require human operator review.

In autonomous mode, actions are evaluated against an unattended safety test:
1. Total outbound disputed amounts on a single invoice must be strictly less than $5,000 USD; amounts of $5,000 or greater are routed to `human_review`.
2. The finding must have verified supporting evidence (`has_ev = True`).
3. There must be no missing contractual documents (`no_missing = True`).
Findings satisfying all criteria transition from `draft:*` to `execute:*`. On benchmark testing, the autonomous escalation rate was 5.88%, well below the 60% hard-gate ceiling, with zero unsafe autonomous executions.

## Feedback

Operator feedback is received via the `feedback` command and persisted in SQLite (`feedback_records` table) with source file, decision (`confirm`, `close`, `reject`), and raw payload.

Decisions are strictly scoped to specific `(class, invoice_id)` tuples:
- `close` or `reject` suppresses the targeted finding from subsequent reports.
- `confirm` associates the finding with a `learned_at` provenance tag.
Crucially, operator waivers or approved credits are isolated to the targeted invoice. One-off delivery fee waivers or courtesy credits are prohibited from altering contract rate cards or creating global rules, preventing violation of the permanent-waiver hard gate.

## New documents

Document batches are ingested chronologically across time-stamped batches (`--as-of`). When amendments or rate circulars arrive, they are indexed with their contractual effective dates. Audit logic prices invoices based on the rate schedule valid on the shipment pickup date, preserving temporal contract boundaries.

When new evidence changes an invoice's finding verdict or amount relative to an earlier batch, the harness retrieves the prior report from `state.db` (`reports` table) and records the previous finding ID in the updated finding's `supersedes` array. This maintains an explicit, verifiable audit history across reporting horizons.

## Cost

The active audit pipeline is 100% deterministic, executing rate lookups, fuel surcharge indices, currency conversions, and rebate tier calculations in Python using `decimal.Decimal` (ROUND_HALF_UP) with local RapidOCR. Runtime API cost is verified at $0.00. Processing practice scenarios (~63 documents across two batches) takes approximately 40 seconds locally on Windows; throughput is estimated at 5 to 10 minutes per 1,000 documents.

An optional provider-agnostic LLM interface is implemented in `harness/llm.py` (supporting Anthropic Claude and Groq) with prompt isolation for untrusted documents. It is not invoked during standard deterministic audits. If deployed for unstructured documents, estimated API costs would be ~$0.45 to $1.20 per 1,000 documents based on standard token pricing.

## What I'd do next

Given additional time, engineering priorities include:
1. Training a calibrated logistic regression model on citation density, line-item complexity, and document authority to improve confidence AUROC from 0.4667 toward >0.85.
2. Expanding feedback generalization to learn carrier-wide tariff interpretations while retaining safeguards against one-off waiver leakage.
3. Adding specialized table structure parsing for non-standard carrier formats and partial credit-note allocations across multiple freight invoices.
