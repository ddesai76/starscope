#!/usr/bin/env python3

# starscope.py:   TEST CAMPAIGN DESIGNER
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-09-14
# VERSION:        0.1.1

# Subclasses perceptor's HTTP handler; test tabs reuse its editor via <iframe>.

"""
Usage
-----
    python3 starscope.py                   # opens the GUI on http://localhost:5791
    python3 starscope.py --port 8080
    python3 starscope.py --no-browser

Tests: `pytest` (or `pytest tests/test_starscope.py`) from the project root
-- also runs perceptor.py's own tests, since this file imports it directly.

Dependencies: perceptor.py (must be importable from the same directory),
and python-docx for the .docx exports.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import threading
import webbrowser
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from docx import Document
from docx.shared import Pt, RGBColor
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

import perceptor


# Data model

@dataclass
class CampaignTestRow:
    seq: int
    test_id: str = ""
    title: str = ""
    status: str = ""   # "", COMPLETE, IN_PROGRESS, TO_DO, BLOCKED
    # Pulled from the linked .test file's own Author metadata, not typed
    # here -- kept in sync opportunistically, no error if it doesn't match yet.
    author: str = ""
    # Same pattern as Author, but not Author itself: a Task may get
    # reassigned in Jira, so baking in the test's original author wouldn't stay meaningful.
    requirements: str = ""


@dataclass
class Campaign:
    id: str = "SC-001"
    title: str = "Untitled Campaign"
    revision: str = "A"
    author: str = ""
    date: str = field(default_factory=lambda: date.today().isoformat())
    project: str = ""
    test_lead: str = ""
    quality_assurance: str = ""
    system: str = ""
    subsystem: str = ""
    # Requirement code + description, same shape as a TestCard's References.
    requirements: list = field(default_factory=list)   # list[(code, description)]
    tests: list = field(default_factory=list)           # list[CampaignTestRow] -- "Test Series"
    # Same shape as tests, for tests that support the series but aren't
    # part of its numbered sequence (one-off checkouts, vendor acceptance).
    ancillary_tests: list = field(default_factory=list)   # list[CampaignTestRow]
    # Free-form writeup blocks: each a (header, text) pair, rendered after
    # the Tests table; text is preserved as-is wherever it's exported.
    sections: list = field(default_factory=list)         # list[(header, text)]
    # Optional Jira Epic every ticket from this campaign links to. Passed
    # down into each open test tab once its iframe loads -- see openTestTab() below.
    jira_epic_key: str = ""

    @staticmethod
    def from_dict(d: dict) -> "Campaign":
        tests = [CampaignTestRow(**t) for t in d.get("tests", [])]
        ancillary_tests = [CampaignTestRow(**t) for t in d.get("ancillary_tests", [])]
        base = {k: v for k, v in d.items() if k not in ("tests", "ancillary_tests")}
        return Campaign(tests=tests, ancillary_tests=ancillary_tests, **base)

    def to_dict(self) -> dict:
        return asdict(self)


STATUS_LABELS = {
    "COMPLETE": "COMPLETE", "IN_PROGRESS": "IN PROGRESS",
    "TO_DO": "TO DO", "BLOCKED": "BLOCKED",
}


def test_filename(test_id: str) -> str:
    """The .test filename a campaign's Tests-table row links to. Derived
    from Test ID rather than stored separately -- one less field to keep
    in sync."""
    return perceptor._safe_filename(test_id, ".test")


def vcrm_filename(campaign_id: str) -> str:
    return perceptor._safe_filename(campaign_id, ".vcrm")


def compute_vcrm(camp: Campaign) -> dict:
    """Verification Cross-Reference Matrix: requirement -> which Test
    Series tests verify it -> rolled-up coverage. Read-only and computed
    fresh each time from data that already exists elsewhere (the
    campaign's Requirements table, and each referenced test's own
    `requirements` field) -- nothing new to keep in sync, same "manual,
    not enforced" traceability philosophy as the rest of the app: a typo
    in either place just means no link shows up, silently, no validation.

    Ancillary Tests are deliberately excluded -- they're invoked FROM a
    Test Series test (a performance check, etc.), not an independent
    verification of a requirement in their own right.
    """
    test_cache: dict = {}

    def get_test(test_id: str):
        if test_id not in test_cache:
            path = os.path.join(os.getcwd(), test_filename(test_id))
            card = None
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        card = perceptor.TestCard.from_dict(json.loads(f.read()))
                except (OSError, json.JSONDecodeError, TypeError):
                    card = None
            test_cache[test_id] = card
        return test_cache[test_id]

    rows = []
    for req_code, req_desc in camp.requirements:
        req_code = (req_code or "").strip()
        if not req_code:
            continue
        covering = []
        for t in camp.tests:
            card = get_test(t.test_id)
            if not card:
                continue
            tokens = [tok.strip() for tok in (card.requirements or "").split(",")]
            if req_code in tokens:
                covering.append({"test_id": t.test_id, "title": t.title, "status": t.status})
        if not covering:
            coverage = "Not Covered"
        elif all(c["status"] == "COMPLETE" for c in covering):
            coverage = "Covered"
        elif any(c["status"] == "COMPLETE" for c in covering):
            coverage = "Partial"
        else:
            coverage = "Planned"
        rows.append({"requirement": req_code, "description": req_desc,
                      "covering_tests": covering, "coverage": coverage})

    return {
        "campaign_id": camp.id, "campaign_title": camp.title,
        # No 'T' separator, no seconds -- this is shown to a human as
        # "when was this generated", not parsed back programmatically.
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "rows": rows,
    }


# Export: print-ready HTML (same visual system as Perceptor's test card).

def export_campaign_print_html(camp: Campaign, host: str = "localhost") -> str:
    h = perceptor._h
    reqs = [(c, d) for c, d in camp.requirements if (c or "").strip() or (d or "").strip()]
    req_rows = "\n".join(f"<tr><td>{h(c)}</td><td>{h(d)}</td></tr>" for c, d in reqs)
    req_section = "" if not reqs else f"""<h2>Requirements</h2>
