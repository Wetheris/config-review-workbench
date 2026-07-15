# Config Review Workbench 1.0.0

Config Review Workbench is an interactive terminal workbench for reviewing exact configuration
differences from **DEV/incoming** into **TEST/current**. It is designed for
Kubernetes, Helm, OpenShift, and YAML-heavy repositories while deliberately
avoiding automatic semantic merge guesses.

## Release model

This repository begins the public release history at **1.0.0**. Features added during
initial development remain part of the 1.0.0 working release until a later public
version is explicitly chosen. Earlier build numbers were internal development
iterations and are not part of the published version history.

The repository is modular for development and testing, but releases remain one
portable executable archive:

```bash
./config-review.pyz
```

The archive includes the pure-Python `ruamel.yaml` package when built with the
default `build.py` settings. The target host only needs Python 3.10 or newer.

## First-run project setup

On the first normal launch, the tool searches nearby workspace directories for a
project that contains sibling `dev` and `test` folders. It shows the project and
environment paths and asks you to confirm them.

When automatic discovery cannot find the project, the tool asks for only the
**project directory** that contains `dev` and `test`:

```text
Enter the project directory that contains the DEV and TEST folders.
Press Tab to complete paths. Relative paths start from your current directory:
  /home/user/repos/config-review-workbench/dist
Project directory: ../../../devops/deployment-configurations/eids/
```

Tab completion is available on terminals with Python `readline` support. You may
also paste an absolute path. If you accidentally provide the `dev` or `test`
directory itself, the tool detects the sibling environment and uses the parent
project directory. Ctrl+C cancels setup cleanly.

Verified paths are stored portably in `.config-review.yaml` as one project plus
the environment directory names:

```yaml
version: 8
paths:
  project: ../../devops/deployment-configurations/eids
  source: dev
  target: test
```

Later launches reuse the saved project without prompting. Command-line paths
override the saved values for that run. Supplying both paths on an unconfigured
project also saves their common parent automatically:

```bash
./config-review.pyz --source /path/to/project/dev --target /path/to/project/test
```

Older configurations containing independent `paths.source` and `paths.target`
values remain supported. Non-interactive runs must use a configured project or
provide both command-line options.

## Source layout

- `core.py` — models, configuration, file safety, diff/filter engine, sessions
- `rendering.py` — Focused Diff, Full Diff, summaries, and presentation building
- `workbench.py` — repository state and review actions
- `tui.py` — curses interface
- `plain.py` — line-oriented fallback interface
- `self_test.py` — built-in regression suite
- `cli.py` — command-line parsing and application startup

## Build

```bash
python3 -m pip install -r requirements-build.txt
python3 build.py
```

The executable is created at `dist/config-review.pyz`.

## Test

```bash
PYTHONPATH=src python3 -m config_review --self-test
python3 dist/config-review.pyz --self-test
```

## Run from source

```bash
PYTHONPATH=src python3 -m config_review \
  --source eids/dev \
  --target eids/test
```

## GitHub publication

Create an empty GitHub repository, then run from this directory:

```bash
git init
git add .
git commit -m "Initial release: Config Review Workbench 1.0.0"
git branch -M main
git remote add origin git@github.com:YOUR-ACCOUNT/config-review.git
git push -u origin main
```

For releases, attach `dist/config-review.pyz` to a GitHub Release rather than
committing generated artifacts if your team prefers source-only history.
