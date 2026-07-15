#!/usr/bin/env python3
"""Run the same quality, test, and security checks locally and in GitLab CI."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]

SOURCE_DIR_NAMES = ("src", "scripts", "tests")
SECURITY_DIR_NAMES = ("src", "scripts")


def existing_paths(names: Sequence[str]) -> list[str]:
    """Return repository-relative paths that currently exist."""
    return [name for name in names if (ROOT / name).exists()]


def run(command: Sequence[str], *, label: str) -> bool:
    """Run one check and return True when it succeeds."""
    print(f"\n== {label} ==")
    print("+", " ".join(command))
    try:
        result = subprocess.run(command, cwd=ROOT, check=False)
    except OSError as exc:
        print(f"ERROR: could not run {command[0]}: {exc}", file=sys.stderr)
        return False

    if result.returncode != 0:
        print(f"FAILED: {label} exited with status {result.returncode}", file=sys.stderr)
        return False
    return True


def quality_checks() -> bool:
    paths = existing_paths(SOURCE_DIR_NAMES)
    if not paths:
        print("ERROR: no source, scripts, or tests directories were found.", file=sys.stderr)
        return False

    ok = run(
        [sys.executable, "-m", "compileall", "-q", *paths],
        label="Python syntax compilation",
    )
    ok = run(["ruff", "check", *paths], label="Ruff lint") and ok
    return ok


def format_check() -> bool:
    paths = existing_paths(SOURCE_DIR_NAMES)
    if not paths:
        print("ERROR: no source, scripts, or tests directories were found.", file=sys.stderr)
        return False
    return run(["ruff", "format", "--check", *paths], label="Ruff format check")


def test_checks() -> bool:
    tests_dir = ROOT / "tests"
    test_files = sorted(tests_dir.rglob("test_*.py")) if tests_dir.exists() else []

    if not test_files:
        print(
            "ERROR: no tests/test_*.py files were found. "
            "Add tests before enabling the blocking test job.",
            file=sys.stderr,
        )
        return False

    # pytest is the authoritative test suite. Do not guess at an embedded
    # --self-test entry point based on text found inside arbitrary scripts.
    return run([sys.executable, "-m", "pytest", "-q"], label="pytest")


def dependency_requirement_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(ROOT.glob("requirements*.txt")):
        # requirements-dev.txt contains the scanners themselves. Auditing it
        # would report tool-chain dependencies instead of application inputs.
        if path.name == "requirements-dev.txt":
            continue
        files.append(path)
    return files


def security_checks() -> bool:
    scan_paths = existing_paths(SECURITY_DIR_NAMES)
    if not scan_paths:
        print("ERROR: no source or scripts directories were found.", file=sys.stderr)
        return False

    # Scan only repository-owned Python code. Scanning "." can descend into
    # .venv and produce findings from pytest, pip, Bandit, and other tools.
    ok = run(
        ["bandit", "-r", *scan_paths, "-x", "tests", "-ll", "-ii"],
        label="Bandit medium-and-higher security scan",
    )

    requirement_files = dependency_requirement_files()
    if requirement_files:
        for path in requirement_files:
            ok = (
                run(
                    ["pip-audit", "-r", str(path.relative_to(ROOT))],
                    label=f"Dependency audit ({path.name})",
                )
                and ok
            )
    else:
        print("\n== dependency audit ==")
        print("No runtime/build requirements files were found; skipping pip-audit.")

    return ok


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("quality", "format", "test", "security", "all"),
        nargs="?",
        default="all",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    checks = {
        "quality": quality_checks,
        "format": format_check,
        "test": test_checks,
        "security": security_checks,
    }
    selected = list(checks) if args.mode == "all" else [args.mode]

    ok = True
    for name in selected:
        ok = checks[name]() and ok

    print("\nAll selected checks passed." if ok else "\nOne or more checks failed.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
