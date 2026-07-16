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
- Clickable TEST and DEV remote line links when repository metadata is available
- Expandable hidden sections in Focused mode
- System, dark, and light themes under **View**
- A session-only **Hide sensitive values** privacy toggle under **View**
- A review panel beneath every active change, plus every hidden Focused change when expanded
- GitLab-style inline expansion for collapsed unchanged ranges
- Exact changed-text emphasis inside paired red/green lines
- Temporary hidden and reviewed file lists under **Review**
- A **Save review…** action that exports the current view as plaintext
- A **Copy displayed diff** action in both normal and privacy modes, including both TEST and
  DEV line-number columns
- Reviewed-files save and print actions

Keyboard shortcuts inside the browser:

| Key | Action |
|---|---|
| `[` / `]` | Previous or next changed file |
| `f` | Focused view |
| `r` | Raw view |
| `p` | Toggle privacy mode |
| `/` | Focus the filename search |
| `e` | Expand or collapse all hidden Focused sections |

## Per-change Git context

Git context is not loaded or displayed automatically. Select **Add Git context** beneath a
change to annotate the first changed line on each available side:

```text
- value: "test-value"
  Last changed in TEST · by Test Author · Test commit subject · a1b2c3d4
+ value: "dev-value"
  Last changed in DEV · by Dev Author · DEV commit subject · e5f6a7b8
```

TEST and DEV are resolved independently so each red/green side shows the history for that
physical line. The viewer tries `git blame` for the exact first changed line and falls back to
the latest commit touching that side's file when line attribution is unavailable or the line
is new. Multi-line changes receive one annotation on the first changed line per side. While
shown, the action becomes **Hide Git context**.

When a GitLab merge commit or squash commit contains a recognizable merge-request reference,
the abbreviated hash opens that merge request directly. Otherwise the hash opens the commit
details page. GitLab's commit page links associated merge requests when that association is
available, so the reviewer can still continue into the original review context without the
workbench requiring GitLab API credentials. Git context is local metadata from the checked-out
repository; selecting the button does not fetch, pull, or modify Git.

## Remote file sanity-check links

When the comparison lives in Git, clickable line numbers open the corresponding TEST or DEV
file in the remote repository at that exact line. Each change panel also includes TEST and DEV
range links. Links open in a new browser tab so the temporary local review workspace stays
open.

The workbench does not hardcode a GitLab hostname. It resolves the repository URL in this
order:

1. `git.repository_url` from the local `.config-review.yaml`;
2. the tracking remote associated with the current branch; and
3. `origin` as an auto-detection fallback.

Common SSH and HTTPS remotes are converted to a credential-free web URL. Configure →
**Git links** can set the complete repository URL explicitly, for example:

```yaml
git:
  repository_url: https://gitlab.example.com/group/project
```

Clearing that setting returns to remote auto-detection. The setting is local because
`.config-review.yaml` is ignored by Git, so private GitLab addresses do not need to enter the
public tool repository and a new build does not overwrite a teammate's choice.
If a web viewer is already open, press `w` again after changing the setting to create a fresh
snapshot containing the new links.

Links prefer the exact fetched upstream commit from the startup Git freshness check. If fetch
failed, no upstream exists, or the working tree is dirty, the link remains a useful comparison
but the browser footer and link tooltip explain why the remote page may not match the local
snapshot exactly. Untracked files or files outside the detected repository receive no link.

## Inline deployment notes

Select **Add note** beneath a change to open its deployment-note editor. Empty textareas are
not created automatically. After text is entered, the button becomes **Edit note** when the
editor is closed. Expanding a hidden Focused difference provides the same opt-in note action
for that exact hidden change. Notes are useful for questions, follow-up checks, release
decisions, or environment-specific reminders.

Notes:

- remain available while moving between files and Focused/Raw views in the current page;
- are not written into DEV, TEST, `.config-review.yaml`, or Git;
- are not silently persisted to browser storage; and
- are included when **Save review…** exports the current view.

The browser warns before closing a page that contains notes that have not been exported.
Reopening the web viewer creates a new snapshot and does not restore notes from the old
page.

## Inline context expansion

Long unchanged ranges are collapsed directly inside the main diff. A gray row with an `↑` or
`↓` control appears where aligned context was omitted. Each click reveals another ten lines
closest to the change, and repeated clicks can continue until the neighboring change or file
boundary is reached.

