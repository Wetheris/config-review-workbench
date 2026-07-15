# Config Review Workbench 1.0.0

Config Review Workbench is an interactive terminal application for reviewing exact
configuration differences from **DEV/incoming** into **TEST/current**.

It is designed for Kubernetes, Helm, OpenShift, YAML, and other text-based
configuration repositories. The tool deliberately avoids guessing whether two
configurations are semantically equivalent or whether a change is safe to promote.

## What it provides

- A grouped file list with active, complete, filtered-only, edited, and uncommitted states
- Focused Diff for collapsing approved environment-specific noise
- Full Diff for viewing the complete, literal TEST and DEV text
- Exact per-change actions: accept DEV, keep TEST, edit TEST, or open Vimdiff
- Project-wide pattern discovery and user-controlled filtering
- Session history, automatic progress saving, and current-run undo
- Conservative write validation, atomic file replacement, and symlink protection
- A single portable `config-review.pyz` executable for deployment

## Screenshots

### First project setup

On the first run, select one project directory containing sibling `dev` and `test`
directories. Relative paths are resolved from the terminal's current directory, and
Tab completion is supported when Python `readline` is available.

![First-run project setup](docs/images/first-run.png)

### Main file list

Files are grouped by their directory structure. Expand a file with Space to inspect
its active-change index, or press Enter to open the full Focused Diff. The footer uses
compact rows and automatically condenses on narrower terminals; press `c` to open the
Configure menu when you need less-common actions.

![Main file list](docs/images/main-screen.png)

### Focused Diff

The selected change has a yellow header and vertical guide. TEST/current values are
shown in red, while DEV/incoming values are shown in green.

![Focused Diff](docs/images/focused-diff.png)

### Pattern Manager

Press `p` from the main file list to inspect repeated project-wide replacements
before deciding whether Focused Diff should collapse them.

![Pattern Manager](docs/images/pattern-manager.png)

### Filtered differences

Filtered changes are still real differences. Focused Diff may collapse them after a
user-approved rule is enabled, while Full Diff always shows the original text.

![Collapsed and expanded filtered changes](docs/images/filtered-diff.png)

## Quick start

Run the packaged executable:

```bash
python3 config-review.pyz
```

From a source checkout:

```bash
python3 dist/config-review.pyz
```

The target system needs Python 3.10 or newer. The packaged archive includes the
pure-Python `ruamel.yaml` dependency.

## First-run project setup

The tool first searches nearby workspace directories for a project containing
sibling `dev` and `test` directories.

When no suitable project is found automatically, it asks for one project directory:

```text
Enter the project directory that contains the DEV and TEST folders.
Press Tab to complete paths. Relative paths start from your current directory:
  /home/user/repos/config-review-workbench/dist
Project directory: ../../examples/demo-project
```

You may provide:

- A relative project path
- An absolute project path
- The `dev` or `test` directory itself; the tool will use its parent when the sibling exists

Ctrl+C cancels setup cleanly.

Verified paths are stored in `.config-review.yaml`:

```yaml
version: 8
paths:
  project: ../../examples/demo-project
  source: dev
  target: test
```

Later launches reuse those paths. Explicit command-line paths override the saved
configuration for that run:

```bash
python3 config-review.pyz \
  --source /path/to/project/dev \
  --target /path/to/project/test
```

### Changing comparison paths later

Press `c` from the main file list and open **Comparison paths**. You can either:

- choose a new project root and let the tool find one sibling DEV/TEST pair beneath it; or
- set the exact DEV/source and TEST/target directories for a custom layout.

When review progress exists, the workbench asks to save the current session before
switching. The verified paths are then written to `.config-review.yaml`, the new
project is rescanned immediately, and any saved session for that comparison can be
loaded. Previously enabled project patterns are disabled during the switch so a new
comparison is never hidden using approvals from a different path; review and enable
them again from Pattern filters. Display settings remain project-configured.

## Basic walkthrough

1. **Start the workbench.**

   ```bash
   python3 config-review.pyz
   ```

2. **Select the project directory** containing `dev/` and `test/` when prompted.

3. **Review the main file list.** Yellow rows contain active differences. Green
   `COMPLETE` files have no remaining visible review work. Gray `FILTERED ONLY`
   files still contain differences hidden by approved filters.

4. **Press Enter on a file** to open Focused Diff. Use `j` and `k` to move through
   active changes. Arrow keys scroll the file without changing the selected block.

