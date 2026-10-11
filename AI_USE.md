# AI use

This document details the AI tools, models, workflows, human oversight, and reconstructed prompt logs utilized throughout this project.

## Tools

1. **Antigravity IDE**: Interactive agentic pair-programming environment powered by Anthropic Claude 3.7 Sonnet. Used for codebase navigation, scaffolding, implementation of ingestion and audit modules, test generation, debugging, and documentation authoring.
2. **OpenAI ChatGPT (GPT-4o)**: Used during early exploratory analysis for architectural brainstorming around logistics audit workflows and document extraction strategies.

## How you worked with them

The development followed a pair-programming model where AI served as an implementation accelerator under direct human supervision:

- **Scaffolding and Implementation**: AI assisted in drafting initial SQLite schemas, regular expression parsers, and audit detectors across `harness/audit/`.
- **Test Generation**: AI was tasked with generating unit test fixtures and YAML evaluation cases covering contract compliance edge cases (e.g. split shipments, extended due dates, volume rebate tiers).
- **Human Verification and Correction**:
  - *Hard Safety Gates*: AI repeatedly proposed broader feedback propagation rules that would have applied single-invoice delivery fee waivers across entire contract rate cards. The human developer intervened, enforcing strict invoice-level scoping to protect the permanent-waiver hard gate.
  - *Regex Brittle Assumptions*: In Phase 6A, the initial AI-assisted parser assumed all invoices bore `INVOICE <ID>` headers and ISO dates, causing the initial exam run to miss Caldrith Logistics' `Charges advice` documents. The human developer diagnosed the root cause, directed the Phase 6B carrier format fix, and verified that original Phase 6A artifacts remained preserved.
  - *Financial Precision*: The human engineer mandated and verified the use of `decimal.Decimal` with `ROUND_HALF_UP` for all calculations, rejecting standard floating-point operations.

## Written without AI

The following components were conceived, verified, or executed independently without AI generation:

1. **Independent Architectural Reviews**: Comprehensive read-only audits conducted before Phase 4, Phase 5, and Phase 6 to verify gate compliance, citation provenance, and score integrity.
2. **Mathematical Spot-Checks**: Manual calculation and verification of Annex F currency conversions, fuel surcharge percentage additions, and quarterly volume rebate tier calculations against raw documents.
3. **Artifact Preservation Strategy**: The decision and protocol to freeze the Phase 6A exam report and answer sheet as the immutable original baseline, creating dedicated Phase 6B validation artifacts.
4. **Safety Policy Design**: Specification of the five-part unattended action gating rules and the supervised-mode execution lockout.

## Prompt log

*Note: The following log is a concise, faithful reconstruction of the chronological prompts, instructions, and iterative steering used across Phases 1 through 8.*

### Phase 1: Ingestion and Fact Model
- **Prompt**: "Inspect the sample documents in data/scenarios. Implement `harness/extract.py` and `harness/db.py` to parse .eml, .pdf, and .xlsx files into a local SQLite database under `--state-dir`. Store raw text, line offsets, page indices, and document metadata. Ensure every extracted record maintains exact citation provenance."
- **Review/Edit**: Verified that page indexing correctly mapped multi-page PDFs and attachments without losing textual offsets.

### Phase 2: Ingestion Unit Tests & Scanned PDF OCR
- **Prompt**: "Implement unit tests in `tests/test_phase2_ingest.py` testing email parsing, PDF text extraction, Excel sheets, and scanned image-only PDF OCR using RapidOCR."
- **Review/Edit**: Confirmed local RapidOCR integration successfully extracted text lines from scanned image-only PDFs.

### Phase 3: Provider-Agnostic LLM Interface & Safety Gates
- **Prompt**: "Implement `harness/llm.py` with `AnthropicProvider` and `GroqProvider` supporting schema validation and boundary prompt isolation for untrusted documents. Enforce that supervised mode never executes outbound actions. Write unit tests in `tests/test_phase3_llm.py`."
- **Review/Edit**: Verified client classes and prompt isolation; confirmed that the runtime audit engine remains purely deterministic without runtime LLM dependency.

