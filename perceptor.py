#!/usr/bin/env python3

# perceptor.py:   TEST CARD EDITOR
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-09-14
# VERSION:        0.1.1
# 
# Single stdlib file; client-side editor state, server only handles exports.

"""
Usage
-----
    python3 perceptor.py                   # open GUI on http://localhost:5790
    python3 perceptor.py --port 8080
    python3 perceptor.py --no-browser

Tests: `pytest` (or `pytest tests/test_perceptor.py`) from the project root.

Dependencies: stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import io
import importlib.util
import json
import os
import re
import sys
import threading
import webbrowser
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


# Data model

@dataclass
class TestPoint:
    seq: int
    action: str = ""
    expected: str = ""
    # What was actually observed when the point was run -- user-entered,
    # stays editable even on a locked point since this is execution data.
    observed: str = ""
    reference: str = ""
    responsible_party: str = ""
    date: str = ""
    time: str = ""        # 24h "HH:MM" -- filled by the UI's "now" button
    initial: str = ""
    quality: str = ""
    # True if this point existed at the moment the card was last exported
    # locked -- see the "Locking" note on TestCard below.
    locked: bool = False
    # Independent of locked -- always toggleable, recolors the point
    # number and whatever fields are currently editable.
    flagged: bool = False
    # Filenames only (see /capture_image); files live in the working
    # directory alongside .test/.camp files, not the image data itself.
    images: list = field(default_factory=list)


def _normalize_pair_list(items: list, width: int) -> list:
    """Backward-compat + shape normalization for nomenclature/references/
    notes/warnings: pads each row to `width` elements (the last of which is
    always the `locked` flag), and accepts a bare string in place of a
    single-element row (old files stored notes/warnings as plain strings
    before locking existed)."""
    out = []
    for item in items:
        row = [item] if isinstance(item, str) else list(item)
        while len(row) < width - 1:
            row.append("")
        if len(row) < width:
            row.append(False)
        out.append(row[:width])
    return out


@dataclass
class TestCard:
    id: str = "TC-001"
    title: str = "Untitled Test Card"
    revision: str = "A"
    author: str = ""
    date: str = field(default_factory=lambda: date.today().isoformat())
    project: str = ""
    test_conductor: str = ""
    quality_assurance: str = ""
    system: str = ""
    subsystem: str = ""
    test_type: str = ""
    # Free-text requirement code(s) this test verifies. Deliberately
    # unstructured -- tracing coverage is a manual, unenforced responsibility.
    requirements: str = ""
    points: list = field(default_factory=list)         # list[TestPoint]
    # [symbol, definition, units, locked]. Symbol may be raw LaTeX (e.g.
    # "\alpha") -- see GREEK_UNICODE / _latex_symbol for how it renders.
    nomenclature: list = field(default_factory=list)
    # Reference documents cited by test points: list of [ref id, description, locked].
    references: list = field(default_factory=list)
    # Card-level callouts, Epsilon3-style: free-text notes and warnings,
    # each rendered as its own table on the printed card.
    notes: list = field(default_factory=list)       # list[[text, locked]]
    warnings: list = field(default_factory=list)     # list[[text, locked]]
    # A workflow marker, not security (plain JSON, hand-editable). Exporting
    # locked freezes every current row and restricts further edits to a few fields.
    locked: bool = False

    @staticmethod
    def from_dict(d: dict) -> "TestCard":
        pts = [TestPoint(**p) for p in d.get("points", [])]
        nom = [tuple(x) for x in _normalize_pair_list(d.get("nomenclature", []), 4)]
        refs = _normalize_pair_list(d.get("references", []), 3)
        notes = _normalize_pair_list(d.get("notes", []), 2)
        warnings = _normalize_pair_list(d.get("warnings", []), 2)
        base = {k: v for k, v in d.items()
                if k not in ("points", "nomenclature", "references", "notes", "warnings")}
        return TestCard(points=pts, nomenclature=nom, references=refs,
                         notes=notes, warnings=warnings, **base)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d



# Nomenclature -- manual entries only (an autoscan approach didn't scan well in practice).

def sorted_nomenclature(card: TestCard) -> list:
    """card.nomenclature as a list of (symbol, definition, units, locked),
    alphabetized. Tolerates old rows missing units and/or the locked flag."""
    rows = [(r[0], r[1], r[2] if len(r) > 2 else "", r[3] if len(r) > 3 else False)
            for r in card.nomenclature]
    return sorted(rows, key=lambda row: row[0].lower())


# LaTeX Greek-letter commands -> Unicode, for the print/HTML view (the
# .tex export uses the LaTeX command itself, so it doesn't need this).
GREEK_UNICODE = {
    "alpha": "\u03b1", "beta": "\u03b2", "gamma": "\u03b3", "delta": "\u03b4",
    "epsilon": "\u03b5", "zeta": "\u03b6", "eta": "\u03b7", "theta": "\u03b8",
    "iota": "\u03b9", "kappa": "\u03ba", "lambda": "\u03bb", "mu": "\u03bc",
    "nu": "\u03bd", "xi": "\u03be", "pi": "\u03c0", "rho": "\u03c1",
    "sigma": "\u03c3", "tau": "\u03c4", "upsilon": "\u03c5", "phi": "\u03c6",
    "chi": "\u03c7", "psi": "\u03c8", "omega": "\u03c9",
    "Gamma": "\u0393", "Delta": "\u0394", "Theta": "\u0398", "Lambda": "\u039b",
    "Xi": "\u039e", "Pi": "\u03a0", "Sigma": "\u03a3", "Upsilon": "\u03a5",
    "Phi": "\u03a6", "Psi": "\u03a8", "Omega": "\u03a9",
}
_GREEK_CMD_RE = re.compile(r"\\([A-Za-z]+)")


def symbol_display(sym: str) -> str:
    """Render a nomenclature symbol for CSV/HTML: LaTeX Greek commands
    (\\alpha, \\Delta_max) become their Unicode letter; anything else
    (V_max, CG) passes through unchanged."""
    return _GREEK_CMD_RE.sub(lambda m: GREEK_UNICODE.get(m.group(1), m.group(0)), sym or "")


# Export: CSV

def export_csv(card: TestCard) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Title", "Revision", "Project", "Test Conductor",
                "Quality Assurance", "Author", "Date", "System", "Subsystem", "Test Type",
                "Requirement(s)"])
    w.writerow([card.id, card.title, card.revision, card.project, card.test_conductor,
                card.quality_assurance, card.author, card.date, card.system,
                card.subsystem, card.test_type, card.requirements])
    w.writerow([])
    w.writerow(["Seq", "Action", "Expected Result", "Observed Result", "Reference",
                "Responsible Party", "Date", "Time", "Initial", "Quality"])
    for p in card.points:
        w.writerow([p.seq, p.action, p.expected, p.observed, p.reference,
                    p.responsible_party, p.date, p.time, p.initial, p.quality])
    return buf.getvalue()


# Export: print-ready HTML (browser print-to-PDF, no new dependency).

def _h(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def export_print_html(card: TestCard, host: str = "localhost") -> str:
    rows = "\n".join(
        f"<tr><td>{p.seq}</td><td>{_h(p.action)}</td><td>{_h(p.expected)}</td>"
        f"<td>{_h(p.observed)}</td><td>{_h(p.reference)}</td>"
        f"<td>{_h(p.responsible_party)}</td><td>{_h(p.date)}</td><td>{_h(p.time)}</td>"
        f"<td>{_h(p.initial)}</td><td>{_h(p.quality)}</td></tr>"
        for p in card.points
    )
    nom = sorted_nomenclature(card)
    nom_rows = "\n".join(
        f"<tr><td>{_h(symbol_display(sym))}</td><td>{_h(defn)}</td><td>{_h(units)}</td></tr>"
        for sym, defn, units, _locked in nom
    )
    nom_section = "" if not nom else f"""<h2>Nomenclature</h2>
