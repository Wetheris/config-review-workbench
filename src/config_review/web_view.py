"""Local browser companion for reviewing DEV-to-TEST differences.

The viewer serves a snapshot generated from the workbench's existing Focused Diff
and Full Diff presentations. It binds only to loopback, uses a random URL token,
and never modifies DEV, TEST, Git, or workbench configuration. Git context is
loaded lazily through a read-only endpoint, while reviewer notes remain in the
browser until the reviewer explicitly exports a plaintext review file.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from .core import (
    ChangeBlock,
    DiffPresentation,
    DisplayLine,
    FileRecord,
    GitCommitContext,
    WorkbenchError,
)
from .rendering import full_unified_diff, review_unified_diff

if TYPE_CHECKING:
    from .workbench import Workbench


@dataclass(slots=True, frozen=True)
class WebViewerLaunch:
    """Details for one started local viewer."""

    url: str
    file_count: int
    browser_opened: bool


GitLookup = dict[str, tuple[FileRecord, ChangeBlock]]


def _display_line_payload(line: DisplayLine) -> dict[str, Any]:
    return {
        "text": line.text,
        "kind": line.kind,
        "testLine": line.test_line,
        "devLine": line.dev_line,
    }


def _change_key(record: FileRecord, block: ChangeBlock) -> str:
    """Return a stable, content-derived key for browser notes and Git lookups."""
    digest = hashlib.sha256()
    values: tuple[object, ...] = (
        record.relative_path,
        block.tag,
        block.old_start,
        block.old_end,
        block.new_start,
        block.new_end,
        *block.old_lines,
        "\0DEV\0",
        *block.new_lines,
    )
    for value in values:
        digest.update(str(value).encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
    return digest.hexdigest()[:24]


def _line_range_text(start: int, end: int) -> str:
    if end <= start:
        return "—"
    first = start + 1
    last = end
    return str(first) if first == last else f"{first}-{last}"


def _presentation_payload(
    workbench: Workbench,
    record: FileRecord,
    presentation: DiffPresentation,
    git_lookup: GitLookup,
) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    for index, block in enumerate(presentation.change_blocks):
        key = _change_key(record, block)
        git_lookup.setdefault(key, (record, block))
        marker_index = (
            presentation.change_line_indexes[index]
            if index < len(presentation.change_line_indexes)
            else 0
        )
        changes.append(
            {
                "key": key,
                "gitContextId": key,
                "label": workbench._change_context_label(record, block),
                "markerIndex": marker_index,
                "panelAfter": min(
                    len(presentation.lines),
                    marker_index + 1 + block.old_count + block.new_count,
                ),
                "testRange": _line_range_text(block.old_start, block.old_end),
                "devRange": _line_range_text(block.new_start, block.new_end),
                "oldLines": list(block.old_lines),
                "newLines": list(block.new_lines),
            }
        )

    return {
        "lines": [_display_line_payload(line) for line in presentation.lines],
        "changes": changes,
        "visibleChanges": presentation.visible_change_count,
        "handled": presentation.handled_count,
        "noiseHidden": presentation.pattern_hidden_count,
        "whitespaceHidden": presentation.whitespace_hidden_count,
        "orderHidden": presentation.mapping_order_hidden_count,
        "orderUnavailable": presentation.mapping_order_unavailable_reason,
    }


def _build_web_diff_snapshot(workbench: Workbench) -> tuple[dict[str, Any], GitLookup]:
    """Build the browser snapshot and its private, read-only Git lookup table."""
    files: list[dict[str, Any]] = []
    git_lookup: GitLookup = {}
    for record in workbench.records:
        workbench.refresh_record(record)
        full = full_unified_diff(record, workbench.settings.context, selected_change=0)
        has_current_difference = not record.equal or bool(record.read_error) or record.binary
        if not has_current_difference:
            continue
        focused = review_unified_diff(
            record,
            workbench.enabled_patterns,
            workbench.settings.context,
            hide_whitespace=workbench.hide_whitespace,
            hide_mapping_order=workbench.hide_mapping_order,
            expand_filtered=False,
            selected_change=0,
        )
        focused_expanded = review_unified_diff(
            record,
            workbench.enabled_patterns,
            workbench.settings.context,
            hide_whitespace=workbench.hide_whitespace,
            hide_mapping_order=workbench.hide_mapping_order,
            expand_filtered=True,
            selected_change=0,
        )
        status, counts = workbench.file_status(record)
        files.append(
            {
                "path": record.relative_path,
                "status": status,
                "states": list(record.states),
                "focused": _presentation_payload(workbench, record, focused, git_lookup),
                "focusedExpanded": _presentation_payload(
                    workbench, record, focused_expanded, git_lookup
                ),
                "raw": _presentation_payload(workbench, record, full, git_lookup),
                "counts": {
                    "active": counts.active,
                    "handled": counts.handled,
                    "noiseHidden": counts.pattern_hidden,
                    "whitespaceHidden": counts.whitespace_hidden,
                    "orderHidden": counts.mapping_order_hidden,
                },
            }
        )

    if not files:
        raise WorkbenchError("No current DEV/TEST differences are available for the web viewer.")

    snapshot = {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(workbench.settings.source),
        "target": str(workbench.settings.target),
        "gitStatus": workbench.git_status.summary,
        "files": files,
    }
    return snapshot, git_lookup


def build_web_diff_snapshot(workbench: Workbench) -> dict[str, Any]:
    """Build the public browser snapshot without exposing private Git objects."""
    snapshot, _git_lookup = _build_web_diff_snapshot(workbench)
    return snapshot


def _commit_payload(context: GitCommitContext) -> dict[str, str]:
    return {
        "source": context.source,
        "hash": context.short_hash,
        "author": context.author,
        "date": context.date,
        "subject": context.subject,
    }


def _git_context_payload(
    workbench: Workbench,
    record: FileRecord,
    block: ChangeBlock,
) -> dict[str, Any]:
    test_context, dev_context = workbench._block_git_context(record, block)

    def newest_first(items: list[GitCommitContext]) -> list[GitCommitContext]:
        return sorted(items, key=lambda item: item.date, reverse=True)

    return {
        # DEV is the incoming side of the comparison and is intentionally first.
        "dev": [_commit_payload(item) for item in newest_first(dev_context)],
        "test": [_commit_payload(item) for item in newest_first(test_context)],
    }


def _render_page(snapshot: dict[str, Any]) -> bytes:
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    # Prevent configuration text containing </script> from ending the data block.
    encoded = encoded.replace("</", r"<\/")
    page = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Config Review Web Diff</title>
<style>
:root {
  color-scheme: dark;
  --bg: #0d1117;
  --panel: #161b22;
  --panel2: #21262d;
  --border: #30363d;
  --text: #e6edf3;
  --muted: #8b949e;
  --accent: #58a6ff;
  --accentbg: #1f6feb33;
  --add: #aff5b4;
  --addbg: #033a16;
  --del: #ffdcd7;
  --delbg: #67060c;
  --hidden: #d2a8ff;
  --hiddenbg: #8957e522;
  --hover: #ffffff08;
  --gutter: #21262d;
  --scroll-thumb: #484f58;
  --scroll-track: #161b22;
  --note: #f2cc60;
  --notebg: #bb800926;
}
:root[data-theme="light"] {
  color-scheme: light;
  --bg: #ffffff;
  --panel: #f6f8fa;
  --panel2: #ffffff;
  --border: #d0d7de;
  --text: #1f2328;
  --muted: #59636e;
  --accent: #0969da;
  --accentbg: #ddf4ff;
  --add: #116329;
  --addbg: #dafbe1;
  --del: #82071e;
  --delbg: #ffebe9;
  --hidden: #6639ba;
  --hiddenbg: #fbefff;
  --hover: #818b981a;
  --gutter: #d8dee4;
  --scroll-thumb: #afb8c1;
  --scroll-track: #f6f8fa;
  --note: #7d4e00;
  --notebg: #fff8c5;
}
* { box-sizing: border-box; }
html, body { height: 100%; min-height: 0; margin: 0; }
body {
  font: 14px/1.45 system-ui, -apple-system, Segoe UI, sans-serif;
  background: var(--bg);
  color: var(--text);
  overflow: hidden;
}
button, input, select, textarea { font: inherit; color: inherit; }
.app {
  height: 100vh;
  height: 100dvh;
  min-height: 0;
  overflow: hidden;
  display: grid;
  grid-template-columns: 330px minmax(0, 1fr);
}
.sidebar {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  background: var(--panel);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
}
.brand { padding: 18px 16px 12px; border-bottom: 1px solid var(--border); }
.brand h1 { font-size: 16px; margin: 0 0 4px; }
.brand p { margin: 0; color: var(--muted); font-size: 12px; }
.search { padding: 12px; border-bottom: 1px solid var(--border); }
.search input {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
}
.tree, .diff {
  overflow: auto;
  scrollbar-gutter: stable;
  scrollbar-color: var(--scroll-thumb) var(--scroll-track);
  scrollbar-width: thin;
}
.tree::-webkit-scrollbar, .diff::-webkit-scrollbar { width: 12px; height: 12px; }
.tree::-webkit-scrollbar-track, .diff::-webkit-scrollbar-track { background: var(--scroll-track); }
.tree::-webkit-scrollbar-thumb, .diff::-webkit-scrollbar-thumb {
  background: var(--scroll-thumb);
  border: 3px solid var(--scroll-track);
  border-radius: 999px;
}
.tree { padding: 8px; flex: 1 1 0; min-height: 0; overscroll-behavior: contain; }
.tree details { margin: 1px 0; }
.tree summary { cursor: pointer; color: var(--muted); padding: 4px 6px; user-select: none; }
.tree .children { padding-left: 14px; }
.file {
  width: 100%;
  display: flex;
  gap: 8px;
  align-items: center;
  text-align: left;
  border: 0;
  border-radius: 6px;
  padding: 6px 8px;
  color: var(--text);
  background: transparent;
  cursor: pointer;
}
.file:hover { background: var(--panel2); }
.file.active { background: var(--accentbg); color: var(--text); }
.file .name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
.badge {
  font-size: 10px;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 1px 6px;
}
.badge.notes { color: var(--note); border-color: var(--note); }
.main { min-width: 0; min-height: 0; overflow: hidden; display: flex; flex-direction: column; }
.toolbar {
  min-height: 70px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--panel);
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.path {
  font: 600 14px ui-monospace, SFMono-Regular, Consolas, monospace;
  min-width: 240px;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.controls { display: flex; gap: 6px; align-items: center; }
.controls button, .view-menu summary, .view-menu button {
  border: 1px solid var(--border);
  background: var(--panel2);
  color: var(--text);
  padding: 6px 10px;
  border-radius: 6px;
  cursor: pointer;
}
.controls button:hover, .view-menu summary:hover, .view-menu button:hover { border-color: var(--muted); }
.controls button.active { background: var(--accent); border-color: var(--accent); color: #fff; }
.view-menu { position: relative; }
.view-menu summary { list-style: none; user-select: none; }
.view-menu summary::-webkit-details-marker { display: none; }
.view-menu[open] summary { border-color: var(--accent); }
.view-menu-panel {
  position: absolute;
  right: 0;
  top: calc(100% + 6px);
  z-index: 20;
  width: 250px;
  padding: 10px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 12px 30px #0005;
}
.menu-label {
  font-size: 11px;
  font-weight: 700;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .05em;
  margin: 2px 0 6px;
}
.theme-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; }
.hidden-row, .git-row { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; margin-top: 6px; }
.view-menu button { padding: 5px 7px; font-size: 12px; }
.view-menu button.active { background: var(--accent); border-color: var(--accent); color: #fff; }
.menu-separator { height: 1px; background: var(--border); margin: 10px 0; }
.meta { width: 100%; color: var(--muted); font-size: 12px; display: flex; gap: 12px; flex-wrap: wrap; }
.diff {
  flex: 1 1 0;
  min-height: 0;
  overscroll-behavior: contain;
  font: 13px/1.45 ui-monospace, SFMono-Regular, Consolas, Liberation Mono, monospace;
}
.line {
  display: grid;
  grid-template-columns: 60px 60px 24px minmax(max-content, 1fr);
  min-height: 20px;
  border-left: 3px solid transparent;
}
.line:hover { background: var(--hover); }
.ln {
  padding: 1px 8px;
  text-align: right;
  color: var(--muted);
  user-select: none;
  border-right: 1px solid var(--gutter);
}
.prefix { padding: 1px 6px; text-align: center; color: var(--muted); user-select: none; }
.code { padding: 1px 10px; white-space: pre; }
.remove, .remove_note, .filtered_remove { background: var(--delbg); color: var(--del); border-left-color: #f85149; }
.add, .add_note, .filtered_add { background: var(--addbg); color: var(--add); border-left-color: #3fb950; }
.hunk, .title, .section, .selector, .selector_selected, .test_file_header, .dev_file_header, .file_header {
  background: var(--accentbg);
  color: var(--accent);
  font-weight: 600;
}
.filtered, .filtered_header, .handled { background: var(--hiddenbg); color: var(--hidden); }
.error { background: var(--delbg); color: var(--del); font-weight: 700; }
.empty { padding: 48px; text-align: center; color: var(--muted); }
.hidden-block {
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  background: var(--hiddenbg);
}
.hidden-block summary { cursor: pointer; list-style: none; user-select: none; }
.hidden-block summary::-webkit-details-marker { display: none; }
.hidden-block summary .line { background: transparent; }
.hidden-block summary .code::before { content: '▶ '; display: inline-block; width: 18px; }
.hidden-block[open] summary .code::before { content: '▼ '; }
.hidden-block-body { overflow: visible; }
.hidden-block .filtered_header .code { font-weight: 700; }
.review-panel {
  margin: 8px 12px 14px 147px;
  min-width: 560px;
  max-width: 980px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
  overflow: hidden;
  font: 13px/1.45 system-ui, -apple-system, Segoe UI, sans-serif;
}
.review-heading {
  display: flex;
  gap: 10px;
  align-items: baseline;
  justify-content: space-between;
  padding: 9px 12px;
  background: var(--panel2);
  border-bottom: 1px solid var(--border);
}
.review-label { font-weight: 700; }
.review-ranges { color: var(--muted); font-size: 12px; white-space: nowrap; }
.git-context { border-bottom: 1px solid var(--border); }
.git-context summary { cursor: pointer; padding: 8px 12px; color: var(--accent); user-select: none; }
.git-context[open] summary { border-bottom: 1px solid var(--border); }
.git-content { padding: 10px 12px; color: var(--text); }
.git-side + .git-side { margin-top: 10px; }
.git-side-title { color: var(--muted); font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; }
.commit {
  display: grid;
  grid-template-columns: 78px minmax(180px, 1fr) auto;
  gap: 8px;
  padding: 5px 0;
  border-bottom: 1px solid var(--border);
}
.commit:last-child { border-bottom: 0; }
.commit-hash { color: var(--accent); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
.commit-subject { overflow-wrap: anywhere; }
.commit-meta { color: var(--muted); font-size: 12px; white-space: nowrap; }
.no-context { color: var(--muted); padding: 4px 0; }
.note-wrap { padding: 10px 12px 12px; }
.note-label { display: flex; justify-content: space-between; gap: 10px; color: var(--note); font-weight: 700; margin-bottom: 6px; }
.note-help { color: var(--muted); font-weight: 400; font-size: 12px; }
.review-note {
  display: block;
  width: 100%;
  min-height: 72px;
  resize: vertical;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
  line-height: 1.4;
}
.review-note:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.footer {
  padding: 6px 12px;
  border-top: 1px solid var(--border);
  background: var(--panel);
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.footer.success { color: var(--add); }
.footer.error { color: var(--del); }
.footer.busy { color: var(--accent); }
@media (max-width: 800px) {
  .app { grid-template-columns: 240px minmax(0, 1fr); }
  .line { grid-template-columns: 46px 46px 20px minmax(max-content, 1fr); }
  .view-menu-panel { right: -4px; }
  .review-panel { margin-left: 115px; }
}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand"><h1>Config Review Web Diff</h1><p id="fileCount"></p></div>
    <div class="search"><input id="search" type="search" placeholder="Filter changed files…" autocomplete="off"></div>
    <div id="tree" class="tree"></div>
  </aside>
  <main class="main">
    <div class="toolbar">
      <div id="path" class="path"></div>
      <div class="controls">
        <button id="prev" title="Previous file ([)">← File</button>
        <button id="next" title="Next file (])">File →</button>
        <button id="focused" class="active">Focused</button>
        <button id="raw">Raw</button>
        <button id="saveReview" title="Save all current-view changes and reviewer notes as plaintext">Save review…</button>
        <details id="viewMenu" class="view-menu">
          <summary>View ▾</summary>
          <div class="view-menu-panel">
            <div class="menu-label">Theme</div>
            <div class="theme-row">
              <button type="button" data-theme-choice="system" class="active">System</button>
              <button type="button" data-theme-choice="dark">Dark</button>
              <button type="button" data-theme-choice="light">Light</button>
            </div>
            <div class="menu-separator"></div>
            <div class="menu-label">Hidden differences</div>
            <div class="hidden-row">
              <button id="expandHidden" type="button">Expand all</button>
              <button id="collapseHidden" type="button">Collapse all</button>
            </div>
            <div class="menu-separator"></div>
            <div class="menu-label">Git context</div>
            <div class="git-row">
              <button id="expandGit" type="button">Expand all</button>
              <button id="collapseGit" type="button">Collapse all</button>
            </div>
          </div>
        </details>
      </div>
      <div id="meta" class="meta"></div>
    </div>
    <div id="diff" class="diff"></div>
    <div id="footer" class="footer"></div>
  </main>
</div>
<script id="snapshot" type="application/json">__SNAPSHOT__</script>
<script>
'use strict';
const snapshot = JSON.parse(document.getElementById('snapshot').textContent);
let mode = 'focused';
let selected = 0;
let visible = snapshot.files.slice();
let themeChoice = 'system';
let notesDirty = false;
const notesByChange = new Map();
const gitContextCache = new Map();
const $ = id => document.getElementById(id);
const systemTheme = window.matchMedia('(prefers-color-scheme: light)');
const prefixFor = kind => kind.includes('remove') || kind === 'remove_note' ? '-' : kind.includes('add') || kind === 'add_note' ? '+' : kind === 'context' || kind === 'filtered_context' ? ' ' : '';

function applyTheme() {
  const resolved = themeChoice === 'system' ? (systemTheme.matches ? 'light' : 'dark') : themeChoice;
  document.documentElement.dataset.theme = resolved;
  document.querySelectorAll('[data-theme-choice]').forEach(button => {
    button.classList.toggle('active', button.dataset.themeChoice === themeChoice);
  });
}

systemTheme.addEventListener?.('change', () => {
  if (themeChoice === 'system') applyTheme();
});

function setStatus(message, kind = '') {
  const footer = $('footer');
  footer.textContent = message;
  footer.className = 'footer' + (kind ? ` ${kind}` : '');
}

function defaultStatus() {
  return `Snapshot ${snapshot.generatedAt} · ${snapshot.gitStatus} · notes remain in this browser until saved`;
}

function lineElement(line) {
  const row = document.createElement('div');
  row.className = 'line ' + line.kind;
  const tl = document.createElement('div');
  tl.className = 'ln';
  tl.textContent = line.testLine ?? '';
  const dl = document.createElement('div');
  dl.className = 'ln';
  dl.textContent = line.devLine ?? '';
  const prefix = document.createElement('div');
  prefix.className = 'prefix';
  prefix.textContent = prefixFor(line.kind);
  const code = document.createElement('div');
  code.className = 'code';
  code.textContent = line.text;
  row.append(tl, dl, prefix, code);
  return row;
}

function treeFrom(files) {
  const root = {folders: new Map(), files: []};
  for (const file of files) {
    const parts = file.path.split('/');
    let node = root;
    for (const part of parts.slice(0, -1)) {
      if (!node.folders.has(part)) node.folders.set(part, {folders: new Map(), files: []});
      node = node.folders.get(part);
    }
    node.files.push({file, name: parts.at(-1)});
  }
  return root;
}

function fileHasNotes(file) {
  for (const viewName of ['focused', 'raw']) {
    for (const change of file[viewName]?.changes ?? []) {
      if ((notesByChange.get(change.key) ?? '').trim()) return true;
    }
  }
  return false;
}

function renderNode(node, host) {
  for (const [name, child] of [...node.folders].sort((a, b) => a[0].localeCompare(b[0]))) {
    const details = document.createElement('details');
    details.open = true;
    const summary = document.createElement('summary');
    summary.textContent = '▾ ' + name;
    details.append(summary);
    const children = document.createElement('div');
    children.className = 'children';
    renderNode(child, children);
    details.append(children);
    host.append(details);
  }
  for (const item of node.files.sort((a, b) => a.name.localeCompare(b.name))) {
    const button = document.createElement('button');
    button.className = 'file' + (snapshot.files[selected] === item.file ? ' active' : '');
    button.title = item.file.path;
    button.onclick = () => {
      selected = snapshot.files.indexOf(item.file);
      render();
    };
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = item.name;
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = item.file.focused.visibleChanges;
    button.append(name, badge);
    if (fileHasNotes(item.file)) {
      const noteBadge = document.createElement('span');
      noteBadge.className = 'badge notes';
      noteBadge.textContent = 'note';
      button.append(noteBadge);
    }
    host.append(button);
  }
}

function renderTree() {
  const host = $('tree');
  host.replaceChildren();
  renderNode(treeFrom(visible), host);
  const noteCount = [...notesByChange.values()].filter(value => value.trim()).length;
  $('fileCount').textContent = `${snapshot.files.length} changed file${snapshot.files.length === 1 ? '' : 's'} · ${noteCount} note${noteCount === 1 ? '' : 's'}`;
}

async function getGitContext(change) {
  if (gitContextCache.has(change.gitContextId)) return gitContextCache.get(change.gitContextId);
  const promise = fetch(`git/${encodeURIComponent(change.gitContextId)}`, {
    credentials: 'same-origin',
    cache: 'no-store',
    headers: {'Accept': 'application/json'},
  }).then(async response => {
    if (!response.ok) throw new Error(`Git context request failed (${response.status})`);
    return response.json();
  }).catch(error => ({dev: [], test: [], error: error.message}));
  gitContextCache.set(change.gitContextId, promise);
  return promise;
}

function commitElement(context) {
  const row = document.createElement('div');
  row.className = 'commit';
  const hash = document.createElement('span');
  hash.className = 'commit-hash';
  hash.textContent = context.hash || '—';
  hash.title = context.source === 'line' ? 'Commit blamed for the changed line range' : 'Latest commit touching the file';
  const subject = document.createElement('span');
  subject.className = 'commit-subject';
  subject.textContent = context.subject || 'No commit subject';
  const meta = document.createElement('span');
  meta.className = 'commit-meta';
  meta.textContent = [context.author, context.date, context.source].filter(Boolean).join(' · ');
  row.append(hash, subject, meta);
  return row;
}

function gitSide(label, contexts) {
  const section = document.createElement('div');
  section.className = 'git-side';
  const title = document.createElement('div');
  title.className = 'git-side-title';
  title.textContent = label;
  section.append(title);
  if (!contexts.length) {
    const empty = document.createElement('div');
    empty.className = 'no-context';
    empty.textContent = 'No tracked commit context was available.';
    section.append(empty);
  } else {
    contexts.forEach(context => section.append(commitElement(context)));
  }
  return section;
}

async function loadGitContext(details, change) {
  const content = details.querySelector('.git-content');
  if (content.dataset.loaded === 'true') return;
  content.textContent = 'Loading local Git context…';
  const context = await getGitContext(change);
  content.replaceChildren();
  if (context.error) {
    const error = document.createElement('div');
    error.className = 'no-context';
    error.textContent = context.error;
    content.append(error);
  }
  content.append(gitSide('Incoming DEV', context.dev ?? []));
  content.append(gitSide('Current TEST', context.test ?? []));
  content.dataset.loaded = 'true';
  const newest = context.dev?.[0];
  if (newest) details.querySelector('summary').textContent = `Git context · DEV ${newest.hash} · ${newest.subject}`;
}

function reviewPanel(change) {
  const panel = document.createElement('section');
  panel.className = 'review-panel';
  panel.dataset.changeKey = change.key;

  const heading = document.createElement('div');
  heading.className = 'review-heading';
  const label = document.createElement('span');
  label.className = 'review-label';
  label.textContent = change.label;
  const ranges = document.createElement('span');
  ranges.className = 'review-ranges';
  ranges.textContent = `TEST ${change.testRange} → DEV ${change.devRange}`;
  heading.append(label, ranges);

  const gitDetails = document.createElement('details');
  gitDetails.className = 'git-context';
  const gitSummary = document.createElement('summary');
  gitSummary.textContent = 'Git context · show latest incoming commit message';
  const gitContent = document.createElement('div');
  gitContent.className = 'git-content';
  gitDetails.append(gitSummary, gitContent);
  gitDetails.addEventListener('toggle', () => {
    if (gitDetails.open) loadGitContext(gitDetails, change);
  });

  const noteWrap = document.createElement('div');
  noteWrap.className = 'note-wrap';
  const noteLabel = document.createElement('label');
  noteLabel.className = 'note-label';
  const noteTitle = document.createElement('span');
  noteTitle.textContent = 'Deployment note';
  const noteHelp = document.createElement('span');
  noteHelp.className = 'note-help';
  noteHelp.textContent = 'kept in this browser until Save review';
  noteLabel.append(noteTitle, noteHelp);
  const textarea = document.createElement('textarea');
  textarea.className = 'review-note';
  textarea.placeholder = 'Add context, a question, or a deployment follow-up for this change…';
  textarea.value = notesByChange.get(change.key) ?? '';
  textarea.addEventListener('input', () => {
    notesByChange.set(change.key, textarea.value);
    notesDirty = true;
    renderTree();
    setStatus('Unsaved reviewer notes · use Save review… to export them', 'busy');
  });
  noteWrap.append(noteLabel, textarea);

  panel.append(heading, gitDetails, noteWrap);
  return panel;
}

function panelsByEnd(view) {
  const result = new Map();
  for (const change of view.changes ?? []) {
    const end = change.panelAfter;
    if (!result.has(end)) result.set(end, []);
    result.get(end).push(change);
  }
  return result;
}

function appendPanels(host, byEnd, lineCount) {
  for (const change of byEnd.get(lineCount) ?? []) host.append(reviewPanel(change));
}

function appendRawLines(host, view) {
  const byEnd = panelsByEnd(view);
  for (let index = 0; index < view.lines.length; index++) {
    host.append(lineElement(view.lines[index]));
    appendPanels(host, byEnd, index + 1);
  }
}

function appendFocusedLines(host, view) {
  const lines = view.lines;
  const byEnd = panelsByEnd(view);
  let pendingContext = null;
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (line.kind === 'filtered_context' && lines[index + 1]?.kind === 'filtered_header') {
      pendingContext = line;
      appendPanels(host, byEnd, index + 1);
      continue;
    }
    if (line.kind !== 'filtered_header') {
      host.append(lineElement(line));
      appendPanels(host, byEnd, index + 1);
      continue;
    }
    const details = document.createElement('details');
    details.className = 'hidden-block';
    const summary = document.createElement('summary');
    const summaryLine = {...line, text: line.text.replace(/^▼\s*/, '')};
    summary.append(lineElement(summaryLine));
    details.append(summary);
    const body = document.createElement('div');
    body.className = 'hidden-block-body';
    if (pendingContext) {
      body.append(lineElement(pendingContext));
      pendingContext = null;
    }
    while (index + 1 < lines.length && lines[index + 1].kind.startsWith('filtered_')) {
      index++;
      body.append(lineElement(lines[index]));
    }
    details.append(body);
    host.append(details);
    appendPanels(host, byEnd, index + 1);
  }
}

function renderDiff() {
  const file = visible.length ? snapshot.files[selected] : null;
  if (!file) {
    $('path').textContent = 'No matching files';
    $('diff').innerHTML = '<div class="empty">No files match the search.</div>';
    return;
  }
  const view = mode === 'focused' ? (file.focusedExpanded ?? file.focused) : file.raw;
  const summaryView = mode === 'focused' ? file.focused : file.raw;
  $('path').textContent = file.path;
  $('focused').classList.toggle('active', mode === 'focused');
  $('raw').classList.toggle('active', mode === 'raw');
  const hidden = summaryView.noiseHidden + summaryView.whitespaceHidden + summaryView.orderHidden;
  $('meta').textContent = `${file.status} · ${summaryView.visibleChanges} visible change${summaryView.visibleChanges === 1 ? '' : 's'}${mode === 'focused' ? ` · ${hidden} hidden (click to expand) · ${summaryView.handled} handled` : ''}`;
  if (!notesDirty) setStatus(defaultStatus());

  const host = $('diff');
  host.replaceChildren();
  if (!view.lines.length || (summaryView.visibleChanges === 0 && mode === 'focused' && hidden === 0)) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = mode === 'focused' ? 'No visible Focused Diff changes. Switch to Raw to inspect all literal differences.' : 'No literal differences.';
    host.append(empty);
    return;
  }
  if (mode === 'focused') appendFocusedLines(host, view);
  else appendRawLines(host, view);
  host.scrollTop = 0;
  host.scrollLeft = 0;
}

function render() {
  const selectedFile = snapshot.files[selected];
  const query = $('search').value.trim().toLowerCase();
  visible = snapshot.files.filter(file => file.path.toLowerCase().includes(query));
  if (selectedFile && !visible.includes(selectedFile) && visible.length) selected = snapshot.files.indexOf(visible[0]);
  renderTree();
  renderDiff();
}

function move(delta) {
  if (!visible.length) return;
  const current = visible.indexOf(snapshot.files[selected]);
  const next = visible[(Math.max(0, current) + delta + visible.length) % visible.length];
  selected = snapshot.files.indexOf(next);
  render();
}

function setAllHidden(open) {
  document.querySelectorAll('.hidden-block').forEach(details => { details.open = open; });
  $('viewMenu').open = false;
}

function setAllGit(open) {
  document.querySelectorAll('.git-context').forEach(details => {
    details.open = open;
    if (open) {
      const key = details.closest('.review-panel')?.dataset.changeKey;
      const file = snapshot.files[selected];
      const view = mode === 'focused' ? (file.focusedExpanded ?? file.focused) : file.raw;
      const change = (view.changes ?? []).find(item => item.key === key);
      if (change) loadGitContext(details, change);
    }
  });
  $('viewMenu').open = false;
}

function exportFilename() {
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');
  return `config-review-${mode}-${stamp}.txt`;
}

async function chooseDestination(filename) {
  if (typeof window.showSaveFilePicker === 'function') {
    try {
      const handle = await window.showSaveFilePicker({
        suggestedName: filename,
        types: [{description: 'Plain text review', accept: {'text/plain': ['.txt']}}],
      });
      return {kind: 'picker', handle};
    } catch (error) {
      if (error?.name === 'AbortError') return {kind: 'cancelled'};
      throw error;
    }
  }
  return {kind: 'download', filename};
}

function formatCommitLines(side, contexts) {
  const lines = [`  ${side}:`];
  if (!contexts?.length) {
    lines.push('    No tracked commit context was available.');
    return lines;
  }
  for (const context of contexts) {
    const metadata = [context.hash, context.author, context.date, context.source].filter(Boolean).join(' · ');
    lines.push(`    ${metadata}`);
    lines.push(`    ${context.subject || 'No commit subject'}`);
  }
  return lines;
}

async function buildPlaintextReview() {
  const lines = [
    'CONFIG REVIEW WORKBENCH',
    '='.repeat(80),
    `Generated: ${new Date().toLocaleString()}`,
    `Snapshot:  ${snapshot.generatedAt}`,
    `View:      ${mode === 'focused' ? 'Focused Diff' : 'Raw Diff'}`,
    `TEST:      ${snapshot.target}`,
    `DEV:       ${snapshot.source}`,
    `Git:       ${snapshot.gitStatus}`,
    '',
  ];
  let exportedChanges = 0;
  for (const file of snapshot.files) {
    const view = mode === 'focused' ? file.focused : file.raw;
    const changes = view.changes ?? [];
    if (!changes.length) continue;
    lines.push('#'.repeat(80));
    lines.push(`FILE: ${file.path}`);
    lines.push(`STATUS: ${file.status}`);
    if (mode === 'focused') {
      const hidden = view.noiseHidden + view.whitespaceHidden + view.orderHidden;
      lines.push(`VISIBLE: ${view.visibleChanges} · HIDDEN: ${hidden} · HANDLED: ${view.handled}`);
    }
    lines.push('');
    for (let index = 0; index < changes.length; index++) {
      const change = changes[index];
      exportedChanges++;
      lines.push(`${index + 1}. ${change.label}`);
      lines.push('-'.repeat(80));
      lines.push(`TEST ${change.testRange} -> DEV ${change.devRange}`);
      lines.push('');
      for (const value of change.oldLines) lines.push(`- ${value}`);
      for (const value of change.newLines) lines.push(`+ ${value}`);
      if (!change.oldLines.length && !change.newLines.length) lines.push('  (No literal lines available for this logical change.)');
      lines.push('');
      const context = await getGitContext(change);
      lines.push('Git context:');
      lines.push(...formatCommitLines('Incoming DEV', context.dev));
      lines.push(...formatCommitLines('Current TEST', context.test));
      if (context.error) lines.push(`    Warning: ${context.error}`);
      lines.push('');
      const note = (notesByChange.get(change.key) ?? '').trim();
      lines.push('Reviewer note:');
      if (note) lines.push(...note.split(/\r?\n/).map(value => `  ${value}`));
      else lines.push('  (none)');
      lines.push('');
    }
  }
  if (!exportedChanges) return null;
  lines.push('='.repeat(80));
  lines.push(`Exported ${exportedChanges} change${exportedChanges === 1 ? '' : 's'} from the ${mode === 'focused' ? 'Focused' : 'Raw'} view.`);
  return lines.join('\n') + '\n';
}

async function writeReview(destination, text, filename) {
  if (destination.kind === 'picker') {
    const writable = await destination.handle.createWritable();
    await writable.write(text);
    await writable.close();
    return destination.handle.name || filename;
  }
  const blob = new Blob([text], {type: 'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = 'none';
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return filename;
}

async function saveReview() {
  const visibleChangeCount = snapshot.files.reduce((total, file) => {
    const view = mode === 'focused' ? file.focused : file.raw;
    return total + (view.changes ?? []).length;
  }, 0);
  if (!visibleChangeCount) {
    setStatus('No visible changes in the current view; no review file was created.', 'error');
    return;
  }
  const filename = exportFilename();
  let destination;
  try {
    // Request the browser picker immediately while the click still counts as a
    // user gesture; Git context is collected only after the destination exists.
    destination = await chooseDestination(filename);
  } catch (error) {
    setStatus(`Could not open save dialog: ${error.message}`, 'error');
    return;
  }
  if (destination.kind === 'cancelled') {
    setStatus('Save cancelled; reviewer notes remain in this browser.', '');
    return;
  }
  setStatus('Collecting Git context and building plaintext review…', 'busy');
  try {
    const text = await buildPlaintextReview();
    if (text === null) {
      setStatus('No visible changes in the current view; no review file was created.', 'error');
      return;
    }
    const savedName = await writeReview(destination, text, filename);
    notesDirty = false;
    setStatus(`Saved plaintext review: ${savedName}`, 'success');
  } catch (error) {
    setStatus(`Could not save review: ${error.message}`, 'error');
  }
}

$('search').addEventListener('input', render);
$('prev').onclick = () => move(-1);
$('next').onclick = () => move(1);
$('focused').onclick = () => { mode = 'focused'; renderDiff(); };
$('raw').onclick = () => { mode = 'raw'; renderDiff(); };
$('saveReview').onclick = saveReview;
$('expandHidden').onclick = () => setAllHidden(true);
$('collapseHidden').onclick = () => setAllHidden(false);
$('expandGit').onclick = () => setAllGit(true);
$('collapseGit').onclick = () => setAllGit(false);
document.querySelectorAll('[data-theme-choice]').forEach(button => {
  button.onclick = () => {
    themeChoice = button.dataset.themeChoice;
    applyTheme();
  };
});
document.addEventListener('keydown', event => {
  if (event.target === $('search') || event.target?.tagName === 'TEXTAREA') return;
  if (event.key === '[') {
    move(-1);
    event.preventDefault();
  } else if (event.key === ']') {
    move(1);
    event.preventDefault();
  } else if (event.key === 'f') {
    mode = 'focused';
    renderDiff();
  } else if (event.key === 'r') {
    mode = 'raw';
    renderDiff();
  } else if (event.key === '/') {
    $('search').focus();
    event.preventDefault();
  } else if (event.key.toLowerCase() === 'e' && mode === 'focused') {
    const blocks = [...document.querySelectorAll('.hidden-block')];
    const open = blocks.some(block => !block.open);
    setAllHidden(open);
  }
});
window.addEventListener('beforeunload', event => {
  if (!notesDirty) return;
  event.preventDefault();
  event.returnValue = '';
});
applyTheme();
render();
</script>
</body>
</html>"""
    return page.replace("__SNAPSHOT__", encoded).encode("utf-8")


