#!/usr/bin/env python3
#
# starscope.py:    Test campaign creator, built on top of Perceptor
# AUTHOR:          DANIEL DESAI
# VERSION:         0.1.0
#
# STARSCOPE is Perceptor plus one more document type (Campaign) and a tab
# bar. It does not reimplement Perceptor's test-card editor -- a Campaign
# tab's "open test" action opens perceptor.py's own editor UI in an iframe
# (served at /test?load=<file>), and the HTTP handler here subclasses
# perceptor's so every existing /save, /export/*, /load, /list_files,
# /static/* route keeps working unchanged for those tabs. This file only
# adds what's new:
# the Campaign data model, the Campaign tab's UI, and Campaign-specific
# export routes (including docx, which is why this file -- unlike
# perceptor.py -- depends on python-docx).
#
"""
Single-file entry point. Run and a browser window opens with a tab bar:
tab 1 is always the Campaign editor; opening a test from the Campaign's
Tests table adds another tab, which is Perceptor's own editor pointed at
that test's .test file.

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
from datetime import date
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from docx import Document
from docx.shared import Pt, RGBColor
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

import perceptor


# ══════════════════════════════════════════════════════════════════════
#  Data model
# ══════════════════════════════════════════════════════════════════════

@dataclass
class CampaignTestRow:
    seq: int
    test_id: str = ""
    title: str = ""
    status: str = ""   # "", COMPLETE, IN_PROGRESS, TO_DO, BLOCKED


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
    # Same shape/columns as tests, rendered as its own section right after
    # it -- for tests that support the series but aren't part of its
    # numbered sequence (e.g. one-off checkouts, vendor acceptance tests).
    ancillary_tests: list = field(default_factory=list)   # list[CampaignTestRow]
    # Free-form writeup blocks, rendered after the Tests table: each is a
    # (header, text) pair. header renders as a heading; text is preserved
    # as-is (not reformatted) wherever it's exported.
    sections: list = field(default_factory=list)         # list[(header, text)]

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


# ══════════════════════════════════════════════════════════════════════
#  Export: print-ready HTML (same visual system as Perceptor's test card)
# ══════════════════════════════════════════════════════════════════════

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
        f"<tr><td>{t.seq}</td><td>{h(t.test_id)}</td><td>{h(t.title)}</td>"
        f"<td>{h(STATUS_LABELS.get(t.status, t.status))}</td></tr>"
        for t in camp.tests
    )
    anc_rows = "\n".join(
        f"<tr><td>{t.seq}</td><td>{h(t.test_id)}</td><td>{h(t.title)}</td>"
        f"<td>{h(STATUS_LABELS.get(t.status, t.status))}</td></tr>"
        for t in camp.ancillary_tests
    )
    anc_section = "" if not camp.ancillary_tests else f"""<h2>Ancillary Tests</h2>