<table><tr><th style="width:20%">Requirement</th><th>Description</th></tr>
{req_rows}
</table>
"""
    test_rows = "\n".join(
        f"<tr><td>{t.seq}</td><td>{h(t.test_id)}</td><td>{h(t.title)}</td><td>{h(t.author)}</td>"
        f"<td>{h(t.requirements)}</td><td>{h(STATUS_LABELS.get(t.status, t.status))}</td></tr>"
        for t in camp.tests
    )
    anc_rows = "\n".join(
        f"<tr><td>{t.seq}</td><td>{h(t.test_id)}</td><td>{h(t.title)}</td><td>{h(t.author)}</td>"
        f"<td>{h(t.requirements)}</td><td>{h(STATUS_LABELS.get(t.status, t.status))}</td></tr>"
        for t in camp.ancillary_tests
    )
    anc_section = "" if not camp.ancillary_tests else f"""<h2>Ancillary Tests</h2>
<table>
<tr><th style="width:6%">#</th><th style="width:14%">Test ID</th><th>Title</th><th style="width:12%">Author</th><th style="width:16%">Requirements</th><th style="width:12%">Status</th></tr>
{anc_rows}
</table>
"""
    sections_html = "\n".join(
        f'<h2>{h(header)}</h2>\n<div class="section-text">{h(text)}</div>'
        for header, text in camp.sections
        if (header or "").strip() or (text or "").strip()
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{h(camp.id)} -- {h(camp.title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=B612:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="http://{host}/static/starscope.css"></head>
<body class="print-doc">
<h1>{h(camp.title)}</h1>
<div class="meta">
  <span><b>ID:</b> {h(camp.id)}</span>
  <span><b>Rev:</b> {h(camp.revision)}</span>
  <span><b>Project:</b> {h(camp.project)}</span>
  <span><b>System/Subsystem:</b> {h(camp.system)} / {h(camp.subsystem)}</span>
  <span><b>Author:</b> {h(camp.author)}</span>
</div>
<div class="meta">
  <span><b>Test Lead:</b> {h(camp.test_lead)}</span>
  <span><b>Quality Assurance:</b> {h(camp.quality_assurance)}</span>
  <span><b>Date:</b> {h(camp.date)}</span>
</div>
{req_section}<h2>Test Series</h2>
<table>
<tr><th style="width:6%">#</th><th style="width:14%">Test ID</th><th>Title</th><th style="width:12%">Author</th><th style="width:16%">Requirements</th><th style="width:12%">Status</th></tr>
{test_rows}
</table>
{anc_section}{sections_html}
<script>window.onload = () => window.print();</script>
</body></html>"""