class _ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    page: bytes
    token: str
    workbench: Workbench
    git_lookup: GitLookup
    git_cache: dict[str, bytes]
    git_cache_lock: threading.Lock


class _ViewerHandler(BaseHTTPRequestHandler):
    server: _ViewerServer
    server_version = "ConfigReviewWebViewer"
    sys_version = ""

    def _send_security_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(self._response_body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self._response_body = body
        self.send_response(status)
        self._send_security_headers(content_type)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        page_paths = {f"/{self.server.token}", f"/{self.server.token}/"}
        if path in page_paths:
            self._send_bytes(200, self.server.page, "text/html; charset=utf-8")
            return

        prefix = f"/{self.server.token}/git/"
        if not path.startswith(prefix):
            self.send_error(404)
            return
        context_id = path[len(prefix) :]
        lookup = self.server.git_lookup.get(context_id)
        if lookup is None or not context_id or "/" in context_id:
            self.send_error(404)
            return

        with self.server.git_cache_lock:
            cached = self.server.git_cache.get(context_id)
        if cached is not None:
            self._send_bytes(200, cached, "application/json; charset=utf-8")
            return

        record, block = lookup
        try:
            payload = _git_context_payload(self.server.workbench, record, block)
        except (OSError, WorkbenchError) as exc:
            payload = {"dev": [], "test": [], "error": str(exc)}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self.server.git_cache_lock:
            self.server.git_cache[context_id] = body
        self._send_bytes(200, body, "application/json; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_error(405)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class LocalWebDiffViewer:
    """Own one loopback-only browser review server thread."""

    def __init__(self) -> None:
        self._server: _ViewerServer | None = None
        self._thread: threading.Thread | None = None
        self.url: str | None = None

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        self.url = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def open(
        self,
        workbench: Workbench,
        *,
        open_browser: bool = True,
    ) -> WebViewerLaunch:
        """Start a fresh snapshot server and optionally open the default browser."""
        snapshot, git_lookup = _build_web_diff_snapshot(workbench)
        page = _render_page(snapshot)
        self.stop()
        token = secrets.token_urlsafe(18)
        server = _ViewerServer(("127.0.0.1", 0), _ViewerHandler)
        server.page = page
        server.token = token
        server.workbench = workbench
        server.git_lookup = git_lookup
        server.git_cache = {}
        server.git_cache_lock = threading.Lock()
        thread = threading.Thread(
            target=server.serve_forever,
            name="config-review-web-viewer",
            daemon=True,
        )
        thread.start()
        port = int(server.server_address[1])
        url = f"http://127.0.0.1:{port}/{token}/"
        self._server = server
        self._thread = thread
        self.url = url
        browser_opened = webbrowser.open(url, new=2) if open_browser else False
        return WebViewerLaunch(
            url=url,
            file_count=len(snapshot["files"]),
            browser_opened=bool(browser_opened),
        )
