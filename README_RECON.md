# BOQ vs. MIR reconciliation add-on for TELETECH CAD UI

## Install
1. Copy `server.py`, `recon_module.py`, `recon_ui.py` into the same folder (replace server.py; your original is in `backup/`).
2. `pip install openpyxl pymupdf`  (PyMuPDF is already required by the app)
3. For OCR drafts install Tesseract (Windows: UB-Mannheim build). If it is not on PATH set
   `TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe`. Optional: add `ara.traineddata` for Arabic.
   Without Tesseract everything still works; you type the MIR lines next to the page image.
4. `python server.py` -> **Documents** page -> panel **BOQ vs. MIR reconciliation**.
If the two new files are missing the app starts normally without the panel.

## What changed in server.py (104 changed lines, 1 original line modified)
import of the add-on; 2 entries in `_AUTH_ROLE_ALLOWLIST` (ADMIN/ENGINEER write; any signed-in owner may read);
`/api/recon/upload` added to the large-upload exemption; `_recon_dispatch()`; GET `/api/recon/{data,page,download}`;
POST `/api/recon/{upload,action}`; UI injection into the Documents view. Existing routes are untouched.

## Data location (per project, survives restart)
`workspace/projects/<project_id>/reconciliation/` : `state.json`, `boq/`, `mir/`, `invoice/` (source originals, never modified), `pages/`, `exports/`.

## Counting rule
A MIR line counts only if: reviewed by a person AND matched to a BOQ row AND approved AND MIR status A or B AND no unit conflict AND not a duplicate.
Status C/D, unknown status, unreviewed and unit-conflict quantities are shown separately, never added.
A MIR is an inspection request: the figure is NOT proof of delivery/installation/invoicing.
Over-BOQ quantities are never capped; remaining may be negative and is flagged.

## Update 2 (OCR fixes) - what changed
* OCR reads each page 3 times (gray / shadow-removed / contrast-enhanced) and votes; English only (Arabic pass was injecting garbage into descriptions).
* Stray punctuation/noise removed from descriptions; garbled text is flagged "description looks garbled" instead of silently trusted.
* Bilingual form (MIR_001/002) has a strip-by-strip fallback; MIR number falls back to the file name (flagged).
* Disagreeing readings of Q.T/TOTAL are kept as "other readings" and flagged.
* Review screen: "Show scan" inline, OCR guesses highlighted, "Use TOTAL as Q.T" button, unsaved edits survive list refresh.
* New warning: same material/unit/qty on different MIR numbers (possible re-submission, e.g. MIR-003 vs MIR-004).
* Identical files uploaded twice (MIR_001_1.pdf = MIR_001.pdf) are still refused by design.


## Update 2 - MIR_001 and the MIR <-> BOQ relationship

**What changed**
- `recon_vision.py` (NEW, put it next to `recon_module.py`): layout reader. It finds the printed table columns, reads MIR no. / date / description / unit / Q.T / TOTAL, and reads the A/B/C/D status from check-box ink on the new form. The previous 3-variant vote stays as fallback (used automatically if OpenCV is missing or the layout reader finds no rows).
- MIR_001 (old Arabic form) now yields a draft line: MIR-001, 23/6/2022, "CAT6A Cables", 100 ROLL, invoice 1302. Before it returned "OCR found no lines".
- Matching: MIR lines now relate to BOQ rows as one MIR line -> one BOQ row. Added: generic words (CAT6A, OM3...) are no longer treated as model numbers; a "kind of material" check (cable vs patch cord / faceplate / outlet / panel) lowers wrong candidates; a weak but clearly leading candidate (e.g. "CAT6A Cables" -> R41) is offered as *suggested* (never auto-approved). Printed supplier words that equal a BOQ brand are used as evidence only.
- OCR runs in a background thread (the web server is no longer blocked); the UI shows running page/total and refreshes itself. A job left over from a previous server run shows as "interrupted".
- Duplicates: new **Exclude as duplicate** button (reviewer decision, audited). Same item + quantity on different MIR numbers raises `possible_resubmission` (warning only, nothing excluded automatically).
- Dates are read as d/m/y only; impossible dates are dropped and uncertain ones are flagged with the alternatives.
- New flags shown in the UI: date uncertain, status read by OCR - confirm, MIR status not read, old form: check every field, rows from plain OCR, table not located.

**Files modified:** `recon_module.py`, `recon_ui.py`, `server.py` (2 small edits in `_recon_dispatch`: async `ocr`, `dup` accepts `duplicate`). **New:** `recon_vision.py`. Originals: `*.pkg1_original`.

