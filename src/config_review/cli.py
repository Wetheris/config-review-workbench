"""Config Review Workbench Cli module.

Part of the modular Config Review Workbench source distribution. Build the portable
``dist/config-review.pyz`` executable with ``python build.py``.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path
from typing import Sequence

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
    DEFAULT_EXCLUDED_DIRS,
    DEFAULT_PROJECT_CONFIG,
    VERSION,
    WorkbenchError,
    debug,
    find_git_root,
    init_project_config,
    load_project_path_settings,
    resolve_configured_project_paths,
    save_project_paths,
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
            "Interactive source-to-target configuration review workbench with auditable "
            "project-wide noise filters and an always-unfiltered Full Diff."
        ),
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Incoming/source directory; overrides the project configuration",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help="Current/target directory; overrides the project configuration",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Project configuration file. Defaults to .config-review.yaml at the Git root "
            "or the common source/target parent."
        ),
    )
    parser.add_argument(
        "--context", type=int, default=4, help="Context lines around unified diff hunks"
    )
    parser.add_argument(
        "--include-secrets", action="store_true", help="Include directories named secrets"
    )
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
    parser.add_argument(
        "--dry-run", action="store_true", help="Disable every workflow that can modify files"
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the built-in regression tests and exit",
    )
    parser.add_argument("--no-tui", action="store_true", help="Use the line-oriented interface")
    parser.add_argument(
        "--no-color", action="store_true", help="Disable ANSI colors in the line-oriented interface"
    )
    parser.add_argument(
        "--force-color",
        action="store_true",
        help="Force ANSI colors in the line-oriented interface",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Print diagnostic information to stderr"
    )
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


def project_base_directory() -> Path:
    """Choose the repository/launch directory used by first-run setup."""
    cwd = Path.cwd().resolve()
    cwd_git_root = find_git_root(cwd)
    if cwd_git_root is not None:
        return cwd_git_root
    executable = Path(sys.argv[0]).expanduser()
    if executable.exists():
        executable_parent = executable.resolve().parent
        executable_git_root = find_git_root(executable_parent)
        return executable_git_root or executable_parent
    return cwd


def resolve_project_config(supplied: Path | None, base: Path) -> Path:
    if supplied is not None:
        return supplied.expanduser().resolve()
    return base / DEFAULT_PROJECT_CONFIG


FIRST_RUN_EXCLUDED_DIRS = set(DEFAULT_EXCLUDED_DIRS) | {
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
}
FIRST_RUN_EXCLUDED_DIRS_LOWER = {name.lower() for name in FIRST_RUN_EXCLUDED_DIRS}


def discover_dev_test_pairs(base: Path, *, max_depth: int = 6) -> list[tuple[Path, Path]]:
    """Find sibling directories named dev and test beneath the project base."""
    base = base.resolve()
    found: list[tuple[Path, Path]] = []
    for root_text, dirnames, _filenames in os.walk(base, followlinks=False):
        root = Path(root_text)
        try:
            depth = len(root.relative_to(base).parts)
        except ValueError:
            continue
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name.lower() not in FIRST_RUN_EXCLUDED_DIRS_LOWER
            and not (root / name).is_symlink()
            and depth < max_depth
        )
        by_lower = {name.lower(): name for name in dirnames}
        if "dev" in by_lower and "test" in by_lower:
            source = (root / by_lower["dev"]).resolve()
            target = (root / by_lower["test"]).resolve()
            if source.is_dir() and target.is_dir():
                found.append((source, target))
    return sorted(
        set(found),
        key=lambda pair: (
            len(pair[0].relative_to(base).parts),
            pair[0].as_posix().lower(),
            pair[1].as_posix().lower(),
        ),
    )


def _relative_display(path: Path, base: Path) -> str:
    try:
        text = Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
        return text or "."
    except ValueError:
        return str(path.resolve())


def _directory_input(prompt: str) -> str:
    """Read a directory path with best-effort shell-style Tab completion."""
    try:
        import readline
    except ImportError:  # pragma: no cover - unavailable on some platforms
        return input(prompt)

    previous_completer = readline.get_completer()
    previous_delimiters = readline.get_completer_delims()

    def complete(text: str, state: int) -> str | None:
        expanded = os.path.expanduser(text or "")
        pattern = f"{expanded}*" if expanded else "*"
        matches: list[str] = []
        for match in sorted(glob.glob(pattern)):
            candidate = Path(match)
            if candidate.is_dir():
                matches.append(match.rstrip(os.sep) + os.sep)
        return matches[state] if state < len(matches) else None

    try:
        readline.set_completer(complete)
        # Treat the full line as a path so completion also works with spaces.
        readline.set_completer_delims("\t\n")
        readline.parse_and_bind("tab: complete")
        return input(prompt)
    finally:
        readline.set_completer(previous_completer)
        readline.set_completer_delims(previous_delimiters)


def _prompt_existing_project_directory() -> Path:
    cwd = Path.cwd().resolve()
    print("\nEnter the project directory that contains the DEV and TEST folders.")
    print("Press Tab to complete paths. Relative paths start from your current directory:")
    print(f"  {cwd}")
    while True:
        raw = _directory_input("Project directory: ").strip()
        if not raw:
            print("Please enter a project directory.")
            continue
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        candidate = candidate.resolve()
        if candidate.is_dir():
            return candidate
        print(f"Directory not found: {candidate}")


def _direct_dev_test_pair(project: Path) -> tuple[Path, Path] | None:
    """Return direct case-insensitive DEV/TEST children of a project directory."""
    try:
        children = {
            child.name.lower(): child
            for child in project.iterdir()
            if child.is_dir() and not child.is_symlink()
        }
    except OSError:
        return None
    source = children.get("dev")
    target = children.get("test")
    if source is None or target is None:
        return None
    return source.resolve(), target.resolve()


def _project_for_pair(source: Path, target: Path) -> Path:
    return (
        source.parent
        if source.parent == target.parent
        else Path(os.path.commonpath([source, target]))
    )


def _confirm_pair(source: Path, target: Path, display_base: Path) -> bool:
    project = _project_for_pair(source, target)
    print("\nFound configuration directories:")
    print(f"  Project:     {_relative_display(project, display_base)}")
    print(f"  DEV/source:  {_relative_display(source, display_base)}")
    print(f"  TEST/target: {_relative_display(target, display_base)}")
    answer = input("Use this project? [Y/n]: ").strip().lower()
    return answer in {"", "y", "yes"}


def _select_pair(
    pairs: Sequence[tuple[Path, Path]],
    display_base: Path,
    *,
    allow_manual: bool,
) -> tuple[Path, Path] | None:
    if not pairs:
        return None
    if len(pairs) == 1:
        return pairs[0] if _confirm_pair(*pairs[0], display_base) else None

    print("\nFound multiple projects containing DEV and TEST:")
    for index, (source, target) in enumerate(pairs, start=1):
        project = _project_for_pair(source, target)
        print(f"  {index}. {_relative_display(project, display_base)}")
    manual_text = ", or M to choose a different directory" if allow_manual else ""
    while True:
        answer = input(f"Select a project number{manual_text}: ").strip().lower()
        if allow_manual and answer in {"m", "manual"}:
            return None
        try:
            selected = int(answer) - 1
        except ValueError:
            print("Enter one of the listed numbers" + (" or M." if allow_manual else "."))
            continue
        if 0 <= selected < len(pairs):
            pair = pairs[selected]
            return pair if _confirm_pair(*pair, display_base) else None
        print("That selection is outside the listed range.")


def discover_nearby_dev_test_pairs(base: Path) -> list[tuple[Path, Path]]:
    """Search sensible nearby roots, including the parent workspace directory."""
    cwd = Path.cwd().resolve()
    candidates = [cwd, base.resolve(), base.resolve().parent]
    roots: list[Path] = []
    for candidate in candidates:
        if candidate not in roots and candidate.is_dir():
            roots.append(candidate)

    found: set[tuple[Path, Path]] = set()
    for root in roots:
        for pair in discover_dev_test_pairs(root, max_depth=6):
            found.add(pair)
    return sorted(
        found,
        key=lambda pair: (
            len(_project_for_pair(*pair).parts),
            _project_for_pair(*pair).as_posix().lower(),
        ),
    )


def _pairs_in_selected_project(project: Path) -> list[tuple[Path, Path]]:
    # A pasted DEV or TEST directory is a common mistake; automatically use its
    # parent when the sibling environment exists.
    if project.name.lower() in {"dev", "test"}:
        direct_parent_pair = _direct_dev_test_pair(project.parent)
        if direct_parent_pair is not None:
            project = project.parent
            print(f"Using parent project directory: {project}")

    direct = _direct_dev_test_pair(project)
    if direct is not None:
        return [direct]
    return discover_dev_test_pairs(project, max_depth=5)


def interactive_first_run_paths(base: Path) -> tuple[Path, Path]:
    """Discover or collect one project directory, then derive DEV and TEST."""
    display_base = Path.cwd().resolve()
    discovered = discover_nearby_dev_test_pairs(base)
    selected = _select_pair(discovered, display_base, allow_manual=True)
    if selected is not None:
        return selected

    if discovered:
        print("Choose the project directory manually.")
    else:
        print("\nNo nearby project containing both DEV and TEST was found automatically.")

    while True:
        project = _prompt_existing_project_directory()
        pairs = _pairs_in_selected_project(project)
        if not pairs:
            print(f"\nNo sibling DEV and TEST directories were found under: {project}")
            print("Choose the directory that contains them, not the DEV or TEST folder itself.")
            continue
        selected = _select_pair(pairs, display_base, allow_manual=True)
        if selected is not None:
            return selected
        print("Choose a different project directory.")


def resolve_project_paths(
    args: argparse.Namespace,
    config_file: Path,
    base: Path,
) -> tuple[Path, Path, bool]:
    """Resolve CLI/config/first-run paths and report whether they were newly saved."""
    if (args.source is None) != (args.target is None):
        raise WorkbenchError("Use --source and --target together, or omit both.")

    if args.source is not None and args.target is not None:
        source = args.source.expanduser()
        target = args.target.expanduser()
        if not source.is_absolute():
            source = Path.cwd() / source
        if not target.is_absolute():
            target = Path.cwd() / target
        source = source.resolve()
        target = target.resolve()
        if not source.is_dir():
            raise WorkbenchError(f"DEV/source directory does not exist: {source}")
        if not target.is_dir():
            raise WorkbenchError(f"TEST/target directory does not exist: {target}")
        configured = load_project_path_settings(config_file)
        newly_saved = not config_file.exists() or not any(configured)
        if newly_saved:
            save_project_paths(config_file, source, target)
        return source, target, newly_saved

    configured_project, configured_source, configured_target = load_project_path_settings(
        config_file
    )
    if configured_project or (configured_source and configured_target):
        source, target = resolve_configured_project_paths(
            config_file,
            configured_project,
            configured_source,
            configured_target,
        )
        return source, target, False
    if configured_source or configured_target:
        raise WorkbenchError(
            f"Configure paths.project, or both paths.source and paths.target, in {config_file}."
        )
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise WorkbenchError(
            "No project directory is configured and setup requires an interactive terminal. "
            "Run with --source and --target once, or add paths.project to "
            f"{config_file}."
        )
    source, target = interactive_first_run_paths(base)
    save_project_paths(config_file, source, target)
    return source, target, True


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

    base = project_base_directory()
    config_file = resolve_project_config(args.config, base)
    if args.init_config:
        try:
            init_project_config(config_file)
        except WorkbenchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"Created {config_file}")
        return 0

    try:
        source, target, newly_saved = resolve_project_paths(args, config_file, base)
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.", file=sys.stderr)
        return 130
    except WorkbenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if newly_saved:
        print(f"Saved verified project paths to {config_file}")
    if not source.is_dir():
        print(
            f"error: configured DEV/source directory does not exist: {source}\n"
            f"Update paths.project in {config_file} or run with --source and --target.",
            file=sys.stderr,
        )
        return 2
    if not target.is_dir():
        print(
            f"error: configured TEST/target directory does not exist: {target}\n"
            f"Update paths.project in {config_file} or run with --source and --target.",
            file=sys.stderr,
        )
        return 2

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