### Phase 4A: Overcharge Detection Engine
- **Prompt**: "Implement `harness/audit/overcharge.py` for freight overcharge detection. Compare billed line items against governing contract base rates, fuel surcharges, and currency conversions using `decimal.Decimal`. Output exact citations and diffs. Add unit tests in `tests/test_phase4a_overcharge.py`."
- **Review/Edit**: Audited rounding rules (`ROUND_HALF_UP`) and pickup-date pricing lookups.

### Phase 4B–4F: Remaining Audit Categories
- **Prompt**: "Implement the remaining audit engines in `harness/audit/`: `duplicate.py` (exact and near-duplicate invoices), `waiver.py` (waived-but-billed charges), `late_fee.py` (invalid late fees and upcoming risks within 14 days), `cashback.py` (quarterly volume rebates), and `expiry.py` (contract termination notice windows). Write unit tests in `tests/test_phase4b_4f_audit.py`."
- **Review/Edit**: Corrected duplicate detector to allow legitimate split shipments sharing the same master AWB with different sub-identifiers.

### Phase 5: Conflicts, Abstention, and Full Assembly
- **Prompt**: "Implement cross-document conflict detection and report assembly in `harness/harness.py`. When rate documents of equal authority contradict, emit a conflict with an `amount_range` and abstain. When key contracts are missing, emit a `cannot_determine` finding with `missing_doc` and `holder`. Implement the five-part autonomous action gating test. Add unit tests in `tests/test_phase5_report.py`."
- **Review/Edit**: Tested practice scenarios W1 batch 01 and 02. Verified that practice grader achieved 71.094/100 with zero hard-gate violations.

### Phase 6A: Exam Mailbox Run
- **Prompt**: "Resolve the Python environment isolation issue where subprocesses lose site-packages when PYTHONPATH is stripped. Run the exam mailbox once: `python tools/run_mailbox.py --data data/exam --harness harness/harness.py --out out/exam_report.json --sheet answer_sheet.json`. Review outputs."
- **Review/Edit**: Diagnosed that the exam run yielded $0.00 in findings and 1 abstention. Human review froze `out/exam_report.json` and `answer_sheet.json` as the original baseline run.

### Phase 6B: Carrier Invoice Format Fix
- **Prompt**: "Investigate why exam carrier Caldrith Logistics invoices were missed. Update `_parse_invoice` in `harness/audit/overcharge.py` and other detectors to support `Charges advice <ID>`, European `DD/MM/YYYY` dates, and `CA\d{8}` waybills. Validate without overwriting the Phase 6A exam artifacts. Write regression tests in `tests/test_phase6b_invoice_detection.py`."
- **Review/Edit**: Verified that all 3 tests in `tests/test_phase6b_invoice_detection.py` passed and separate output files (`out/phase6b_exam_report.json`, `phase6b_answer_sheet.json`) recovered $8,355.65 with 184/184 citations resolving.

### Phase 7: Custom Evaluation Framework
- **Prompt**: "Inspect `evals/run_evals.py`. Create 12 custom YAML evaluation cases in `evals/cases/` covering carrier format variations, negative controls, split shipments, waiver scope boundaries, extended due dates, and action gating. Execute `evals/run_evals.py` and record results."
- **Review/Edit**: Confirmed all 14 evaluation cases (12 custom, 2 sample) passed with a 1.0 score in `evals/results.json`.

### Phase 8: Final Documentation and Submission Validation
- **Prompt**: "Complete `DESIGN.md` (under 1,000 words across 8 exact headings), `SOLUTION.md` (PowerShell commands, verified metrics, bug report across 4 exact headings), and `AI_USE.md` (tools, workflow, non-AI contributions, prompt log across 4 exact headings). Run `python tools/check_submission.py` to achieve 9 of 9 passing checks."
- **Review/Edit**: Audited documentation against actual implementation, verified exact test counts and filenames, and confirmed that all validator checks pass.
