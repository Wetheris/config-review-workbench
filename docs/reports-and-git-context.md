# Visible-Diff Reports and Git Context

The report feature is deliberately file-scoped. It does not generate a project-wide
release report or include files the reviewer did not open.

## Report scope

The report uses the same selectable change blocks as the current file view:

- **Focused Diff** includes only active differences that remain visible after noise
  filters, whitespace filtering, safe YAML order reconciliation, and handled-change
  history are applied.
- **Full Diff** includes the literal selectable text differences from the canonical raw
  diff.

This makes the report an export of what the reviewer is currently evaluating rather
than a second diff engine. Hidden differences remain available in Full Diff but are not
silently added to a Focused Diff report.

Reports are Markdown files saved under the visible `reports/` directory. That directory is
ignored by Git. The same report can be opened in the configured editor or printed to
the terminal. If the current view has zero selectable differences, the report menu is
blocked and no empty report or report directory is created.

Older builds used `.config-review-reports/`. Existing files there are left alone and the
old directory remains ignored, but newly generated reports use `reports/` so reviewers
can find and share them without enabling hidden-file display.

## Report layout

The Markdown output is organized for quick scanning:

1. A title names the current file and view.
2. A compact summary table shows short TEST/DEV paths, repository freshness, and what
   the current Focused Diff omitted.
3. Each visible change gets a numbered context heading and a one-line TEST-to-DEV
   location summary.
4. The literal changed lines appear in a dedicated diff block.
5. Optional Git attribution appears in a table below the corresponding change.

Paths are shown relative to the selected project or Git root when possible, rather than
as long machine-specific absolute paths.

## Context labels

Context labels are deterministic, offline hints. They do not claim to know why a
change was made. The classifier examines the changed lines plus a small amount of
nearby DEV and TEST text and labels common configuration areas such as:

- environment variables and named configuration items;
- image or version settings;
- endpoints and routing;
- resources and scaling;
- security configuration;
- logging and schedules;
- service networking;
- secret or credential references.

Unknown changes use the neutral label **Configuration value**. No configuration text
is sent to an external service.

## Git commit context

When Git context is enabled, each report change tries to identify useful local history
for both the TEST and DEV line ranges:

1. Run `git blame --line-porcelain` for the exact current line range.
2. Collect up to two unique commits affecting those lines.
3. Show the short hash, date, author, and commit subject.
4. If line attribution is unavailable, moved, untracked, or uncommitted, fall back to
   the latest commit touching that file and label it as a **file fallback**.

Commit text is context, not proof of intent. A moved block or broad commit can still
have a subject that does not fully explain the selected difference.

## Startup Git freshness check

On startup, the workbench finds the Git repository containing TEST and performs a
best-effort freshness check:

1. Read the current branch, commit, upstream, and working-tree status.
2. When an upstream exists, run a non-interactive
   `git fetch --quiet --prune --no-tags` with a bounded timeout.
3. Compare `HEAD` with the upstream tracking ref and report ahead/behind counts.

The check updates remote-tracking metadata only. It never runs `git pull`, merges,
resets, checks out branches, or modifies tracked configuration files.

When authentication, network access, or the remote is unavailable, startup continues
and remote freshness is shown as **unverified**. The Rescan action repeats both the
configuration scan and the Git freshness check.

## Limitations

- The check verifies the repository containing TEST. DEV and TEST are normally sibling
  directories in the same repository; unusual layouts should be verified manually.
- A clean and up-to-date branch does not guarantee that the selected directories point
  to the intended release environments.
- Uncommitted lines may not have a usable blame commit and therefore use file-level
  fallback context.
- Reports can contain configuration values visible in the diff. Treat generated reports
  with the same care as the source configuration.
