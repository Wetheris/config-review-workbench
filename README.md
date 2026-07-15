# Config Review Workbench 1.0.0

Config Review Workbench is an interactive terminal application for reviewing
exact differences between an incoming **source** configuration tree and a
current **target** tree. It is useful for Kubernetes, Helm, OpenShift, YAML, and
other text-based configuration repositories while deliberately avoiding
automatic semantic merge guesses.

The interface may describe the two sides as **DEV/incoming** and
**TEST/current**, but the paths can point to any two project directories.

## Release model

This repository begins the public release history at **1.0.0**. Earlier build
numbers were internal development iterations and are not part of the published
version history.

The repository is modular for development and testing, while releases remain a
single portable executable archive:

```bash
./config-review.pyz --source environments/dev --target environments/test
```

The archive includes the pure-Python `ruamel.yaml` package when built with the
default `build.py` settings. The target host only needs Python 3.10 or newer.

## Project names

- Product: **Config Review Workbench**
- Repository: `config-review-workbench`
- Command: `config-review`
- Portable executable: `config-review.pyz`
- Python package: `config_review`
- Project configuration: `.config-review.yaml`

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
  --source environments/dev \
  --target environments/test
```

With no path arguments, the command compares `dev/` against `test/` in the
current directory.

## Project configuration

Create a starter project configuration with:

```bash
./config-review.pyz --init-config
```

This creates `.config-review.yaml` at the Git root or the common source/target
parent directory.

## GitHub publication

Create an empty GitHub repository named `config-review-workbench`, then run from
this directory:

```bash
git init
git add .
git commit -m "Release Config Review Workbench 1.0.0"
git branch -M main
git remote add origin git@github.com:YOUR-ACCOUNT/config-review-workbench.git
git push -u origin main

git tag -a v1.0.0 -m "Config Review Workbench 1.0.0"
git push origin v1.0.0
```

For releases, attach `dist/config-review.pyz` to a GitHub Release rather than
committing generated artifacts when your team prefers source-only history.