**Verified on the real files (tests/test_recon.py: 40 PASS, 0 FAIL; export opened in LibreOffice: 0 formula errors)**
CAT6A, BOQ row R41 = 955 rolls @ 6,500 (BOQ unit and currency are not stated in the Excel, so they are flagged):
| Scenario | Approved (A/B) | Remaining | % |
|---|---|---|---|
| MIR-001 100 (A) + 002 150 (B) + 003 150 (B) + 005 298 (B) | 698 | 257 | 73.09 % |
| same, MIR-003 excluded as a re-submission of MIR-002 | 548 | 407 | 57.38 % |
MIR-004 (150, status C) is never counted. The 548 / 407 / 57.38 % figure from your example is reproduced only if MIR-003 is excluded; the system flags that pair as a possible re-submission and leaves the decision to the reviewer.

**Known limitations (honest list)**
- OCR is a draft. Old-form (MIR_001 / MIR_002) status check-boxes are NOT read automatically (pen marks cover them); the reviewer must set A/B/C. Same for MIR-007 on the new form.
- MIR_002_acssessories.pdf: the table is not located, so the page gives "no lines" and needs a manual line (Add line).
- MIR-003 / MIR-004 have a blank Q.T and TOTAL 150: use "Use TOTAL as Q.T" after checking the scan.
- Dates of MIR-006/007 and the old MIR-002 are not read (left empty rather than guessed).
- M10LE-262 faceplate has no exact BOQ row; pick the BOQ row manually.
- MGS600 outlets appear on three BOQ rows (R47/R50/R53): shown as ambiguous; reviewer chooses. One MIR line maps to one BOQ row; to split, add manual lines.
- Needs: Python 3.10+, openpyxl, pymupdf, opencv-python, numpy, Tesseract (set TESSERACT_CMD on Windows).


## Update 3 - circular BOQ summary, financial balance correction, and supplier invoices

- The BOQ executive summary is now rendered as circular progress indicators, consistent with the brand quantity rings.
- Financial calculations now use the BOQ line total as the authoritative contract value when present. If the BOQ total is present but unit price is blank, the effective rate is derived as line total / BOQ quantity. This prevents the old calculation from silently ignoring the line-total column.
- Remaining financial value is calculated per BOQ row, then aggregated by brand. It is not calculated from a mixed-unit quantity total.
- Approved invoice line amounts are used as the financial value for that BOQ row. If there is no approved invoice amount for a row, the existing MIR-derived estimate remains the fallback. Invoice values are never added on top of MIR estimates for the same row, preventing double counting.
- Invoice rows with a confirmed unit conflict are excluded until corrected. Invoice value above the BOQ row value is retained as an overrun and reported as an error; it is not capped.
- A new **فواتير التوريد** upload option accepts scanned invoice PDFs. It runs OCR (English + Arabic when the Arabic Tesseract language pack is installed), extracts likely invoice reference/date/description/unit/quantity/line amount as drafts, and uses the same manual review + BOQ match approval workflow. OCR never approves or counts an invoice automatically.
- Project Documents import now classifies PDFs whose filenames contain "invoice" or "فاتورة" as invoices; other PDFs remain MIRs.
- A one-click **استيراد BOQ + MIR + الفواتير من الريبو** action scans the repository root and imports the supplied BOQ workbook, MIR PDFs, and invoice PDFs into the selected project. Existing BOQ data is preserved; if one is already loaded, the repository BOQ is skipped rather than replacing it.
- The export now includes MIR estimate, approved invoice value, recognized financial value, remaining value, and invoice source documents. The executive and brand summary formulas use the recognized financial value.

**Important accounting rule:** MIR confirms inspection/quantity evidence; an invoice is financial evidence. If an approved invoice is matched to a BOQ row, its reviewed line amount is used for that row's financial progress. Otherwise the row falls back to the MIR-derived estimate. This is a per-row choice, not a sum of both sources.

**Current invoice fixture in this repository:** `فاتورة رقم 1 بنى سويف لامر التوريد القديم.pdf`. The application can upload it directly through the new invoice control, or import it from the project's Documents list if it has already been added there. The generic invoice OCR is conservative and must be checked against the scan before confirming the record.


## Optional GLM Vision OCR for invoices

The invoice reader can use GLM vision to read the scanned page layout directly instead of relying only on Tesseract. This is optional; if no GLM key is configured or the API call fails, the existing Tesseract pipeline remains the fallback.

Add these settings to the project `.env` file (never paste the key into the UI):

```dotenv
GLM_API_KEY=your_zai_api_key
TELETECH_INVOICE_VISION_PROVIDER=glm
TELETECH_INVOICE_GLM_MODEL=glm-4.6v
TELETECH_INVOICE_GLM_BASE_URL=https://api.z.ai/api/paas/v4
```

If your account uses a different compatible Z.AI/BigModel endpoint, set `TELETECH_INVOICE_GLM_BASE_URL` to that account's documented API base URL. The GLM result is still a draft: raw printed numeric strings are retained, arithmetic is checked, and a reviewer must confirm the line and BOQ match. The cache is separated by provider and model so switching from Gemini/OpenAI to GLM does not reuse an older provider's extraction.
