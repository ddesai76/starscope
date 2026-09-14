# STARSCOPE & Perceptor

**STARSCOPE** is a local, tabbed campaign editor for organizing aerospace test plans, requirements, and test suites. It exports to `.camp`, `.docx`, `.vcrm`, and print/PDF.

**Perceptor** (`perceptor.py`) is a lightweight standalone test card creator ("Epsilon3-lite"). It runs independently for single test cards or embedded directly inside STARSCOPE tabs.

> **Note:** Neither tool executes or tracks live tests—they structure test plans into clean, consistent document exports.

---

## Quick Start

```bash
# Run STARSCOPE (Campaign Editor) -> http://localhost:5791
python3 starscope.py [--port <n>] [--no-browser]

# Run Perceptor (Single Test Card Editor) -> http://localhost:5790
python3 perceptor.py [--port <n>] [--no-browser]
```

### Dependencies
* **Runtime:** Standard Python 3 library (`http.server`).
* **STARSCOPE Exports:** `python-docx` (`pip install python-docx`) for Word generation.
* **Development/Testing:** `pytest` (`pip install pytest`).

---

## Core Features & Workflow

### 1. STARSCOPE (Campaign Management)
* **Metadata & Requirements:** Set campaign ID (`SC-` prefix), revision, lead, and project info. Includes a manual Requirements definition table.
* **Test Series:** Numbered test list (`1, 2, 3...`). Clicking **Open** launches or creates a `.test` file in a dedicated tab.
* **Free-form Writeup Sections:** Repeating header and raw-text blocks rendered after the test list.
* **Verification Cross-Reference Matrix (VCRM):** Automatically cross-references campaign Requirements against individual test card `Requirement(s)` fields to generate a read-only coverage report (`Covered`, `Partial`, `Planned`, `Not Covered`). Exports to `.vcrm` (JSON), `.docx`, and PDF. Excludes ancillary tests.

### 2. Perceptor (Test Card Editor)
* **Card Metadata:** Project, system, test conductor, QA, date, and `Requirement(s)` mapping string (e.g., `REQ-BATT-014, REQ-BATT-021`).
* **Test Points:** Incremented by 10 (`10, 20, 30...`). Tracks action, expected result, reference, responsible party (RP), date, time, initial, and sign-off status (`RFI`, `PASS`, `CAUTION`, `FAIL`).
* **Nomenclature & References:** Supports raw LaTeX math/symbols (`\alpha`, `\Delta_max`) and cited documents.
* **Notes & Warnings:** Free-text callout boxes rendered in blue (Notes) and red (Warnings).

### 3. File Locking
* Save/export a `.test` file as **Locked** to freeze baseline content before test execution.
* **Locked behavior:** Freezes metadata, initial test points, nomenclature, and references. Sign-off fields (Date, Time, Initial, Quality) and newly inserted test points remain editable.
* STARSCOPE tabs display a 🔒 icon on locked files.

### 4. Flag, Capture & Jira Integration
* **Flag (🚩):** Highlights a test point in NVG amber during interaction without restricting edit rights.
* **Capture (📸 / 📷):** Captures single frames via `getUserMedia()` from connected webcams or UVC-compliant devices (e.g., thermal cameras). Saves images as local `<timestamp>.jpg` files attached as point thumbnails.
* **Jira Ticket Creation (🎫):**
  * Requires placing `jira_ticket.py` and `jira_config.json` in the working directory.
  * Displays a bug-creation button on test points marked `CAUTION` or `FAIL`.
  * Displays a task-creation button on Test Series rows in STARSCOPE for planning work.
  * Auto-links created tickets to a campaign's Jira Epic key if specified.

---

## Export Formats

| Scope | Format | Purpose |
| :--- | :--- | :--- |
| **STARSCOPE** | `.camp` | Native JSON campaign save file. |
| | `.docx` | Formatted Word document with tables and styled callouts. |
| | `.vcrm` | VCRM snapshot JSON file. |
| | **Print / PDF** | Browser-rendered campaign summary document. |
| **Perceptor** | `.test` | Native JSON test card save file (supports locked/unlocked state). |
| | `.csv` | Flat data dump of metadata and test points. |
| | **Print / PDF** | Formatted B612 single-page or multi-page field test card. |

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ STARSCOPE (starscope.py)                                    │
│ ├─ Serves UI (static/starscope.css)                         │
│ ├─ Generates Campaign & VCRM .docx / .camp / PDF exports     │
│ └─ Hosts embedded Test Card tabs                            │
│    └─ <iframe src="/test?load=TC-01.test">                 │
│       └─ Perceptor UI (perceptor.py + static/perceptor.css) │
└─────────────────────────────────────────────────────────────┘
```

* **Zero Duplication:** `starscope.py` imports `perceptor.py` directly to reuse its data models (`TestCard`, `TestPoint`) and HTTP handlers.
* **Isolated Dependencies:** `python-docx` is confined to STARSCOPE export endpoints. Perceptor relies strictly on the Python standard library.
* **File Architecture:** The entire suite consists of 4 main source files:
  * `starscope.py`
  * `perceptor.py`
  * `static/starscope.css`
  * `static/perceptor.css`

---

## Comparison Summary

* **vs. Epsilon3:** Epsilon3 is a cloud SaaS platform designed for live procedure execution, telemetry integration, and multi-user e-signatures. STARSCOPE/Perceptor is a local, lightweight tool designed for the *drafting and structuring phase* before execution.
* **vs. Word/Excel:** Eliminates formatting drift, re-typing of nomenclature tables, and manual VCRM cross-referencing by treating test plans as single-source JSON data that compiles into clean documents.