5. **Press Enter on the selected change** to open its action panel. Choose to accept
   DEV, keep TEST, edit TEST, pull DEV and edit, or open Vimdiff.

6. **Open Full Diff whenever needed.** Full Diff ignores every pattern and display
   filter and shows the literal current TEST and DEV text.

7. **Quit normally to save review progress.** The next launch asks whether to restore
   the saved session or start fresh.

## Keyboard controls

### Main file list

| Key | Action |
|---|---|
| `j` / `k`, `↑` / `↓` | Move through files and expanded changes |
| `Space` | Expand or collapse a file's change index |
| `Enter` | Open the selected file or selected change |
| `[` / `]` | Previous or next file |
| `c` | Open Configure for comparison paths, patterns, display filters, config editing, and rescan |
| `p` | Open Pattern Manager directly |
| `f` | Open Display Filters directly |
| `u` | Undo this run's changes for the selected file |
| `s` | Rescan DEV and TEST directly |
| `x` | Edit `.config-review.yaml` directly |
| `?` | Help |
| `q` | Quit |

### Configure menu

| Item | Purpose |
|---|---|
| Comparison paths | Change the project root or set exact DEV and TEST directories |
| Pattern filters | Review and toggle project-wide noise filters |
| Display filters | Control whitespace, YAML mapping-order, and focused contrast presentation |
| Edit project config | Open `.config-review.yaml` in the configured editor |
| Rescan | Refresh the current DEV and TEST directory trees |

The direct `p`, `f`, `s`, and `x` shortcuts remain available, but the compact footer
only advertises the most common navigation plus `c`, `?`, and `q` on small screens.

### Diff views

| Key | Action |
|---|---|
| `j` / `k` | Next or previous active change |
| `↑` / `↓`, `Page Up` / `Page Down` | Scroll through the file |
| `←` / `→` | Horizontal scrolling |
| `[` / `]` | Previous or next file |
| `Enter` | Open actions for the selected change |
| `h` | Expand or collapse filtered blocks |
| `v` | Switch between Focused Diff and Full Diff |
| `f` | Display Filters |
| `b` | Back |
| `q` | Quit |

## Focused Diff and Full Diff

**Focused Diff** may collapse changes matched by explicitly enabled project patterns
or display filters. Every collapsed block remains represented by a marker explaining
why it is hidden.

**Full Diff** never hides anything. It is the authoritative view when you need to
inspect the exact TEST/current and DEV/incoming text.

## Pattern Manager

Press `p` from the main file list to open the Pattern Manager. It scans the current
set of changed files for repeated TEST/current → DEV/incoming replacements and
groups suggestions into categories such as environment identity, application
domains, endpoints, user references, and storage identifiers.

Pattern suggestions are **visible by default**. Discovery alone never hides a
change.

### Understanding the columns

| Column | Meaning |
|---|---|
| `STATE` | `VISIBLE` means matching changes remain expanded in Focused Diff. `HIDDEN` means the rule is enabled and matching changes are collapsed. `LOCKED` identifies always-reviewed changes that patterns cannot hide. |
| `MATCHES` | Number of changed blocks matched by the pattern or category. |
| `FILES` | Number of unique files containing those matches. |
| `OVERLAP` | Number of matched blocks also covered by another suggested or saved pattern. |
| `CATEGORY / PATTERN` | The pattern group and the individual replacement rule. |

A hidden change is not removed, accepted, or marked complete. It is only collapsed
in Focused Diff with a `FILTERED DIFF (HIDDEN)` marker and a brief reason. Full Diff
always shows the original TEST and DEV lines.

### Reviewing and enabling patterns

- Use `↑` / `↓` or `j` / `k` to select a row.
- Press `Enter` on a pattern to preview its regexes and matching examples with nearby
  context.
- Press `Space` on an individual pattern to toggle it.
- Press `Space` on a category to toggle every pattern in that category.
- Press `f` to open Display Filters.
- Press `x` to inspect or edit `.config-review.yaml`.

Review a pattern's examples before enabling it. Suggested patterns are regex-based
evidence of a repeated replacement, not proof that the two values are semantically
equivalent. Broad hostname or endpoint suggestions deserve particular scrutiny.

When a changed block matches several patterns, `OVERLAP` reports that relationship.
The block remains hidden while **any** enabled matching pattern still applies. All
matching reasons remain available in Filter Details.