<table><tr><th style="width:15%">Symbol</th><th>Definition</th><th style="width:15%">Units</th></tr>
{nom_rows}
</table>
"""
    refs = [(r, d) for r, d, _locked in card.references if (r or "").strip() or (d or "").strip()]
    ref_rows = "\n".join(f"<tr><td>{_h(r)}</td><td>{_h(d)}</td></tr>" for r, d in refs)
    ref_section = "" if not refs else f"""<h2>References</h2>
<table><tr><th style="width:20%">Reference</th><th>Description</th></tr>
{ref_rows}
</table>
"""
    warnings = [w for w, _locked in card.warnings if w.strip()]
    warn_bars = "\n".join(f'<div class="warn-bar">\u26a0 {_h(w)}</div>' for w in warnings)
    warn_section = "" if not warnings else f"""<h2>Warnings</h2>
{warn_bars}
"""
    notes = [n for n, _locked in card.notes if n.strip()]
    note_bars = "\n".join(f'<div class="note-bar">\U0001f4dd {_h(n)}</div>' for n in notes)
    note_section = "" if not notes else f"""<h2>Notes</h2>
{note_bars}
"""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{_h(card.id)} -- {_h(card.title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=B612:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="http://{host}/static/perceptor.css"></head>
<body class="print-doc">
<h1>{_h(card.title)}</h1>
<div class="meta">
  <span><b>ID:</b> {_h(card.id)}</span>
  <span><b>Rev:</b> {_h(card.revision)}</span>
  <span><b>Project:</b> {_h(card.project)}</span>
  <span><b>System/Subsystem:</b> {_h(card.system)} / {_h(card.subsystem)}</span>
  <span><b>Test Type:</b> {_h(card.test_type)}</span>
  <span><b>Requirement(s):</b> {_h(card.requirements)}</span>
  <span><b>Author:</b> {_h(card.author)}</span>
</div>
<div class="meta">
  <span><b>Test Conductor:</b> {_h(card.test_conductor)}</span>
  <span><b>Quality Assurance:</b> {_h(card.quality_assurance)}</span>
  <span><b>Date:</b> {_h(card.date)}</span>
</div>
{warn_section}{note_section}{nom_section}{ref_section}<h2>Test Points</h2>
<table class="test-points">
<colgroup>
<col style="width:4%"><col style="width:16%"><col style="width:16%"><col style="width:16%">
<col style="width:10%"><col style="width:8%"><col style="width:8%"><col style="width:6%">
<col style="width:6%"><col style="width:10%">
</colgroup>
<tr><th>#</th><th>Action</th><th>Expected Result</th><th>Observed Result</th><th>Reference</th>
<th>RP</th><th>Date</th><th>Time</th><th>Initial</th><th>Quality</th></tr>
{rows}
</table>
<script>window.onload = () => window.print();</script>
</body></html>"""


# Export: AIAA-format LaTeX -- suppressed from the UI for now, route left in place.

_LATEX_SPECIAL = {
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}", "\\": r"\textbackslash{}",
}
_LATEX_RE = re.compile("|".join(re.escape(k) for k in _LATEX_SPECIAL))


def latex_escape(s: str) -> str:
    return _LATEX_RE.sub(lambda m: _LATEX_SPECIAL[m.group(0)], s or "")


def _latex_symbol(sym: str) -> str:
    # Renders as real LaTeX math for a raw command (\alpha) or a plain
    # subscripted var (V_max); anything else (CG, PID) stays literal text.
    m = re.match(r"^(\\[A-Za-z]+)(?:_([A-Za-z0-9]+))?$", sym or "")
    if m:
        base, sub = m.group(1), m.group(2)
        return f"${base}_{{{sub}}}$" if sub else f"${base}$"
    if "_" in sym and re.match(r"^[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9]+$", sym):
        base, sub = sym.split("_", 1)
        return f"${base}_{{{sub}}}$"
    return latex_escape(sym)


