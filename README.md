# Starscope

**Starscope** is a design tool for aerospace test plans, and test suites, with Jira integration. It exports to native (.json) formats  `.camp`, `.test`, `.vcrm`, as well as .csv, .docx, and .pdf.

It uses the **Perceptor** (`perceptor.py`) module, a standalone test card creator, which can run independently for single test cards or open as a tab in Starscope.

---

## Quick Start

```bash
# Run Starscope (test campaign designer) -> http://localhost:5791
python3 starscope.py [--port <n>] [--no-browser]

# Run Perceptor (test card editor) -> http://localhost:5790
python3 perceptor.py [--port <n>] [--no-browser]
```

### Dependencies
* **Runtime:** Standard Python 3 library (`http.server`).
* **Exports:** `python-docx` (`pip install python-docx`) for Word generation.
* **Development/Testing:** `pytest` (`pip install pytest`).
* **Jira integration:** Jira account and API key.

---

## Core Features & Workflow

### 1. Starscope (Campaign)
* **Metadata & Requirements:** Set campaign ID (`SC-` prefix), revision, lead, and project info. Includes a manual Requirements definition table.
* **Test Series:** Numbered test list (`1, 2, 3...`). Clicking **Open** launches or creates a `.test` file in a dedicated tab.
* **Free-form Writeup Sections:** Repeating header and raw-text blocks rendered after the test list.
* **Verification Cross-Reference Matrix (VCRM):** Automatically cross-references campaign Requirements against individual test card `Requirement(s)` fields to generate a read-only coverage report (`Covered`, `Partial`, `Planned`, `Not Covered`). Exports to `.vcrm` (JSON), `.docx`, and PDF. Excludes ancillary tests.

### 2. Perceptor (Test Card)
* **Card Metadata:** Project, system, test conductor, QA, date, and `Requirement(s)` mapping string (e.g., `REQ-BATT-014, REQ-BATT-021`).
* **Test Points:** Incremented by 10 (`10, 20, 30...`). Tracks action, expected result, reference, responsible party (RP), date, time, initial, and sign-off status (`RFI`, `PASS`, `CAUTION`, `FAIL`).
* **Nomenclature & References:** Supports raw LaTeX math/symbols (`\alpha`, `\Delta_max`) and cited documents.
* **Notes & Warnings:** Free-text callout boxes rendered in blue (Notes) and red (Warnings).

### 3. File Locking
* Save/export a `.test` file as **Locked** to freeze baseline content before test execution.
* **Locked behavior:** Freezes metadata, initial test points, nomenclature, and references. Sign-off fields (Date, Time, Initial, Quality) and newly inserted test points remain editable.
* Starscope tabs display a lock icon on locked files.

### 4. Flag, Capture & Jira Integration
* **Flag:** Highlights a test point in NVG amber during interaction without restricting edit rights.
* **Preview/Capture:** Captures single frames via `getUserMedia()` from connected webcams or UVC-compliant devices (e.g., thermal cameras). Saves images as local `<timestamp>.jpg` files attached as point thumbnails.
* **Jira Ticket Creation:**
  * Requires placing `jira_ticket.py` and `jira_config.json` in the working directory.
  * Displays a bug-creation button on Test Points marked `CAUTION` or `FAIL`.
  * Displays a task-creation button on Test Series rows in Starscope for planning work.
  * Auto-links created tickets to a campaign's Jira Epic key if specified.

---

## Export Formats

| Format | Purpose |
|:--- | :--- |
| `.camp` | Native JSON campaign save file. |
| `.test` | Native JSON test card save file (supports locked/unlocked state). |
| `.vcrm` | VCRM snapshot JSON file. |
| `.csv` | Flat data export of metadata and test points. |
| `.docx` | Formatted Word document with tables and styled callouts. |
| **Print / PDF** | Browser-rendered campaign document or test card. |

---

## Codebase Architecture

```
├── jira_config.json
├── jira_ticket.py
├── perceptor.py
├── starscope.py
├── static
│   ├── perceptor.css
│   └── starscope.css│
```
---