Enabled project patterns are saved in `.config-review.yaml` and apply across the
whole project on later runs.

For the exact candidate-generation rules and safeguards, see
[Filter Discovery Logic](docs/filter-discovery.md).

### Always-reviewed changes

Pattern rules cannot hide protected changes such as:

- Versions, image tags, chart versions, and revisions
- Replica counts and CPU or memory resources
- Security-related settings
- Additions and removals
- Structural changes

These rows appear as `LOCKED` or remain `VISIBLE`, even when nearby environment
differences are filtered.

## Display Filters

Display Filters are separate from project patterns:

- Show or hide whitespace-only changes
- Hide safe YAML mapping-order-only changes
- Mute non-focused diff content

Display Filters change how Focused Diff is presented. Full Diff remains completely
unfiltered.
## Applying changes safely

Accept DEV revalidates the selected hunk immediately before writing. If the current
TEST content no longer matches the reviewed block uniquely, the tool refuses the
operation and leaves the file untouched.

TEST writes use atomic replacement and preserve existing file modes. Symlinked TEST
paths are viewable but intentionally blocked from modification.

## Sessions and undo

Review progress is saved automatically when the tool exits. Saved sessions contain
fingerprints and review metadata rather than raw changed configuration values.

Undo Session Changes restores the selected TEST file to its state at the beginning
of the current process while preserving changes that already existed before launch.
Exact undo bytes are captured lazily before the tool's first write and remain
memory-only, so undo is unavailable after the process exits.

## Demo project

A sanitized sample project is included under:

```text
examples/demo-project/
├── dev/config/app.yaml
└── test/config/app.yaml
```

Run the workbench from the repository and choose `examples/demo-project` during
project setup to explore the basic workflow without using a real configuration repo.

## Development commands

Install the development tools once inside a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

The Makefile provides short, consistent commands for local development:

```bash
make help       # List all available commands
make test       # Run pytest
make self-test  # Run the application regression suite from source
make lint       # Compile Python and run Ruff correctness checks
make format     # Apply safe Ruff fixes and format Python files
make security   # Run Bandit and dependency vulnerability checks
make check      # Run all read-only checks
make build      # Test, build, and validate the packaged .pyz
make clean      # Remove generated build and cache files
```

The commands are intentionally separate. `make check` is read-only and is the best
final check before committing or pushing. `make format` is separate because it modifies
source files. `make build` validates both the source application and the generated
portable archive, since packaging can introduce problems that source-only tests cannot
catch.

## Build

Install the build dependency and create the portable executable:

```bash
python3 -m pip install -r requirements-build.txt
make build
```

The portable executable is created at:

```text
dist/config-review.pyz
```

## Test without Make

The underlying commands remain available when `make` is not installed:

```bash
python3 -m pytest -q
PYTHONPATH=src python3 -m config_review --self-test
python3 dist/config-review.pyz --self-test
python3 scripts/check_project.py all
```

## Run from source

```bash
PYTHONPATH=src python3 -m config_review \
  --source examples/demo-project/dev \
  --target examples/demo-project/test
```

## Source layout

- `core.py` — models, configuration, file safety, diff/filter engine, and sessions
- `rendering.py` — Focused Diff, Full Diff, summaries, and presentation building
- `workbench.py` — repository state and review actions
- `tui.py` — curses interface
- `plain.py` — line-oriented fallback interface
- `self_test.py` — built-in regression suite
- `cli.py` — command-line parsing and application startup

The source is modular for development and testing, while releases remain a single
portable executable archive.

## License

Config Review Workbench is available under the MIT License. See [LICENSE](LICENSE).

The portable `.pyz` bundles `ruamel.yaml`. Its copyright and license terms are
included in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and inside the built
archive.

## Known limitations

- Python 3.10 or newer is required.
- The curses TUI is intended primarily for Linux, macOS, WSL, and SSH terminals.
- The tool compares configuration as text and does not determine whether a change is
  operationally correct or safe to deploy.
- Mapping-order filtering requires YAML that can be parsed unambiguously.
- Current-run undo does not persist after the application exits.

## Release model

This repository begins its public release history at **1.0.0**. Features added during
initial development remain part of the 1.0.0 working release until a later public
version is explicitly chosen. Earlier build numbers were internal development
iterations and are not part of the published version history.