def export_tex(card: TestCard) -> str:
    nom = sorted_nomenclature(card)
    nom_lines = "\n".join(
        f"{_latex_symbol(sym)} \\> = \\> {latex_escape(defn) or 'TBD'}"
        f"{' [' + latex_escape(units) + ']' if units else ''} \\\\"
        for sym, defn, units, _locked in nom
    )
    row_lines = "\n".join(
        f"{p.seq} & {latex_escape(p.action)} & {latex_escape(p.expected)} & "
        f"{latex_escape(p.reference)} \\\\"
        for p in card.points
    )
    # Plain article class + AIAA-style structure; swap in \documentclass{aiaa}
    # below if that LaTeX class is installed.
    return f"""% {card.id} -- {card.title}, generated by perceptor.py
% AIAA-style test plan document. Intended to be imported into
% LibreOffice (LaTeX extension) and extended after the test is run.
\\documentclass[11pt]{{article}}
\\usepackage[margin=1in]{{geometry}}
\\usepackage{{booktabs}}
\\usepackage{{array}}
\\usepackage{{amsmath,amssymb}}

\\title{{{latex_escape(card.title)} --- Test Plan}}
\\author{{{latex_escape(card.author)}}}
\\date{{{latex_escape(card.date)}}}

\\begin{{document}}
\\maketitle

\\noindent
\\begin{{tabular}}{{@{{}}ll@{{}}}}
\\textbf{{Project:}} & {latex_escape(card.project)} \\\\
\\textbf{{Test Conductor:}} & {latex_escape(card.test_conductor)} \\\\
\\textbf{{System / Subsystem:}} & {latex_escape(card.system)} / {latex_escape(card.subsystem)} \\\\
\\textbf{{Test Type:}} & {latex_escape(card.test_type)} \\\\
\\textbf{{Card ID / Rev:}} & {latex_escape(card.id)} / {latex_escape(card.revision)} \\\\
\\end{{tabular}}
\\vspace{{1em}}

\\section*{{Nomenclature}}
\\begin{{tabbing}}
XXXXXXXXXXXXXXXX \\= \\kill
{nom_lines if nom_lines else '(none) \\\\'}
\\end{{tabbing}}

\\section{{Test Points}}

\\begin{{table}}[htbp]
\\centering
\\caption{{Test points --- {latex_escape(card.title)} (Rev {latex_escape(card.revision)})}}
\\label{{tab:test_points}}
\\begin{{tabular}}{{@{{}}p{{0.05\\linewidth}}p{{0.30\\linewidth}}p{{0.35\\linewidth}}p{{0.20\\linewidth}}@{{}}}}
\\toprule
\\# & Action & Expected Result & Reference \\\\
\\midrule
{row_lines if row_lines else ' & & & \\\\'}
\\bottomrule
\\end{{tabular}}
\\end{{table}}

\\end{{document}}
"""


# Browser UI

_INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Perceptor -- test card creator</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=B612+Mono:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/perceptor.css">
</head>
<body class="editor-doc">
<h1>PERCEPTOR</h1>
<div class="sub">test card / test point creator</div>

<h2>Card metadata</h2>
<div class="meta">
  <div class="field"><label>ID</label><input id="m-id"></div>
  <div class="field"><label>Title</label><input id="m-title"></div>
  <div class="field"><label>Revision</label><input id="m-revision"></div>
  <div class="field"><label>Project</label><input id="m-project"></div>
  <div class="field"><label>Author</label><input id="m-author"></div>
  <div class="field"><label>Test Conductor</label><input id="m-test_conductor"></div>
  <div class="field"><label>Quality Assurance</label><input id="m-quality_assurance"></div>
  <div class="field"><label>Date</label><input id="m-date"></div>
  <div class="field"><label>System / Subsystem</label><input id="m-system"></div>
  <div class="field"><label>Test type</label><input id="m-test_type"></div>
  <div class="field"><label>Requirement(s)</label><input id="m-requirements"></div>
</div>

<h2 class="no-rule">Test points</h2>
<div id="pointsList"></div>
<div class="row"><button class="btn btn-plus" id="addPointBtn" title="add point">+</button></div>

<h2 class="no-rule">Nomenclature</h2>
<table class="nomtable" id="nomTable">
  <tr><th>Symbol</th><th>Definition</th><th>Units</th><th class="blank-th" style="width:60px"></th></tr>
</table>
<div class="row"><button class="btn btn-plus" id="addNomBtn" title="add symbol">+</button></div>

<h2 class="no-rule">References</h2>
<table class="nomtable" id="refTable">
  <tr><th>Reference</th><th>Description</th><th class="blank-th" style="width:60px"></th></tr>
</table>
<div class="row"><button class="btn btn-plus" id="addRefBtn" title="add reference">+</button></div>


<h2 class="no-rule">Warnings</h2>
<div id="warningsList"></div>
<div class="row"><button class="btn btn-plus" id="addWarningBtn" title="add warning">+</button></div>

<h2 class="no-rule">Notes</h2>
<div id="notesList"></div>
<div class="row"><button class="btn btn-plus" id="addNoteBtn" title="add note">+</button></div>


<h2>Save / load / export</h2>
<div class="row" id="exportRow">
  <button class="btn" id="loadJsonBtn">import test file</button>
  <input type="file" id="loadJsonFile" accept=".test,.json,application/json" style="display:none">
  <button class="btn" id="saveUnlockedBtn">export unlocked test file (.test)</button>
  <button class="btn" id="saveLockedBtn">export locked test file (.test)</button>
  <button class="btn" id="exportCsvBtn">export .csv</button>
  <button class="btn" id="exportPrintBtn">generate card</button>
</div>
<div id="saveStatus" class="hint"></div>

<script>
// Hide the app-title branding when embedded (e.g. a STARSCOPE tab's
// iframe) -- redundant there since STARSCOPE has its own header.
if (window.self !== window.top) {
  document.querySelectorAll("h1, .sub").forEach(el => el.style.visibility = "hidden");
}

// True only if jira_ticket.py was found in the working directory at
// startup -- see jira_ticket.py's own docstring.
const JIRA_AVAILABLE = __JIRA_AVAILABLE__;

// Set by STARSCOPE once, right after this iframe loads (openTestTab() in
// starscope.py); `var` (not `let`) so that external write reaches this binding.
var starscopeJiraEpicKey = "";

let card = {
  id: "TC-001", title: "Untitled Test Card", revision: "A", author: "",
  date: new Date().toISOString().slice(0,10), project: "", test_conductor: "",
  quality_assurance: "", system: "", subsystem: "", test_type: "", requirements: "",
  points: [], nomenclature: [], references: [], notes: [], warnings: [], locked: false
};

