# Local Web Diff Viewer

The web viewer is a browser-based companion for release reviewers who want to scan every
currently changed file without navigating the terminal interface one file at a time. It
remains review-only for DEV and TEST: it cannot merge, edit, update terminal completion, or
modify the comparison configuration. Its hide/review/note state is temporary browser memory.

## Opening it

Press `w` from the main file list. The workbench rescans DEV and TEST, builds a snapshot,
starts a temporary local server, and asks the operating system to open the URL in the
default browser.

If the browser does not open automatically, the terminal status line shows a URL similar
to:

```text
http://127.0.0.1:43127/random-token/
```

The server remains available while the workbench process is running. Pressing `w` again
replaces it with a newly generated snapshot.

## Interface

The viewer provides:

- A searchable directory tree containing only files with current DEV/TEST differences
- Previous and next changed-file navigation
- **Focused**, which mirrors the workbench's current noise and display filters
- **Raw**, which shows the complete literal text comparison
- TEST and DEV line-number gutters
- Expandable hidden sections in Focused mode
- System, dark, and light themes under **View**
- A review panel beneath every active change, plus every hidden Focused change when expanded
- Incremental TEST/DEV file context around each change
- Temporary hidden and reviewed file lists under **Review**
- A **Save review…** action that exports the current view as plaintext
- Reviewed-files save and print actions

Keyboard shortcuts inside the browser:

| Key | Action |
|---|---|
| `[` / `]` | Previous or next changed file |
| `f` | Focused view |
| `r` | Raw view |
| `/` | Focus the filename search |
| `e` | Expand or collapse all hidden Focused sections |

## Per-change Git context

Each reviewable change has a collapsed **Git context** section. It is loaded only when the
reviewer expands it, so opening the viewer does not run Git blame across every changed
line.

The incoming DEV side is shown first because it normally provides the most useful reason
for a release change. The viewer:

1. tries `git blame` for the exact changed lines;
2. displays the associated commit subject, author, date, and abbreviated hash; and
3. falls back to the latest commit touching the file when line attribution is unavailable
   or the line is new.

The TEST context is shown beneath DEV for comparison. Git context is local metadata from
the checked-out repository. It does not fetch, pull, or contact a remote when a section is
expanded.

## Inline deployment notes

A reviewer can type a deployment note beneath any active change. Expanding a hidden Focused difference also reveals a note field for that exact hidden change. Notes are useful for
questions, follow-up checks, release decisions, or environment-specific reminders.

Notes:

- remain available while moving between files and Focused/Raw views in the current page;
- are not written into DEV, TEST, `.config-review.yaml`, or Git;
- are not silently persisted to browser storage; and
- are included when **Save review…** exports the current view.

The browser warns before closing a page that contains notes that have not been exported.
Reopening the web viewer creates a new snapshot and does not restore notes from the old
page.

## Expandable file context

Open **File context** beneath any active or expanded hidden change to inspect nearby lines
from both TEST and DEV. The initial view includes ten lines above and below the changed range.
Use **Show 10 more above** or **Show 10 more below** repeatedly to expand in chunks until the
start or end of the file is reached.

Context is loaded from the immutable snapshot captured when the web viewer opened. It does not
reread the working tree. Context expansion is intentionally temporary: moving to another file
clears all expanded-context ranges, so returning to the file starts compact again. Notes and
review status are not cleared by file navigation.

## Temporary file review workflow

The file header provides two separate actions:

- **Hide file** removes the file from the active tree because it is not useful to inspect now.
- **Mark reviewed** records that the reviewer finished the file and moves it into the Reviewed
  list.

Hidden does not mean reviewed. The **Review** menu reports Remaining, Reviewed, and Hidden
counts, lists both groups separately, and allows hidden files to be restored or reviewed files
to be reopened and marked unreviewed. A file can remain reviewed even if it is also hidden;
reviewed status controls reviewed-report inclusion, while hidden status only controls active
navigation.

All of this state is held in JavaScript memory. Refreshing or closing the page resets hidden
files, reviewed files, review timestamps, notes, and context expansion. Nothing is written to
Git, DEV, TEST, `.config-review.yaml`, browser storage, or the terminal review session.

### Reviewed-files reports

The Review menu can **Save reviewed report…** as plaintext or **Print reviewed report…** using
the browser print dialog. These reports contain only files explicitly marked reviewed and
include the current Focused or Raw changes, line ranges, Git context, notes, and the time each
file was marked reviewed. Reviewed files with no exportable changes in the current mode still
appear so the report accurately reflects the review checklist.

## Plaintext export

**Save review…** exports the currently selected mode:

- Focused exports every active quick-review change. A hidden Focused change is included only
  when the reviewer deliberately added a note to it, so filtered noise does not flood the
  output by default.
- Raw exports every literal text change.

The plaintext file contains:

- snapshot time, TEST and DEV roots, and known Git status;
- each changed file and its status;
- deterministic context labels and TEST-to-DEV line ranges;
- removed and added lines;
- incoming DEV and current TEST commit context; and
- the reviewer's inline note for each change.

Microsoft Edge and other Chromium-based browsers use the browser's native file-save dialog
when the File System Access API is available. Browsers without that API fall back to a
normal `.txt` download. Cancelling the dialog leaves the notes in the page. The tool does
not choose or write an export path without the reviewer explicitly using **Save review…**.

## Snapshot behavior

The browser page is an in-memory snapshot. It does not continuously watch the repository,
rerun the comparison, or reread source files. This avoids surprising background work and
keeps the terminal workbench authoritative.

Reopen the viewer from the terminal after:

- editing DEV or TEST;
- accepting or undoing a change;
- changing filters;
- switching comparison roots; or
- pulling newer Git content.

The footer in the browser shows when the snapshot was generated and the Git status known
at that time.

## Security model

The viewer remains conservative:

- The HTTP server binds only to `127.0.0.1`, never all network interfaces.
- The URL contains a random token and the server returns `404` for every other path.
- There are no endpoints for edits, merges, commands, notes, exports, or arbitrary file
  writes.
- Read-only endpoints return Git metadata or bounded nearby lines only for a known snapshot
  change identifier.
- Plaintext export is performed by the browser only after an explicit save action.
- The server exposes rendered diff data only; it cannot browse arbitrary filesystem paths.
- All HTML, CSS, and JavaScript is embedded. It loads no external assets and sends no
  telemetry.
- Browser caching is disabled and restrictive response headers are applied.
- Configuration text, commit messages, and notes are rendered with text-only DOM APIs rather
  than interpreted as HTML.

A local process owned by the same user can still connect to loopback and inspect the page.
The random token reduces accidental discovery but is not a substitute for operating-system
account isolation.

## WSL and SSH

On a local WSL installation, the workbench uses one explicit Windows browser handoff
through `cmd.exe` when available, with `wslview` as a fallback. It does not call the generic
Python browser chain under WSL, which avoids duplicate tabs and noisy failed `gio` attempts.
If neither handoff is available, the terminal prints the URL for manual opening.

When the workbench runs on a remote SSH host, `127.0.0.1` refers to that remote host. Use
SSH local port forwarding when policy permits, for example by forwarding the displayed
remote port to the same local port, then open the tokenized URL through the forwarded port.
The tool intentionally does not bind to a public interface to make remote access easier.

## Deliberate exclusions

The viewer still does not include:

- editing or merge actions;
- terminal mark-complete or undo controls;
- filter configuration;
- live filesystem watching;
- persistent/shared comments; or
- shared multi-user hosting.

Keeping deployment-changing actions in the terminal prevents two separate workflows from
drifting apart. Browser notes and exports are personal review aids, not shared workflow
state.
