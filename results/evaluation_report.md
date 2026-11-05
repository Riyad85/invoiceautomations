# Evaluation report

Run: 2026-11-04 22:35 | Files: 32 | Concurrency: 3 | Database empty before run: yes

All numbers below were measured by `evaluate.py` against the live system. Nothing was edited by hand.

## Headline numbers

| Metric | Result |
|---|---|
| Field-level extraction accuracy | 81.2% (182/224 fields) |
| Documents with every field and line item correct | 78.6% (22/28) |
| Invented values (truth = not present, system gave a value) | 0 |
| Line-item recall / precision | 87.7% / 99.3% (143 of 163 true lines matched, 144 extracted) |
| Processing success rate (files that should process) | 85.7% |
| Failure rate (files that should process but failed) | 14.3% |
| Corrupt files handled correctly (marked failed, no invented data) | 4/4 |
| Final status equals expected status | 84.4% (27/32) |
| Planted-error / duplicate flags detected | 80.0% (4/5) |
| Clean documents wrongly given an error flag | 0.0% (0/23) |
| Processing time, mean / median / p90 / max | 11.6 / 8.1 / 20.8 / 39.4 seconds |

## Accuracy per field

| Field | Accuracy |
|---|---|
| invoice_number | 78.6% |
| invoice_date | 78.6% |
| supplier_name | 82.1% |
| customer_name | 82.1% |
| subtotal | 82.1% |
| tax | 82.1% |
| total | 82.1% |
| currency | 82.1% |

## Accuracy per file type

| Type | Documents | Field accuracy | Fully correct documents |
|---|---|---|---|
| csv | 2 | 100.0% | 2 |
| digital_pdf | 13 | 100.0% | 13 |
| multipage_pdf | 1 | 100.0% | 1 |
| poor_quality | 2 | 37.5% | 0 |
| scanned | 6 | 66.7% | 4 |
| scanned_pdf | 1 | 0.0% | 0 |
| xlsx | 3 | 66.7% | 2 |

## Files that did not behave as expected

- **scan_04.png** (scanned): expected `completed`, got `failed`
  - wrong fields: invoice_number (truth='INV-2026-1012', got=None); invoice_date (truth='2026-04-06', got=None); supplier_name (truth='BrightSpark Electrical', got=None); customer_name (truth='Riverside Facilities', got=None); subtotal (truth=449.0, got=None); tax (truth=89.8, got=None); total (truth=538.8, got=None); currency (truth='GBP', got=None)
  - message: OCR: Try spacing your requests out using the batching settings under 'Options'
- **scan_06.pdf** (scanned_pdf): expected `completed`, got `failed`
  - wrong fields: invoice_number (truth='INV-2026-1014', got=None); invoice_date (truth='2026-04-12', got=None); supplier_name (truth='BrightSpark Electrical', got=None); customer_name (truth='Apex HVAC Co.', got=None); subtotal (truth=896.05, got=None); tax (truth=179.21, got=None); total (truth=1075.26, got=None); currency (truth='GBP', got=None)
  - message: OCR: Try spacing your requests out using the batching settings under 'Options'
- **poor_quality_01.jpg** (poor_quality): expected `needs_review`, got `completed`
  - wrong fields: invoice_number (truth='INV-2026-1015', got='INV-2020-1013'); invoice_date (truth='2026-04-15', got='2020-04-12')
- **poor_quality_02.jpg** (poor_quality): expected `needs_review`, got `failed`
  - wrong fields: invoice_number (truth='INV-2026-1016', got=None); invoice_date (truth='2026-04-18', got=None); supplier_name (truth='Metro Pipe & Fittings', got=None); customer_name (truth='Riverside Facilities', got=None); subtotal (truth=355.0, got=None); tax (truth=71.0, got=None); total (truth=426.0, got=None); currency (truth='GBP', got=None)
  - message: OCR: Try spacing your requests out using the batching settings under 'Options'
- **dup_resend.jpg** (scanned): expected `needs_review`, got `failed`
  - wrong fields: invoice_number (truth='INV-2026-1021', got=None); invoice_date (truth='2026-05-03', got=None); supplier_name (truth='ClearAir Filters Ltd', got=None); customer_name (truth='Harbour Home Services', got=None); subtotal (truth=719.7, got=None); tax (truth=143.94, got=None); total (truth=863.64, got=None); currency (truth='GBP', got=None)
  - expected flags NOT raised: DUPLICATE_INVOICE
  - message: OCR: 503 - "{\n  \"error\": {\n    \"code\": 503,\n    \"message\": \"This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.\",\n    \"status\": \"UNAVAILABLE\"\n  }\n}\n"
- **sheet_03.xlsx** (xlsx): expected `completed`, got `completed`
  - wrong fields: invoice_number (truth='INV-2026-1024', got=None); invoice_date (truth='2026-05-12', got=None); supplier_name (truth='Northwind Cooling Ltd', got=None); customer_name (truth='Oakwood Property Group', got=None); subtotal (truth=196.7, got=None); tax (truth=39.34, got=None); total (truth=236.04, got=None); currency (truth='GBP', got=None)

## How to read these numbers (limits)

- The dataset has only 32 files and was generated synthetically, so results will be better than on messy real-world documents. Treat them as a controlled baseline, not a production guarantee.
- A field counts as correct only when it equals the label (numbers within 0.01, dates exact, names compared ignoring case and punctuation). A field the label marks as absent counts as wrong if the system returned any value.
- Documents that failed unexpectedly count every field as wrong, so failures lower accuracy.
- Time is measured by the client from the start of the upload until the status becomes final, polling every 2 seconds (about +/- 2 s resolution). It includes AI calls but not the later embedding step.
- Files were processed with limited concurrency; timings on a quiet system or paid plan will differ.