const metaFields = ["id","title","revision","author","date","project","test_conductor","quality_assurance","test_type","requirements"];
const ALWAYS_EDITABLE_META = new Set(["test_conductor", "quality_assurance", "date"]);
function bindMeta() {
  metaFields.forEach(f => {
    const el = document.getElementById("m-" + f);
    el.value = card[f];
    el.disabled = !!card.locked && !ALWAYS_EDITABLE_META.has(f);
    el.addEventListener("input", () => { card[f] = el.value; });
  });
  const sysEl = document.getElementById("m-system");
  sysEl.value = [card.system, card.subsystem].filter(Boolean).join(" / ");
  sysEl.disabled = !!card.locked;
  sysEl.addEventListener("input", () => {
    const parts = sysEl.value.split("/").map(s => s.trim());
    card.system = parts[0] || ""; card.subsystem = parts[1] || "";
  });
}

function nextSeq() {
  const max = card.points.reduce((m, p) => Math.max(m, +p.seq || 0), 0);
  return Math.floor(max / 10) * 10 + 10;
}

// Google Material Symbols, embedded inline (not a file/CDN) so they work
// fully offline; fill="currentColor" picks up each button's own hover color.
const ICON_FLAG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M200-120v-680h360l16 80h224v400H520l-16-80H280v280h-80Zm300-440Zm86 160h134v-240H510l-16-80H280v240h290l16 80Z"/></svg>';
const ICON_PHOTO_CAMERA = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M480-260q75 0 127.5-52.5T660-440q0-75-52.5-127.5T480-620q-75 0-127.5 52.5T300-440q0 75 52.5 127.5T480-260Zm0-80q-42 0-71-29t-29-71q0-42 29-71t71-29q42 0 71 29t29 71q0 42-29 71t-71 29ZM160-120q-33 0-56.5-23.5T80-200v-480q0-33 23.5-56.5T160-760h126l74-80h240l74 80h126q33 0 56.5 23.5T880-680v480q0 33-23.5 56.5T800-120H160Zm0-80h640v-480H638l-73-80H395l-73 80H160v480Zm320-240Z"/></svg>';
const ICON_ADD_A_PHOTO = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M440-440ZM120-120q-33 0-56.5-23.5T40-200v-480q0-33 23.5-56.5T120-760h126l74-80h240v80H355l-73 80H120v480h640v-360h80v360q0 33-23.5 56.5T760-120H120Zm640-560v-80h-80v-80h80v-80h80v80h80v80h-80v80h-80ZM440-260q75 0 127.5-52.5T620-440q0-75-52.5-127.5T440-620q-75 0-127.5 52.5T260-440q0 75 52.5 127.5T440-260Zm0-80q-42 0-71-29t-29-71q0-42 29-71t71-29q42 0 71 29t29 71q0 42-29 71t-71 29Z"/></svg>';
const ICON_SPEED = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M480-316.5q38-.5 56-27.5l224-336-336 224q-27 18-28.5 55t22.5 61q24 24 62 23.5Zm0-483.5q59 0 113.5 16.5T696-734l-76 48q-33-17-68.5-25.5T480-720q-133 0-226.5 93.5T160-400q0 42 11.5 83t32.5 77h552q23-38 33.5-79t10.5-85q0-36-8.5-70T766-540l48-76q30 47 47.5 100T880-406q1 57-13 109t-41 99q-11 18-30 28t-40 10H204q-21 0-40-10t-30-28q-26-45-40-95.5T80-400q0-83 31.5-155.5t86-127Q252-737 325-768.5T480-800Zm7 313Z"/></svg>';
const ICON_BOOKMARK_ADD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M200-120v-640q0-33 23.5-56.5T280-840h240v80H280v518l200-86 200 86v-278h80v400L480-240 200-120Zm80-640h240-240Zm400 160v-80h-80v-80h80v-80h80v80h80v80h-80v80h-80Z"/></svg>';
const ICON_ARROW_UPWARD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M440-160v-487L216-423l-56-57 320-320 320 320-56 57-224-224v487h-80Z"/></svg>';
const ICON_ARROW_DOWNWARD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M440-800v487L216-537l-56 57 320 320 320-320-56-57-224 224v-487h-80Z"/></svg>';
const ICON_CLOSE = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="m256-200-56-56 224-224-224-224 56-56 224 224 224-224 56 56-224 224 224 224-56 56-224-224-224 224Z"/></svg>';
// A thumbnail chip's own label icon, distinct from the point-button-cluster icons above.
const ICON_IMAGE = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M200-120q-33 0-56.5-23.5T120-200v-560q0-33 23.5-56.5T200-840h560q33 0 56.5 23.5T840-760v560q0 33-23.5 56.5T760-120H200Zm0-80h560v-560H200v560Zm40-80h480L570-480 450-320l-90-120-120 160Zm-40 80v-560 560Z"/></svg>';

const QUALITY_OPTIONS = [
  ["", "[None]"],
  ["RFI", "\U0001F535 RFI"],
  ["PASS", "\U0001F7E2 PASS"],
  ["CAUTION", "\U0001F7E0 CAUTION"],
  ["FAIL", "\U0001F534 FAIL"],
];
function qualitySelect(i, current) {
  const opts = QUALITY_OPTIONS.map(([v, label]) =>
    `<option value="${v}"${v === current ? " selected" : ""}>${label}</option>`).join("");
  return `<select data-i="${i}" data-f="quality">${opts}</select>`;
}

// One camera stream shared across all points, acquired once via
// getUserMedia() -- reaches any UVC-class video device the OS exposes.
let cameraStream = null;
let cameraVideo = null;
let cameraCanvas = null;
let previewOpenIndex = null;

function ensureCameraElements() {
  if (!cameraVideo) {
    cameraVideo = document.createElement("video");
    cameraVideo.autoplay = true;
    cameraVideo.muted = true;
    cameraVideo.playsInline = true;
    cameraVideo.className = "cam-preview";
  }
  if (!cameraCanvas) cameraCanvas = document.createElement("canvas");
}

async function ensureCameraStream() {
  if (cameraStream) return cameraStream;
  ensureCameraElements();
  cameraStream = await navigator.mediaDevices.getUserMedia({video: true});
  cameraVideo.srcObject = cameraStream;
  return cameraStream;
}

function reattachPreviewIfOpen() {
  if (previewOpenIndex === null) return;
  const slot = document.getElementById("pt-camera-" + previewOpenIndex);
  if (slot && cameraVideo) slot.appendChild(cameraVideo);
}

function showCamMessage(i, text) {
  const el = document.getElementById("pt-cam-msg-" + i);
  if (!el) return;
  el.textContent = text;
  setTimeout(() => { if (el.textContent === text) el.textContent = ""; }, 4000);
}

