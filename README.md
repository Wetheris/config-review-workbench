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

The first normal launch looks beneath the repository root for sibling directories
named `dev` and `test`. When one likely pair is found, the tool displays both
paths and asks you to confirm them. If multiple pairs are found, you can select
one; if no pair is found, the tool asks for the DEV/source and TEST/target
directories relative to the repository or executable location.

Verified paths are stored relative to `.config-review.yaml`:

```yaml
version: 8
paths:
  source: eids/dev
  target: eids/test
```

Later launches reuse these paths without prompting. Command-line paths override
the saved values for that run. Supplying both paths on an unconfigured project
also saves them automatically:

```bash
./config-review.pyz --source eids/dev --target eids/test
```

Non-interactive runs must provide both options or use a configuration file that
already contains `paths.source` and `paths.target`.

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
