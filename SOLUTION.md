# My solution

This document outlines the architecture, operational commands, verified performance metrics, known limitations, and evaluation findings for the freight invoice audit harness.

## How to run it

The solution runs on Python 3.11+. The commands below use Windows PowerShell syntax.

### 1. Environment Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r harness/requirements.txt
```

### 2. Run Test Suite

Run the full 34-test unit and regression suite across all 6 test modules:

```powershell
$env:PYTHONPATH = "c:\nova-takehome;.venv\Lib\site-packages"
python -m pytest tests/
```

### 3. Ingest, Report, and Feedback CLI

Execute individual harness lifecycle stages:

```powershell
# Ingest batch documents into state database
python harness/harness.py ingest data/scenarios/W1/batch_01 --state-dir .state

# Generate audit report (writes JSON to stdout)
python harness/harness.py report --as-of 2035-05-31 --mode supervised --state-dir .state > out/report.json

# Ingest operator feedback decisions
python harness/harness.py feedback data/scenarios/W1/batch_01/feedback.json --state-dir .state
```

### 4. Practice Mailbox Grading

Grade the solution against the practice mailbox benchmark:

```powershell
python grader/run_grader.py --harness harness/harness.py --data data/scenarios --out-card report_card.json
```

### 5. Exam Mailbox Execution

Generate the final exam report and answer sheet:

```powershell
python tools/run_mailbox.py --data data/exam --harness harness/harness.py --out out/exam_report.json --sheet answer_sheet.json
```

### 6. Custom Evaluations

Run the evaluation test suite (14 cases):

```powershell
python evals/run_evals.py --harness harness/harness.py
```

### 7. Submission Validation

Validate that all submission requirements are met:

```powershell
$env:PYTHONPATH = "c:\nova-takehome;.venv\Lib\site-packages"; $env:PYTHONIOENCODING = "utf-8"
python tools/check_submission.py
```

## What works

The audit engine implements a deterministic pipeline covering all six contract compliance categories: overcharges, duplicate billings, waived-but-billed items, late fees and late-fee risks, cashback/volume rebates, and contract expiry windows.

### Verified Results

1. **Practice Benchmark Score**: **71.094 / 100** on `data/scenarios` (`report_card.json`).
   - **Detection F1 with evidence**: 21.053 / 25 (precision: 1.0, recall: 0.7273, F1: 0.8421; 16 true hits, 0 false positives).
   - **Amount accuracy**: 18.668 / 20 ($13,198.65 recovered of $14,140.09 at stake, $0.00 wrongly disputed).
   - **Clean controls**: 5.0 / 5.0 (16 of 16 clean controls correctly left alone).
   - **Adaptation to new documents**: 9.545 / 15 (7 of 11 adaptation events passed).
   - **Gating and confidence**: 8.495 / 15 (17 of 24 actions correct in both modes; escalation rate 5.88%).
   - **Conflict and abstention**: 5.0 / 10.
   - **Hard Safety Gates**: 5 of 5 passed (prompt injection: clear; permanent waiver: clear; supervised execute: clear; over-escalation: clear; unsafe autonomous: clear).

2. **Automated Unit & Regression Tests**: **34 of 34 passed** across the 6 test files in `tests/`:
   - `tests/test_phase2_ingest.py` (5 tests)
   - `tests/test_phase3_llm.py` (7 tests)
   - `tests/test_phase4a_overcharge.py` (5 tests)
   - `tests/test_phase4b_4f_audit.py` (6 tests)
   - `tests/test_phase5_report.py` (8 tests)
   - `tests/test_phase6b_invoice_detection.py` (3 tests)

3. **Custom Evaluation Framework**: **14 of 14 passed** (score 1.0) via `evals/run_evals.py`, comprising 12 custom YAML cases (`eval_01_*.yaml` through `eval_12_*.yaml`) and 2 sample cases.

4. **Phase 6B Exam Validation**: Validated on dedicated artifacts (`out/phase6b_exam_report.json` and `phase6b_answer_sheet.json`). All 184 citations resolve against source documents with zero missing documents. Detected **$8,355.65 in total savings** ($3,884.04 overpayments, $4,461.32 cashback rebate, $10.29 late fees avoided).

5. **Phase 6A Exam Output Preserved**: The original Phase 6A artifacts (`out/exam_report.json` and `answer_sheet.json`) remain preserved intact as the historical baseline run ($0.00 savings, 1 abstention on `CDU-3151571` due to missing Master Services Agreement).

## What doesn't

While the system is robust against false positives and unsafe actions, several known limitations exist in the current implementation:

1. **Missed Practice Findings**: In `data/scenarios/W1`, the engine misses overcharges on `TX-US-2035-0256` and `TX-US-2035-0259`, waived charges on `TX-US-2034-0383` and `TX-US-2035-0267`, and an invalid late fee on `TX-US-2035-0265`. These cases involve subtle multi-hop email chains and non-standard line descriptions that strict regex parsing did not capture.
2. **Rebate Amount Discrepancy**: On rebate finding `TBX-US-EXP-01/2035-Q1/rebate`, the engine computed $4,228.75 compared to the benchmark expectation of $4,450.07. Strict quarterly boundary filters excluded certain invoices billed near the cutoff date.
3. **Conservative Feedback Learning**: Feedback adaptation scored 3.333 / 10. The engine deliberately scopes human feedback to the specific targeted invoice to avoid violating the permanent waiver hard gate. As a result, it does not extrapolate one-off operator approvals into carrier-wide rule changes.
4. **Confidence Calibration**: The confidence metric achieved AUROC of 0.4667 and Brier score of 0.1088. Confidence values (0.95, 0.90, 0.50) are rule-assigned heuristics rather than outputs of a trained probabilistic model.
5. **Format Variations**: While regex parsing handles standard invoice headers and Caldrith's `Charges advice`, highly idiosyncratic invoice layouts or OCR errors in unstructured tables require manual review or specialized extraction templates.

## The eval case that caught a bug

- **Evaluation Case**: `evals/cases/eval_01_charges_advice_overcharge.yaml`
- **The Bug**: During the initial Phase 6A exam run, the engine reported $0.00 in financial savings and missed all carrier overcharges. Root-cause inspection revealed that the exam carrier Caldrith Logistics formatted invoices under the title `Charges advice <ID>` instead of `INVOICE <ID>`, with dates formatted as `DD/MM/YYYY`, Caldrith Air Waybills formatted as `CA\d{8}`, and amounts containing thousands separators (e.g. `1,701.31`). The initial parser in `harness/audit/overcharge.py::_parse_invoice` matched only `INVOICE <ID>` headers and ISO dates, causing the engine to skip Caldrith billing documents entirely.
- **The Fix**: Extended `_parse_invoice` and carrier detection logic across `harness/audit/` to recognize `Charges advice` headings, parse `DD/MM/YYYY` dates, normalize comma-separated decimal numbers, and associate `CA\d{8}` waybills with governing carrier agreements.
- **Validation**: Evaluation case `eval_01_charges_advice_overcharge.yaml` verified that Caldrith charges are accurately ingested and flagged for overcharge. Focused regression tests (`tests/test_phase6b_invoice_detection.py`) confirmed 3/3 passing cases, and Phase 6B validation uncovered $8,355.65 in legitimate recovery without regressing any practice scenario results.