async function capturePointImage(i) {
  try {
    await ensureCameraStream();
  } catch (err) {
    showCamMessage(i, "No camera available");
    return;
  }
  const w = cameraVideo.videoWidth, h = cameraVideo.videoHeight;
  if (!w || !h) { showCamMessage(i, "Camera not ready yet -- try again"); return; }
  cameraCanvas.width = w; cameraCanvas.height = h;
  cameraCanvas.getContext("2d").drawImage(cameraVideo, 0, 0, w, h);
  const dataUrl = cameraCanvas.toDataURL("image/jpeg", 0.9);
  try {
    const res = await fetch("/capture_image", {method: "POST", body: JSON.stringify({data: dataUrl})});
    const result = await res.json();
    if (!result.saved) throw new Error(result.error || "unknown error");
    card.points[i].images = card.points[i].images || [];
    card.points[i].images.push(result.filename);
    renderPoints();
    showCamMessage(i, "Captured " + result.filename);
  } catch (err) {
    showCamMessage(i, "Capture failed: " + err);
  }
}

async function createJiraTicket(i) {
  const p = card.points[i];
  const msgEl = document.getElementById("pt-jira-msg-" + i);
  if (msgEl) msgEl.innerHTML = "Creating ticket...";
  const summary = `${card.id} pt ${p.seq}: ${(p.action || "(no action text)").slice(0, 120)}`;
  const description = [
    `Test: ${card.id} -- ${card.title}`,
    `Point ${p.seq}`,
    `Action: ${p.action || ""}`,
    `Expected: ${p.expected || ""}`,
    `Observed: ${p.observed || ""}`,
    `Reference: ${p.reference || ""}`,
    `Quality: ${p.quality || ""}`,
  ].join("\\n");
  try {
    const res = await fetch("/create_jira_ticket", {
      method: "POST",
      body: JSON.stringify({summary, description, epic_key: starscopeJiraEpicKey || ""}),
    });
    const result = await res.json();
    if (!msgEl) return;
    if (result.success) {
      msgEl.innerHTML = `Created <a href="${esc(result.url)}" target="_blank" rel="noopener">${esc(result.key)}</a>`;
    } else {
      msgEl.textContent = `Ticket creation failed: ${result.error || "unknown error"}`;
    }
  } catch (err) {
    if (msgEl) msgEl.textContent = `Ticket creation failed: ${err}`;
  }
}

function togglePreview(i) {
  if (previewOpenIndex === i) {
    if (cameraVideo && cameraVideo.parentElement) cameraVideo.parentElement.removeChild(cameraVideo);
    previewOpenIndex = null;
    return;
  }
  ensureCameraStream().then(() => {
    const slot = document.getElementById("pt-camera-" + i);
    if (cameraVideo.parentElement) cameraVideo.parentElement.removeChild(cameraVideo);
    slot.appendChild(cameraVideo);
    previewOpenIndex = i;
  }).catch(() => {
    showCamMessage(i, "No camera available");
  });
}

function renderPoints() {
  const list = document.getElementById("pointsList");
  list.innerHTML = "";
  card.points.forEach((p, i) => {
    const locked = !!p.locked;
    const flagged = !!p.flagged;
    const dis = locked ? "disabled" : "";
    const div = document.createElement("div");
    div.className = "pt-card" + (locked ? " locked" : "") + (flagged ? " flagged" : "");
    const imgChips = (p.images || []).map(fn =>
      `<span class="thumb-chip">${ICON_IMAGE} ${esc(fn)} <button class="btn mini btn-x" data-rm-img="${esc(fn)}" data-i="${i}">x</button></span>`
    ).join("");
    // Ticket is offered on CAUTION/FAIL (a verdict), not on flagged,
    // which is just a "pay attention here" highlight independent of pass/fail.
    const showJiraBtn = JIRA_AVAILABLE && (p.quality === "CAUTION" || p.quality === "FAIL");
    div.innerHTML = `
      <div class="pt-head">
        <input class="seq" data-i="${i}" data-f="seq" value="${esc(p.seq)}">
        ${locked ? '<span class="lock-tag">\U0001F512 locked</span>' : ""}
        <div class="pt-btns">
          <button class="btn mini btn-icon flag-btn${flagged ? " flagged" : ""}" data-act="flag" data-i="${i}" title="flag">${ICON_FLAG}</button>
          <button class="btn mini btn-icon" data-act="preview" data-i="${i}" title="camera preview">${ICON_PHOTO_CAMERA}</button>
          <button class="btn mini btn-icon" data-act="capture" data-i="${i}" title="capture image">${ICON_ADD_A_PHOTO}</button>
          <button class="btn mini btn-icon" data-act="measure" data-i="${i}" title="take measurement (not yet implemented)">${ICON_SPEED}</button>
          ${showJiraBtn ? `<button class="btn mini btn-icon" data-act="jira" data-i="${i}" title="create Jira ticket">${ICON_BOOKMARK_ADD}</button>` : ""}
          <span class="pt-btn-gap"></span>
          <button class="btn mini btn-icon" data-act="up" data-i="${i}" title="move up">${ICON_ARROW_UPWARD}</button>
          <button class="btn mini btn-icon" data-act="down" data-i="${i}" title="move down">${ICON_ARROW_DOWNWARD}</button>
          <button class="btn mini btn-icon btn-x" data-act="del" data-i="${i}" ${dis} title="delete point">${ICON_CLOSE}</button>
        </div>
      </div>
      <div class="pt-main">
        <div><label>Action</label><textarea data-i="${i}" data-f="action" ${dis}>${esc(p.action)}</textarea></div>
        <div><label>Expected Result</label><textarea data-i="${i}" data-f="expected" ${dis}>${esc(p.expected)}</textarea></div>
        <div><label>Observed Result</label><textarea data-i="${i}" data-f="observed">${esc(p.observed)}</textarea></div>
        <div><label>Reference</label><textarea data-i="${i}" data-f="reference" ${dis}>${esc(p.reference)}</textarea></div>
      </div>
      <div class="pt-sign">
        <div><label>Responsible Party</label><input data-i="${i}" data-f="responsible_party" value="${esc(p.responsible_party)}" ${dis}></div>
        <div><label>Date</label><input data-i="${i}" data-f="date" value="${esc(p.date)}"></div>
        <div><label>Time (24h)</label>
          <div class="time-row">
            <input data-i="${i}" data-f="time" value="${esc(p.time)}">
            <button class="btn mini" data-now="${i}">now</button>
          </div>
        </div>
        <div><label>Initial</label><input data-i="${i}" data-f="initial" value="${esc(p.initial)}"></div>
        <div><label>Quality</label>${qualitySelect(i, p.quality)}</div>
      </div>
      <div class="pt-camera" id="pt-camera-${i}"></div>
      <div class="pt-images" id="pt-images-${i}">${imgChips}</div>
      <div class="pt-cam-msg" id="pt-cam-msg-${i}"></div>
      <div class="pt-cam-msg" id="pt-jira-msg-${i}"></div>`;
    list.appendChild(div);
  });
  reattachPreviewIfOpen();
  list.querySelectorAll("input[data-f], textarea[data-f], select[data-f]").forEach(el => {
    el.addEventListener("input", () => {
      card.points[+el.dataset.i][el.dataset.f] = el.value;
    });
  });
  list.querySelectorAll("select[data-f]").forEach(el => {
    el.addEventListener("change", () => {
      card.points[+el.dataset.i][el.dataset.f] = el.value;
      // Quality change needs a re-render: the Jira ticket button's
      // visibility is derived from it, not just displayed.
      if (el.dataset.f === "quality") renderPoints();
    });
  });
  list.querySelectorAll("button[data-act]").forEach(btn => {
    btn.addEventListener("click", () => {
      const i = +btn.dataset.i, act = btn.dataset.act;
      if (act === "del") {
        if (card.points[i].locked) return;   // locked points can't be removed
        card.points.splice(i, 1);
      }
      if (act === "up" && i > 0) [card.points[i-1], card.points[i]] = [card.points[i], card.points[i-1]];
      if (act === "down" && i < card.points.length - 1) [card.points[i+1], card.points[i]] = [card.points[i], card.points[i+1]];
      if (act === "flag") {
        card.points[i].flagged = !card.points[i].flagged;   // works regardless of locked
      }
      if (act === "capture") { capturePointImage(i); return; }
      if (act === "preview") { togglePreview(i); return; }
      if (act === "jira") { createJiraTicket(i); return; }
      // "measure" is a stub -- falls through to the plain re-render below.
      renderPoints();
    });
  });
  list.querySelectorAll("button[data-rm-img]").forEach(btn => {
    btn.addEventListener("click", () => {
      const i = +btn.dataset.i, fn = btn.dataset.rmImg;
      // Detaches the filename from this point only -- the .jpg file
      // itself is left on disk, not deleted.
      card.points[i].images = (card.points[i].images || []).filter(x => x !== fn);
      renderPoints();
    });
  });
  list.querySelectorAll("button[data-now]").forEach(btn => {
    btn.addEventListener("click", () => {
      const i = +btn.dataset.now, d = new Date(), pad = n => String(n).padStart(2, "0");
      card.points[i].date = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
      card.points[i].time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;   // 24h
      renderPoints();
    });
  });
}

