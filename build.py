#!/usr/bin/env python3
"""Build one portable config-review.pyz archive from the modular source tree."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")
SOURCE = ROOT / "src" / "config_review"
DEFAULT_OUTPUT = ROOT / "dist" / "config-review.pyz"


def vendor_ruamel(staging: Path) -> None:
    """Copy the installed pure-Python ruamel namespace into the archive."""
    spec = importlib.util.find_spec("ruamel.yaml")
    if spec is None or spec.origin is None:
        raise RuntimeError(
            "ruamel.yaml is not installed. Install requirements-build.txt or "
            "run build.py with --no-vendor-ruamel."
        )
    package_dir = Path(spec.origin).resolve().parents[1]  # .../site-packages/ruamel
    shutil.copytree(
        package_dir,
        staging / "ruamel",
        dirs_exist_ok=True,
        ignore=COPY_IGNORE,
    )


def build(output: Path, *, include_ruamel: bool = True) -> Path:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="config-review-build-") as temp_name:
        staging = Path(temp_name)
        shutil.copytree(SOURCE, staging / "config_review", ignore=COPY_IGNORE)
        (staging / "__main__.py").write_text(
            "from config_review.cli import main\nraise SystemExit(main())\n",
            encoding="utf-8",
        )
        if include_ruamel:
            vendor_ruamel(staging)
        zipapp.create_archive(
            staging,
            target=output,
            interpreter="/usr/bin/env python3",
            compressed=True,
        )
    output.chmod(output.stat().st_mode | 0o111)
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-vendor-ruamel", action="store_true")
    args = parser.parse_args()
    result = build(args.output, include_ruamel=not args.no_vendor_ruamel)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