<table>
<tr><th style="width:6%">#</th><th style="width:18%">Test ID</th><th>Title</th><th style="width:16%">Status</th></tr>
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
<tr><th style="width:6%">#</th><th style="width:18%">Test ID</th><th>Title</th><th style="width:16%">Status</th></tr>
{test_rows}
</table>
{anc_section}{sections_html}
<script>window.onload = () => window.print();</script>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════
#  Export: .docx (python-docx -- the one STARSCOPE-only dependency;
#  perceptor.py itself stays stdlib-only)
# ══════════════════════════════════════════════════════════════════════

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
        doc, ["#", "Test ID", "Title", "Status"],
        [(t.seq, t.test_id, t.title, STATUS_LABELS.get(t.status, t.status)) for t in camp.tests],
    )

    if camp.ancillary_tests:
        doc.add_heading("Ancillary Tests", level=1)
        _add_table(
            doc, ["#", "Test ID", "Title", "Status"],
            [(t.seq, t.test_id, t.title, STATUS_LABELS.get(t.status, t.status))
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


# ══════════════════════════════════════════════════════════════════════
#  Browser UI: tab bar + Campaign editor. Test tabs are perceptor.py's
#  own editor, reused unmodified via <iframe src="/test?load=...">.
# ══════════════════════════════════════════════════════════════════════

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
    </div>

    <h2 class="no-rule">Requirements</h2>
    <table class="datatable" id="reqTable">
      <tr><th>Requirement</th><th>Description</th><th class="blank-th" style="width:60px"></th></tr>
    </table>
    <div class="row"><button class="btn" id="addReqBtn">+ add requirement</button></div>

    <h2 class="no-rule">Test Series</h2>
    <table class="datatable tests-format" id="testsTable">
      <colgroup>
      <col style="width:6%"><col style="width:16%"><col style="width:34%">
      <col style="width:14%"><col style="width:30%">
      </colgroup>
      <tr><th>#</th><th>Test ID</th><th>Title</th><th>Status</th><th class="blank-th"></th></tr>
    </table>
    <div class="row"><button class="btn" id="addTestBtn">+ add test</button></div>
    <div class="row">
      <select id="browseTestFiles"><option value="">browse existing .test files...</option></select>
      <button class="btn" id="addBrowsedTestBtn">add</button>
    </div>
    <div id="browseStatus" class="hint"></div>

    <h2 class="no-rule">Ancillary Tests</h2>
    <table class="datatable tests-format" id="ancTestsTable">
      <colgroup>
      <col style="width:6%"><col style="width:16%"><col style="width:34%">
      <col style="width:14%"><col style="width:30%">
      </colgroup>
      <tr><th>#</th><th>Test ID</th><th>Title</th><th>Status</th><th class="blank-th"></th></tr>
    </table>
    <div class="row"><button class="btn" id="addAncTestBtn">+ add test</button></div>
    <div class="row">
      <select id="browseAncTestFiles"><option value="">browse existing .test files...</option></select>
      <button class="btn" id="addBrowsedAncTestBtn">add</button>
    </div>
    <div id="ancBrowseStatus" class="hint"></div>

    <h2 class="no-rule">Sections</h2>
    <div id="sectionsList"></div>
    <div class="row"><button class="btn" id="addSectionBtn">+ add section</button></div>

    <h2>Save / load / export</h2>
    <div class="row">
      <button class="btn" id="loadCampBtn">import campaign file</button>
      <input type="file" id="loadCampFile" accept=".camp,application/json" style="display:none">
      <button class="btn" id="saveCampBtn">export campaign file (.camp)</button>
      <button class="btn" id="printCampBtn">generate .pdf</button>
      <button class="btn" id="docxCampBtn">export .docx</button>
    </div>
    <div id="saveStatus"></div>
  </div>
</div>

<script>
let camp = {
  id: "SC-001", title: "Untitled Campaign", revision: "A", author: "",
  date: new Date().toISOString().slice(0,10), project: "", test_lead: "",
  quality_assurance: "", system: "", subsystem: "",
  requirements: [], tests: [], ancillary_tests: [], sections: []
};

function esc(s) { return (s??"").toString().replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

const metaFields = ["id","title","revision","author","date","project","test_lead","quality_assurance"];
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
      <td><button class="btn mini" data-del="${i}">x</button></td>`;
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

const STATUS_OPTIONS = [
  ["", "(none)"],
  ["COMPLETE", "\U0001F535 COMPLETE"],
  ["IN_PROGRESS", "\U0001F7E2 IN PROGRESS"],
  ["TO_DO", "\U0001F7E0 TO DO"],
  ["BLOCKED", "\U0001F534 BLOCKED"],
];
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
      <td><input value="${esc(row.title)}" data-i="${i}" data-f="title"></td>
      <td>${statusSelect(i, row.status)}</td>
      <td class="actions-cell">
        <button class="btn" data-open="${i}">open</button>
        <button class="btn" data-del="${i}">remove</button>
      </td>`;
    t.appendChild(tr);
  });
  t.querySelectorAll("input[data-f], select[data-f]").forEach(el => {
    const ev = el.tagName === "SELECT" ? "change" : "input";
    el.addEventListener(ev, () => { list[+el.dataset.i][el.dataset.f] = el.value; });
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
}
function renderTests() { renderTestsTable(camp.tests, "testsTable"); }
function renderAncillaryTests() { renderTestsTable(camp.ancillary_tests, "ancTestsTable"); }

document.getElementById("addTestBtn").addEventListener("click", () => {
  camp.tests.push({seq: nextSeqFor(camp.tests), test_id: "", title: "", status: ""});
  renderTests();
});
document.getElementById("addAncTestBtn").addEventListener("click", () => {
  camp.ancillary_tests.push({seq: nextSeqFor(camp.ancillary_tests), test_id: "", title: "", status: ""});
  renderAncillaryTests();
});

// Browse .test files already on disk, rather than requiring the exact
// Test ID up front -- useful for opening a file created earlier (by this
// person or a teammate sharing the working directory) that isn't yet a
// row in this campaign's Test Series or Ancillary Tests table.
async function refreshFileList(selectId) {
  const sel = document.getElementById(selectId);
  const current = sel.value;
  try {
    const res = await fetch("/list_files?ext=.test");
    const data = await res.json();
    sel.innerHTML = '<option value="">browse existing .test files...</option>' +
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
  // Grab the title too, if we can -- an empty-Title row for every file
  // someone browses in isn't very useful. Not fatal if this fails, the
  // row still gets added with a blank title to fill in by hand.
  let title = "";
  try {
    const res = await fetch("/load?file=" + encodeURIComponent(filename));
    if (res.ok) { const data = await res.json(); title = data.title || ""; }
  } catch (err) { /* leave title blank */ }
  list.push({seq: nextSeqFor(list), test_id: testId, title: title, status: ""});
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
        <button class="btn mini" data-del="${i}">x</button>
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
    bindMeta(); renderRequirements(); renderTests(); renderAncillaryTests(); renderSections();
  });
});
document.getElementById("printCampBtn").addEventListener("click", async () => {
  // Open synchronously, before the await below -- by the time a fetch()
  // resolves, the browser has lost the click's "user gesture" context and
  // many popup blockers will silently kill a window.open() called after
  // that point. Write a placeholder now, fill it in once the real content
  // arrives.
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
    panel.innerHTML = `
      <div class="test-chrome">
        <span class="fname">${esc(filename)}</span>
      </div>
      <iframe src="/test?load=${encodeURIComponent(filename)}"></iframe>`;
    document.getElementById("panels").appendChild(panel);
    const iframe = panel.querySelector("iframe");
    // The docx export button lives inside the iframe's own export row
    // (next to Perceptor's other export buttons) rather than in this
    // outer chrome bar -- inject it once the editor has finished
    // loading, since perceptor.py itself stays dependency-free and
    // doesn't define this button.
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
      // Lock icon on the tab, so a locked file is identifiable without
      // opening it. Perceptor's own ?load= fetch is async and typically
      // resolves after this iframe "load" event fires, so a one-shot
      // check here would usually see the blank pre-load default card --
      // retry briefly instead of assuming it's ready immediately.
      pollForLockIcon(iframe, filename, 10);
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

bindMeta();
camp.tests = [{seq: 1, test_id: "", title: "", status: ""}];
renderRequirements();
renderTests();
renderAncillaryTests();
renderSections();
renderTabBar();
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════
#  HTTP server -- subclasses perceptor's handler so every existing
#  /save, /export/*, /load, /list_files, /static/* route keeps working
#  unchanged for test tabs; only the Campaign-specific routes are new here.
# ══════════════════════════════════════════════════════════════════════

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
            # Perceptor's own editor, reused unmodified inside a tab's
            # iframe; ?load=<file> (read by perceptor.py's own script)
            # opens a specific .test file.
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