def export_vcrm_print_html(vcrm: dict, host: str = "localhost") -> str:
    h = perceptor._h

    def covering_str(row):
        if not row["covering_tests"]:
            return "--"
        parts = [f"{c['test_id']} ({STATUS_LABELS.get(c['status'], c['status'] or '[None]')})"
                  for c in row["covering_tests"]]
        return ", ".join(parts)

    rows_html = "\n".join(
        f"<tr><td>{h(r['requirement'])}</td><td>{h(r['description'])}</td>"
        f"<td>{h(covering_str(r))}</td><td>{h(r['coverage'])}</td><td>{h(vcrm['generated'])}</td></tr>"
        for r in vcrm["rows"]
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{h(vcrm['campaign_id'])} VCRM -- {h(vcrm['campaign_title'])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=B612:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="http://{host}/static/starscope.css"></head>
<body class="print-doc">
<h1>Verification Cross-Reference Matrix</h1>
<table>
<tr><th style="width:14%">Requirement</th><th>Description</th><th style="width:24%">Covering Tests</th><th style="width:10%">Coverage</th><th style="width:12%">Updated</th></tr>
{rows_html}
</table>
<script>window.onload = () => window.print();</script>
</body></html>"""


# Export: .docx (python-docx -- the one STARSCOPE-only dependency).

_HEADER_FILL = "E7EEF5"
_WARN_FILL = "F8D7D7"
_NOTE_FILL = "E7EEF5"


def _shade_cell(cell, hex_fill: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)


def _add_table(doc: Document, headers: list, rows: list, widths=None):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for i, htext in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = htext
        cell.paragraphs[0].runs[0].bold = True
        _shade_cell(cell, _HEADER_FILL)
    for row in rows:
        cells = table.add_row().cells
        for i, val in enumerate(row):
            cells[i].text = str(val)
    return table


def _add_callout(doc: Document, text: str, hex_fill: str) -> None:
    table = doc.add_table(rows=1, cols=1)
    table.autofit = True
    cell = table.rows[0].cells[0]
    cell.text = text
    _shade_cell(cell, hex_fill)
    # No visible grid for callouts -- clear the borders python-docx's
    # default table style would otherwise draw.
    tbl = table._tbl
    tblPr = tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "nil")
        borders.append(el)
    tblPr.append(borders)


def export_campaign_docx(camp: Campaign) -> bytes:
    doc = Document()
    doc.add_heading(camp.title, level=0)
    meta = doc.add_paragraph()
    meta.add_run(
        f"ID: {camp.id}    Rev: {camp.revision}    Project: {camp.project}    "
        f"System/Subsystem: {camp.system} / {camp.subsystem}    Author: {camp.author}\n"
        f"Test Lead: {camp.test_lead}    Quality Assurance: {camp.quality_assurance}    "
        f"Date: {camp.date}"
    ).font.size = Pt(9.5)

    reqs = [(c, d) for c, d in camp.requirements if (c or "").strip() or (d or "").strip()]
    if reqs:
        doc.add_heading("Requirements", level=1)
        _add_table(doc, ["Requirement", "Description"], reqs)

    doc.add_heading("Test Series", level=1)
    _add_table(
        doc, ["#", "Test ID", "Title", "Author", "Requirements", "Status"],
        [(t.seq, t.test_id, t.title, t.author, t.requirements, STATUS_LABELS.get(t.status, t.status))
         for t in camp.tests],
    )

    if camp.ancillary_tests:
        doc.add_heading("Ancillary Tests", level=1)
        _add_table(
            doc, ["#", "Test ID", "Title", "Author", "Requirements", "Status"],
            [(t.seq, t.test_id, t.title, t.author, t.requirements, STATUS_LABELS.get(t.status, t.status))
             for t in camp.ancillary_tests],
        )

    for header, text in camp.sections:
        if not (header or "").strip() and not (text or "").strip():
            continue
        doc.add_heading(header, level=1)
        for line in (text or "").split("\n"):
            doc.add_paragraph(line)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def export_vcrm_docx(vcrm: dict) -> bytes:
    doc = Document()
    doc.add_heading("Verification Cross-Reference Matrix", level=0)

    def covering_str(row):
        if not row["covering_tests"]:
            return "--"
        parts = [f"{c['test_id']} ({STATUS_LABELS.get(c['status'], c['status'] or '[None]')})"
                  for c in row["covering_tests"]]
        return ", ".join(parts)

    _add_table(
        doc, ["Requirement", "Description", "Covering Tests", "Coverage", "Updated"],
        [(r["requirement"], r["description"], covering_str(r), r["coverage"], vcrm["generated"])
         for r in vcrm["rows"]],
    )

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def export_test_docx(card: "perceptor.TestCard") -> bytes:
    doc = Document()
    doc.add_heading(card.title, level=0)
    meta = doc.add_paragraph()
    meta.add_run(
        f"ID: {card.id}    Rev: {card.revision}    Project: {card.project}    "
        f"System/Subsystem: {card.system} / {card.subsystem}    Test Type: {card.test_type}    "
        f"Author: {card.author}\n"
        f"Test Conductor: {card.test_conductor}    Quality Assurance: {card.quality_assurance}    "
        f"Date: {card.date}    Requirement(s): {card.requirements}"
    ).font.size = Pt(9.5)

    warnings = [w for w, _locked in card.warnings if w.strip()]
    if warnings:
        doc.add_heading("Warnings", level=1)
        for w in warnings:
            _add_callout(doc, "\u26a0 " + w, _WARN_FILL)

    notes = [n for n, _locked in card.notes if n.strip()]
    if notes:
        doc.add_heading("Notes", level=1)
        for n in notes:
            _add_callout(doc, "\U0001f4dd " + n, _NOTE_FILL)

    nom = perceptor.sorted_nomenclature(card)
    if nom:
        doc.add_heading("Nomenclature", level=1)
        _add_table(
            doc, ["Symbol", "Definition", "Units"],
            [(perceptor.symbol_display(s), d, u) for s, d, u, _locked in nom],
        )

    refs = [(r, d) for r, d, _locked in card.references if (r or "").strip() or (d or "").strip()]
    if refs:
        doc.add_heading("References", level=1)
        _add_table(doc, ["Reference", "Description"], refs)

    doc.add_heading("Test Points", level=1)
    _add_table(
        doc,
        ["#", "Action", "Expected Result", "Observed Result", "Reference", "Responsible Party",
         "Date", "Time", "Initial", "Quality"],
        [(p.seq, p.action, p.expected, p.observed, p.reference, p.responsible_party,
          p.date, p.time, p.initial, p.quality) for p in card.points],
    )

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# Browser UI: tab bar + Campaign editor; test tabs reuse perceptor.py's editor via <iframe>.

_CAMPAIGN_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>\U0001F52D STARSCOPE</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=B612+Mono:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/starscope.css">
</head>
<body class="editor-doc">
<div id="tabbar"></div>
<div id="panels">
  <div class="panel active" id="panel-campaign">
    <h1>STARSCOPE</h1>
    <div class="sub">Test Campaign Designer</div>

    <h2>Campaign metadata</h2>
    <div class="meta">
      <div class="field"><label>ID</label><input id="c-id"></div>
      <div class="field"><label>Title</label><input id="c-title"></div>
      <div class="field"><label>Revision</label><input id="c-revision"></div>
      <div class="field"><label>Project</label><input id="c-project"></div>
      <div class="field"><label>Author</label><input id="c-author"></div>
      <div class="field"><label>Test Lead</label><input id="c-test_lead"></div>
      <div class="field"><label>Quality Assurance</label><input id="c-quality_assurance"></div>
      <div class="field"><label>Date</label><input id="c-date"></div>
      <div class="field"><label>System / Subsystem</label><input id="c-system"></div>
      <div class="field" id="jira-epic-field" style="display:none"><label>Jira Epic Key</label><input id="c-jira_epic_key" placeholder="PROJ-142"></div>
    </div>

    <h2 class="no-rule">Requirements</h2>
    <table class="datatable" id="reqTable">
      <tr><th>Requirement</th><th>Description</th><th class="blank-th" style="width:60px"></th></tr>
    </table>
    <div class="row"><button class="btn btn-plus" id="addReqBtn" title="add requirement">+</button></div>

    <h2 class="no-rule">Test Series</h2>
    <table class="datatable tests-format" id="testsTable">
      <colgroup>
      <col style="width:4%"><col style="width:10%"><col style="width:33%">
      <col style="width:12%"><col style="width:12%"><col style="width:9%"><col style="width:20%">
      </colgroup>
      <tr><th>#</th><th>Test ID</th><th>Title</th><th>Author</th><th>Requirements</th><th>Status</th><th class="blank-th"></th></tr>
    </table>
    <div class="row"><button class="btn btn-plus" id="addTestBtn" title="add test">+</button></div>
    <div class="row">
      <select id="browseTestFiles"><option value="">Browse existing .test files...</option></select>
      <button class="btn" id="addBrowsedTestBtn">select</button>
    </div>
    <div id="browseStatus" class="hint"></div>

    <h2 class="no-rule">Ancillary Tests</h2>
    <table class="datatable tests-format" id="ancTestsTable">
      <colgroup>
      <col style="width:4%"><col style="width:10%"><col style="width:33%">
      <col style="width:12%"><col style="width:12%"><col style="width:9%"><col style="width:20%">
      </colgroup>
      <tr><th>#</th><th>Test ID</th><th>Title</th><th>Author</th><th>Requirements</th><th>Status</th><th class="blank-th"></th></tr>
    </table>
    <div class="row"><button class="btn btn-plus" id="addAncTestBtn" title="add test">+</button></div>
    <div class="row">
      <select id="browseAncTestFiles"><option value="">Browse existing .test files...</option></select>
      <button class="btn" id="addBrowsedAncTestBtn">select</button>
    </div>
    <div id="ancBrowseStatus" class="hint"></div>

    <h2 class="no-rule">Sections</h2>
    <div id="sectionsList"></div>
    <div class="row"><button class="btn btn-plus" id="addSectionBtn" title="add section">+</button></div>

    <h2>Save / load / export</h2>
    <div class="row">
      <button class="btn" id="loadCampBtn">import campaign file</button>
      <input type="file" id="loadCampFile" accept=".camp,application/json" style="display:none">
      <button class="btn" id="saveCampBtn">export campaign file (.camp)</button>
      <button class="btn" id="printCampBtn">generate .pdf</button>
      <button class="btn" id="docxCampBtn">export .docx</button>
      <button class="btn" id="genVcrmBtn">generate VCRM</button>
    </div>
    <div id="saveStatus"></div>
  </div>
</div>

<script>
let camp = {
  id: "SC-001", title: "Untitled Campaign", revision: "A", author: "",
  date: new Date().toISOString().slice(0,10), project: "", test_lead: "",
  quality_assurance: "", system: "", subsystem: "", jira_epic_key: "",
  requirements: [], tests: [], ancillary_tests: [], sections: []
};

// True only if jira_ticket.py was found in the working directory at startup.
const JIRA_AVAILABLE = __JIRA_AVAILABLE__;
if (JIRA_AVAILABLE) document.getElementById("jira-epic-field").style.display = "";

function esc(s) { return (s??"").toString().replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

const metaFields = ["id","title","revision","author","date","project","test_lead","quality_assurance","jira_epic_key"];
function bindMeta() {
  metaFields.forEach(f => {
    const el = document.getElementById("c-" + f);
    el.value = camp[f];
    el.addEventListener("input", () => {
      camp[f] = el.value;
      if (f === "id" || f === "title") renderTabBar();
    });
  });
  const sysEl = document.getElementById("c-system");
  sysEl.value = [camp.system, camp.subsystem].filter(Boolean).join(" / ");
  sysEl.addEventListener("input", () => {
    const parts = sysEl.value.split("/").map(s => s.trim());
    camp.system = parts[0] || ""; camp.subsystem = parts[1] || "";
  });
}

function renderRequirements() {
  const t = document.getElementById("reqTable");
  t.querySelectorAll("tr.datarow").forEach(r => r.remove());
  camp.requirements.forEach(([code, desc], i) => {
    const tr = document.createElement("tr");
    tr.className = "datarow";
    tr.innerHTML = `
      <td><input value="${esc(code)}" data-i="${i}" data-f="0" placeholder="REQ-1234"></td>
      <td><input value="${esc(desc)}" data-i="${i}" data-f="1" placeholder="description"></td>
      <td><button class="btn mini btn-x" data-del="${i}">x</button></td>`;
    t.appendChild(tr);
  });
  t.querySelectorAll("input[data-i]").forEach(inp => {
    inp.addEventListener("input", () => { camp.requirements[+inp.dataset.i][+inp.dataset.f] = inp.value; });
  });
  t.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => { camp.requirements.splice(+btn.dataset.del, 1); renderRequirements(); });
  });
}
document.getElementById("addReqBtn").addEventListener("click", () => {
  camp.requirements.push(["", ""]);
  renderRequirements();
});

// Google Material Symbols, embedded inline (not a file/CDN) so they work
// fully offline; fill="currentColor" picks up each button's own hover color.
const ICON_OPEN_IN_NEW = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M200-120q-33 0-56.5-23.5T120-200v-560q0-33 23.5-56.5T200-840h280v80H200v560h560v-280h80v280q0 33-23.5 56.5T760-120H200Zm188-212-56-56 372-372H560v-80h280v280h-80v-144L388-332Z"/></svg>';
const ICON_CLOSE = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="m256-200-56-56 224-224-224-224 56-56 224 224 224-224 56 56-224 224 224 224-56 56-224-224-224 224Z"/></svg>';
const ICON_BOOKMARK_ADD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M200-120v-640q0-33 23.5-56.5T280-840h240v80H280v518l200-86 200 86v-278h80v400L480-240 200-120Zm80-640h240-240Zm400 160v-80h-80v-80h80v-80h80v80h80v80h-80v80h-80Z"/></svg>';

const STATUS_OPTIONS = [
  ["", "[None]"],
  ["COMPLETE", "\U0001F535 COMPLETE"],
  ["IN_PROGRESS", "\U0001F7E2 IN PROGRESS"],
  // No native colored-circle emoji is genuinely magenta -- purple is the
  // closest a real Unicode circle gets.
  ["TO_DO", "\U0001F7E3 TO DO"],
  ["BLOCKED", "\U0001F534 BLOCKED"],
];
// Plain-text labels (no colored dot -- that's UI-only, never shown in the
// VCRM or any export), matching STATUS_LABELS in starscope.py.
const STATUS_LABELS = {
  COMPLETE: "COMPLETE", IN_PROGRESS: "IN PROGRESS", TO_DO: "TO DO", BLOCKED: "BLOCKED",
};
function statusSelect(i, current) {
  const opts = STATUS_OPTIONS.map(([v, label]) =>
    `<option value="${v}"${v === current ? " selected" : ""}>${label}</option>`).join("");
  return `<select data-i="${i}" data-f="status">${opts}</select>`;
}

function nextSeqFor(list) {
  const max = list.reduce((m, t) => Math.max(m, +t.seq || 0), 0);
  return max + 1;   // campaigns number tests 1, 2, 3... not by 10s
}

function renderTestsTable(list, tableId) {
  const t = document.getElementById(tableId);
  t.querySelectorAll("tr.datarow").forEach(r => r.remove());
  list.forEach((row, i) => {
    const tr = document.createElement("tr");
    tr.className = "datarow";
    tr.innerHTML = `
      <td><input value="${esc(row.seq)}" data-i="${i}" data-f="seq"></td>
      <td><input value="${esc(row.test_id)}" data-i="${i}" data-f="test_id" placeholder="TC-001"></td>
      <td class="meta-cell">${esc(row.title || "")}</td>
      <td class="meta-cell">${esc(row.author || "")}</td>
      <td class="meta-cell">${esc(row.requirements || "")}</td>
      <td>${statusSelect(i, row.status)}</td>
      <td class="actions-cell">
        <button class="btn btn-icon" data-open="${i}" title="open">${ICON_OPEN_IN_NEW}</button>
        <button class="btn btn-icon" data-del="${i}" title="remove">${ICON_CLOSE}</button>
        ${JIRA_AVAILABLE ? `<button class="btn btn-icon" data-jira="${i}" title="create Jira ticket">${ICON_BOOKMARK_ADD}</button>` : ""}
      </td>`;
    t.appendChild(tr);
  });
  t.querySelectorAll("input[data-f], select[data-f]").forEach(el => {
    const ev = el.tagName === "SELECT" ? "change" : "input";
    el.addEventListener(ev, () => {
      list[+el.dataset.i][el.dataset.f] = el.value;
    });
  });
  // Title/Author/Requirements are pulled from the linked .test file --
  // refresh on blur (not every keystroke) so focus isn't yanked mid-type.
  t.querySelectorAll('input[data-f="test_id"]').forEach(el => {
    el.addEventListener("blur", () => backfillMetadataForRow(list, tableId, +el.dataset.i));
  });
  t.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => { list.splice(+btn.dataset.del, 1); renderTestsTable(list, tableId); });
  });
  t.querySelectorAll("button[data-open]").forEach(btn => {
    btn.addEventListener("click", () => {
      const row = list[+btn.dataset.open];
      if ((row.test_id || "").trim()) openTestTab(row.test_id, row.title);
    });
  });
  t.querySelectorAll("button[data-jira]").forEach(btn => {
    btn.addEventListener("click", () => createTestJiraTicket(list, tableId, +btn.dataset.jira));
  });
}
function renderTests() { renderTestsTable(camp.tests, "testsTable"); }
function renderAncillaryTests() { renderTestsTable(camp.ancillary_tests, "ancTestsTable"); }

// Reuses the browse-picker's own status line for ticket-click results,
// rather than adding a third message area -- the two actions never overlap.
const TICKET_STATUS_EL_FOR_TABLE = { testsTable: "browseStatus", ancTestsTable: "ancBrowseStatus" };

async function createTestJiraTicket(list, tableId, i) {
  const row = list[i];
  const statusEl = document.getElementById(TICKET_STATUS_EL_FOR_TABLE[tableId]);
  const summary = `${row.test_id || "(no ID)"}: ${row.title || "(no title)"}`;
  const description = [
    `Test: ${row.test_id || ""} -- ${row.title || ""}`,
    `Status: ${STATUS_LABELS[row.status] || row.status || "[None]"}`,
    `Requirement(s): ${row.requirements || ""}`,
  ].join("\\n");
  statusEl.textContent = "Creating ticket...";
  try {
    const res = await fetch("/create_jira_ticket", {
      method: "POST",
      body: JSON.stringify({
        summary, description, issue_type: "Task",
        epic_key: camp.jira_epic_key || "",
      }),
    });
    const result = await res.json();
    if (result.success) {
      statusEl.innerHTML = `Created <a href="${esc(result.url)}" target="_blank" rel="noopener">${esc(result.key)}</a>`;
    } else {
      statusEl.textContent = `Ticket creation failed: ${result.error || "unknown error"}`;
    }
  } catch (err) {
    statusEl.textContent = `Ticket creation failed: ${err}`;
  }
}

async function backfillMetadataForRow(list, tableId, i) {
  const row = list[i];
  const testId = (row.test_id || "").trim();
  if (!testId) return;
  const filename = testId.replace(/[^A-Za-z0-9._-]/g, "_") + ".test";
  try {
    const res = await fetch("/load?file=" + encodeURIComponent(filename));
    if (!res.ok) return;   // no matching file yet -- leave title/author/requirements as-is, silently
    const data = await res.json();
    row.title = data.title || "";
    row.author = data.author || "";
    row.requirements = data.requirements || "";
    renderTestsTable(list, tableId);
  } catch (err) {
    // transient error -- same "silent, no validation" spirit as the rest
    // of this traceability; nothing here is load-bearing.
  }
}

document.getElementById("addTestBtn").addEventListener("click", () => {
  camp.tests.push({seq: nextSeqFor(camp.tests), test_id: "", title: "", author: "", requirements: "", status: ""});
  renderTests();
});
document.getElementById("addAncTestBtn").addEventListener("click", () => {
  camp.ancillary_tests.push({seq: nextSeqFor(camp.ancillary_tests), test_id: "", title: "", author: "", requirements: "", status: ""});
  renderAncillaryTests();
});

// Browse .test files already on disk, rather than requiring the exact
// Test ID up front -- for a file created earlier by this person or a teammate.
async function refreshFileList(selectId) {
  const sel = document.getElementById(selectId);
  const current = sel.value;
  try {
    const res = await fetch("/list_files?ext=.test");
    const data = await res.json();
    sel.innerHTML = '<option value="">Browse existing .test files...</option>' +
      (data.files || []).map(f => `<option value="${esc(f)}">${esc(f)}</option>`).join("");
    if ((data.files || []).includes(current)) sel.value = current;
  } catch (err) {
    document.getElementById(selectId === "browseTestFiles" ? "browseStatus" : "ancBrowseStatus")
      .textContent = `Couldn't list files: ${err}`;
  }
}
async function addBrowsedFile(selectId, list, tableId, statusId) {
  const sel = document.getElementById(selectId);
  const filename = sel.value;
  const status = document.getElementById(statusId);
  if (!filename) return;
  const testId = filename.replace(/\\.test$/, "");
  if (list.some(t => t.test_id === testId)) {
    status.textContent = `${testId} is already in this table.`;
    return;
  }
  // Grab title/author/requirements too, if we can -- not fatal if this
  // fails, the row just gets added with those left blank to fill in by hand.
  let title = "", author = "", requirements = "";
  try {
    const res = await fetch("/load?file=" + encodeURIComponent(filename));
    if (res.ok) {
      const data = await res.json();
      title = data.title || ""; author = data.author || ""; requirements = data.requirements || "";
    }
  } catch (err) { /* leave title/author/requirements blank */ }
  list.push({seq: nextSeqFor(list), test_id: testId, title: title, author: author,
             requirements: requirements, status: ""});
  renderTestsTable(list, tableId);
}
document.getElementById("browseTestFiles").addEventListener("focus", () => refreshFileList("browseTestFiles"));
document.getElementById("addBrowsedTestBtn").addEventListener("click", () =>
  addBrowsedFile("browseTestFiles", camp.tests, "testsTable", "browseStatus"));
document.getElementById("browseAncTestFiles").addEventListener("focus", () => refreshFileList("browseAncTestFiles"));
document.getElementById("addBrowsedAncTestBtn").addEventListener("click", () =>
  addBrowsedFile("browseAncTestFiles", camp.ancillary_tests, "ancTestsTable", "ancBrowseStatus"));
refreshFileList("browseTestFiles");
refreshFileList("browseAncTestFiles");

function renderSections() {
  const list = document.getElementById("sectionsList");
  list.innerHTML = "";
  camp.sections.forEach(([header, text], i) => {
    const div = document.createElement("div");
    div.className = "sec-card";
    div.innerHTML = `
      <div class="sec-head">
        <input value="${esc(header)}" data-i="${i}" data-f="0" placeholder="Section header">
        <button class="btn mini btn-x" data-del="${i}">x</button>
      </div>
      <textarea data-i="${i}" data-f="1" placeholder="Write or paste anything here">${esc(text)}</textarea>`;
    list.appendChild(div);
  });
  list.querySelectorAll("input[data-i], textarea[data-i]").forEach(el => {
    el.addEventListener("input", () => { camp.sections[+el.dataset.i][+el.dataset.f] = el.value; });
  });
  list.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => { camp.sections.splice(+btn.dataset.del, 1); renderSections(); });
  });
}
document.getElementById("addSectionBtn").addEventListener("click", () => {
  camp.sections.push(["", ""]);
  renderSections();
});

function download(filename, blobOrText, mime) {
  const blob = blobOrText instanceof Blob ? blobOrText : new Blob([blobOrText], {type: mime || "text/plain"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
}

document.getElementById("saveCampBtn").addEventListener("click", async () => {
  const status = document.getElementById("saveStatus");
  status.textContent = "saving...";
  try {
    const res = await fetch("/save_campaign", {method: "POST", body: JSON.stringify(camp)});
    const data = await res.json();
    status.textContent = data.saved ? `Saved to ${data.path}` : `Save failed: ${data.error || "unknown error"}`;
  } catch (err) {
    status.textContent = `Save failed: ${err}`;
  }
});
document.getElementById("loadCampBtn").addEventListener("click", () => {
  document.getElementById("loadCampFile").click();
});
document.getElementById("loadCampFile").addEventListener("change", (e) => {
  const f = e.target.files[0]; if (!f) return;
  f.text().then(txt => {
    camp = JSON.parse(txt);
    if (!camp.requirements) camp.requirements = [];
    if (!camp.tests) camp.tests = [];
    if (!camp.ancillary_tests) camp.ancillary_tests = [];
    if (!camp.sections) camp.sections = [];
    if (!camp.jira_epic_key) camp.jira_epic_key = "";
    bindMeta(); renderRequirements(); renderTests(); renderAncillaryTests(); renderSections();
  });
});
document.getElementById("printCampBtn").addEventListener("click", async () => {
  // Open synchronously, before the await below -- popup blockers can kill
  // a window.open() called after a fetch() resolves.
  const w = window.open("", "_blank");
  if (w) w.document.write("Generating...");
  try {
    const res = await fetch("/export/campaign/print", {method: "POST", body: JSON.stringify(camp)});
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const text = await res.text();
    if (w) { w.document.open(); w.document.write(text); w.document.close(); }
  } catch (err) {
    if (w) { w.document.open(); w.document.write("Failed to generate: " + err); w.document.close(); }
    document.getElementById("saveStatus").textContent = `Print failed: ${err}`;
  }
});
document.getElementById("docxCampBtn").addEventListener("click", async () => {
  const status = document.getElementById("saveStatus");
  try {
    const res = await fetch("/export/campaign/docx", {method: "POST", body: JSON.stringify(camp)});
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const blob = await res.blob();
    download((camp.id || "campaign") + ".docx", blob);
  } catch (err) {
    status.textContent = `Docx export failed: ${err}`;
  }
});

// ---- Tab bar ----
let openTabs = [];   // [{key, label}], key = filename for test tabs
let activeTab = "campaign";

function renderTabBar() {
  const bar = document.getElementById("tabbar");
  bar.innerHTML = "";
  const campBtn = document.createElement("button");
  campBtn.className = "tab-btn" + (activeTab === "campaign" ? " active" : "");
  campBtn.textContent = camp.id || "Campaign";
  campBtn.addEventListener("click", () => switchTab("campaign"));
  bar.appendChild(campBtn);
  openTabs.forEach(tab => {
    const btn = document.createElement("button");
    btn.className = "tab-btn" + (activeTab === tab.key ? " active" : "");
    btn.innerHTML = `<span>${esc(tab.label || tab.key)}</span><span class="close-x" data-close="${esc(tab.key)}">x</span>`;
    btn.addEventListener("click", (e) => {
      if (e.target.dataset.close) { closeTestTab(e.target.dataset.close); return; }
      switchTab(tab.key);
    });
    bar.appendChild(btn);
  });
}

function switchTab(key) {
  activeTab = key;
  document.getElementById("panel-campaign").classList.toggle("active", key === "campaign");
  openTabs.forEach(tab => {
    const panel = document.getElementById("panel-" + tab.key);
    if (panel) panel.classList.toggle("active", key === tab.key);
  });
  renderTabBar();
}

function openTestTab(testId, title) {
  const filename = testId.replace(/[^A-Za-z0-9._-]/g, "_") + ".test";
  if (!openTabs.some(t => t.key === filename)) {
    openTabs.push({key: filename, label: testId});
    const panel = document.createElement("div");
    panel.className = "test-panel";
    panel.id = "panel-" + filename;
    panel.innerHTML = `<iframe src="/test?load=${encodeURIComponent(filename)}"></iframe>`;
    document.getElementById("panels").appendChild(panel);
    const iframe = panel.querySelector("iframe");
    // The docx export button lives inside the iframe's own export row --
    // injected once the editor finishes loading, since perceptor.py stays dependency-free.
    iframe.addEventListener("load", () => {
      const idoc = iframe.contentDocument;
      const row = idoc && idoc.getElementById("exportRow");
      if (row && !row.querySelector("#exportDocxBtn")) {
        const btn = idoc.createElement("button");
        btn.className = "btn";
        btn.id = "exportDocxBtn";
        btn.textContent = "export .docx";
        btn.addEventListener("click", async () => {
          const card = iframe.contentWindow.getCard ? iframe.contentWindow.getCard() : null;
          if (!card) { alert("Test editor isn't ready yet -- try again in a moment."); return; }
          const res = await fetch("/export/test/docx", {method: "POST", body: JSON.stringify(card)});
          const blob = await res.blob();
          download((card.id || "test") + ".docx", blob);
        });
        row.appendChild(btn);
      }
      // Perceptor's own ?load= fetch is async and typically resolves
      // after this "load" event fires, so retry briefly rather than assume it's ready.
      pollForLockIcon(iframe, filename, 10);
      // The epic key is a plain variable Perceptor sets synchronously at
      // script top, so it's already there by the time "load" fires -- no retry needed.
      if (iframe.contentWindow) {
        iframe.contentWindow.starscopeJiraEpicKey = camp.jira_epic_key || "";
      }
    });
  }
  switchTab(filename);
}

function pollForLockIcon(iframe, filename, attemptsLeft) {
  if (attemptsLeft <= 0) return;
  const iCard = iframe.contentWindow.getCard ? iframe.contentWindow.getCard() : null;
  if (iCard && iCard.locked) {
    const tab = openTabs.find(t => t.key === filename);
    const lockIcon = "\U0001F512";
    if (tab && !tab.label.startsWith(lockIcon)) {
      tab.label = lockIcon + " " + tab.label;
      renderTabBar();
    }
    return;
  }
  setTimeout(() => pollForLockIcon(iframe, filename, attemptsLeft - 1), 200);
}

function closeTestTab(key) {
  openTabs = openTabs.filter(t => t.key !== key);
  const panel = document.getElementById("panel-" + key);
  if (panel) panel.remove();
  if (activeTab === key) switchTab("campaign");
  else renderTabBar();
}

// A single reserved tab, regenerated in place rather than duplicated on
// repeat clicks. Read-only: shows a computed snapshot, not edited in the UI.
const VCRM_KEY = "__vcrm__";
let currentVcrm = null;

function vcrmCoveringStr(row) {
  if (!row.covering_tests.length) return "--";
  return row.covering_tests.map(c => `${c.test_id} (${STATUS_LABELS[c.status] || c.status || "[None]"})`).join(", ");
}

async function generateVcrm() {
  const status = document.getElementById("saveStatus");
  try {
    const res = await fetch("/vcrm", {method: "POST", body: JSON.stringify(camp)});
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    currentVcrm = await res.json();
  } catch (err) {
    status.textContent = `VCRM generation failed: ${err}`;
    return;
  }

  if (!openTabs.some(t => t.key === VCRM_KEY)) {
    openTabs.push({key: VCRM_KEY, label: "VCRM"});
  }
  let panel = document.getElementById("panel-" + VCRM_KEY);
  if (!panel) {
    panel = document.createElement("div");
    panel.className = "panel";
    panel.id = "panel-" + VCRM_KEY;
    document.getElementById("panels").appendChild(panel);
  }
  const rows = currentVcrm.rows.map(r => `
    <tr>
      <td>${esc(r.requirement)}</td>
      <td>${esc(r.description)}</td>
      <td>${esc(vcrmCoveringStr(r))}</td>
      <td>${esc(r.coverage)}</td>
      <td>${esc(currentVcrm.generated)}</td>
    </tr>`).join("");
  panel.innerHTML = `
    <h1 style="visibility:hidden">STARSCOPE</h1>
    <div class="sub" style="visibility:hidden">Test Campaign Designer</div>
    <h2 class="no-rule">Verification Cross-Reference Matrix</h2>
    <table class="datatable">
      <tr><th>Requirement</th><th>Description</th><th>Covering Tests</th><th>Status</th><th>Updated</th></tr>
      ${rows || '<tr><td colspan="5">No requirements entered on this campaign yet.</td></tr>'}
    </table>
    <div class="row">
      <button class="btn" id="saveVcrmBtn">export .vcrm file</button>
      <button class="btn" id="printVcrmBtn">generate .pdf</button>
      <button class="btn" id="docxVcrmBtn">export .docx</button>
    </div>
    <div id="vcrmStatus" class="hint"></div>`;

  const vcrmStatus = panel.querySelector("#vcrmStatus");
  panel.querySelector("#saveVcrmBtn").addEventListener("click", async () => {
    try {
      const res = await fetch("/save_vcrm", {method: "POST", body: JSON.stringify(currentVcrm)});
      const data = await res.json();
      vcrmStatus.textContent = data.saved ? `Saved to ${data.path}` : `Save failed: ${data.error || "unknown error"}`;
    } catch (err) {
      vcrmStatus.textContent = `Save failed: ${err}`;
    }
  });
  panel.querySelector("#printVcrmBtn").addEventListener("click", async () => {
    // Open synchronously, before the await below -- see the same note on
    // printCampBtn above for why.
    const w = window.open("", "_blank");
    if (w) w.document.write("Generating...");
    try {
      const res = await fetch("/export/vcrm/print", {method: "POST", body: JSON.stringify(currentVcrm)});
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      const text = await res.text();
      if (w) { w.document.open(); w.document.write(text); w.document.close(); }
    } catch (err) {
      if (w) { w.document.open(); w.document.write("Failed to generate: " + err); w.document.close(); }
      vcrmStatus.textContent = `Print failed: ${err}`;
    }
  });
  panel.querySelector("#docxVcrmBtn").addEventListener("click", async () => {
    try {
      const res = await fetch("/export/vcrm/docx", {method: "POST", body: JSON.stringify(currentVcrm)});
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      const blob = await res.blob();
      download((currentVcrm.campaign_id || "vcrm") + ".docx", blob);
    } catch (err) {
      vcrmStatus.textContent = `Docx export failed: ${err}`;
    }
  });

  switchTab(VCRM_KEY);
}
document.getElementById("genVcrmBtn").addEventListener("click", generateVcrm);

bindMeta();
camp.tests = [{seq: 1, test_id: "", title: "", author: "", requirements: "", status: ""}];
renderRequirements();
renderTests();
renderAncillaryTests();
renderSections();
renderTabBar();
</script>
</body>
</html>
"""
# Reuses perceptor's already-computed flag rather than checking again --
# availability doesn't depend on which of the two apps is asking.
_CAMPAIGN_HTML = _CAMPAIGN_HTML.replace(
    "__JIRA_AVAILABLE__", "true" if perceptor.JIRA_AVAILABLE else "false")


# HTTP server -- subclasses perceptor's handler so its routes keep working for test tabs.

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class _Handler(perceptor._Handler):
    def _read_campaign(self) -> Campaign:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        return Campaign.from_dict(json.loads(body))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send(200, "text/html", _CAMPAIGN_HTML.encode())
        elif path == "/test":
            # Perceptor's own editor, reused unmodified; ?load=<file> opens a specific .test file.
            self._send(200, "text/html", perceptor._INDEX_HTML.encode())
        else:
            # /load, /list_files, /static/*, and 404s all handled the
            # same as when perceptor.py runs standalone.
            super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/save_campaign":
            try:
                camp = self._read_campaign()
            except (json.JSONDecodeError, TypeError) as e:
                self._send_json(400, {"error": str(e)})
                return
            filename = perceptor._safe_filename(camp.id, ".camp")
            out_path = os.path.join(os.getcwd(), filename)
            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(camp.to_dict(), f, indent=2)
            except OSError as e:
                self._send_json(500, {"saved": False, "error": str(e)})
                return
            self._send_json(200, {"saved": True, "filename": filename, "path": out_path})
        elif path == "/export/campaign/print":
            try:
                camp = self._read_campaign()
            except (json.JSONDecodeError, TypeError) as e:
                self._send_json(400, {"error": str(e)})
                return
            self._send(200, "text/html",
                       export_campaign_print_html(camp, self.headers.get("Host", "localhost")).encode())
        elif path == "/export/campaign/docx":
            try:
                camp = self._read_campaign()
            except (json.JSONDecodeError, TypeError) as e:
                self._send_json(400, {"error": str(e)})
                return
            self._send(200, _DOCX_MIME, export_campaign_docx(camp))
        elif path == "/export/test/docx":
            try:
                card = self._read_card()
            except (json.JSONDecodeError, TypeError) as e:
                self._send_json(400, {"error": str(e)})
                return
            self._send(200, _DOCX_MIME, export_test_docx(card))
        elif path == "/vcrm":
            try:
                camp = self._read_campaign()
            except (json.JSONDecodeError, TypeError) as e:
                self._send_json(400, {"error": str(e)})
                return
            self._send_json(200, compute_vcrm(camp))
        elif path == "/save_vcrm":
            length = int(self.headers.get("Content-Length", 0))
            try:
                vcrm = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": str(e)})
                return
            filename = vcrm_filename(vcrm.get("campaign_id", "VCRM"))
            out_path = os.path.join(os.getcwd(), filename)
            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(vcrm, f, indent=2)
            except OSError as e:
                self._send_json(500, {"saved": False, "error": str(e)})
                return
            self._send_json(200, {"saved": True, "filename": filename, "path": out_path})
        elif path == "/export/vcrm/print":
            length = int(self.headers.get("Content-Length", 0))
            try:
                vcrm = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": str(e)})
                return
            self._send(200, "text/html",
                       export_vcrm_print_html(vcrm, self.headers.get("Host", "localhost")).encode())
        elif path == "/export/vcrm/docx":
            length = int(self.headers.get("Content-Length", 0))
            try:
                vcrm = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": str(e)})
                return
            self._send(200, _DOCX_MIME, export_vcrm_docx(vcrm))
        else:
            # /save, /export/csv, /export/print for test tabs.
            super().do_POST()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=5791)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--selftest", action="store_true",
                    help="deprecated: tests moved to tests/, run `pytest` instead")
    args = p.parse_args()

    if args.selftest:
        print("Tests moved out of this file -- run `pytest` (or `pytest tests/test_starscope.py`) instead.")
        return 1

    url = f"http://localhost:{args.port}"
    print(f"Serving GUI at {url}")
    print("Press Ctrl+C to stop\n")

    server = ThreadingHTTPServer(("localhost", args.port), _Handler)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        print("Shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
