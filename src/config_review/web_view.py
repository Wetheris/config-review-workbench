"""Read-only local web diff viewer.

The viewer deliberately serves a static snapshot generated from the workbench's
existing Focused Diff and Full Diff presentations. It binds only to loopback,
uses a random URL token, performs no writes, and has no external assets.
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from .core import DiffPresentation, DisplayLine, WorkbenchError
from .rendering import full_unified_diff, review_unified_diff

if TYPE_CHECKING:
    from .workbench import Workbench


@dataclass(slots=True, frozen=True)
class WebViewerLaunch:
    """Details for one started local viewer."""

    url: str
    file_count: int
    browser_opened: bool


def _display_line_payload(line: DisplayLine) -> dict[str, Any]:
    return {
        "text": line.text,
        "kind": line.kind,
        "testLine": line.test_line,
        "devLine": line.dev_line,
    }


def _presentation_payload(presentation: DiffPresentation) -> dict[str, Any]:
    return {
        "lines": [_display_line_payload(line) for line in presentation.lines],
        "visibleChanges": presentation.visible_change_count,
        "handled": presentation.handled_count,
        "noiseHidden": presentation.pattern_hidden_count,
        "whitespaceHidden": presentation.whitespace_hidden_count,
        "orderHidden": presentation.mapping_order_hidden_count,
        "orderUnavailable": presentation.mapping_order_unavailable_reason,
    }


def build_web_diff_snapshot(workbench: Workbench) -> dict[str, Any]:
    """Build a read-only snapshot from the workbench's canonical diff views."""
    files: list[dict[str, Any]] = []
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
                "focused": _presentation_payload(focused),
                "focusedExpanded": _presentation_payload(focused_expanded),
                "raw": _presentation_payload(full),
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

    return {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(workbench.settings.source),
        "target": str(workbench.settings.target),
        "gitStatus": workbench.git_status.summary,
        "files": files,
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
:root{color-scheme:dark;--bg:#0d1117;--panel:#161b22;--panel2:#21262d;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--accent:#58a6ff;--accentbg:#1f6feb33;--add:#aff5b4;--addbg:#033a16;--del:#ffdcd7;--delbg:#67060c;--hidden:#d2a8ff;--hiddenbg:#8957e522;--hover:#ffffff08;--gutter:#21262d;--scroll-thumb:#484f58;--scroll-track:#161b22}
:root[data-theme="light"]{color-scheme:light;--bg:#ffffff;--panel:#f6f8fa;--panel2:#ffffff;--border:#d0d7de;--text:#1f2328;--muted:#59636e;--accent:#0969da;--accentbg:#ddf4ff;--add:#116329;--addbg:#dafbe1;--del:#82071e;--delbg:#ffebe9;--hidden:#6639ba;--hiddenbg:#fbefff;--hover:#818b981a;--gutter:#d8dee4;--scroll-thumb:#afb8c1;--scroll-track:#f6f8fa}
*{box-sizing:border-box}html,body{height:100%;margin:0}body{font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--text);overflow:hidden}button,input,select{font:inherit;color:inherit}.app{height:100%;display:grid;grid-template-columns:330px 1fr}.sidebar{min-width:0;background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column}.brand{padding:18px 16px 12px;border-bottom:1px solid var(--border)}.brand h1{font-size:16px;margin:0 0 4px}.brand p{margin:0;color:var(--muted);font-size:12px}.search{padding:12px;border-bottom:1px solid var(--border)}.search input{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text)}.tree,.diff{overflow:auto;scrollbar-gutter:stable;scrollbar-color:var(--scroll-thumb) var(--scroll-track);scrollbar-width:thin}.tree::-webkit-scrollbar,.diff::-webkit-scrollbar{width:12px;height:12px}.tree::-webkit-scrollbar-track,.diff::-webkit-scrollbar-track{background:var(--scroll-track)}.tree::-webkit-scrollbar-thumb,.diff::-webkit-scrollbar-thumb{background:var(--scroll-thumb);border:3px solid var(--scroll-track);border-radius:999px}.tree{padding:8px;flex:1}.tree details{margin:1px 0}.tree summary{cursor:pointer;color:var(--muted);padding:4px 6px;user-select:none}.tree .children{padding-left:14px}.file{width:100%;display:flex;gap:8px;align-items:center;text-align:left;border:0;border-radius:6px;padding:6px 8px;color:var(--text);background:transparent;cursor:pointer}.file:hover{background:var(--panel2)}.file.active{background:var(--accentbg);color:var(--text)}.file .name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}.badge{font-size:10px;color:var(--muted);border:1px solid var(--border);border-radius:999px;padding:1px 6px}.main{min-width:0;display:flex;flex-direction:column}.toolbar{min-height:70px;padding:12px 16px;border-bottom:1px solid var(--border);background:var(--panel);display:flex;align-items:center;gap:12px;flex-wrap:wrap}.path{font:600 14px ui-monospace,SFMono-Regular,Consolas,monospace;min-width:240px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.controls{display:flex;gap:6px;align-items:center}.controls button,.view-menu summary,.view-menu button{border:1px solid var(--border);background:var(--panel2);color:var(--text);padding:6px 10px;border-radius:6px;cursor:pointer}.controls button:hover,.view-menu summary:hover,.view-menu button:hover{border-color:var(--muted)}.controls button.active{background:var(--accent);border-color:var(--accent);color:#fff}.view-menu{position:relative}.view-menu summary{list-style:none;user-select:none}.view-menu summary::-webkit-details-marker{display:none}.view-menu[open] summary{border-color:var(--accent)}.view-menu-panel{position:absolute;right:0;top:calc(100% + 6px);z-index:20;width:240px;padding:10px;background:var(--panel);border:1px solid var(--border);border-radius:8px;box-shadow:0 12px 30px #0005}.menu-label{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin:2px 0 6px}.theme-row,.hidden-row{display:grid;grid-template-columns:repeat(3,1fr);gap:5px}.hidden-row{grid-template-columns:1fr 1fr;margin-top:6px}.view-menu button{padding:5px 7px;font-size:12px}.view-menu button.active{background:var(--accent);border-color:var(--accent);color:#fff}.menu-separator{height:1px;background:var(--border);margin:10px 0}.meta{width:100%;color:var(--muted);font-size:12px;display:flex;gap:12px;flex-wrap:wrap}.diff{flex:1;font:13px/1.45 ui-monospace,SFMono-Regular,Consolas,Liberation Mono,monospace}.line{display:grid;grid-template-columns:60px 60px 24px minmax(max-content,1fr);min-height:20px;border-left:3px solid transparent}.line:hover{background:var(--hover)}.ln{padding:1px 8px;text-align:right;color:var(--muted);user-select:none;border-right:1px solid var(--gutter)}.prefix{padding:1px 6px;text-align:center;color:var(--muted);user-select:none}.code{padding:1px 10px;white-space:pre}.remove,.remove_note,.filtered_remove{background:var(--delbg);color:var(--del);border-left-color:#f85149}.add,.add_note,.filtered_add{background:var(--addbg);color:var(--add);border-left-color:#3fb950}.hunk,.title,.section,.selector,.selector_selected,.test_file_header,.dev_file_header,.file_header{background:var(--accentbg);color:var(--accent);font-weight:600}.filtered,.filtered_header,.handled{background:var(--hiddenbg);color:var(--hidden)}.error{background:var(--delbg);color:var(--del);font-weight:700}.empty{padding:48px;text-align:center;color:var(--muted)}.hidden-block{border-top:1px solid var(--border);border-bottom:1px solid var(--border);background:var(--hiddenbg)}.hidden-block summary{cursor:pointer;list-style:none;user-select:none}.hidden-block summary::-webkit-details-marker{display:none}.hidden-block summary .line{background:transparent}.hidden-block summary .code::before{content:'▶ ';display:inline-block;width:18px}.hidden-block[open] summary .code::before{content:'▼ '}.hidden-block-body{overflow:visible}.hidden-block .filtered_header .code{font-weight:700}.footer{padding:6px 12px;border-top:1px solid var(--border);background:var(--panel);color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@media(max-width:800px){.app{grid-template-columns:240px 1fr}.line{grid-template-columns:46px 46px 20px minmax(max-content,1fr)}.view-menu-panel{right:-4px}}
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
const snapshot=JSON.parse(document.getElementById('snapshot').textContent);
let mode='focused',selected=0,visible=snapshot.files.slice(),themeChoice='system';
const $=id=>document.getElementById(id);
const systemTheme=window.matchMedia('(prefers-color-scheme: light)');
const prefixFor=kind=>kind.includes('remove')||kind==='remove_note'?'-':kind.includes('add')||kind==='add_note'?'+':kind==='context'||kind==='filtered_context'?' ':'';
function applyTheme(){const resolved=themeChoice==='system'?(systemTheme.matches?'light':'dark'):themeChoice;document.documentElement.dataset.theme=resolved;document.querySelectorAll('[data-theme-choice]').forEach(button=>button.classList.toggle('active',button.dataset.themeChoice===themeChoice))}
systemTheme.addEventListener?.('change',()=>{if(themeChoice==='system')applyTheme()});
function lineElement(line){const row=document.createElement('div');row.className='line '+line.kind;const tl=document.createElement('div');tl.className='ln';tl.textContent=line.testLine??'';const dl=document.createElement('div');dl.className='ln';dl.textContent=line.devLine??'';const p=document.createElement('div');p.className='prefix';p.textContent=prefixFor(line.kind);const code=document.createElement('div');code.className='code';code.textContent=line.text;row.append(tl,dl,p,code);return row}
function treeFrom(files){const root={folders:new Map(),files:[]};for(const file of files){const parts=file.path.split('/');let node=root;for(const part of parts.slice(0,-1)){if(!node.folders.has(part))node.folders.set(part,{folders:new Map(),files:[]});node=node.folders.get(part)}node.files.push({file,name:parts.at(-1)})}return root}
function renderNode(node,host){for(const [name,child] of [...node.folders].sort((a,b)=>a[0].localeCompare(b[0]))){const d=document.createElement('details');d.open=true;const s=document.createElement('summary');s.textContent='▾ '+name;d.append(s);const c=document.createElement('div');c.className='children';renderNode(child,c);d.append(c);host.append(d)}for(const item of node.files.sort((a,b)=>a.name.localeCompare(b.name))){const b=document.createElement('button');b.className='file'+(snapshot.files[selected]===item.file?' active':'');b.title=item.file.path;b.onclick=()=>{selected=snapshot.files.indexOf(item.file);render()};const n=document.createElement('span');n.className='name';n.textContent=item.name;const badge=document.createElement('span');badge.className='badge';badge.textContent=item.file.focused.visibleChanges;b.append(n,badge);host.append(b)}}
function renderTree(){const host=$('tree');host.replaceChildren();renderNode(treeFrom(visible),host);$('fileCount').textContent=`${snapshot.files.length} changed file${snapshot.files.length===1?'':'s'} · read-only snapshot`}
function appendFocusedLines(host,lines){let pendingContext=null;for(let index=0;index<lines.length;index++){const line=lines[index];if(line.kind==='filtered_context'&&lines[index+1]?.kind==='filtered_header'){pendingContext=line;continue}if(line.kind!=='filtered_header'){host.append(lineElement(line));continue}const details=document.createElement('details');details.className='hidden-block';const summary=document.createElement('summary');const summaryLine={...line,text:line.text.replace(/^▼\s*/,'')};summary.append(lineElement(summaryLine));details.append(summary);const body=document.createElement('div');body.className='hidden-block-body';if(pendingContext){body.append(lineElement(pendingContext));pendingContext=null}while(index+1<lines.length&&lines[index+1].kind.startsWith('filtered_')){index++;body.append(lineElement(lines[index]))}details.append(body);host.append(details)}}
function renderDiff(){const file=visible.length?snapshot.files[selected]:null;if(!file){$('path').textContent='No matching files';$('diff').innerHTML='<div class="empty">No files match the search.</div>';return}const view=mode==='focused'?(file.focusedExpanded??file.focused):file.raw;const summaryView=mode==='focused'?file.focused:file.raw;$('path').textContent=file.path;$('focused').classList.toggle('active',mode==='focused');$('raw').classList.toggle('active',mode==='raw');const hidden=summaryView.noiseHidden+summaryView.whitespaceHidden+summaryView.orderHidden;$('meta').textContent=`${file.status} · ${summaryView.visibleChanges} visible change${summaryView.visibleChanges===1?'':'s'}${mode==='focused'?` · ${hidden} hidden (click to expand) · ${summaryView.handled} handled`:''}`;$('footer').textContent=`Snapshot ${snapshot.generatedAt} · ${snapshot.gitStatus} · reopen from the terminal to refresh`;
const host=$('diff');host.replaceChildren();if(!view.lines.length||summaryView.visibleChanges===0&&mode==='focused'&&hidden===0){const e=document.createElement('div');e.className='empty';e.textContent=mode==='focused'?'No visible Focused Diff changes. Switch to Raw to inspect all literal differences.':'No literal differences.';host.append(e);return}if(mode==='focused')appendFocusedLines(host,view.lines);else for(const line of view.lines)host.append(lineElement(line));host.scrollTop=0;host.scrollLeft=0}
function render(){const selectedFile=snapshot.files[selected];visible=snapshot.files.filter(f=>f.path.toLowerCase().includes($('search').value.trim().toLowerCase()));if(selectedFile&&!visible.includes(selectedFile)&&visible.length)selected=snapshot.files.indexOf(visible[0]);renderTree();renderDiff()}
function move(delta){if(!visible.length)return;const current=visible.indexOf(snapshot.files[selected]);const next=visible[(Math.max(0,current)+delta+visible.length)%visible.length];selected=snapshot.files.indexOf(next);render()}
function setAllHidden(open){document.querySelectorAll('.hidden-block').forEach(details=>{details.open=open});$('viewMenu').open=false}
$('search').addEventListener('input',render);$('prev').onclick=()=>move(-1);$('next').onclick=()=>move(1);$('focused').onclick=()=>{mode='focused';renderDiff()};$('raw').onclick=()=>{mode='raw';renderDiff()};$('expandHidden').onclick=()=>setAllHidden(true);$('collapseHidden').onclick=()=>setAllHidden(false);document.querySelectorAll('[data-theme-choice]').forEach(button=>button.onclick=()=>{themeChoice=button.dataset.themeChoice;applyTheme()});document.addEventListener('keydown',e=>{if(e.target===$('search'))return;if(e.key==='['){move(-1);e.preventDefault()}else if(e.key===']'){move(1);e.preventDefault()}else if(e.key==='f'){mode='focused';renderDiff()}else if(e.key==='r'){mode='raw';renderDiff()}else if(e.key==='/'){$('search').focus();e.preventDefault()}else if(e.key.toLowerCase()==='e'&&mode==='focused'){const blocks=[...document.querySelectorAll('.hidden-block')];const open=blocks.some(block=>!block.open);setAllHidden(open)}});applyTheme();render();
</script>
</body>
</html>"""
    return page.replace("__SNAPSHOT__", encoded).encode("utf-8")


class _ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    page: bytes
    token: str


class _ViewerHandler(BaseHTTPRequestHandler):
    server: _ViewerServer
    server_version = "ConfigReviewWebViewer"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        allowed = {f"/{self.server.token}", f"/{self.server.token}/"}
        path = self.path.split("?", 1)[0]
        if path not in allowed:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.server.page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
        )
        self.end_headers()
        self.wfile.write(self.server.page)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class LocalWebDiffViewer:
    """Own one loopback-only read-only web viewer process thread."""

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
        snapshot = build_web_diff_snapshot(workbench)
        page = _render_page(snapshot)
        self.stop()
        token = secrets.token_urlsafe(18)
        server = _ViewerServer(("127.0.0.1", 0), _ViewerHandler)
        server.page = page
        server.token = token
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