function esc(s) { return (s??"").toString().replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

document.getElementById("addPointBtn").addEventListener("click", () => {
  card.points.push({seq: nextSeq(), action:"", expected:"", observed:"", reference:"",
                     responsible_party:"", date:"", time:"", initial:"", quality:"",
                     locked: false, flagged: false, images: []});
  renderPoints();
});

function renderNomenclature() {
  const t = document.getElementById("nomTable");
  t.querySelectorAll("tr.nomrow").forEach(r => r.remove());
  card.nomenclature.forEach(([sym, defn, units, locked], i) => {
    const dis = locked ? "disabled" : "";
    const tr = document.createElement("tr");
    tr.className = "nomrow";
    tr.innerHTML = `
      <td><input value="${esc(sym)}" data-i="${i}" data-f="0" placeholder="\\\\alpha" ${dis}></td>
      <td><input value="${esc(defn)}" data-i="${i}" data-f="1" placeholder="definition" ${dis}></td>
      <td><input value="${esc(units||"")}" data-i="${i}" data-f="2" placeholder="units" ${dis}></td>
      <td><button class="btn mini btn-x" data-del="${i}" ${dis}>x</button></td>`;
    t.appendChild(tr);
  });
  t.querySelectorAll("input[data-i]").forEach(inp => {
    inp.addEventListener("input", () => {
      card.nomenclature[+inp.dataset.i][+inp.dataset.f] = inp.value;
    });
  });
  t.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => {
      if (card.nomenclature[+btn.dataset.del][3]) return;   // locked row
      card.nomenclature.splice(+btn.dataset.del, 1);
      renderNomenclature();
    });
  });
}
document.getElementById("addNomBtn").addEventListener("click", () => {
  card.nomenclature.push(["", "", "", false]);
  renderNomenclature();
});

function renderReferences() {
  const t = document.getElementById("refTable");
  t.querySelectorAll("tr.refrow").forEach(r => r.remove());
  card.references.forEach(([ref, desc, locked], i) => {
    const dis = locked ? "disabled" : "";
    const tr = document.createElement("tr");
    tr.className = "refrow";
    tr.innerHTML = `
      <td><input value="${esc(ref)}" data-i="${i}" data-f="0" placeholder="REQ-1234" ${dis}></td>
      <td><input value="${esc(desc)}" data-i="${i}" data-f="1" placeholder="description" ${dis}></td>
      <td><button class="btn mini btn-x" data-del="${i}" ${dis}>x</button></td>`;
    t.appendChild(tr);
  });
  t.querySelectorAll("input[data-i]").forEach(inp => {
    inp.addEventListener("input", () => {
      card.references[+inp.dataset.i][+inp.dataset.f] = inp.value;
    });
  });
  t.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => {
      if (card.references[+btn.dataset.del][2]) return;   // locked row
      card.references.splice(+btn.dataset.del, 1);
      renderReferences();
    });
  });
}
document.getElementById("addRefBtn").addEventListener("click", () => {
  card.references.push(["", "", false]);
  renderReferences();
});

