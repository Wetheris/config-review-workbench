"""Config Review Workbench Cli module.

Part of the modular Config Review Workbench source distribution. Build the portable
``dist/config-review.pyz`` executable with ``python build.py``.
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

try:
    import curses
except ImportError:  # pragma: no cover
    curses = None  # type: ignore[assignment]

try:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
except ImportError:
    YAML = None  # type: ignore[assignment]
    CommentedMap = dict  # type: ignore[assignment,misc]
    CommentedSeq = list  # type: ignore[assignment,misc]

from . import core as _core
from .core import (
    AppSettings,
    DEBUG_LOG_PATH,
    DEFAULT_PROJECT_CONFIG,
    VERSION,
    WorkbenchError,
    debug,
    find_git_root,
    init_project_config,
)
from .workbench import (
    Workbench,
)
from .tui import (
    Tui,
)
from .plain import (
    run_plain,
)
from .self_test import (
    run_regression_tests,
)

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="config-review",
        description=(
            "Interactive source-to-target configuration review workbench with user-approved "
            "project-wide pattern suggestions and an always-unfiltered Full Diff."
        )
    )
    parser.add_argument("--source", type=Path, default=Path("dev"), help="Incoming/source directory")
    parser.add_argument("--target", type=Path, default=Path("test"), help="Current/target directory to review or edit")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Project configuration file. Defaults to .config-review.yaml at the Git root "
            "or the common source/target parent."
        ),
    )
    parser.add_argument("--context", type=int, default=4, help="Context lines around unified diff hunks")
    parser.add_argument("--include-secrets", action="store_true", help="Include directories named secrets")
    parser.add_argument(
        "--edit-command",
        "--manual-editor",
        default=os.environ.get("EDITOR", "vim"),
        help="Command used to edit TEST or the project configuration",
    )
    parser.add_argument(
        "--vimdiff-command",
        "--editor",
        default="vimdiff",
        help="Command used for side-by-side TEST/DEV editing",
    )
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="Create a starter .config-review.yaml project configuration and exit",
    )
    parser.add_argument("--dry-run", action="store_true", help="Disable every workflow that can modify files")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the built-in regression tests and exit",
    )
    parser.add_argument("--no-tui", action="store_true", help="Use the line-oriented interface")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors in the line-oriented interface")
    parser.add_argument("--force-color", action="store_true", help="Force ANSI colors in the line-oriented interface")
    parser.add_argument("--debug", action="store_true", help="Print diagnostic information to stderr")
    parser.add_argument(
        "--debug-log",
        type=Path,
        default=None,
        help=(
            "Write parser/diff/filter diagnostics to a permission-restricted log file. "
            "This also enables debug logging and is recommended with the curses UI."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args(argv)
    if args.context < 0:
        parser.error("--context must be zero or greater")
    return args

def resolve_project_config(source: Path, target: Path, supplied: Path | None) -> Path:
    if supplied is not None:
        return supplied.expanduser().resolve()
    git_root = find_git_root(target) or find_git_root(source)
    if git_root is not None:
        return git_root / DEFAULT_PROJECT_CONFIG
    try:
        common = Path(os.path.commonpath([source, target]))
    except ValueError:
        common = Path.cwd()
    return common / DEFAULT_PROJECT_CONFIG

def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    _core.DEBUG_LOG_PATH = args.debug_log.expanduser().resolve() if args.debug_log else None
    _core.DEBUG_ENABLED = bool(args.debug or _core.DEBUG_LOG_PATH is not None)
    if args.no_color:
        _core.COLOR_ENABLED = False
    elif args.force_color:
        _core.COLOR_ENABLED = True

    debug(
        "Diagnostic logging enabled",
        version=VERSION,
        destination=str(_core.DEBUG_LOG_PATH) if _core.DEBUG_LOG_PATH else "stderr",
    )

    if args.self_test:
        return run_regression_tests()

    source = args.source.expanduser().resolve()
    target = args.target.expanduser().resolve()
    if not source.is_dir():
        print(f"error: DEV source directory does not exist: {source}", file=sys.stderr)
        return 2
    if not target.exists():
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"error: could not create TEST target directory {target}: {exc}", file=sys.stderr)
            return 2
    if not target.is_dir():
        print(f"error: TEST target is not a directory: {target}", file=sys.stderr)
        return 2

    config_file = resolve_project_config(source, target, args.config)
    if args.init_config:
        try:
            init_project_config(config_file)
        except WorkbenchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"Created {config_file}")
        return 0

    settings = AppSettings(
        source=source,
        target=target,
        config_file=config_file,
        context=args.context,
        include_secrets=args.include_secrets,
        edit_command=args.edit_command,
        vimdiff_command=args.vimdiff_command,
        dry_run=args.dry_run,
    )
    try:
        workbench = Workbench(settings)
    except WorkbenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for diagnostic in workbench.config_diagnostics:
        debug("config diagnostic", message=diagnostic)

    use_tui = (
        not args.no_tui
        and curses is not None
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM", "") not in {"", "dumb"}
    )
    if use_tui:
        try:
            curses.wrapper(Tui(workbench).run)
            return 0
        except curses.error as exc:
            print(f"warning: curses UI failed ({exc}); using line interface", file=sys.stderr)
    return run_plain(workbench)