Only equal TEST/DEV lines are exposed as expandable context. The server calculates each gap
from the immutable launch-time snapshot and refuses arbitrary paths or ranges, so expansion
cannot become a general file browser. Moving to another file clears all expanded gap state;
returning to the file starts compact again. Notes and reviewed/hidden status are unaffected.

Each control is anchored to the exact physical boundary where those lines were omitted. It is
not attached to a logical change marker, so expanding leading, trailing, or between-change
context cannot place an earlier line number underneath a later one. The same boundary model is
used for normal rows and rows inside an expanded filtered block.

### Physical line-order safeguard

A uniquely named YAML list item can move to a very different location while also changing.
The terminal can safely present that as one logical replacement, but a single GitLab-style
two-column file timeline cannot place crossed TEST and DEV coordinates without making one
side's line numbers move backward.

When the web snapshot detects that condition, it keeps the literal rows in physical file order
but does not discard the semantic keyed-list result. The one real value change remains one
active review item. Its TEST and DEV rows appear at their separate physical positions, joined by
an **ACTIVE CHANGE continues** marker, while unchanged adjacent moved entries are collapsed as
**YAML keyed-list order** noise.

The browser labels this as **logical YAML changes mapped onto physical file positions**. This
keeps line-number gutters, expanded context, notes, and Git links trustworthy without turning
one logical value change back into duplicate add/delete panels. The terminal's compact logical
keyed-list review remains unchanged.

## Exact changed-text emphasis

When a removed line and an added line are similar enough to pair safely, the viewer emphasizes
the exact changed token inside the existing red and green line colors. For example,
`iesp-test-east` and `iesp-dev-east` emphasize only `test` and `dev`. Pairing is conservative
and monotonic; unrelated lines remain ordinary whole-line additions and removals.

The terminal uses the same computed ranges with bold reverse-video highlighting, because bold
alone is not reliably visible across terminal themes. Plaintext exports remain literal and do not insert
formatting characters into configuration values.

## Privacy mode for external review

Use **View → Hide sensitive values** before taking a screenshot, copying visible diff text, or
creating a plaintext review for an external analysis tool. The `p` shortcut toggles the same
session-only mode.

Privacy mode preserves keys, syntax, line numbers, and the fact that two protected values are
the same or different. Repeated protected values receive stable aliases such as `[SECRET-1]`,
`[PERSON-1]`, or `[ENDPOINT-1]` for the lifetime of that viewer snapshot. It recognizes common:

- credential keys and long token-like values;
- URLs, hosts, IP addresses, email addresses, UUIDs, and local user-directory names;
- user, owner, contact, author, assignee, principal, and service-account fields;
- namespace, cluster, environment, storage, repository, and similar internal identifiers;
- Kubernetes `secretKeyRef` and `configMapKeyRef` names and keys; and
- environment-variable values when the preceding variable name indicates a sensitive purpose.

While privacy mode is on, the browser also:

- removes remote-repository links from line gutters and change panels;
- removes the per-change Git-context and note actions so author names, commit subjects, and
  reviewer-note contents are not shown;
- replaces absolute DEV and TEST roots with generic labels in exports; and
- keeps **Copy displayed diff** available, but copies only the currently visible redacted diff
  rows, context-gap labels, and both line-number columns; and
- adds `-private` to exported filenames.

Privacy mode is deliberately conservative but heuristic. A secret stored under an innocent key,
or a person's name embedded in arbitrary prose, may not be recognized. Review the redacted output
before sending it outside the approved environment.

The toggle is **not** a security boundary. The original values remain in the local in-memory page
so the reviewer can switch back to the normal view. Browser developer tools or the page source can
still reveal them. Share only a privacy-mode plaintext export or screenshot; do not upload, save, or
send the viewer HTML itself.


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
include the current Focused or Raw changes, line ranges, any Git context explicitly added,
any non-empty notes, and the time each file was marked reviewed. Reviewed files with no
exportable changes in the current mode still appear so the report accurately reflects the
review checklist.

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
- separate TEST and DEV last-change context only where **Add Git context** was selected; and
- non-empty reviewer notes.

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
- Read-only endpoints return Git metadata or bounded aligned context gaps only for a known
  snapshot identifier.
- Plaintext export is performed by the browser only after an explicit save action.
- Privacy-mode exports contain the precomputed redacted values and omit Git context and notes, but
  the live local page still contains the original snapshot so the mode can be toggled off.
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