function renderTextList(listId, key) {
  const el = document.getElementById(listId);
  el.innerHTML = "";
  card[key].forEach(([val, locked], i) => {
    const dis = locked ? "disabled" : "";
    const row = document.createElement("div");
    row.className = "list-row";
    row.innerHTML = `<textarea data-key="${key}" data-i="${i}" ${dis}>${esc(val)}</textarea>
      <button class="btn mini btn-x" data-key="${key}" data-del="${i}" ${dis}>x</button>`;
    el.appendChild(row);
  });
  el.querySelectorAll("textarea[data-key]").forEach(ta => {
    ta.addEventListener("input", () => { card[ta.dataset.key][+ta.dataset.i][0] = ta.value; });
  });
  el.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => {
      if (card[btn.dataset.key][+btn.dataset.del][1]) return;   // locked row
      card[btn.dataset.key].splice(+btn.dataset.del, 1);
      renderTextList(listId, btn.dataset.key);
    });
  });
}
document.getElementById("addNoteBtn").addEventListener("click", () => {
  card.notes.push(["", false]); renderTextList("notesList", "notes");
});
document.getElementById("addWarningBtn").addEventListener("click", () => {
  card.warnings.push(["", false]); renderTextList("warningsList", "warnings");
});

function download(filename, text, mime) {
  const blob = new Blob([text], {type: mime || "text/plain"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
}

function freezeCurrentCard() {
  card.locked = true;
  card.points.forEach(p => { p.locked = true; });
  card.nomenclature.forEach(row => { row[3] = true; });
  card.references.forEach(row => { row[2] = true; });
  card.notes.forEach(row => { row[1] = true; });
  card.warnings.forEach(row => { row[1] = true; });
}
function unfreezeCard() {
  card.locked = false;
  card.points.forEach(p => { p.locked = false; });
  card.nomenclature.forEach(row => { row[3] = false; });
  card.references.forEach(row => { row[2] = false; });
  card.notes.forEach(row => { row[1] = false; });
  card.warnings.forEach(row => { row[1] = false; });
}
function rerenderAll() {
  bindMeta(); renderPoints(); renderNomenclature(); renderReferences();
  renderTextList("notesList", "notes"); renderTextList("warningsList", "warnings");
}
async function saveCard(statusEl) {
  statusEl.textContent = "saving...";
  try {
    const res = await fetch("/save", {method: "POST", body: JSON.stringify(card)});
    const data = await res.json();
    statusEl.textContent = data.saved ? `Saved to ${data.path}` : `Save failed: ${data.error || "unknown error"}`;
  } catch (err) {
    statusEl.textContent = `Save failed: ${err}`;
  }
}
document.getElementById("saveLockedBtn").addEventListener("click", () => {
  // Locking is a workflow marker, not security. Freezing happens
  // client-side so the editor reflects it immediately, not just the file.
  freezeCurrentCard();
  rerenderAll();
  saveCard(document.getElementById("saveStatus"));
});
document.getElementById("saveUnlockedBtn").addEventListener("click", () => {
  unfreezeCard();
  rerenderAll();
  saveCard(document.getElementById("saveStatus"));
});
document.getElementById("loadJsonBtn").addEventListener("click", () => {
  document.getElementById("loadJsonFile").click();
});
function normalizePairList(items, width) {
  // Pads each row to `width` (the last element the locked flag); accepts
  // a bare string for a single-element row (pre-locking notes/warnings).
  return (items || []).map(item => {
    let row = typeof item === "string" ? [item] : item.slice();
    while (row.length < width - 1) row.push("");
    if (row.length < width) row.push(false);
    return row.slice(0, width);
  });
}
function normalizeCard(c) {
  c.nomenclature = normalizePairList(c.nomenclature, 4);
  c.references = normalizePairList(c.references, 3);
  c.notes = normalizePairList(c.notes, 2);
  c.warnings = normalizePairList(c.warnings, 2);
  if (!c.quality_assurance) c.quality_assurance = "";
  if (!c.requirements) c.requirements = "";
  if (!c.locked) c.locked = false;
  (c.points || []).forEach(p => {
    if (!("locked" in p)) p.locked = false;
    if (!("flagged" in p)) p.flagged = false;
    if (!p.images) p.images = [];
    if (!("observed" in p)) p.observed = "";
  });
  return c;
}
function loadCardIntoUI(c) {
  card = normalizeCard(c);
  bindMeta(); renderPoints(); renderNomenclature(); renderReferences();
  renderTextList("notesList", "notes"); renderTextList("warningsList", "warnings");
}
document.getElementById("loadJsonFile").addEventListener("change", (e) => {
  const f = e.target.files[0]; if (!f) return;
  f.text().then(txt => loadCardIntoUI(JSON.parse(txt)));
});

async function postExport(path, filename, mime) {
  // Open the print window synchronously, before the await below -- many
  // popup blockers kill a window.open() called after a fetch() resolves.
  let w = null;
  if (path === "/export/print") {
    w = window.open("", "_blank");
    if (w) w.document.write("Generating...");
  }
  try {
    const res = await fetch(path, {method: "POST", body: JSON.stringify(card)});
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const text = await res.text();
    if (path === "/export/print") {
      if (w) { w.document.open(); w.document.write(text); w.document.close(); }
    } else {
      download(filename, text, mime);
    }
  } catch (err) {
    if (w) { w.document.open(); w.document.write("Failed to generate: " + err); w.document.close(); }
    document.getElementById("saveStatus").textContent = `Export failed: ${err}`;
  }
}
document.getElementById("exportCsvBtn").addEventListener("click", () =>
  postExport("/export/csv", (card.id || "card") + ".csv", "text/csv"));
document.getElementById("exportPrintBtn").addEventListener("click", () =>
  postExport("/export/print"));

bindMeta();
card.points = [{seq:10, action:"", expected:"", observed:"", reference:"",
                responsible_party:"", date:"", time:"", initial:"", quality:"",
                locked:false, flagged:false, images:[]}];
renderPoints();
renderNomenclature();
renderReferences();
renderTextList("notesList", "notes");
renderTextList("warningsList", "warnings");

// If opened as ?load=<filename>, fetch that .test file and populate the
// editor with it -- how STARSCOPE opens a specific test in a tab.
const _loadFile = new URLSearchParams(window.location.search).get("load");
if (_loadFile) {
  fetch("/load?file=" + encodeURIComponent(_loadFile))
    .then(r => { if (!r.ok) throw new Error("not found"); return r.json(); })
    .then(loadCardIntoUI)
    .catch(() => { document.getElementById("m-id").value = _loadFile.replace(/\\.test$/, ""); });
}

// Lets an embedding page (STARSCOPE's tab chrome) read the live editor
// state, since `card` is a module-scope `let` otherwise unreachable from an iframe.
window.getCard = () => card;
</script>
</body>
</html>
"""


def _safe_filename(stem: str, ext: str) -> str:
    """Turn a card id into a filesystem-safe filename, confined to the
    current directory (no path separators from user-controlled input)."""
    stem = os.path.basename((stem or "card").strip()) or "card"
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
    return stem + ext


def _load_jira_module():
    """Optional Jira integration -- see jira_ticket.py's own docstring.
    Looked up in the CURRENT WORKING DIRECTORY specifically (same place
    .test/.camp files and captured photos already live), not next to
    perceptor.py itself, so dropping the file in only turns this on for
    that one working directory. Absent (the common case) or broken in any
    way -> None, silently, so the rest of the app never has to think
    about whether Jira is involved."""
    path = os.path.join(os.getcwd(), "jira_ticket.py")
    if not os.path.isfile(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("jira_ticket", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


_jira = _load_jira_module()
JIRA_AVAILABLE = _jira is not None
_INDEX_HTML = _INDEX_HTML.replace("__JIRA_AVAILABLE__", "true" if JIRA_AVAILABLE else "false")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _read_card(self) -> TestCard:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        return TestCard.from_dict(json.loads(body))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send(200, "text/html", _INDEX_HTML.encode())
        elif path == "/load":
            # Read a .test file by name from the working directory -- how
            # STARSCOPE opens a specific test in a tab.
            qs = parse_qs(parsed.query)
            filename = (qs.get("file") or [""])[0]
            safe = os.path.basename(filename)
            file_path = os.path.join(os.getcwd(), safe)
            if not safe or not os.path.isfile(file_path):
                self._send_json(404, {"error": f"not found: {safe!r}"})
                return
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    self._send(200, "application/json", f.read().encode())
            except OSError as e:
                self._send_json(500, {"error": str(e)})
        elif path == "/list_files":
            # Lists files in the working directory by extension -- lets a
            # picker browse what's already on disk.
            qs = parse_qs(parsed.query)
            ext = (qs.get("ext") or [""])[0]
            if not re.match(r"^\.[A-Za-z0-9]+$", ext or ""):
                self._send_json(400, {"error": "ext must look like '.test'"})
                return
            try:
                names = sorted(
                    f for f in os.listdir(os.getcwd())
                    if f.endswith(ext) and os.path.isfile(f)
                )
            except OSError as e:
                self._send_json(500, {"error": str(e)})
                return
            self._send_json(200, {"files": names})
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        else:
            self._send(404, "text/plain", b"not found")

    def _serve_static(self, filename: str) -> None:
        # CSS assets live next to the script itself, not cwd (reserved for
        # user data), so this resolves the same regardless of launch dir.
        safe = os.path.basename(filename)
        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        file_path = os.path.join(static_dir, safe)
        if not safe or not os.path.isfile(file_path):
            self._send(404, "text/plain", b"not found")
            return
        content_type = "text/css" if safe.endswith(".css") else "application/octet-stream"
        try:
            with open(file_path, "rb") as f:
                self._send(200, content_type, f.read())
        except OSError:
            self._send(500, "text/plain", b"error reading static file")

    def _handle_capture_image(self):
        # Separate from the TestCard-shaped routes below -- this payload
        # is just {"data": "data:image/jpeg;base64,..."}, not a card.
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
            data_url = payload["data"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        try:
            _header, b64data = data_url.split(",", 1)
            raw = base64.b64decode(b64data)
        except (ValueError, binascii.Error) as e:
            self._send_json(400, {"error": f"bad image data: {e}"})
            return
        # Millisecond resolution so a quick burst of captures doesn't
        # collide on the same filename.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"{ts}.jpg"
        out_path = os.path.join(os.getcwd(), filename)
        try:
            with open(out_path, "wb") as f:
                f.write(raw)
        except OSError as e:
            self._send_json(500, {"saved": False, "error": str(e)})
            return
        self._send_json(200, {"saved": True, "filename": filename, "path": out_path})

    def _handle_create_jira_ticket(self):
        # {"summary": ..., "description": ..., "epic_key": ..., "issue_type": ...}.
        # issue_type optional -- falls back to cfg["issue_type"] if omitted.
        if not JIRA_AVAILABLE:
            self._send_json(404, {"success": False, "error": "Jira integration not available"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as e:
            self._send_json(400, {"success": False, "error": f"bad request: {e}"})
            return
        try:
            result = _jira.create_ticket(
                payload.get("summary", ""), payload.get("description", ""),
                payload.get("epic_key", ""), payload.get("issue_type") or None,
            )
        except Exception as e:
            # jira_ticket.py is user-supplied; a bug in it shouldn't take the server down.
            result = {"success": False, "error": f"jira_ticket.py raised: {e}"}
        self._send_json(200, result)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/capture_image":
            self._handle_capture_image()
        elif path == "/create_jira_ticket":
            self._handle_create_jira_ticket()
            return
        try:
            card = self._read_card()
        except (json.JSONDecodeError, TypeError) as e:
            self._send_json(400, {"error": str(e)})
            return
        if path == "/export/csv":
            self._send(200, "text/csv", export_csv(card).encode())
        elif path == "/export/print":
            self._send(200, "text/html", export_print_html(card, self.headers.get("Host", "localhost")).encode())
        elif path == "/export/tex":
            self._send(200, "text/x-tex", export_tex(card).encode())
        elif path == "/save":
            # Written to the server's working directory, not pushed to the
            # browser -- meant to be re-opened by this program.
            filename = _safe_filename(card.id, ".test")
            out_path = os.path.join(os.getcwd(), filename)
            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(card.to_dict(), f, indent=2)
            except OSError as e:
                self._send_json(500, {"saved": False, "error": str(e)})
                return
            self._send_json(200, {"saved": True, "filename": filename, "path": out_path})
        else:
            self._send(404, "text/plain", b"not found")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=5790)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--selftest", action="store_true",
                    help="deprecated: tests moved to tests/, run `pytest` instead")
    args = p.parse_args()

    if args.selftest:
        print("Tests moved out of this file -- run `pytest` (or `pytest tests/test_perceptor.py`) instead.")
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
