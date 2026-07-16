# Read-Only Web Diff Viewer

The web viewer is a browser-based overview for release reviewers who want to scan every
currently changed file without navigating the terminal interface one file at a time.
It is intentionally a viewer, not a second editing interface.

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
- The current file status, visible-change count, and hidden/handled summary

Keyboard shortcuts inside the browser:

| Key | Action |
|---|---|
| `[` / `]` | Previous or next changed file |
| `f` | Focused view |
| `r` | Raw view |
| `/` | Focus the filename search |

## Snapshot behavior

The browser page is an in-memory snapshot. It does not continuously watch the repository,
rerun Git, or reread source files. This avoids surprising background work and keeps the
terminal workbench authoritative.

Reopen the viewer from the terminal after:

- editing DEV or TEST;
- accepting or undoing a change;
- changing filters;
- switching comparison roots; or
- pulling newer Git content.

The footer in the browser shows when the snapshot was generated and the Git status known
at that time.

## Security model

The first version is deliberately conservative:

- The HTTP server binds only to `127.0.0.1`, never all network interfaces.
- The URL contains a random token and the server returns `404` for every other path.
- The page is read-only. There are no endpoints for edits, merges, commands, or file writes.
- The server exposes rendered diff data only; it cannot browse arbitrary filesystem paths.
- All HTML, CSS, and JavaScript is embedded. It loads no external assets and sends no telemetry.
- Browser caching is disabled and restrictive response headers are applied.
- Configuration text is inserted as JSON and rendered with `textContent`, preventing it from
  being interpreted as HTML.

A local process owned by the same user can still connect to loopback and inspect the page.
The random token reduces accidental discovery but is not a substitute for operating-system
account isolation.

## WSL and SSH

On a local WSL installation, the default Windows browser will often open the loopback URL
directly. Behavior depends on the installed browser integration.

When the workbench runs on a remote SSH host, `127.0.0.1` refers to that remote host. Use
SSH local port forwarding when policy permits, for example by forwarding the displayed
remote port to the same local port, then open the tokenized URL through the forwarded port.
The tool intentionally does not bind to a public interface to make remote access easier.

## Deliberate exclusions

The initial viewer does not include:

- editing or merge actions;
- mark-complete or undo controls;
- reports or Git blame details;
- filter configuration;
- live filesystem watching; or
- shared/multi-user hosting.

Keeping these actions in the terminal prevents two separate review workflows from drifting
apart and makes the browser safe to use as an at-a-glance companion.
