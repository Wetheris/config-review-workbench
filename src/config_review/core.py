"""Config Review Workbench Core module.

Part of the modular Config Review Workbench source distribution. Build the portable
``dist/config-review.pyz`` executable with ``python build.py``.
"""

from __future__ import annotations

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
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlsplit

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

VERSION = "1.0.0"

DEFAULT_PROJECT_CONFIG = ".config-review.yaml"

MIN_PATTERN_MATCHES = 2

MIN_PATTERN_FILES = 2

BROAD_PATTERN_MIN_MATCHES = 4

YAML_SUFFIXES = {".yaml", ".yml"}

DEFAULT_EXCLUDED_DIRS = {".git", "__pycache__", ".pytest_cache", "secrets"}

CATEGORY_ENVIRONMENT = "Environment identity"

CATEGORY_APP_DOMAINS = "Application domains"

CATEGORY_ENDPOINTS = "Endpoints"

CATEGORY_USERS_REFERENCES = "Users / references"

CATEGORY_STORAGE_DATA = "Storage / data"

CATEGORY_OTHER = "Other repeated values"

CATEGORY_ALWAYS_REVIEWED = "Always reviewed"

CATEGORY_ORDER = (
    CATEGORY_ENVIRONMENT,
    CATEGORY_APP_DOMAINS,
    CATEGORY_ENDPOINTS,
    CATEGORY_USERS_REFERENCES,
    CATEGORY_STORAGE_DATA,
    CATEGORY_OTHER,
)

# File sections are derived from repository paths rather than hard-coded project
# names. The first two parent directories become a stable display group, such as
# ``SERVICES / API`` or ``PLATFORMS / WEST``.

ANSI = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "magenta": "\033[95m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}

COLOR_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

DEBUG_ENABLED = False

DEBUG_LOG_PATH: Path | None = None


class WorkbenchError(RuntimeError):
    """A safe, user-facing failure."""


@dataclass(slots=True)
class PatternRule:
    id: str
    name: str
    test_regex: str
    dev_regex: str
    files: tuple[str, ...]
    category: str = CATEGORY_OTHER
    enabled: bool = False
    kind: str = "generated"
    source: str = "project"
    test_compiled: re.Pattern[str] = field(init=False, repr=False)
    dev_compiled: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.test_compiled = re.compile(self.test_regex)
        self.dev_compiled = re.compile(self.dev_regex)

    def applies_to(self, relative_path: str) -> bool:
        return not self.files or any(
            fnmatch.fnmatch(relative_path, pattern) for pattern in self.files
        )


@dataclass(slots=True)
class PatternExample:
    relative_path: str
    old_line: str
    new_line: str
    old_line_number: int
    new_line_number: int
    old_context_before: str | None = None
    old_context_after: str | None = None
    new_context_before: str | None = None
    new_context_after: str | None = None


@dataclass(slots=True)
class PatternCandidate:
    rule: PatternRule
    examples: list[PatternExample]
    match_count: int
    file_count: int
    affected_files: tuple[str, ...]
    overlap_count: int = 0
    persisted: bool = False


@dataclass(slots=True)
class ProtectedChangeSummary:
    name: str
    match_count: int
    file_count: int
    examples: list[PatternExample]


@dataclass(slots=True)
class ChangeBlock:
    tag: str
    old_start: int
    old_end: int
    new_start: int
    new_end: int
    old_lines: list[str]
    new_lines: list[str]
    hidden_by: tuple[str, ...] = ()
    protected_reason: str | None = None
    # Logical blocks may be refined for Focused Diff while still being rendered
    # at the original difflib opcode location. Full Diff never uses this field.
    opcode_key: tuple[str, int, int, int, int] | None = None

    @property
    def is_hidden(self) -> bool:
        return bool(self.hidden_by)

    @property
    def old_count(self) -> int:
        return self.old_end - self.old_start

    @property
    def new_count(self) -> int:
        return self.new_end - self.new_start


@dataclass(slots=True)
class HandledChange:
    """One review decision for a concrete text change block."""

    action: str
    decision_token: str
    tracking_tokens: tuple[str, ...]
    old_start: int
    old_end: int
    new_start: int
    new_end: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    context_tokens: tuple[str, ...]
    order: int

    @property
    def preview(self) -> str:
        # Raw lines exist only in memory. Restored history is hydrated from the
        # current diff when it can be matched safely; resolved-away history uses
        # a generic label rather than persisting configuration content.
        if self.old_lines and self.new_lines:
            return f"{_preview_text(self.old_lines, 42)}  →  {_preview_text(self.new_lines, 42)}"
        if self.old_lines:
            return f"removed: {_preview_text(self.old_lines, 58)}"
        if self.new_lines:
            return f"added: {_preview_text(self.new_lines, 58)}"
        return "saved review decision"


@dataclass(slots=True)
class FileRecord:
    relative_path: str
    dev_path: Path
    test_path: Path
    initial_test_exists: bool
    initial_test_hash: str | None
    initial_test_bytes: bytes | None = field(default=None, repr=False)
    initial_test_mode: int | None = None
    undo_snapshot_captured: bool = False
    test_symlink_path: str | None = None
    last_known_test_exists: bool = False
    last_known_test_hash: str | None = None
    uncommitted: bool = False
    edited: bool = False
    resolved: bool = False
    resolved_mode: str | None = None  # "auto" | "manual"
    dev_exists: bool = False
    test_exists: bool = False
    dev_text: str = ""
    test_text: str = ""
    equal: bool = False
    binary: bool = False
    read_error: str | None = None
    modified_change_tokens: set[str] = field(default_factory=set)
    kept_change_tokens: set[str] = field(default_factory=set)
    handled_changes: list[HandledChange] = field(default_factory=list)
    next_handled_order: int = 1

    def refresh(self) -> None:
        self.dev_exists, self.dev_text, dev_binary, dev_error = read_text_file(self.dev_path)
        self.test_exists, self.test_text, test_binary, test_error = read_text_file(self.test_path)
        self.binary = dev_binary or test_binary
        errors = [item for item in (dev_error, test_error) if item]
        self.read_error = "; ".join(errors) if errors else None
        self.equal = (
            self.dev_exists == self.test_exists
            and self.dev_text == self.test_text
            and not self.read_error
        )
        current_hash = file_hash(self.test_path) if self.test_exists else None
        self.edited = (
            self.test_exists != self.initial_test_exists or current_hash != self.initial_test_hash
        )

    @property
    def change_kind(self) -> str:
        if self.equal:
            return "SYNCED"
        if self.dev_exists and not self.test_exists:
            return "DEV ONLY"
        if self.test_exists and not self.dev_exists:
            return "TEST ONLY"
        return "CHANGED"

    @property
    def pair_signature(self) -> str:
        digest = hashlib.sha256()
        digest.update(b"DEV\0")
        digest.update(self.dev_text.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0TEST\0")
        digest.update(self.test_text.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(str(self.dev_exists).encode())
        digest.update(str(self.test_exists).encode())
        return digest.hexdigest()

    @property
    def states(self) -> list[str]:
        states: list[str] = []
        if self.uncommitted:
            states.append("UNCOMMITTED")
        if self.edited:
            states.append("EDITED")
        if self.test_symlink_path:
            states.append("SYMLINK")
        return states


def _display_section_part(part: str) -> str:
    """Return a compact, readable section label for one path component."""
    return part.replace("_", " ").replace("-", " ").upper()


def file_section(relative_path: str) -> str:
    """Return a generic, stable presentation section for a repository path.

    The first two parent-directory components are used. Files at the comparison
    root are grouped under ``ROOT``. This keeps the UI useful for arbitrary
    repositories while preserving familiar groups such as ``PLATFORMS / WEST`` when
    those paths happen to exist.
    """
    parents = [part for part in Path(relative_path).parts[:-1] if part not in {"", "."}]
    if not parents:
        return "ROOT"
    return " / ".join(_display_section_part(part) for part in parents[:2])


def file_record_sort_key(record: FileRecord) -> tuple[int, str, str]:
    section = file_section(record.relative_path)
    return (
        0 if section == "ROOT" else 1,
        section,
        record.relative_path.lower(),
    )


def grouped_file_rows(records: Sequence[FileRecord]) -> list[tuple[str, int | None]]:
    """Build section headers plus record indexes for the main file list."""
    rows: list[tuple[str, int | None]] = []
    current_section: str | None = None
    for index, record in enumerate(records):
        section = file_section(record.relative_path)
        if section != current_section:
            rows.append((section, None))
            current_section = section
        rows.append(("", index))
    return rows


@dataclass(slots=True)
class MappingScalarAnalysis:
    """Parsed scalar mapping entries plus an explicit availability state."""

    entries: dict[int, tuple[tuple[Any, ...], str, tuple[str, Any], str]]
    unavailable_reason: str | None = None


@dataclass(slots=True)
class KeyedListItem:
    """One complete YAML list mapping identified by a unique scalar ``name``."""

    parent: tuple[Any, ...]
    identity: tuple[str, Any]
    start: int
    end: int
    lines: tuple[str, ...]


@dataclass(slots=True)
class KeyedListAnalysis:
    """Conservative ``name``-keyed list items plus parser availability state."""

    items: dict[tuple[Any, ...], KeyedListItem]
    unavailable_reason: str | None = None


@dataclass(slots=True)
class MappingOrderResult:
    """YAML order reconciliation output and any parser availability reason."""

    blocks: list[ChangeBlock]
    unavailable_reason: str | None = None


@dataclass(slots=True)
class FilterResult:
    """One canonical diff calculation shared by every presentation layer.

    ``opcodes`` and ``blocks`` are produced together exactly once. Focused Diff,
    Full Diff, line numbers, navigation, and Filter Details must consume these
    stored results rather than asking difflib to align the text again.
    """

    opcodes: list[tuple[str, int, int, int, int]]
    blocks: list[ChangeBlock]
    hidden: list[ChangeBlock]
    visible: list[ChangeBlock]
    mapping_order_unavailable_reason: str | None = None


@dataclass(slots=True)
class DisplayLine:
    """One screen line with optional TEST/DEV source coordinates."""

    text: str
    kind: str = "text"
    test_line: int | None = None
    dev_line: int | None = None
    emphasis_ranges: tuple[tuple[int, int], ...] = ()


def display_line_body_width(line: DisplayLine) -> int:
    """Return the horizontally scrollable width for one rendered diff line."""
    if line.test_line is None and line.dev_line is None:
        return len(line.text)
    prefix_width = 4 if line.kind in {"filtered_remove", "filtered_add", "filtered_context"} else 2
    return prefix_width + len(line.text)


def maximum_horizontal_offset(
    lines: Sequence[DisplayLine],
    number_width: int,
    screen_width: int,
    *,
    x: int = 1,
    selected_body_range: tuple[int, int] | None = None,
) -> int:
    """Bound horizontal scrolling to content that can actually extend off-screen."""
    maximum = 0
    for index, line in enumerate(lines):
        gutter_width = 0
        if line.test_line is not None or line.dev_line is not None:
            gutter_width = number_width * 2 + 4  # TEST + space + DEV + " │ "
        guide_width = (
            2
            if selected_body_range is not None
            and selected_body_range[0] <= index < selected_body_range[1]
            else 0
        )
        available = max(1, screen_width - x - 1 - gutter_width - guide_width)
        maximum = max(maximum, display_line_body_width(line) - available)
    return max(0, maximum)


@dataclass(slots=True)
class DiffPresentation:
    lines: list[DisplayLine]
    filter_result: FilterResult
    change_line_indexes: list[int] = field(default_factory=list)
    change_line_ranges: list[tuple[int, int]] = field(default_factory=list)
    change_blocks: list[ChangeBlock] = field(default_factory=list)
    selected_change: int | None = None
    number_width: int = 4
    handled_count: int = 0
    pattern_hidden_count: int = 0
    whitespace_hidden_count: int = 0
    mapping_order_hidden_count: int = 0
    mapping_order_unavailable_reason: str | None = None

    @property
    def visible_change_count(self) -> int:
        return len(self.change_line_indexes)

    @property
    def selected_line_index(self) -> int | None:
        if self.selected_change is None or not self.change_line_indexes:
            return None
        index = max(0, min(self.selected_change, len(self.change_line_indexes) - 1))
        return self.change_line_indexes[index]

    @property
    def selected_line_range(self) -> tuple[int, int] | None:
        if self.selected_change is None or not self.change_line_ranges:
            return None
        index = max(0, min(self.selected_change, len(self.change_line_ranges) - 1))
        return self.change_line_ranges[index]

    @property
    def selected_block(self) -> ChangeBlock | None:
        if self.selected_change is None or not self.change_blocks:
            return None
        index = max(0, min(self.selected_change, len(self.change_blocks) - 1))
        return self.change_blocks[index]


def selected_diff_body_range(
    presentation: DiffPresentation,
) -> tuple[int, int] | None:
    """Return the exact remove/add line range beneath the selected-change arrow."""
    marker_index = presentation.selected_line_index
    block = presentation.selected_block
    if marker_index is None or block is None:
        return None
    start = marker_index + 1
    end = min(
        len(presentation.lines),
        start + block.old_count + block.new_count,
    )
    return (start, end) if end > start else None


@dataclass(slots=True)
class ReviewMenuResult:
    selected_change: int
    changed: bool = False
    quit: bool = False
    file_delta: int = 0


@dataclass(slots=True)
class ReviewCounts:
    active: int
    handled: int
    pattern_hidden: int
    whitespace_hidden: int
    mapping_order_hidden: int


@dataclass(slots=True)
class MainListRow:
    """One presentation row in the expandable main file list."""

    kind: str  # section | file | change | summary
    record_index: int | None = None
    section: str = ""
    change_index: int | None = None
    summary: str = ""
    block: ChangeBlock | None = None


@dataclass(slots=True)
class GitRepositoryStatus:
    """Best-effort freshness and working-tree state for the comparison repository."""

    root: Path | None
    branch: str = "no-git"
    commit: str = ""
    upstream: str = ""
    upstream_commit: str = ""
    ahead: int = 0
    behind: int = 0
    dirty_count: int = 0
    fetch_attempted: bool = False
    fetch_ok: bool = False
    fetch_error: str = ""

    @property
    def warning(self) -> bool:
        return bool(
            self.root is None
            or self.behind
            or self.dirty_count
            or not self.upstream
            or (self.fetch_attempted and not self.fetch_ok)
        )

    @property
    def summary(self) -> str:
        if self.root is None:
            return "Git: not available for this comparison"
        parts = [f"Git: {self.branch}"]
        if self.upstream:
            if self.behind:
                parts.append(f"{self.behind} behind {self.upstream}")
            elif self.ahead:
                parts.append(f"{self.ahead} ahead of {self.upstream}")
            elif self.fetch_ok:
                parts.append(f"up to date with {self.upstream}")
            else:
                parts.append(f"remote freshness unverified ({self.upstream})")
        else:
            parts.append("no upstream")
        parts.append(f"{self.dirty_count} local change(s)" if self.dirty_count else "clean")
        return " · ".join(parts)


@dataclass(slots=True, frozen=True)
class GitCommitContext:
    """One commit used as report context for a changed line range."""

    source: str  # line | file
    commit_hash: str
    short_hash: str
    author: str
    date: str
    subject: str
    merge_request_ref: str | None = None


@dataclass(slots=True)
class AppSettings:
    source: Path
    target: Path
    config_file: Path
    context: int
    include_secrets: bool
    edit_command: str
    vimdiff_command: str
    dry_run: bool


class SessionStore:
    """Hold in-memory review progress and save one explicit last-session snapshot.

    The current run still starts empty until the reviewer chooses whether to load
    the previous snapshot. Exiting the workbench writes the current snapshot
    automatically; choosing Start fresh on the next launch deletes it.
    """

    FORMAT_VERSION = 5

    def __init__(self, source: Path, target: Path, git_root: Path | None) -> None:
        self.source = source.resolve()
        self.target = target.resolve()
        self.git_root = git_root.resolve() if git_root is not None else None
        key_material = f"{self.source}\0{self.target}".encode()
        key = hashlib.sha256(key_material).hexdigest()[:20]
        if self.git_root is not None:
            self.path = self.git_root / ".git" / "config-review-workbench" / f"session-{key}.json"
        else:
            cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
            self.path = cache_root / "config-review-workbench" / f"session-{key}.json"
        self.data: dict[str, Any] = {
            "version": self.FORMAT_VERSION,
            "files": {},
        }
        self.saved_data: dict[str, Any] | None = None
        self.load_saved()

    @property
    def has_saved(self) -> bool:
        return self.saved_data is not None

    @property
    def saved_metadata(self) -> Mapping[str, Any]:
        if not isinstance(self.saved_data, Mapping):
            return {}
        metadata = self.saved_data.get("metadata", {})
        return metadata if isinstance(metadata, Mapping) else {}

    def load_saved(self) -> None:
        self.saved_data = None
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        # Versions before 5 may contain raw TEST/DEV lines. Delete them instead
        # of retaining potentially sensitive configuration content on disk.
        if isinstance(raw, Mapping) and raw.get("version") != self.FORMAT_VERSION:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            return

        if (
            isinstance(raw, Mapping)
            and raw.get("version") == self.FORMAT_VERSION
            and isinstance(raw.get("files", {}), Mapping)
            and isinstance(raw.get("metadata", {}), Mapping)
        ):
            self.saved_data = {
                "version": self.FORMAT_VERSION,
                "metadata": dict(raw.get("metadata", {})),
                "files": dict(raw.get("files", {})),
            }

    @staticmethod
    def _serialize_handled(entry: HandledChange) -> dict[str, Any]:
        # Never persist configuration text. Tokens are one-way SHA-256 digests;
        # coordinates and order are review metadata only.
        return {
            "action": entry.action,
            "decision_token": entry.decision_token,
            "tracking_tokens": list(entry.tracking_tokens),
            "context_tokens": list(entry.context_tokens),
            "old_start": entry.old_start,
            "old_end": entry.old_end,
            "new_start": entry.new_start,
            "new_end": entry.new_end,
            "order": entry.order,
        }

    @staticmethod
    def _deserialize_handled(raw: Any) -> HandledChange | None:
        if not isinstance(raw, Mapping):
            return None
        try:
            action = str(raw["action"])
            decision_token = str(raw["decision_token"])
            tracking_tokens = tuple(str(item) for item in raw.get("tracking_tokens", []))
            context_tokens = tuple(str(item) for item in raw.get("context_tokens", []))
            return HandledChange(
                action=action,
                decision_token=decision_token,
                tracking_tokens=tracking_tokens,
                old_start=int(raw["old_start"]),
                old_end=int(raw["old_end"]),
                new_start=int(raw["new_start"]),
                new_end=int(raw["new_end"]),
                old_lines=(),
                new_lines=(),
                context_tokens=context_tokens,
                order=int(raw["order"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _rebuild_tracking(record: FileRecord) -> None:
        record.kept_change_tokens.clear()
        record.modified_change_tokens.clear()
        for entry in record.handled_changes:
            if entry.action == "KEPT TEST":
                record.kept_change_tokens.add(entry.decision_token)
            else:
                record.modified_change_tokens.update(entry.tracking_tokens)
        record.next_handled_order = (
            max((entry.order for entry in record.handled_changes), default=0) + 1
        )

    @staticmethod
    def _entries_from_raw(raw: Mapping[str, Any]) -> list[HandledChange]:
        return [
            entry
            for entry in (
                SessionStore._deserialize_handled(item) for item in raw.get("handled_changes", [])
            )
            if entry is not None
        ]

    def start_fresh(self) -> None:
        self.data = {
            "version": self.FORMAT_VERSION,
            "files": {},
        }

    def restore_record(
        self,
        record: FileRecord,
        filter_signature: str,
        *,
        saved: bool = False,
        hide_mapping_order: bool = False,
    ) -> bool:
        source_data = self.saved_data if saved else self.data
        if not isinstance(source_data, Mapping):
            return False
        files = source_data.get("files", {})
        raw = files.get(record.relative_path) if isinstance(files, Mapping) else None
        if not isinstance(raw, Mapping):
            return False

        entries = self._entries_from_raw(raw)
        record.handled_changes = entries

        same_pair = str(raw.get("pair_signature", "")) == record.pair_signature
        if entries:
            current = compute_filter_result(
                record.test_text,
                record.dev_text,
                [],
                record.relative_path,
                hide_mapping_order=hide_mapping_order,
            )
            # Reconcile against current content without treating an original
            # reappeared diff as still handled after an apply/edit action.
            match_handled_changes(record, current.blocks)
            if not same_pair:
                record.handled_changes = reconciled_handled_entries(record, current.blocks)

        self._rebuild_tracking(record)
        same_filter = str(raw.get("filter_signature", "")) == filter_signature
        mode = str(raw.get("completion_mode", ""))
        if same_pair and same_filter and mode in {"auto", "manual"}:
            record.resolved = True
            record.resolved_mode = mode
        else:
            record.resolved = False
            record.resolved_mode = None
        return True

    def save_record(self, record: FileRecord, filter_signature: str) -> None:
        files = self.data.setdefault("files", {})
        files[record.relative_path] = {
            "pair_signature": record.pair_signature,
            "filter_signature": filter_signature,
            "completion_mode": record.resolved_mode if record.resolved else None,
            "handled_changes": [
                self._serialize_handled(entry)
                for entry in sorted(record.handled_changes, key=lambda item: item.order)
            ],
        }

    def clear_record(self, relative_path: str) -> None:
        files = self.data.setdefault("files", {})
        files.pop(relative_path, None)

    def clear_all(self) -> None:
        self.start_fresh()

    def delete_saved(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
        self.saved_data = None

    def save_to_disk(self, metadata: Mapping[str, Any]) -> None:
        payload = {
            "version": self.FORMAT_VERSION,
            "metadata": dict(metadata),
            "files": dict(self.data.get("files", {})),
        }
        temp_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise WorkbenchError(f"Could not save review session: {exc}") from exc
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
        self.saved_data = payload

    def saved_summary(
        self,
        records_by_path: Mapping[str, FileRecord],
        filter_signature: str,
        current_branch: str,
        current_commit: str,
        *,
        hide_mapping_order: bool = False,
    ) -> dict[str, Any] | None:
        if not isinstance(self.saved_data, Mapping):
            return None
        metadata = self.saved_metadata
        files = self.saved_data.get("files", {})
        if not isinstance(files, Mapping):
            files = {}

        total_handled = 0
        verified_handled = 0
        exact_file_count = 0
        saved_file_count = 0
        changed_file_count = 0
        for relative_path, raw_value in files.items():
            if not isinstance(raw_value, Mapping):
                continue
            saved_file_count += 1
            entries = self._entries_from_raw(raw_value)
            total_handled += len(entries)
            record = records_by_path.get(str(relative_path))
            if record is None:
                changed_file_count += 1
                continue
            same_pair = str(raw_value.get("pair_signature", "")) == record.pair_signature
            same_filter = str(raw_value.get("filter_signature", "")) == filter_signature
            if same_pair and same_filter:
                exact_file_count += 1
                verified_handled += len(entries)
                continue
            changed_file_count += 1
            if not entries:
                continue
            original_entries = record.handled_changes
            try:
                record.handled_changes = entries
                current = compute_filter_result(
                    record.test_text,
                    record.dev_text,
                    [],
                    record.relative_path,
                    hide_mapping_order=hide_mapping_order,
                )
                assignments, _ = match_handled_changes(record, current.blocks)
                verified_handled += len({entry.order for entry in assignments.values()})
            finally:
                record.handled_changes = original_entries

        saved_source = str(metadata.get("source", ""))
        saved_target = str(metadata.get("target", ""))
        saved_repo = str(metadata.get("repository", ""))
        current_repo = str(self.git_root or "")
        checkout_exact = (
            saved_source == str(self.source)
            and saved_target == str(self.target)
            and saved_repo == current_repo
            and str(metadata.get("branch", "")) == current_branch
            and str(metadata.get("commit", "")) == current_commit
        )
        exact = checkout_exact and changed_file_count == 0 and exact_file_count == saved_file_count
        progress = metadata.get("progress", {})
        if not isinstance(progress, Mapping):
            progress = {}
        return {
            "exact": exact,
            "checkout_exact": checkout_exact,
            "saved_at": str(metadata.get("saved_at", "unknown")),
            "branch": str(metadata.get("branch", "unknown")),
            "commit": str(metadata.get("commit", "")),
            "tool_version": str(metadata.get("tool_version", "unknown")),
            "files_reviewed": int(progress.get("files_reviewed", 0) or 0),
            "files_total": int(progress.get("files_total", saved_file_count) or saved_file_count),
            "total_handled": total_handled,
            "verified_handled": verified_handled,
            "saved_file_count": saved_file_count,
            "changed_file_count": changed_file_count,
            "path": str(self.path),
        }


def debug(message: str, **fields: Any) -> None:
    """Emit diagnostic metadata without logging configuration values.

    ``--debug`` writes to stderr. ``--debug-log`` writes the same trace to a
    permission-restricted file, which is safer for curses sessions. Callers
    must pass only coordinates, counts, rule names, hashes, and error metadata.
    """
    if not DEBUG_ENABLED:
        return

    timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    rendered = [f"[{timestamp}] [DEBUG] {message}"]
    rendered.extend(f"        {key}={value!r}" for key, value in fields.items())
    payload = "\n".join(rendered) + "\n"

    if DEBUG_LOG_PATH is None:
        sys.stderr.write(payload)
        sys.stderr.flush()
        return

    try:
        DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(DEBUG_LOG_PATH.parent, 0o700)
        except OSError:
            pass
        fd = os.open(
            DEBUG_LOG_PATH,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(payload)
        try:
            os.chmod(DEBUG_LOG_PATH, 0o600)
        except OSError:
            pass
    except OSError as exc:
        sys.stderr.write(f"[DEBUG LOG ERROR] {exc}\n")
        sys.stderr.write(payload)
        sys.stderr.flush()


def color(text: Any, *styles: str) -> str:
    rendered = str(text)
    if not COLOR_ENABLED or not styles:
        return rendered
    prefix = "".join(ANSI[name] for name in styles if name in ANSI)
    return f"{prefix}{rendered}{ANSI['reset']}" if prefix else rendered


def file_hash(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def read_text_file(path: Path) -> tuple[bool, str, bool, str | None]:
    if not path.exists():
        return False, "", False, None
    try:
        data = path.read_bytes()
    except OSError as exc:
        return True, "", False, f"{path}: {exc}"
    if b"\x00" in data:
        return True, "", True, f"{path}: binary/NUL content is not displayable"
    try:
        return True, data.decode("utf-8"), False, None
    except UnicodeDecodeError:
        return (
            True,
            data.decode("utf-8", errors="replace"),
            False,
            f"{path}: invalid UTF-8 was replaced for display",
        )


def read_file_metadata(path: Path) -> tuple[bool, int | None, str | None, str | None]:
    """Read startup existence, mode, and hash without retaining file contents."""
    if not path.exists():
        return False, None, None, None
    try:
        mode = path.stat().st_mode
        digest = file_hash(path)
    except OSError as exc:
        return True, None, None, str(exc)
    if digest is None:
        return True, mode, None, f"Could not hash {path}"
    return True, mode, digest, None


def read_file_snapshot(path: Path) -> tuple[bool, bytes | None, int | None, str | None]:
    """Capture exact bytes lazily from a regular file without following a final symlink."""
    if path.is_symlink():
        return True, None, None, f"Refusing to snapshot symlinked TEST path: {path}"
    if not path.exists():
        return False, None, None, None

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        return True, None, None, str(exc)

    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return True, None, None, f"TEST path is not a regular file: {path}"
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        return True, data, info.st_mode, hashlib.sha256(data).hexdigest()
    except OSError as exc:
        return True, None, None, str(exc)
    finally:
        os.close(descriptor)


def symlink_component(path: Path, root: Path) -> Path | None:
    """Return the first symlink in path beneath root without resolving through it."""
    root_absolute = Path(os.path.abspath(root))
    path_absolute = Path(os.path.abspath(path))
    try:
        relative = path_absolute.relative_to(root_absolute)
    except ValueError:
        return path_absolute if path_absolute.is_symlink() else None

    current = root_absolute
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return current
        except OSError:
            return current
    return None


def parse_editor_command(command_text: str) -> list[str]:
    """Split an editor command safely and expand a leading ~ in its executable path."""
    command = shlex.split(command_text)
    if command and command[0].startswith("~"):
        command[0] = str(Path(command[0]).expanduser())
    return command


def discover_yaml_files(root: Path, excluded_dirs: set[str]) -> set[str]:
    if not root.is_dir():
        return set()
    found: set[str] = set()
    for current, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in excluded_dirs]
        base = Path(current)
        for name in names:
            candidate = base / name
            if candidate.suffix.lower() in YAML_SUFFIXES:
                found.add(candidate.relative_to(root).as_posix())
    return found


def find_git_root(start: Path) -> Path | None:
    current = start.resolve()
    while not current.exists() and current != current.parent:
        current = current.parent
    cwd = current if current.is_dir() else current.parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def git_checkout_identity(git_root: Path | None) -> tuple[str, str]:
    """Return the current branch label and full commit SHA when available."""
    if git_root is None:
        return "no-git", ""

    def run(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(git_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except OSError:
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    commit = run("rev-parse", "HEAD")
    branch = run("symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        branch = "detached HEAD" if commit else "unknown"
    return branch, commit


def git_uncommitted_paths(git_root: Path | None) -> set[Path]:
    if git_root is None:
        return set()
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=str(git_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return set()
    if result.returncode != 0:
        return set()

    fields = result.stdout.decode(errors="surrogateescape").split("\0")
    paths: set[Path] = set()
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path_text = entry[3:] if len(entry) >= 4 else entry
        if status in {"R ", " R", "C ", " C"} and index < len(fields):
            path_text = fields[index]
            index += 1
        paths.add((git_root / path_text).resolve())
    return paths


def _run_git_text(
    git_root: Path,
    args: Sequence[str],
    *,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run one read-only Git query and return ``(code, stdout, stderr)``."""
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(git_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
            env=command_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def git_repository_status(
    git_root: Path | None,
    *,
    fetch_remote: bool = True,
    timeout: float = 8.0,
) -> GitRepositoryStatus:
    """Check branch freshness without modifying tracked working-tree files.

    A best-effort ``git fetch --prune --no-tags`` is used so a "behind" warning
    reflects the current remote rather than a potentially stale local tracking
    ref. Authentication prompts are disabled and failures become visible status
    text instead of blocking startup indefinitely.
    """
    if git_root is None:
        return GitRepositoryStatus(root=None)

    root = git_root.resolve()
    branch, commit = git_checkout_identity(root)
    status = GitRepositoryStatus(root=root, branch=branch, commit=commit)

    status.dirty_count = len(git_uncommitted_paths(root))

    code, upstream, _ = _run_git_text(
        root,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
    )
    if code == 0:
        status.upstream = upstream

    if fetch_remote and status.upstream:
        status.fetch_attempted = True
        code, _, error = _run_git_text(
            root,
            ["fetch", "--quiet", "--prune", "--no-tags"],
            timeout=timeout,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )
        status.fetch_ok = code == 0
        if code != 0:
            status.fetch_error = (error.splitlines()[-1] if error else "git fetch failed")[:240]

    if status.upstream:
        code, upstream_commit, _ = _run_git_text(root, ["rev-parse", status.upstream])
        if code == 0:
            status.upstream_commit = upstream_commit
        code, counts, _ = _run_git_text(
            root,
            ["rev-list", "--left-right", "--count", f"HEAD...{status.upstream}"],
        )
        if code == 0:
            fields = counts.split()
            if len(fields) == 2 and all(field.isdigit() for field in fields):
                status.ahead, status.behind = (int(fields[0]), int(fields[1]))
    return status


def git_remote_to_web_url(remote_url: str) -> str | None:
    """Convert one common Git remote syntax into a credential-free web URL."""
    value = remote_url.strip()
    if not value:
        return None

    # SCP-style SSH remotes, for example git@gitlab.example:group/project.git.
    if "://" not in value:
        match = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.+)", value)
        if match:
            host, path = match.groups()
            path = path.strip("/")
            if path.endswith(".git"):
                path = path[:-4]
            return f"https://{host}/{path}" if path else None
        return None

    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"}:
        host = parsed.hostname or ""
        if not host:
            return None
        port = f":{parsed.port}" if parsed.port is not None else ""
        path = parsed.path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return f"{parsed.scheme}://{host}{port}{path}" if path else None

    if parsed.scheme in {"ssh", "git"}:
        host = parsed.hostname or ""
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return f"https://{host}/{path}" if host and path else None
    return None


def normalize_git_repository_url(repository_url: str) -> str:
    """Validate and normalize a configured full repository web URL."""
    value = repository_url.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WorkbenchError(
            "Git repository URL must be a full http:// or https:// repository URL."
        )
    if parsed.query or parsed.fragment:
        raise WorkbenchError("Git repository URL cannot include a query string or fragment.")
    host = parsed.hostname
    port = f":{parsed.port}" if parsed.port is not None else ""
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path or path == "/":
        raise WorkbenchError(
            "Git repository URL must include the full group/project repository path."
        )
    return f"{parsed.scheme}://{host}{port}{path}"


def git_remote_repository_url(git_root: Path | None, remote: str = "origin") -> str | None:
    """Return an auto-detected repository web URL for one configured Git remote."""
    if git_root is None:
        return None
    code, value, _ = _run_git_text(git_root, ["remote", "get-url", remote])
    return git_remote_to_web_url(value) if code == 0 else None


def git_repository_file_url(
    repository_url: str,
    commit: str,
    relative_path: str,
    *,
    line_start: int | None = None,
    line_end: int | None = None,
) -> str:
    """Build a read-only GitLab/GitHub file permalink for an exact commit."""
    base = normalize_git_repository_url(repository_url)
    host = (urlsplit(base).hostname or "").lower()
    encoded_commit = quote(commit, safe="")
    encoded_path = quote(relative_path.lstrip("/"), safe="/")
    if host == "github.com" or host.endswith(".github.com"):
        url = f"{base}/blob/{encoded_commit}/{encoded_path}"
        if line_start:
            end = line_end or line_start
            url += f"#L{line_start}" if end == line_start else f"#L{line_start}-L{end}"
        return url

    # GitLab and self-hosted GitLab use /-/blob and #Lstart-end anchors.
    url = f"{base}/-/blob/{encoded_commit}/{encoded_path}"
    if line_start:
        end = line_end or line_start
        url += f"#L{line_start}" if end == line_start else f"#L{line_start}-{end}"
    return url


def git_repository_commit_url(repository_url: str, commit: str) -> str:
    """Build a provider-aware web URL for one exact commit."""
    base = normalize_git_repository_url(repository_url)
    host = (urlsplit(base).hostname or "").lower()
    encoded_commit = quote(commit, safe="")
    if host == "github.com" or host.endswith(".github.com"):
        return f"{base}/commit/{encoded_commit}"
    return f"{base}/-/commit/{encoded_commit}"


def git_repository_merge_request_url(
    repository_url: str,
    merge_request_ref: str | None,
) -> str | None:
    """Resolve a GitLab merge-request reference against a repository URL."""
    if not merge_request_ref:
        return None
    reference = merge_request_ref.strip().rstrip(".,;:)")
    if reference.startswith(("https://", "http://")):
        repository_host = (urlsplit(repository_url).hostname or "").lower()
        reference_host = (urlsplit(reference).hostname or "").lower()
        if not repository_host or repository_host != reference_host:
            return None
        return reference

    match = re.fullmatch(
        r"(?:(?P<project>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+))?!"
        r"(?P<iid>\d+)",
        reference,
    )
    if match is None:
        return None

    base = normalize_git_repository_url(repository_url)
    parsed = urlsplit(base)
    host = (parsed.hostname or "").lower()
    if host == "github.com" or host.endswith(".github.com"):
        return None

    project = match.group("project")
    if project:
        project_path = quote(project.strip("/"), safe="/")
        base = f"{parsed.scheme}://{parsed.netloc}/{project_path}"
    return f"{base}/-/merge_requests/{match.group('iid')}"


def _git_relative_path(git_root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(git_root.resolve()).as_posix()
    except ValueError:
        return None


def _gitlab_merge_request_reference(message: str) -> str | None:
    direct_url = re.search(
        r"https?://[^\s<>]+/-/merge_requests/\d+",
        message,
        re.IGNORECASE,
    )
    if direct_url:
        return direct_url.group(0).rstrip(".,;:)")

    reference = re.search(
        r"(?:See\s+merge\s+request\s+)?"
        r"(?:(?P<project>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+))?"
        r"!(?P<iid>\d+)\b",
        message,
        re.IGNORECASE,
    )
    if reference is None:
        return None
    project = reference.group("project")
    return f"{project or ''}!{reference.group('iid')}"


def _git_commit_details(git_root: Path, commit: str, *, source: str) -> GitCommitContext | None:
    code, output, _ = _run_git_text(
        git_root,
        [
            "show",
            "-s",
            "--date=short",
            "--format=%H%x00%h%x00%an%x00%ad%x00%s%x00%B",
            commit,
        ],
    )
    if code != 0 or not output:
        return None
    fields = output.split("\0", 5)
    if len(fields) != 6:
        return None
    commit_hash, short_hash, author, date, subject, body = fields
    return GitCommitContext(
        source=source,
        commit_hash=commit_hash,
        short_hash=short_hash,
        author=author,
        date=date,
        subject=subject,
        merge_request_ref=_gitlab_merge_request_reference(body),
    )


def git_commit_context_for_range(
    git_root: Path | None,
    path: Path,
    start_line: int,
    end_line: int,
    *,
    limit: int = 2,
) -> list[GitCommitContext]:
    """Return line-level commit context, falling back to the latest file commit."""
    if git_root is None or not path.exists():
        return []
    relative = _git_relative_path(git_root, path)
    if relative is None:
        return []

    hashes: list[str] = []
    use_line_blame = start_line > 0 and end_line >= start_line
    if use_line_blame:
        start = max(1, start_line)
        end = max(start, end_line)
        code, output, _ = _run_git_text(
            git_root,
            ["blame", "--line-porcelain", "-L", f"{start},{end}", "--", relative],
            timeout=5.0,
            env={"GIT_PAGER": "cat"},
        )
    else:
        code, output = 1, ""
    if code == 0:
        for line in output.splitlines():
            match = re.fullmatch(r"([0-9a-f]{40,64}) \d+ \d+(?: \d+)?", line)
            if not match:
                continue
            commit = match.group(1)
            if set(commit) == {"0"} or commit in hashes:
                continue
            hashes.append(commit)
            if len(hashes) >= limit:
                break

    contexts = [
        context
        for commit in hashes
        if (context := _git_commit_details(git_root, commit, source="line")) is not None
    ]
    if contexts:
        return contexts

    code, commit, _ = _run_git_text(git_root, ["log", "-1", "--format=%H", "--", relative])
    if code == 0 and commit:
        context = _git_commit_details(git_root, commit, source="file")
        return [context] if context is not None else []
    return []


def _ensure_not_symlink_target(path: Path) -> None:
    if path.is_symlink():
        raise OSError(f"Refusing to replace symlinked TEST path: {path}")


def atomic_copy(source: Path, target: Path) -> None:
    _ensure_not_symlink_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode if target.exists() else None
    with tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=f".{target.name}.", delete=False
    ) as handle:
        temp = Path(handle.name)
        with source.open("rb") as source_handle:
            shutil.copyfileobj(source_handle, handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        if mode is not None:
            os.chmod(temp, mode)
        _ensure_not_symlink_target(target)
        os.replace(temp, target)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """Write exact bytes atomically, optionally restoring a captured file mode."""
    _ensure_not_symlink_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = path.stat().st_mode if path.exists() else None
    final_mode = mode if mode is not None else existing_mode
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if final_mode is not None:
            os.chmod(temp, final_mode)
        _ensure_not_symlink_target(path)
        os.replace(temp, path)
    finally:
        if temp is not None and temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 text atomically while preserving the existing file mode."""
    _ensure_not_symlink_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp, mode)
        _ensure_not_symlink_target(path)
        os.replace(temp, path)
    finally:
        if temp is not None and temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


PROJECT_CONFIG_TEMPLATE = """# Config Review Workbench project configuration
#
# Pattern suggestions are discovered across all changed files. Qualifying noise
# suggestions start hidden for quick review, while saved visible choices override
# that default. Patterns apply project-wide and are organized into categories.
# Version/image/release and operational changes are always left visible.
# Full Diff is always unfiltered.

version: 9

# Verified project directories. Paths are stored relative to this configuration
# file so the repository remains portable between machines and checkouts.
# The first normal launch discovers or prompts for these values automatically.
paths:
  source:
  target:

# Optional full repository web URL used by the local web viewer for exact
# line links. Leave blank to auto-detect from the current Git remote.
git:
  repository_url:

scan:
  exclude_dirs:
    - .git
    - __pycache__
    - .pytest_cache
    - secrets

display:
  # Whitespace-only changes are hidden by default in Focused Diff. Enable this
  # only when you explicitly want indentation/spacing-only blocks shown.
  # Full Diff always shows the original whitespace.
  show_whitespace: false

  # Hidden by default. Focused Diff collapses exact scalar mapping moves and
  # unique name-keyed list moves. Changed named items remain visible as one
  # logical replacement. Ambiguous, invalid, or templated YAML stays visible.
  hide_mapping_order: true

  # Off by default. When enabled, keeps the selected change bright while using a
  # softer palette for surrounding and expanded filtered diff content.
  mute_non_focused: false

patterns: []
"""


def _category_for_kind_or_name(kind: str, name: str = "") -> str:
    if kind == "environment-fragment":
        return CATEGORY_ENVIRONMENT
    if kind in {"url-domain", "host-domain"}:
        return CATEGORY_APP_DOMAINS
    if kind in {"url-shape", "host-shape", "ip-shape"}:
        return CATEGORY_ENDPOINTS

    lowered = name.lower()
    if re.search(
        r"(?:namespace|cluster|region|environment|profile|site|location|\benv\b)", lowered
    ):
        return CATEGORY_ENVIRONMENT
    if re.search(r"(?:url|uri|host|hostname|endpoint|address|\bip\b|port)", lowered):
        return CATEGORY_ENDPOINTS
    if re.search(
        r"(?:user|username|account|principal|client|service.?account|reference|\bref\b|configmap|secret)",
        lowered,
    ):
        return CATEGORY_USERS_REFERENCES
    if re.search(
        r"(?:bucket|database|\bdb\b|schema|index|storage|\bs3\b|table|topic|queue)", lowered
    ):
        return CATEGORY_STORAGE_DATA
    return CATEGORY_OTHER


def _yaml_load(path: Path) -> Any:
    if YAML is None:
        raise WorkbenchError(
            f"{path} exists, but ruamel.yaml is unavailable. Install "
            "python3-ruamel.yaml to read the project configuration."
        )
    yaml = YAML(typ="safe")
    try:
        return yaml.load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WorkbenchError(f"Could not parse project configuration {path}: {exc}") from exc


def _yaml_write(path: Path, data: Mapping[str, Any]) -> None:
    if YAML is None:
        raise WorkbenchError(
            "ruamel.yaml is required to save pattern choices. Install python3-ruamel.yaml."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    temp = path.with_name(f".{path.name}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            yaml.dump(dict(data), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def load_project_path_settings(path: Path) -> tuple[str | None, str | None, str | None]:
    """Load the configured project, source, and target directory strings.

    New configurations store one project directory plus source/target names. Older
    configurations that stored full source and target paths remain supported.
    Values are returned without resolving them so callers can apply the correct
    base directory semantics.
    """
    if not path.exists():
        return None, None, None
    root = _yaml_load(path) or {}
    if not isinstance(root, Mapping):
        raise WorkbenchError(f"Project configuration root must be a mapping: {path}")
    paths = root.get("paths", {}) or {}
    if not isinstance(paths, Mapping):
        raise WorkbenchError(f"'paths' must be a mapping in {path}")

    def clean(value: Any) -> str | None:
        result = str(value).strip() if value is not None else None
        return result or None

    return clean(paths.get("project")), clean(paths.get("source")), clean(paths.get("target"))


def load_project_paths(path: Path) -> tuple[str | None, str | None]:
    """Compatibility helper returning the configured source and target values."""
    _project, source, target = load_project_path_settings(path)
    return source, target


def resolve_configured_path(config_file: Path, value: str) -> Path:
    """Resolve one configured path relative to the project configuration."""
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = config_file.parent / candidate
    return candidate.resolve()


def resolve_configured_project_paths(
    config_file: Path,
    project_value: str | None,
    source_value: str | None,
    target_value: str | None,
) -> tuple[Path, Path]:
    """Resolve new project-based or legacy independent source/target settings."""
    if project_value:
        project = resolve_configured_path(config_file, project_value)
        source_name = source_value or "dev"
        target_name = target_value or "test"
        source = Path(source_name).expanduser()
        target = Path(target_name).expanduser()
        if not source.is_absolute():
            source = project / source
        if not target.is_absolute():
            target = project / target
        return source.resolve(), target.resolve()

    if not source_value or not target_value:
        raise WorkbenchError(
            f"Configure paths.project, or both paths.source and paths.target, in {config_file}."
        )
    return (
        resolve_configured_path(config_file, source_value),
        resolve_configured_path(config_file, target_value),
    )


def _portable_config_path(config_file: Path, path: Path) -> str:
    """Return a stable path string relative to the project configuration."""
    resolved = path.expanduser().resolve()
    try:
        relative = Path(os.path.relpath(resolved, config_file.parent.resolve()))
        return relative.as_posix()
    except ValueError:
        # Different Windows drives cannot be represented with a relative path.
        return str(resolved)


def _default_project_config_data() -> dict[str, Any]:
    return {
        "version": 9,
        "scan": {"exclude_dirs": sorted(DEFAULT_EXCLUDED_DIRS)},
        "display": {
            "show_whitespace": False,
            "hide_mapping_order": True,
            "mute_non_focused": False,
        },
        "patterns": [],
    }


def save_project_paths(path: Path, source: Path, target: Path) -> None:
    """Merge verified source/target directories into the project configuration.

    Sibling source and target directories are stored as one portable project path
    plus their directory names. Unusual non-sibling layouts retain the legacy
    independent path representation.
    """
    if path.exists():
        root = _yaml_load(path) or {}
        if not isinstance(root, Mapping):
            raise WorkbenchError(f"Project configuration root must be a mapping: {path}")
        data: dict[str, Any] = dict(root)
    else:
        data = _default_project_config_data()
    try:
        current_version = int(data.get("version", 0) or 0)
    except (TypeError, ValueError):
        current_version = 0
    data["version"] = max(current_version, 9)

    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if source.parent == target.parent:
        data["paths"] = {
            "project": _portable_config_path(path, source.parent),
            "source": source.name,
            "target": target.name,
        }
    else:
        data["paths"] = {
            "source": _portable_config_path(path, source),
            "target": _portable_config_path(path, target),
        }
    data.setdefault("scan", {"exclude_dirs": sorted(DEFAULT_EXCLUDED_DIRS)})
    data.setdefault(
        "display",
        {
            "show_whitespace": False,
            "hide_mapping_order": True,
            "mute_non_focused": False,
        },
    )
    data.setdefault("patterns", [])
    _yaml_write(path, data)


def load_git_repository_url(path: Path) -> str | None:
    """Load an optional configured full repository web URL."""
    if not path.exists():
        return None
    root = _yaml_load(path) or {}
    if not isinstance(root, Mapping):
        raise WorkbenchError(f"Project configuration root must be a mapping: {path}")
    git = root.get("git", {}) or {}
    if not isinstance(git, Mapping):
        raise WorkbenchError(f"'git' must be a mapping in {path}")
    raw = git.get("repository_url")
    if raw is None or not str(raw).strip():
        return None
    return normalize_git_repository_url(str(raw))


def save_git_repository_url(path: Path, repository_url: str | None) -> None:
    """Persist or clear the optional repository web URL without touching other settings."""
    if path.exists():
        root = _yaml_load(path) or {}
        if not isinstance(root, Mapping):
            raise WorkbenchError(f"Project configuration root must be a mapping: {path}")
        data: dict[str, Any] = dict(root)
    else:
        data = _default_project_config_data()

    try:
        current_version = int(data.get("version", 0) or 0)
    except (TypeError, ValueError):
        current_version = 0
    data["version"] = max(current_version, 9)

    existing_git = data.get("git", {}) or {}
    git: dict[str, Any] = dict(existing_git) if isinstance(existing_git, Mapping) else {}
    if repository_url and repository_url.strip():
        git["repository_url"] = normalize_git_repository_url(repository_url)
    else:
        git.pop("repository_url", None)
    if git:
        data["git"] = git
    else:
        data.pop("git", None)
    _yaml_write(path, data)


def load_project_config(
    path: Path,
) -> tuple[list[PatternRule], set[str], bool, bool, bool, list[str]]:
    patterns: list[PatternRule] = []
    diagnostics: list[str] = []
    excluded = set(DEFAULT_EXCLUDED_DIRS)
    hide_whitespace = True
    hide_mapping_order = True
    mute_non_focused = False
    if not path.exists():
        return (
            patterns,
            excluded,
            hide_whitespace,
            hide_mapping_order,
            mute_non_focused,
            diagnostics,
        )

    root = _yaml_load(path) or {}
    if not isinstance(root, Mapping):
        raise WorkbenchError(f"Project configuration root must be a mapping: {path}")
    try:
        config_version = int(root.get("version", 0))
    except (TypeError, ValueError):
        config_version = 0

    scan = root.get("scan", {}) or {}
    if isinstance(scan, Mapping):
        raw_excludes = scan.get("exclude_dirs")
        if isinstance(raw_excludes, Sequence) and not isinstance(
            raw_excludes, (str, bytes, bytearray)
        ):
            excluded = {str(item) for item in raw_excludes}

    display = root.get("display", {}) or {}
    if isinstance(display, Mapping):
        show_whitespace = bool(display.get("show_whitespace", False))
        hide_whitespace = not show_whitespace
        hide_mapping_order = bool(display.get("hide_mapping_order", True))
        # v6 introduced muting as an enabled-by-default preference. v7 changes
        # the default to OFF, so old generated configs receive the new default
        # once; users can explicitly re-enable it from Display Filters.
        mute_non_focused = (
            bool(display.get("mute_non_focused", False)) if config_version >= 7 else False
        )

    raw_patterns = root.get("patterns", []) or []
    if not isinstance(raw_patterns, Sequence) or isinstance(raw_patterns, (str, bytes, bytearray)):
        raise WorkbenchError(f"'patterns' must be a list in {path}")

    # v8.7 patterns are project-wide and categorized. File scopes are ignored,
    # and duplicate global regex pairs are consolidated.
    by_signature: dict[tuple[str, str, str], PatternRule] = {}
    for number, raw in enumerate(raw_patterns, start=1):
        if not isinstance(raw, Mapping):
            diagnostics.append(f"pattern {number}: skipped; expected a mapping")
            continue
        name = str(raw.get("name", f"Pattern {number}")).strip() or f"Pattern {number}"
        test_regex = str(raw.get("test_regex", "")).strip()
        dev_regex = str(raw.get("dev_regex", "")).strip()
        kind = str(raw.get("kind", "project")).strip() or "project"
        if not test_regex or not dev_regex:
            diagnostics.append(
                f"pattern {number} ({name}): skipped; test_regex and dev_regex are required"
            )
            continue
        if raw.get("files"):
            diagnostics.append(
                f"pattern {number} ({name}): file scope ignored; v8.7 patterns are project-wide"
            )
        try:
            candidate = PatternRule(
                id=_pattern_rule_id(kind, (), test_regex, dev_regex),
                name=name,
                test_regex=test_regex,
                dev_regex=dev_regex,
                files=(),
                category=str(raw.get("category", "")).strip()
                or _category_for_kind_or_name(kind, name),
                enabled=bool(raw.get("enabled", False)),
                kind=kind,
                source=str(path),
            )
        except re.error as exc:
            diagnostics.append(f"pattern {number} ({name}): skipped; invalid regex: {exc}")
            continue
        signature = (test_regex, dev_regex, kind)
        existing = by_signature.get(signature)
        if existing is None:
            by_signature[signature] = candidate
        else:
            existing.enabled = existing.enabled or candidate.enabled
    patterns.extend(by_signature.values())
    return patterns, excluded, hide_whitespace, hide_mapping_order, mute_non_focused, diagnostics


def save_project_config(
    path: Path,
    patterns: Sequence[PatternRule],
    excluded_dirs: set[str],
    hide_whitespace: bool,
    hide_mapping_order: bool,
    mute_non_focused: bool,
) -> None:
    # Merge known settings into the existing local configuration instead of
    # rebuilding it from scratch. Pulling or replacing the application never
    # touches this file, and normal in-app saves preserve unrelated user keys.
    data: dict[str, Any] = {}
    if path.exists():
        existing = _yaml_load(path) or {}
        if not isinstance(existing, Mapping):
            raise WorkbenchError(f"Project configuration root must be a mapping: {path}")
        data = dict(existing)

    try:
        current_version = int(data.get("version", 0) or 0)
    except (TypeError, ValueError):
        current_version = 0
    data["version"] = max(current_version, 9)
    data["scan"] = {"exclude_dirs": sorted(excluded_dirs)}

    existing_display = data.get("display", {}) or {}
    display: dict[str, Any] = (
        dict(existing_display) if isinstance(existing_display, Mapping) else {}
    )
    display.update(
        {
            "show_whitespace": not bool(hide_whitespace),
            "hide_mapping_order": bool(hide_mapping_order),
            "mute_non_focused": bool(mute_non_focused),
        }
    )
    data["display"] = display
    data["patterns"] = []
    raw_patterns: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    category_rank = {name: index for index, name in enumerate(CATEGORY_ORDER)}
    for rule in sorted(
        patterns,
        key=lambda item: (
            category_rank.get(item.category, len(category_rank)),
            item.name.lower(),
            item.id,
        ),
    ):
        signature = (rule.test_regex, rule.dev_regex, rule.kind)
        if signature in seen:
            continue
        seen.add(signature)
        raw_patterns.append(
            {
                "id": _pattern_rule_id(rule.kind, (), rule.test_regex, rule.dev_regex),
                "name": rule.name,
                "test_regex": rule.test_regex,
                "dev_regex": rule.dev_regex,
                "category": rule.category,
                "enabled": bool(rule.enabled),
                "kind": rule.kind,
            }
        )
    data["patterns"] = raw_patterns
    _yaml_write(path, data)


def init_project_config(path: Path) -> None:
    if path.exists():
        raise WorkbenchError(f"Refusing to overwrite existing project configuration: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PROJECT_CONFIG_TEMPLATE, encoding="utf-8")


def _pattern_rule_id(
    kind: str,
    files: Sequence[str],
    test_regex: str,
    dev_regex: str,
) -> str:
    digest = hashlib.sha256()
    for item in (kind, *sorted(files), test_regex, dev_regex):
        digest.update(item.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
    return digest.hexdigest()[:20]


def _pattern_matches_block(
    rule: PatternRule,
    relative_path: str,
    tag: str,
    old_lines: Sequence[str],
    new_lines: Sequence[str],
) -> bool:
    if tag != "replace" or not rule.applies_to(relative_path):
        return False
    old_changed = [line for line in old_lines if line.strip()]
    new_changed = [line for line in new_lines if line.strip()]
    if not old_changed or not new_changed:
        return False
    return all(rule.test_compiled.search(line) for line in old_changed) and all(
        rule.dev_compiled.search(line) for line in new_changed
    )


def _whitespace_normalized_lines(lines: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    for line in lines:
        compact = re.sub(r"\s+", "", line)
        if compact:
            normalized.append(compact)
    return normalized


def is_whitespace_only_block(
    tag: str,
    old_lines: Sequence[str],
    new_lines: Sequence[str],
) -> bool:
    """Return True when removing whitespace makes both sides identical.

    This intentionally includes indentation changes. It is never enabled by
    default, and Full Diff always shows the original text.
    """
    if tag == "insert":
        return bool(new_lines) and not any(line.strip() for line in new_lines)
    if tag == "delete":
        return bool(old_lines) and not any(line.strip() for line in old_lines)
    if tag != "replace":
        return False
    return list(old_lines) != list(new_lines) and _whitespace_normalized_lines(
        old_lines
    ) == _whitespace_normalized_lines(new_lines)


_RELEASE_REVIEW_RE = re.compile(
    r"(?:api.?version|app.?version|version|chart|image|image.?tag|tag|digest|commit|sha(?:256)?|revision|git.?ref|dependency)",
    re.IGNORECASE,
)

_OPERATIONAL_REVIEW_RE = re.compile(
    r"(?:replica|cpu|memory|resource|request|limit|security|privileged|run.?as|capabilit|read.?only.?root|allow.?privilege)",
    re.IGNORECASE,
)


def _nearby_change_context(lines: Sequence[str], start: int, end: int) -> str:
    """Return only a directly associated scalar label, not arbitrary nearby YAML."""
    del end
    if start <= 0:
        return ""
    previous = lines[start - 1].strip()
    match = re.match(r"^-?\s*name\s*:\s*(.+?)\s*$", previous, re.IGNORECASE)
    return match.group(1).strip("\"'") if match else ""


def always_review_reason(
    tag: str,
    old_all: Sequence[str],
    new_all: Sequence[str],
    old_start: int,
    old_end: int,
    new_start: int,
    new_end: int,
) -> str | None:
    old_block = list(old_all[old_start:old_end])
    new_block = list(new_all[new_start:new_end])
    if is_whitespace_only_block(tag, old_block, new_block):
        return None
    if tag in {"insert", "delete"} or len(old_block) != 1 or len(new_block) != 1:
        return "Added, removed, or structural changes"

    old_parsed = _parse_scalar_line(old_block[0])
    new_parsed = _parse_scalar_line(new_block[0])
    key_text = " ".join(
        part
        for part in (
            old_parsed[0] if old_parsed else "",
            new_parsed[0] if new_parsed else "",
            _nearby_change_context(old_all, old_start, old_end),
            _nearby_change_context(new_all, new_start, new_end),
        )
        if part
    )
    if _RELEASE_REVIEW_RE.search(key_text):
        return "Version, image, chart, or revision updates"
    if _OPERATIONAL_REVIEW_RE.search(key_text):
        return "Replica, resource, or security updates"
    return None


def classify_block(
    relative_path: str,
    tag: str,
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    patterns: Sequence[PatternRule],
    *,
    hide_whitespace: bool = False,
    protected_reason: str | None = None,
) -> tuple[str, ...]:
    matched: list[str] = []
    if protected_reason is None:
        matched.extend(
            rule.name
            for rule in patterns
            if rule.enabled
            and _pattern_matches_block(rule, relative_path, tag, old_lines, new_lines)
        )
    if hide_whitespace and is_whitespace_only_block(tag, old_lines, new_lines):
        matched.append("Whitespace-only")
    return tuple(sorted(set(matched)))


_DIFF_MAPPING_SCALAR_RE = re.compile(
    r"^(?P<indent>\s*)(?P<key>[^:#][^:]*?)\s*:\s*(?P<value>.*?)\s*$"
)

_DIFF_LIST_SCALAR_RE = re.compile(r"^(?P<indent>\s*)-\s+(?P<value>.*?)\s*$")

_DIFF_LIST_MAPPING_VALUE_RE = re.compile(r"^[^\s'\"{}\[\]][^:]*:\s(?:.*)$")


def _diff_scalar_identity(line: str) -> tuple[str, str] | None:
    """Return a conservative identity for a simple mapping scalar line.

    This is intentionally not YAML interpretation. It only recognizes an
    unambiguous ``key: scalar`` text line at a specific indentation level.
    Sequence entries, mapping headers, block scalars, and comment-only values
    are excluded so mixed-block refinement cannot pair unrelated structures.
    """
    match = _DIFF_MAPPING_SCALAR_RE.match(line)
    if match is None:
        return None
    key = match.group("key").strip()
    value = match.group("value").strip()
    if not key or key.startswith("-"):
        return None
    if not value or value.startswith(("#", "|", ">")):
        return None
    return match.group("indent"), key


def _diff_list_scalar_indent(line: str) -> str | None:
    """Return indentation for one unambiguous plain scalar list item.

    This deliberately excludes list mappings (``- name: value``), nested
    collections, block scalars, tags, aliases, and comment-only entries. It is
    only used to split equal-length positional list replacements so approved
    text patterns can inspect each scalar pair independently.
    """
    match = _DIFF_LIST_SCALAR_RE.match(line)
    if match is None:
        return None
    value = match.group("value").strip()
    if not value or value.startswith(("#", "|", ">", "{", "[", "&", "*", "!")):
        return None
    # Reject the common YAML list-mapping form while still allowing URLs and
    # host:port values, whose colon is not followed by whitespace.
    if _DIFF_LIST_MAPPING_VALUE_RE.match(value):
        return None
    return match.group("indent")


def _append_gap_opcode(
    output: list[tuple[str, int, int, int, int]],
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    i1: int,
    i2: int,
    j1: int,
    j2: int,
) -> None:
    """Append one unmatched gap without inventing semantic correspondence."""
    old_count = i2 - i1
    new_count = j2 - j1
    if not old_count and not new_count:
        return
    if not old_count:
        output.append(("insert", i1, i1, j1, j2))
        return
    if not new_count:
        output.append(("delete", i1, i2, j1, j1))
        return

    # Preserve the old scalar-friendly behavior only when every positional
    # pair has the exact same conservative scalar identity. Otherwise retain a
    # single replace block rather than guessing how unrelated lines correspond.
    if old_count == new_count and old_count > 1:
        identities = [
            (
                _diff_scalar_identity(old_lines[old_index]),
                _diff_scalar_identity(new_lines[new_index]),
            )
            for old_index, new_index in zip(range(i1, i2), range(j1, j2))
        ]
        if all(
            old_identity is not None and old_identity == new_identity
            for old_identity, new_identity in identities
        ):
            for offset in range(old_count):
                output.append(
                    ("replace", i1 + offset, i1 + offset + 1, j1 + offset, j1 + offset + 1)
                )
            return

        # Equal-length runs of plain scalar list items are safe to compare by
        # position. Requiring one shared indentation level on both sides keeps
        # nested or structurally mixed YAML grouped rather than guessed.
        list_indents = [
            (
                _diff_list_scalar_indent(old_lines[old_index]),
                _diff_list_scalar_indent(new_lines[new_index]),
            )
            for old_index, new_index in zip(range(i1, i2), range(j1, j2))
        ]
        if (
            all(
                old_indent is not None and old_indent == new_indent
                for old_indent, new_indent in list_indents
            )
            and len({old_indent for old_indent, _ in list_indents}) == 1
        ):
            for offset in range(old_count):
                output.append(
                    ("replace", i1 + offset, i1 + offset + 1, j1 + offset, j1 + offset + 1)
                )
            return

    output.append(("replace", i1, i2, j1, j2))


def _refine_mixed_replace(
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    i1: int,
    i2: int,
    j1: int,
    j2: int,
) -> list[tuple[str, int, int, int, int]]:
    """Split a mixed replace block around unique same-key scalar anchors.

    Example::

        - appDomain: test.example
        - createClusterResources: false
        + appDomain: dev.example

    becomes one ``appDomain`` replacement and one independent deletion. Only
    identities occurring exactly once on both sides are eligible, and anchors
    must remain in order. Ambiguous or crossing candidates stay grouped.
    """
    old_identity_positions: dict[tuple[str, str], list[int]] = {}
    new_identity_positions: dict[tuple[str, str], list[int]] = {}

    for index in range(i1, i2):
        identity = _diff_scalar_identity(old_lines[index])
        if identity is not None:
            old_identity_positions.setdefault(identity, []).append(index)
    for index in range(j1, j2):
        identity = _diff_scalar_identity(new_lines[index])
        if identity is not None:
            new_identity_positions.setdefault(identity, []).append(index)

    unique_pairs = [
        (old_positions[0], new_identity_positions[identity][0], identity)
        for identity, old_positions in old_identity_positions.items()
        if len(old_positions) == 1 and len(new_identity_positions.get(identity, ())) == 1
    ]
    if not unique_pairs:
        output: list[tuple[str, int, int, int, int]] = []
        _append_gap_opcode(output, old_lines, new_lines, i1, i2, j1, j2)
        return output

    # Select the longest non-crossing chain of candidate anchors. The identity
    # sequences are unique, so SequenceMatcher provides a deterministic LCS.
    unique_pairs.sort(key=lambda item: item[0])
    old_identities = [identity for _, _, identity in unique_pairs]
    new_ordered_pairs = sorted(unique_pairs, key=lambda item: item[1])
    new_identities = [identity for _, _, identity in new_ordered_pairs]
    matcher = difflib.SequenceMatcher(None, old_identities, new_identities, autojunk=False)

    pair_by_identity = {
        identity: (old_index, new_index) for old_index, new_index, identity in unique_pairs
    }
    anchors: list[tuple[int, int]] = []
    for match in matcher.get_matching_blocks():
        for offset in range(match.size):
            identity = old_identities[match.a + offset]
            anchors.append(pair_by_identity[identity])
    anchors.sort()

    if not anchors:
        output = []
        _append_gap_opcode(output, old_lines, new_lines, i1, i2, j1, j2)
        return output

    output: list[tuple[str, int, int, int, int]] = []
    old_cursor = i1
    new_cursor = j1
    for old_index, new_index in anchors:
        _append_gap_opcode(
            output,
            old_lines,
            new_lines,
            old_cursor,
            old_index,
            new_cursor,
            new_index,
        )
        output.append(("replace", old_index, old_index + 1, new_index, new_index + 1))
        old_cursor = old_index + 1
        new_cursor = new_index + 1
    _append_gap_opcode(output, old_lines, new_lines, old_cursor, i2, new_cursor, j2)
    return output


def refined_opcodes(
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    *,
    debug_label: str = "",
) -> list[tuple[str, int, int, int, int]]:
    """Return stable text opcodes with conservative scalar granularity."""
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    raw_opcodes = list(matcher.get_opcodes())
    debug(
        "Raw difflib alignment completed",
        file=debug_label,
        opcode_count=len(raw_opcodes),
        opcodes=[(tag, i1 + 1, i2, j1 + 1, j2) for tag, i1, i2, j1, j2 in raw_opcodes],
    )
    output: list[tuple[str, int, int, int, int]] = []
    for tag, i1, i2, j1, j2 in raw_opcodes:
        if tag == "replace":
            refined = _refine_mixed_replace(old_lines, new_lines, i1, i2, j1, j2)
            output.extend(refined)
            debug(
                "Replace opcode refined",
                file=debug_label,
                input_range=(i1 + 1, i2, j1 + 1, j2),
                input_counts=(i2 - i1, j2 - j1),
                output_opcodes=[
                    (r_tag, r_i1 + 1, r_i2, r_j1 + 1, r_j2)
                    for r_tag, r_i1, r_i2, r_j1, r_j2 in refined
                ],
            )
        else:
            output.append((tag, i1, i2, j1, j2))
    return output


def _scalar_signature(value: Any) -> tuple[str, Any] | None:
    """Return a stable signature for plain YAML scalar values only."""
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int) and not isinstance(value, bool):
        return ("int", int(value))
    if isinstance(value, float):
        return ("float", float(value))
    if isinstance(value, str):
        return ("str", str(value))
    return None


def _mapping_scalar_line_entries(
    text: str,
    *,
    label: str,
) -> MappingScalarAnalysis:
    """Parse exact scalar mapping entries and report why analysis is unavailable.

    Template markers are intentionally not pre-rejected. Quoted or otherwise
    valid template text is allowed through; ruamel.yaml is the authority on
    whether mapping-order analysis can safely proceed.
    """
    line_count = len(text.splitlines())
    template_markers_present = "{{" in text and "}}" in text
    debug(
        "Mapping-order YAML parse started",
        side=label,
        line_count=line_count,
        character_count=len(text),
        template_markers_present=template_markers_present,
    )

    if YAML is None:
        reason = "ruamel.yaml is not installed"
        debug("Mapping-order YAML parse unavailable", side=label, reason=reason)
        return MappingScalarAnalysis(entries={}, unavailable_reason=reason)

    yaml = YAML(typ="rt")
    try:
        documents = list(yaml.load_all(text))
    except Exception as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            reason = f"YAML parse failed near line {int(mark.line) + 1}"
            debug(
                "Mapping-order YAML parse failed",
                side=label,
                exception_type=type(exc).__name__,
                line=int(mark.line) + 1,
                column=int(mark.column) + 1,
                reason=reason,
            )
        else:
            reason = f"YAML parse failed: {type(exc).__name__}"
            debug(
                "Mapping-order YAML parse failed",
                side=label,
                exception_type=type(exc).__name__,
                reason=reason,
            )
        return MappingScalarAnalysis(entries={}, unavailable_reason=reason)

    source_lines = text.splitlines()
    entries: dict[int, tuple[tuple[Any, ...], str, tuple[str, Any], str]] = {}

    def walk(value: Any, path: tuple[Any, ...]) -> None:
        if isinstance(value, CommentedMap):
            for key, child in value.items():
                try:
                    line, _column = value.lc.key(key)
                except Exception:
                    line = None
                signature = _scalar_signature(child)
                if (
                    line is not None
                    and signature is not None
                    and 0 <= int(line) < len(source_lines)
                ):
                    entries[int(line)] = (
                        path,
                        str(key),
                        signature,
                        source_lines[int(line)],
                    )
                walk(child, path + (("key", str(key)),))
            return
        if isinstance(value, CommentedSeq):
            for index, child in enumerate(value):
                # Sequence order remains meaningful. The index is retained in
                # the parent path so entries in different list items never pair.
                walk(child, path + (("index", index),))

    for doc_index, document in enumerate(documents):
        walk(document, (("doc", doc_index),))

    debug(
        "Mapping-order YAML parse completed",
        side=label,
        document_count=len(documents),
        scalar_mapping_entry_count=len(entries),
        template_markers_present=template_markers_present,
    )
    return MappingScalarAnalysis(entries=entries)


def _yaml_line_indent(line: str) -> int:
    """Return the number of leading spaces on one YAML source line."""
    return len(line) - len(line.lstrip(" "))


def _last_sequence_item_end(
    source_lines: Sequence[str],
    start: int,
    dash_indent: int,
) -> int:
    """Find the conservative end of the last parsed item in one YAML sequence."""
    for line_index in range(start + 1, len(source_lines)):
        raw_line = source_lines[line_index]
        stripped = raw_line.strip()
        if not stripped:
            continue
        indent = _yaml_line_indent(raw_line)
        if indent < dash_indent:
            return line_index
        if indent == dash_indent and raw_line.lstrip().startswith("-"):
            return line_index
    return len(source_lines)


def _keyed_list_item_entries(
    text: str,
    *,
    label: str,
) -> KeyedListAnalysis:
    """Parse complete mapping list items with a unique scalar ``name`` key.

    This is deliberately narrower than general semantic YAML comparison. A
    sequence is eligible only when every item is a mapping, every mapping has a
    scalar ``name``, and those names are unique within that one sequence. The
    exact source line ranges are retained so a later merge still operates on
    concrete TEST and DEV text rather than reconstructed YAML.
    """
    if YAML is None:
        return KeyedListAnalysis(items={}, unavailable_reason="ruamel.yaml is not installed")

    yaml = YAML(typ="rt")
    try:
        documents = list(yaml.load_all(text))
    except Exception as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            reason = f"YAML parse failed near line {int(mark.line) + 1}"
        else:
            reason = f"YAML parse failed: {type(exc).__name__}"
        debug("Keyed-list YAML parse unavailable", side=label, reason=reason)
        return KeyedListAnalysis(items={}, unavailable_reason=reason)

    source_lines = text.splitlines()
    entries: dict[tuple[Any, ...], KeyedListItem] = {}

    def walk(value: Any, path: tuple[Any, ...]) -> None:
        if isinstance(value, CommentedMap):
            for key, child in value.items():
                walk(child, path + (("key", str(key)),))
            return

        if not isinstance(value, CommentedSeq):
            return

        keyed_children: list[tuple[int, CommentedMap, tuple[str, Any], int]] = []
        eligible = bool(value)
        seen_identities: set[tuple[str, Any]] = set()
        for index, child in enumerate(value):
            if not isinstance(child, CommentedMap) or "name" not in child:
                eligible = False
                break
            identity = _scalar_signature(child.get("name"))
            if identity is None or identity in seen_identities:
                eligible = False
                break
            try:
                start_line, _column = value.lc.item(index)
            except Exception:
                eligible = False
                break
            start = int(start_line)
            if not (0 <= start < len(source_lines)):
                eligible = False
                break
            raw_line = source_lines[start]
            if not raw_line.lstrip().startswith("-"):
                eligible = False
                break
            seen_identities.add(identity)
            keyed_children.append((index, child, identity, start))

        if eligible and keyed_children:
            starts = [start for _index, _child, _identity, start in keyed_children]
            if starts != sorted(starts) or len(set(starts)) != len(starts):
                eligible = False

        if eligible and keyed_children:
            for position, (_index, child, identity, start) in enumerate(keyed_children):
                if position + 1 < len(keyed_children):
                    end = keyed_children[position + 1][3]
                else:
                    dash_indent = _yaml_line_indent(source_lines[start])
                    end = _last_sequence_item_end(source_lines, start, dash_indent)
                if end <= start:
                    eligible = False
                    break
                entry_key = path + (("name", identity),)
                entries[entry_key] = KeyedListItem(
                    parent=path,
                    identity=identity,
                    start=start,
                    end=end,
                    lines=tuple(source_lines[start:end]),
                )

        # Preserve stable paths for nested structures. Eligible named items use
        # their name instead of their positional index; ambiguous sequences keep
        # the index and are never reconciled as order-insensitive.
        if eligible and keyed_children:
            for _index, child, identity, _start in keyed_children:
                walk(child, path + (("item-name", identity),))
        else:
            for index, child in enumerate(value):
                walk(child, path + (("index", index),))

    for doc_index, document in enumerate(documents):
        walk(document, (("doc", doc_index),))

    debug(
        "Keyed-list YAML parse completed",
        side=label,
        document_count=len(documents),
        keyed_item_count=len(entries),
    )
    return KeyedListAnalysis(items=entries)


def _whole_keyed_items_for_range(
    items: Mapping[tuple[Any, ...], KeyedListItem],
    start: int,
    end: int,
) -> list[tuple[tuple[Any, ...], KeyedListItem]] | None:
    """Return complete, contiguous keyed items that exactly cover a diff range."""
    matches = sorted(
        (
            (entry_key, item)
            for entry_key, item in items.items()
            if item.start >= start and item.end <= end
        ),
        key=lambda pair: pair[1].start,
    )
    if not matches or matches[0][1].start != start or matches[-1][1].end != end:
        return None
    parent = matches[0][1].parent
    cursor = start
    for _entry_key, item in matches:
        if item.parent != parent or item.start != cursor:
            return None
        cursor = item.end
    return matches if cursor == end else None


def _is_yaml_order_reason(reason: str) -> bool:
    return reason.startswith(("YAML mapping order", "YAML keyed-list order"))


def _is_yaml_order_continuation(hidden_by: Sequence[str]) -> bool:
    return len(hidden_by) == 1 and hidden_by[0].endswith("order continuation")


def _reconcile_keyed_list_blocks(
    blocks: Sequence[ChangeBlock],
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    test_text: str,
    dev_text: str,
    *,
    relative_path: str,
) -> MappingOrderResult:
    """Collapse moved ``name``-keyed YAML items into safe logical replacements.

    Only a pure TEST deletion and pure DEV insertion are paired, and only when
    both ranges consist entirely of the same unique named items under the same
    parsed sequence. Unchanged moved items become order-only noise. Changed
    moved items remain visible as one concrete replacement using their real
    TEST and DEV line ranges. Anything partial, duplicated, templated, or
    ambiguous falls back to the original text diff unchanged.
    """
    test_analysis = _keyed_list_item_entries(test_text, label=f"TEST:{relative_path}")
    dev_analysis = _keyed_list_item_entries(dev_text, label=f"DEV:{relative_path}")
    reasons = [
        reason
        for reason in (test_analysis.unavailable_reason, dev_analysis.unavailable_reason)
        if reason
    ]
    if reasons:
        return MappingOrderResult(
            blocks=list(blocks),
            unavailable_reason="; ".join(dict.fromkeys(reasons)),
        )

    delete_candidates: dict[
        tuple[tuple[Any, ...], frozenset[tuple[Any, ...]]],
        list[tuple[int, list[tuple[tuple[Any, ...], KeyedListItem]]]],
    ] = defaultdict(list)
    insert_candidates: dict[
        tuple[tuple[Any, ...], frozenset[tuple[Any, ...]]],
        list[tuple[int, list[tuple[tuple[Any, ...], KeyedListItem]]]],
    ] = defaultdict(list)

    for block_index, block in enumerate(blocks):
        if block.old_count and block.new_count == 0:
            items = _whole_keyed_items_for_range(
                test_analysis.items, block.old_start, block.old_end
            )
            if items:
                key = (items[0][1].parent, frozenset(entry_key for entry_key, _item in items))
                delete_candidates[key].append((block_index, items))
        elif block.new_count and block.old_count == 0:
            items = _whole_keyed_items_for_range(dev_analysis.items, block.new_start, block.new_end)
            if items:
                key = (items[0][1].parent, frozenset(entry_key for entry_key, _item in items))
                insert_candidates[key].append((block_index, items))

    output = list(blocks)
    used_blocks: set[int] = set()
    reconciled = 0
    for key, delete_matches in delete_candidates.items():
        insert_matches = insert_candidates.get(key, [])
        if len(delete_matches) != 1 or len(insert_matches) != 1:
            continue
        delete_index, old_items = delete_matches[0]
        insert_index, new_items = insert_matches[0]
        if delete_index in used_blocks or insert_index in used_blocks:
            continue

        old_by_key = dict(old_items)
        new_by_key = dict(new_items)
        changed_keys = [
            entry_key
            for entry_key, _item in old_items
            if old_by_key[entry_key].lines != new_by_key[entry_key].lines
        ]

        delete_source = output[delete_index]
        insert_source = output[insert_index]
        if changed_keys:
            changed_old = [old_by_key[entry_key] for entry_key in changed_keys]
            changed_new = [new_by_key[entry_key] for entry_key in changed_keys]
            old_start = min(item.start for item in changed_old)
            old_end = max(item.end for item in changed_old)
            new_start = min(item.start for item in changed_new)
            new_end = max(item.end for item in changed_new)
            hidden_by: tuple[str, ...] = ()
        else:
            old_start = delete_source.old_start
            old_end = delete_source.old_end
            new_start = insert_source.new_start
            new_end = insert_source.new_end
            hidden_by = ("YAML keyed-list order",)

        # Anchor the logical replacement at the TEST deletion opcode. This keeps
        # merge actions tied to the concrete TEST range while the DEV insertion
        # opcode becomes a silent continuation for Focused Diff rendering.
        output[delete_index] = _logical_block(
            source=delete_source,
            tag="replace",
            old_start=old_start,
            old_end=old_end,
            new_start=new_start,
            new_end=new_end,
            old_lines=old_lines[old_start:old_end],
            new_lines=new_lines[new_start:new_end],
            hidden_by=hidden_by,
        )
        output[insert_index] = _logical_block(
            source=insert_source,
            tag=insert_source.tag,
            old_start=insert_source.old_start,
            old_end=insert_source.old_start,
            new_start=insert_source.new_start,
            new_end=insert_source.new_start,
            old_lines=[],
            new_lines=[],
            hidden_by=("YAML keyed-list order continuation",),
        )
        used_blocks.update({delete_index, insert_index})
        reconciled += 1
        debug(
            "Keyed-list move reconciled",
            file=relative_path,
            parent=key[0],
            named_item_count=len(old_items),
            changed_item_count=len(changed_keys),
            test_range=(old_start + 1, old_end),
            dev_range=(new_start + 1, new_end),
        )

    debug(
        "Keyed-list reconciliation completed",
        file=relative_path,
        reconciled_pair_count=reconciled,
    )
    return MappingOrderResult(blocks=output)


def _logical_block(
    *,
    source: ChangeBlock,
    tag: str,
    old_start: int,
    old_end: int,
    new_start: int,
    new_end: int,
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    hidden_by: tuple[str, ...] = (),
) -> ChangeBlock:
    return ChangeBlock(
        tag=tag,
        old_start=old_start,
        old_end=old_end,
        new_start=new_start,
        new_end=new_end,
        old_lines=list(old_lines),
        new_lines=list(new_lines),
        hidden_by=hidden_by,
        protected_reason=None,
        opcode_key=source.opcode_key
        or (
            source.tag,
            source.old_start,
            source.old_end,
            source.new_start,
            source.new_end,
        ),
    )


def _reconcile_mapping_order_blocks(
    blocks: Sequence[ChangeBlock],
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    test_text: str,
    dev_text: str,
    *,
    relative_path: str,
) -> MappingOrderResult:
    """Hide exact scalar mapping entries moved under the same parsed parent.

    The reconciliation is deliberately narrow. It only handles unique atomic
    lines when difflib represents the move as one pure insertion/deletion plus
    one one-line replacement. More complex or ambiguous moves remain visible.
    Lists are never treated as order-insensitive.
    """
    debug(
        "Mapping-order reconciliation started",
        file=relative_path,
        input_block_count=len(blocks),
    )
    test_analysis = _mapping_scalar_line_entries(test_text, label=f"TEST:{relative_path}")
    dev_analysis = _mapping_scalar_line_entries(dev_text, label=f"DEV:{relative_path}")
    reasons = [
        reason
        for reason in (
            test_analysis.unavailable_reason,
            dev_analysis.unavailable_reason,
        )
        if reason
    ]
    if reasons:
        unavailable_reason = "; ".join(dict.fromkeys(reasons))
        debug(
            "Mapping-order reconciliation unavailable",
            file=relative_path,
            reason=unavailable_reason,
        )
        return MappingOrderResult(
            blocks=list(blocks),
            unavailable_reason=unavailable_reason,
        )

    old_entries = test_analysis.entries
    new_entries = dev_analysis.entries
    old_events: dict[tuple[Any, ...], list[tuple[int, int]]] = defaultdict(list)
    new_events: dict[tuple[Any, ...], list[tuple[int, int]]] = defaultdict(list)

    for block_index, block in enumerate(blocks):
        if block.old_count == 1 and len(block.old_lines) == 1:
            entry = old_entries.get(block.old_start)
            if entry is not None:
                parent, key, value_signature, raw_line = entry
                identity = (parent, key, value_signature, raw_line)
                old_events[identity].append((block_index, block.old_start))
        if block.new_count == 1 and len(block.new_lines) == 1:
            entry = new_entries.get(block.new_start)
            if entry is not None:
                parent, key, value_signature, raw_line = entry
                identity = (parent, key, value_signature, raw_line)
                new_events[identity].append((block_index, block.new_start))

    debug(
        "Mapping-order candidate events collected",
        file=relative_path,
        test_identity_count=len(old_events),
        dev_identity_count=len(new_events),
        test_event_count=sum(len(items) for items in old_events.values()),
        dev_event_count=sum(len(items) for items in new_events.values()),
    )

    output = list(blocks)
    used_blocks: set[int] = set()
    hidden_pairs = 0
    for identity, old_matches in old_events.items():
        new_matches = new_events.get(identity, [])
        parent, key, value_signature, raw_line = identity
        identity_hash = hashlib.sha256(raw_line.encode("utf-8")).hexdigest()[:12]
        if len(old_matches) != 1 or len(new_matches) != 1:
            if old_matches and new_matches:
                debug(
                    "Mapping-order candidate skipped as ambiguous",
                    file=relative_path,
                    key=key,
                    parent=parent,
                    scalar_type=value_signature[0],
                    value_hash=identity_hash,
                    test_match_count=len(old_matches),
                    dev_match_count=len(new_matches),
                )
            continue
        old_block_index, old_line_index = old_matches[0]
        new_block_index, new_line_index = new_matches[0]
        if old_block_index == new_block_index:
            debug(
                "Mapping-order candidate stayed inside one block",
                file=relative_path,
                key=key,
                block_index=old_block_index,
                value_hash=identity_hash,
            )
            continue
        if old_block_index in used_blocks or new_block_index in used_blocks:
            debug(
                "Mapping-order candidate skipped because a block was already paired",
                file=relative_path,
                key=key,
                test_block_index=old_block_index,
                dev_block_index=new_block_index,
                value_hash=identity_hash,
            )
            continue

        old_source = output[old_block_index]
        new_source = output[new_block_index]
        if raw_line != new_lines[new_line_index]:
            debug(
                "Mapping-order candidate failed exact text verification",
                file=relative_path,
                key=key,
                test_line=old_line_index + 1,
                dev_line=new_line_index + 1,
                value_hash=identity_hash,
            )
            continue

        # The pure side becomes the compact mapping-order marker; an adjacent
        # one-line replacement is reduced to the real remaining insertion or
        # deletion. A pure delete+insert pair uses one marker plus one silent
        # continuation block so the original opcode stream remains renderable.
        reconciliation_shape: str | None = None
        if old_source.new_count == 0 and new_source.old_count == 0:
            reconciliation_shape = "delete+insert"
            output[old_block_index] = _logical_block(
                source=old_source,
                tag="replace",
                old_start=old_line_index,
                old_end=old_line_index + 1,
                new_start=new_line_index,
                new_end=new_line_index + 1,
                old_lines=[raw_line],
                new_lines=[raw_line],
                hidden_by=("YAML mapping order",),
            )
            output[new_block_index] = _logical_block(
                source=new_source,
                tag="insert",
                old_start=new_source.old_start,
                old_end=new_source.old_end,
                new_start=new_source.new_start,
                new_end=new_source.new_end,
                old_lines=[],
                new_lines=new_source.new_lines,
                hidden_by=("YAML mapping order continuation",),
            )
        elif old_source.new_count == 0 and new_source.old_count == 1 and new_source.new_count == 1:
            reconciliation_shape = "delete+replace"
            output[old_block_index] = _logical_block(
                source=old_source,
                tag="replace",
                old_start=old_line_index,
                old_end=old_line_index + 1,
                new_start=new_line_index,
                new_end=new_line_index + 1,
                old_lines=[raw_line],
                new_lines=[raw_line],
                hidden_by=("YAML mapping order",),
            )
            output[new_block_index] = _logical_block(
                source=new_source,
                tag="delete",
                old_start=new_source.old_start,
                old_end=new_source.old_end,
                new_start=new_source.new_start,
                new_end=new_source.new_start,
                old_lines=new_source.old_lines,
                new_lines=[],
            )
        elif new_source.old_count == 0 and old_source.old_count == 1 and old_source.new_count == 1:
            reconciliation_shape = "replace+insert"
            output[new_block_index] = _logical_block(
                source=new_source,
                tag="replace",
                old_start=old_line_index,
                old_end=old_line_index + 1,
                new_start=new_line_index,
                new_end=new_line_index + 1,
                old_lines=[raw_line],
                new_lines=[raw_line],
                hidden_by=("YAML mapping order",),
            )
            output[old_block_index] = _logical_block(
                source=old_source,
                tag="insert",
                old_start=old_source.old_start,
                old_end=old_source.old_start,
                new_start=old_source.new_start,
                new_end=old_source.new_end,
                old_lines=[],
                new_lines=old_source.new_lines,
            )
        else:
            debug(
                "Mapping-order candidate shape was unsupported",
                file=relative_path,
                key=key,
                test_block_shape=(old_source.old_count, old_source.new_count),
                dev_block_shape=(new_source.old_count, new_source.new_count),
                value_hash=identity_hash,
            )
            continue

        hidden_pairs += 1
        used_blocks.update({old_block_index, new_block_index})
        debug(
            "Mapping-order candidate reconciled",
            file=relative_path,
            key=key,
            parent=parent,
            scalar_type=value_signature[0],
            value_hash=identity_hash,
            test_line=old_line_index + 1,
            dev_line=new_line_index + 1,
            shape=reconciliation_shape,
        )

    debug(
        "Mapping-order reconciliation completed",
        file=relative_path,
        input_block_count=len(blocks),
        output_block_count=len(output),
        hidden_pair_count=hidden_pairs,
    )
    return MappingOrderResult(blocks=output)


def _opcode_coordinate_key(block: ChangeBlock) -> tuple[str, int, int, int, int]:
    return block.opcode_key or (
        block.tag,
        block.old_start,
        block.old_end,
        block.new_start,
        block.new_end,
    )


def compute_filter_result(
    test_text: str,
    dev_text: str,
    patterns: Sequence[PatternRule],
    relative_path: str,
    *,
    hide_whitespace: bool = False,
    hide_mapping_order: bool = False,
) -> FilterResult:
    """Calculate and classify the file diff exactly once."""
    old_lines = test_text.splitlines()
    new_lines = dev_text.splitlines()
    debug(
        "Canonical diff calculation started",
        file=relative_path,
        test_line_count=len(old_lines),
        dev_line_count=len(new_lines),
        enabled_pattern_count=sum(1 for pattern in patterns if pattern.enabled),
        hide_whitespace=hide_whitespace,
        hide_mapping_order=hide_mapping_order,
    )

    opcodes = refined_opcodes(old_lines, new_lines, debug_label=relative_path)
    opcode_counts: dict[str, int] = defaultdict(int)
    for tag, i1, i2, j1, j2 in opcodes:
        opcode_counts[tag] += 1
        debug(
            "Canonical diff opcode",
            file=relative_path,
            tag=tag,
            test_range=(i1 + 1, i2),
            dev_range=(j1 + 1, j2),
            test_count=i2 - i1,
            dev_count=j2 - j1,
        )
    debug(
        "Canonical diff opcodes completed",
        file=relative_path,
        opcode_count=len(opcodes),
        opcode_counts=dict(opcode_counts),
    )

    blocks: list[ChangeBlock] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        block = ChangeBlock(
            tag=tag,
            old_start=i1,
            old_end=i2,
            new_start=j1,
            new_end=j2,
            old_lines=list(old_lines[i1:i2]),
            new_lines=list(new_lines[j1:j2]),
            opcode_key=(tag, i1, i2, j1, j2),
        )
        blocks.append(block)
        debug(
            "Canonical change block created",
            file=relative_path,
            tag=tag,
            test_range=(i1 + 1, i2),
            dev_range=(j1 + 1, j2),
            test_count=block.old_count,
            dev_count=block.new_count,
        )

    mapping_order_unavailable_reason: str | None = None
    if hide_mapping_order:
        keyed_reconciliation = _reconcile_keyed_list_blocks(
            blocks,
            old_lines,
            new_lines,
            test_text,
            dev_text,
            relative_path=relative_path,
        )
        blocks = keyed_reconciliation.blocks
        mapping_reconciliation = _reconcile_mapping_order_blocks(
            blocks,
            old_lines,
            new_lines,
            test_text,
            dev_text,
            relative_path=relative_path,
        )
        blocks = mapping_reconciliation.blocks
        reasons = [
            reason
            for reason in (
                keyed_reconciliation.unavailable_reason,
                mapping_reconciliation.unavailable_reason,
            )
            if reason
        ]
        if reasons:
            mapping_order_unavailable_reason = "; ".join(dict.fromkeys(reasons))

    classified: list[ChangeBlock] = []
    for block_index, block in enumerate(blocks):
        if any(_is_yaml_order_reason(reason) for reason in block.hidden_by):
            classified.append(block)
            debug(
                "Change block classified",
                file=relative_path,
                block_index=block_index,
                tag=block.tag,
                test_range=(block.old_start + 1, block.old_end),
                dev_range=(block.new_start + 1, block.new_end),
                protected_reason=None,
                hidden_by=block.hidden_by,
                route="mapping-order-hidden",
            )
            continue
        protected_reason = always_review_reason(
            block.tag,
            old_lines,
            new_lines,
            block.old_start,
            block.old_end,
            block.new_start,
            block.new_end,
        )
        hidden_by = classify_block(
            relative_path,
            block.tag,
            block.old_lines,
            block.new_lines,
            patterns,
            hide_whitespace=hide_whitespace,
            protected_reason=protected_reason,
        )
        block.hidden_by = hidden_by
        block.protected_reason = protected_reason
        classified.append(block)
        debug(
            "Change block classified",
            file=relative_path,
            block_index=block_index,
            tag=block.tag,
            test_range=(block.old_start + 1, block.old_end),
            dev_range=(block.new_start + 1, block.new_end),
            protected_reason=protected_reason,
            hidden_by=hidden_by,
            route="hidden" if hidden_by else "visible",
        )

    hidden = [block for block in classified if block.is_hidden]
    visible = [block for block in classified if not block.is_hidden]
    debug(
        "Canonical diff calculation completed",
        file=relative_path,
        block_count=len(classified),
        visible_block_count=len(visible),
        hidden_block_count=len(hidden),
        mapping_order_unavailable_reason=mapping_order_unavailable_reason,
    )
    return FilterResult(
        opcodes=opcodes,
        blocks=classified,
        hidden=hidden,
        visible=visible,
        mapping_order_unavailable_reason=mapping_order_unavailable_reason,
    )


_YAML_SCALAR_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[^:#][^:]*?)\s*:\s*(?P<value>.*?)\s*$")

_LIST_SCALAR_RE = re.compile(r"^(?P<indent>\s*)-\s*(?P<value>.*?)\s*$")

_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$", re.IGNORECASE)

_HOST_PORT_RE = re.compile(
    r"^(?P<host>(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,})(?::\d+)?$",
    re.IGNORECASE,
)

_IPV4_PORT_RE = re.compile(
    r"^(?P<ip>(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3})(?::\d+)?$"
)

_CHAINED_URL_RE = re.compile(
    r"^(?P<scheme>(?:[a-z][a-z0-9+.-]*:){1,2})//(?P<host>[^/:\s]+)(?::\d+)?",
    re.IGNORECASE,
)

_SENSITIVE_KEY_RE = re.compile(
    r"(?:password|passwd|token|secret|api[_-]?key|private[_-]?key|access[_-]?key|credential)",
    re.IGNORECASE,
)


def _strip_scalar_quotes_and_comment(value: str) -> str:
    value = re.sub(r"\s+#.*$", "", value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def _parse_scalar_line(line: str) -> tuple[str, str] | None:
    match = _YAML_SCALAR_RE.match(line)
    if match:
        return match.group("key").strip(), _strip_scalar_quotes_and_comment(match.group("value"))
    list_match = _LIST_SCALAR_RE.match(line)
    if list_match:
        return "<list-item>", _strip_scalar_quotes_and_comment(list_match.group("value"))
    return None


def _url_parts(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.scheme and parsed.hostname:
        return parsed.scheme.lower(), parsed.hostname.lower()

    # Handles chained schemes such as jdbc:postgresql://host:5432/db.
    chained = _CHAINED_URL_RE.match(value)
    if chained:
        return chained.group("scheme").rstrip(":").lower(), chained.group("host").lower()
    return None


def _host_value(value: str) -> str | None:
    match = _HOST_PORT_RE.fullmatch(value)
    return match.group("host").lower() if match else None


def _ip_value(value: str) -> str | None:
    match = _IPV4_PORT_RE.fullmatch(value)
    return match.group("ip") if match else None


def _apps_domain(host: str) -> str | None:
    lowered = host.lower()
    marker = ".apps."
    index = lowered.find(marker)
    if index < 0:
        return None
    return lowered[index + 1 :]


def _line_regex_for_exact_scalar(key: str, value: str) -> str:
    escaped_value = re.escape(value)
    if key == "<list-item>":
        return rf"^\s*-\s*[\"']?{escaped_value}[\"']?\s*(?:#.*)?$"
    return rf"^\s*{re.escape(key)}\s*:\s*[\"']?{escaped_value}[\"']?\s*(?:#.*)?$"


def _line_regex_for_url_domain(key: str, domain: str) -> str:
    prefix = r"^\s*-\s*" if key == "<list-item>" else rf"^\s*{re.escape(key)}\s*:\s*"
    return (
        rf"(?i){prefix}[\"']?(?:[a-z][a-z0-9+.-]*:){{1,2}}//"
        rf"[a-z0-9.-]+\.{re.escape(domain)}(?::\d+)?(?:/[^\s\"']*)?"
        rf"[\"']?\s*(?:#.*)?$"
    )


def _line_regex_for_host_domain(key: str, domain: str) -> str:
    prefix = r"^\s*-\s*" if key == "<list-item>" else rf"^\s*{re.escape(key)}\s*:\s*"
    return (
        rf"(?i){prefix}[\"']?[a-z0-9.-]+\.{re.escape(domain)}(?::\d+)?"
        rf"[\"']?\s*(?:#.*)?$"
    )


def _line_prefix_regex(key: str) -> str:
    return r"^\s*-\s*" if key == "<list-item>" else rf"^\s*{re.escape(key)}\s*:\s*"


def _line_regex_for_url_shape(key: str) -> str:
    return (
        rf"(?i){_line_prefix_regex(key)}[\"']?(?:[a-z][a-z0-9+.-]*:){{1,2}}//[^\s\"']+"
        rf"[\"']?\s*(?:#.*)?$"
    )


def _line_regex_for_host_shape(key: str) -> str:
    return (
        rf"(?i){_line_prefix_regex(key)}[\"']?"
        rf"(?:[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?\.)+[a-z]{{2,}}(?::\d+)?"
        rf"[\"']?\s*(?:#.*)?$"
    )


def _line_regex_for_ip_shape(key: str) -> str:
    return (
        rf"{_line_prefix_regex(key)}[\"']?"
        rf"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){{3}}"
        rf"(?::\d+)?[\"']?\s*(?:#.*)?$"
    )


def _line_regex_for_fragment(key: str, fragment: str) -> str:
    return rf"{_line_prefix_regex(key)}.*{re.escape(fragment)}.*$"


def _scalar_fragment_pairs(old_value: str, new_value: str) -> list[tuple[str, str]]:
    """Return conservative literal substitutions inside one scalar value pair.

    Token-level comparison catches environment words such as ``test`` -> ``dev``
    even when character-level alignment would split them into tiny edits.
    """
    candidates: set[tuple[str, str]] = set()

    old_tokens = re.findall(r"[A-Za-z0-9]+", old_value)
    new_tokens = re.findall(r"[A-Za-z0-9]+", new_value)
    token_matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    for tag, i1, i2, j1, j2 in token_matcher.get_opcodes():
        if tag != "replace":
            continue
        old_span = old_tokens[i1:i2]
        new_span = new_tokens[j1:j2]
        if len(old_span) == len(new_span):
            pairs = zip(old_span, new_span)
        elif old_span and new_span:
            pairs = [(".".join(old_span), ".".join(new_span))]
        else:
            pairs = []
        for old_fragment, new_fragment in pairs:
            if len(old_fragment) < 3 or len(new_fragment) < 3:
                continue
            if old_fragment.isdigit() or new_fragment.isdigit():
                continue
            candidates.add((old_fragment, new_fragment))

    # Character-level fallback finds longer substitutions inside a token.
    matcher = difflib.SequenceMatcher(None, old_value, new_value, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        old_fragment = old_value[i1:i2]
        new_fragment = new_value[j1:j2]
        old_signal = re.sub(r"[^A-Za-z0-9]", "", old_fragment)
        new_signal = re.sub(r"[^A-Za-z0-9]", "", new_fragment)
        if len(old_signal) < 3 or len(new_signal) < 3:
            continue
        if old_signal.isdigit() or new_signal.isdigit():
            continue
        if len(old_fragment) > 100 or len(new_fragment) > 100:
            continue
        candidates.add((old_fragment, new_fragment))

    # Longest fragments are normally the most specific and useful. Limiting
    # each replacement prevents a single URL from flooding the suggestion list.
    return sorted(candidates, key=lambda item: (-(len(item[0]) + len(item[1])), item))[:3]


def _short_pattern_value(value: str, limit: int = 48) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _build_pattern_example(record: FileRecord, block: ChangeBlock) -> PatternExample:
    old_all = record.test_text.splitlines()
    new_all = record.dev_text.splitlines()
    return PatternExample(
        relative_path=record.relative_path,
        old_line=block.old_lines[0] if block.old_lines else "",
        new_line=block.new_lines[0] if block.new_lines else "",
        old_line_number=block.old_start + 1,
        new_line_number=block.new_start + 1,
        old_context_before=old_all[block.old_start - 1] if block.old_start > 0 else None,
        old_context_after=old_all[block.old_end] if block.old_end < len(old_all) else None,
        new_context_before=new_all[block.new_start - 1] if block.new_start > 0 else None,
        new_context_after=new_all[block.new_end] if block.new_end < len(new_all) else None,
    )


def _line_regex_for_any_fragment(fragment: str) -> str:
    return rf"(?i)^.*{re.escape(fragment)}.*$"


def _looks_like_environment_substitution(
    old_fragment: str,
    new_fragment: str,
    source_name: str,
    target_name: str,
) -> bool:
    """Return True for strong current-environment -> incoming-environment signals."""
    old_lower = old_fragment.lower()
    new_lower = new_fragment.lower()
    source_lower = source_name.lower().strip()
    target_lower = target_name.lower().strip()

    if source_lower and target_lower and source_lower != target_lower:
        old_matches = (
            old_lower == target_lower
            or old_lower.startswith(target_lower)
            or target_lower in old_lower
        )
        new_matches = (
            new_lower == source_lower
            or new_lower.startswith(source_lower)
            or source_lower in new_lower
        )
        if old_matches and new_matches:
            return True

    environment_words = {
        "dev",
        "development",
        "test",
        "stage",
        "staging",
        "prod",
        "production",
    }
    return old_lower in environment_words and new_lower in environment_words


def _candidate_qualifies(
    matches: Sequence[tuple[FileRecord, ChangeBlock]],
    minimum_matches: int,
) -> bool:
    file_count = len({record.relative_path for record, _ in matches})
    return len(matches) >= minimum_matches or file_count >= MIN_PATTERN_FILES


def _category_for_scalar_pattern(key: str, old_value: str, new_value: str) -> str:
    key_text = key.lower()
    if re.search(r"(?:namespace|cluster|region|environment|profile|site|location|^env$)", key_text):
        return CATEGORY_ENVIRONMENT
    if _url_parts(old_value) and _url_parts(new_value):
        return CATEGORY_ENDPOINTS
    if _host_value(old_value) and _host_value(new_value):
        return CATEGORY_ENDPOINTS
    if _ip_value(old_value) and _ip_value(new_value):
        return CATEGORY_ENDPOINTS
    if re.search(r"(?:url|uri|host|hostname|endpoint|address|\bip\b|port)", key_text):
        return CATEGORY_ENDPOINTS
    if re.search(
        r"(?:user|username|account|principal|client|service.?account|reference|\bref\b|configmap|secret)",
        key_text,
    ):
        return CATEGORY_USERS_REFERENCES
    if re.search(r"(?:bucket|database|^db$|schema|index|storage|^s3$|table|topic|queue)", key_text):
        return CATEGORY_STORAGE_DATA
    return CATEGORY_OTHER


def _inferred_project_pattern_rules(
    block_refs: Sequence[tuple[FileRecord, ChangeBlock]],
    source_name: str,
    target_name: str,
) -> list[PatternRule]:
    """Infer deterministic project-wide regex pairs from repeated replacements."""
    grouped: dict[tuple[Any, ...], list[tuple[FileRecord, ChangeBlock]]] = defaultdict(list)
    metadata: dict[tuple[Any, ...], tuple[str, str, str, str, str, int]] = {}

    for record, block in block_refs:
        if block.protected_reason is not None:
            continue
        if block.tag != "replace" or len(block.old_lines) != 1 or len(block.new_lines) != 1:
            continue
        old_parsed = _parse_scalar_line(block.old_lines[0])
        new_parsed = _parse_scalar_line(block.new_lines[0])
        if old_parsed is None or new_parsed is None or old_parsed[0] != new_parsed[0]:
            continue
        key = old_parsed[0]
        old_value = old_parsed[1]
        new_value = new_parsed[1]
        if old_value == new_value or not old_value or not new_value:
            continue
        if _SENSITIVE_KEY_RE.search(key) or len(old_value) > 500 or len(new_value) > 500:
            continue

        old_url = _url_parts(old_value)
        new_url = _url_parts(new_value)
        if old_url and new_url:
            signature = ("url-shape", key)
            grouped[signature].append((record, block))
            metadata[signature] = (
                f"Broad URL replacements under {key}",
                _line_regex_for_url_shape(key),
                _line_regex_for_url_shape(key),
                "url-shape",
                CATEGORY_ENDPOINTS,
                BROAD_PATTERN_MIN_MATCHES,
            )

            old_domain = _apps_domain(old_url[1])
            new_domain = _apps_domain(new_url[1])
            if old_domain and new_domain and old_domain != new_domain:
                signature = ("project-url-domain", old_domain, new_domain)
                grouped[signature].append((record, block))
                metadata[signature] = (
                    f"Project OpenShift domain: {old_domain} → {new_domain}",
                    _line_regex_for_any_fragment(old_domain),
                    _line_regex_for_any_fragment(new_domain),
                    "url-domain",
                    CATEGORY_APP_DOMAINS,
                    MIN_PATTERN_MATCHES,
                )

        old_host = _host_value(old_value)
        new_host = _host_value(new_value)
        if old_host and new_host:
            signature = ("host-shape", key)
            grouped[signature].append((record, block))
            metadata[signature] = (
                f"Broad hostname replacements under {key}",
                _line_regex_for_host_shape(key),
                _line_regex_for_host_shape(key),
                "host-shape",
                CATEGORY_ENDPOINTS,
                BROAD_PATTERN_MIN_MATCHES,
            )

            old_domain = _apps_domain(old_host)
            new_domain = _apps_domain(new_host)
            if old_domain and new_domain and old_domain != new_domain:
                signature = ("project-host-domain", old_domain, new_domain)
                grouped[signature].append((record, block))
                metadata[signature] = (
                    f"Project OpenShift host domain: {old_domain} → {new_domain}",
                    _line_regex_for_any_fragment(old_domain),
                    _line_regex_for_any_fragment(new_domain),
                    "host-domain",
                    CATEGORY_APP_DOMAINS,
                    MIN_PATTERN_MATCHES,
                )

        if _ip_value(old_value) and _ip_value(new_value):
            signature = ("ip-shape", key)
            grouped[signature].append((record, block))
            metadata[signature] = (
                f"Broad IP address replacements under {key}",
                _line_regex_for_ip_shape(key),
                _line_regex_for_ip_shape(key),
                "ip-shape",
                CATEGORY_ENDPOINTS,
                BROAD_PATTERN_MIN_MATCHES,
            )

        if key != "<list-item>":
            signature = ("exact-scalar", key, old_value, new_value)
            grouped[signature].append((record, block))
            metadata[signature] = (
                f"Repeated {key} replacement: {_short_pattern_value(old_value)} → "
                f"{_short_pattern_value(new_value)}",
                _line_regex_for_exact_scalar(key, old_value),
                _line_regex_for_exact_scalar(key, new_value),
                "exact-scalar",
                _category_for_scalar_pattern(key, old_value, new_value),
                MIN_PATTERN_MATCHES,
            )

        for old_fragment, new_fragment in _scalar_fragment_pairs(old_value, new_value):
            signature = ("fragment", key, old_fragment, new_fragment)
            grouped[signature].append((record, block))
            metadata[signature] = (
                f"Repeated fragment under {key}: {_short_pattern_value(old_fragment)} → "
                f"{_short_pattern_value(new_fragment)}",
                _line_regex_for_fragment(key, old_fragment),
                _line_regex_for_fragment(key, new_fragment),
                "fragment",
                _category_for_scalar_pattern(key, old_value, new_value),
                MIN_PATTERN_MATCHES,
            )

            # A repeated current-target -> incoming-source label inside URLs,
            # usernames, namespaces, buckets, or arbitrary scalar values is a
            # strong environment signal. This intentionally spans YAML keys.
            if (
                old_fragment in old_value
                and new_fragment in new_value
                and _looks_like_environment_substitution(
                    old_fragment, new_fragment, source_name, target_name
                )
            ):
                signature = ("environment-fragment", old_fragment.lower(), new_fragment.lower())
                grouped[signature].append((record, block))
                metadata[signature] = (
                    f"Environment label: {_short_pattern_value(old_fragment)} → "
                    f"{_short_pattern_value(new_fragment)}",
                    _line_regex_for_any_fragment(old_fragment),
                    _line_regex_for_any_fragment(new_fragment),
                    "environment-fragment",
                    CATEGORY_ENVIRONMENT,
                    MIN_PATTERN_MATCHES,
                )

    rule_specs: list[tuple[int, int, int, PatternRule]] = []
    kind_priority = {
        "environment-fragment": 0,
        "url-domain": 1,
        "host-domain": 1,
        "exact-scalar": 2,
        "fragment": 3,
        "url-shape": 4,
        "host-shape": 4,
        "ip-shape": 4,
    }
    for signature, matched_refs in grouped.items():
        name, test_regex, dev_regex, kind, category, minimum = metadata[signature]
        if not _candidate_qualifies(matched_refs, minimum):
            continue
        file_count = len({record.relative_path for record, _ in matched_refs})
        rule = PatternRule(
            id=_pattern_rule_id(kind, (), test_regex, dev_regex),
            name=name,
            test_regex=test_regex,
            dev_regex=dev_regex,
            files=(),
            category=category,
            enabled=True,
            kind=kind,
            source="suggested",
        )
        rule_specs.append((kind_priority.get(kind, 9), -file_count, -len(matched_refs), rule))

    rule_specs.sort(key=lambda item: (item[0], item[1], item[2], item[3].name.lower()))
    rules: list[PatternRule] = []
    fragment_count = 0
    for _, _, _, rule in rule_specs:
        if rule.kind == "fragment":
            if fragment_count >= 12:
                continue
            fragment_count += 1
        rules.append(rule)
    return rules


def _sample_pattern_examples(
    matches: Sequence[tuple[FileRecord, ChangeBlock]],
    limit: int = 10,
) -> list[PatternExample]:
    """Prefer examples from different files before filling remaining slots."""
    selected: list[tuple[FileRecord, ChangeBlock]] = []
    seen_files: set[str] = set()
    for item in matches:
        record, _ = item
        if record.relative_path in seen_files:
            continue
        selected.append(item)
        seen_files.add(record.relative_path)
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        selected_ids = {(record.relative_path, id(block)) for record, block in selected}
        for item in matches:
            record, block = item
            if (record.relative_path, id(block)) in selected_ids:
                continue
            selected.append(item)
            if len(selected) >= limit:
                break
    return [_build_pattern_example(record, block) for record, block in selected]


def discover_project_pattern_candidates(
    records: Sequence[FileRecord],
    saved_patterns: Sequence[PatternRule],
    source_name: str,
    target_name: str,
) -> list[PatternCandidate]:
    block_refs: list[tuple[FileRecord, ChangeBlock]] = []
    for record in records:
        if record.equal or record.binary or record.read_error:
            continue
        unfiltered = compute_filter_result(
            record.test_text, record.dev_text, [], record.relative_path
        )
        block_refs.extend((record, block) for block in unfiltered.blocks)

    by_id = {rule.id: rule for rule in saved_patterns}
    for rule in _inferred_project_pattern_rules(
        block_refs, source_name=source_name, target_name=target_name
    ):
        by_id.setdefault(rule.id, rule)

    saved_ids = {rule.id for rule in saved_patterns}
    entries: list[tuple[PatternCandidate, frozenset[tuple[Any, ...]]]] = []
    for rule in by_id.values():
        matching = [
            (record, block)
            for record, block in block_refs
            if _pattern_matches_block(
                rule,
                record.relative_path,
                block.tag,
                block.old_lines,
                block.new_lines,
            )
        ]
        if not matching and rule.id not in saved_ids:
            continue
        affected_files = tuple(sorted({record.relative_path for record, _ in matching}))
        match_keys = frozenset(
            (
                record.relative_path,
                block.tag,
                block.old_start,
                block.old_end,
                block.new_start,
                block.new_end,
            )
            for record, block in matching
        )
        entries.append(
            (
                PatternCandidate(
                    rule=rule,
                    examples=_sample_pattern_examples(matching),
                    match_count=len(matching),
                    file_count=len(affected_files),
                    affected_files=affected_files,
                    persisted=rule.id in saved_ids,
                ),
                match_keys,
            )
        )

    kind_priority = {
        "environment-fragment": 0,
        "url-domain": 1,
        "host-domain": 1,
        "exact-scalar": 2,
        "fragment": 3,
        "url-shape": 4,
        "host-shape": 4,
        "ip-shape": 4,
        "project": 5,
    }
    category_rank = {name: index for index, name in enumerate(CATEGORY_ORDER)}
    entries.sort(
        key=lambda item: (
            category_rank.get(item[0].rule.category, len(category_rank)),
            kind_priority.get(item[0].rule.kind, 6),
            -item[0].file_count,
            -item[0].match_count,
            item[0].rule.name.lower(),
        )
    )

    # A generated suggestion covering exactly the same blocks as any earlier
    # saved or generated rule is redundant. Saved rules themselves remain
    # visible so the project configuration can be audited.
    kept: list[tuple[PatternCandidate, frozenset[tuple[Any, ...]]]] = []
    seen_match_sets: set[frozenset[tuple[Any, ...]]] = set()
    for candidate, match_keys in entries:
        if not candidate.persisted and match_keys and match_keys in seen_match_sets:
            continue
        kept.append((candidate, match_keys))
        if match_keys:
            seen_match_sets.add(match_keys)

    coverage_count: dict[tuple[Any, ...], int] = defaultdict(int)
    for _, match_keys in kept:
        for key in match_keys:
            coverage_count[key] += 1
    for candidate, match_keys in kept:
        candidate.overlap_count = sum(1 for key in match_keys if coverage_count.get(key, 0) > 1)
    return [candidate for candidate, _ in kept]


def discover_always_reviewed_summaries(
    records: Sequence[FileRecord],
) -> list[ProtectedChangeSummary]:
    grouped: dict[str, list[tuple[FileRecord, ChangeBlock]]] = defaultdict(list)
    for record in records:
        if record.equal or record.binary or record.read_error:
            continue
        result = compute_filter_result(record.test_text, record.dev_text, [], record.relative_path)
        for block in result.blocks:
            if block.protected_reason:
                grouped[block.protected_reason].append((record, block))

    preferred_order = (
        "Version, image, chart, or revision updates",
        "Replica, resource, or security updates",
        "Added, removed, or structural changes",
    )
    output: list[ProtectedChangeSummary] = []
    for name in preferred_order:
        matches = grouped.get(name, [])
        if not matches:
            continue
        output.append(
            ProtectedChangeSummary(
                name=name,
                match_count=len(matches),
                file_count=len({record.relative_path for record, _ in matches}),
                examples=_sample_pattern_examples(matches, limit=8),
            )
        )
    for name in sorted(set(grouped) - set(preferred_order)):
        matches = grouped[name]
        output.append(
            ProtectedChangeSummary(
                name=name,
                match_count=len(matches),
                file_count=len({record.relative_path for record, _ in matches}),
                examples=_sample_pattern_examples(matches, limit=8),
            )
        )
    return output


def _line_digest(lines: Sequence[str]) -> str | None:
    if not lines:
        return None
    return hashlib.sha256("\n".join(lines).encode("utf-8", errors="surrogatepass")).hexdigest()


def change_tracking_tokens(block: ChangeBlock) -> set[str]:
    """Return stable session tokens for either side of a changed block."""
    tokens: set[str] = set()
    dev_digest = _line_digest(block.new_lines)
    test_digest = _line_digest(block.old_lines)
    if dev_digest is not None:
        tokens.add(f"DEV:{dev_digest}")
    if test_digest is not None:
        tokens.add(f"TEST:{test_digest}")
    return tokens


def _context_digest(lines: Sequence[str]) -> str | None:
    if not lines:
        return None
    return hashlib.sha256("\n".join(lines).encode("utf-8", errors="surrogatepass")).hexdigest()


def change_context_tokens(record: FileRecord, block: ChangeBlock, radius: int = 2) -> set[str]:
    """Hash nearby unchanged text without persisting the text itself."""
    test_lines = record.test_text.splitlines()
    dev_lines = record.dev_text.splitlines()
    regions = (
        ("TEST-BEFORE", test_lines[max(0, block.old_start - radius) : block.old_start]),
        ("TEST-AFTER", test_lines[block.old_end : block.old_end + radius]),
        ("DEV-BEFORE", dev_lines[max(0, block.new_start - radius) : block.new_start]),
        ("DEV-AFTER", dev_lines[block.new_end : block.new_end + radius]),
    )
    tokens: set[str] = set()
    for label, lines in regions:
        digest = _context_digest(lines)
        if digest is not None:
            tokens.add(f"{label}:{digest}")
    return tokens


def _hydrate_handled_entry(
    entry: HandledChange,
    record: FileRecord,
    block: ChangeBlock,
) -> None:
    """Attach current lines for in-memory previews after a safe rematch."""
    entry.old_start = block.old_start
    entry.old_end = block.old_end
    entry.new_start = block.new_start
    entry.new_end = block.new_end
    entry.old_lines = tuple(block.old_lines)
    entry.new_lines = tuple(block.new_lines)
    entry.context_tokens = tuple(sorted(change_context_tokens(record, block)))


def change_was_modified(record: FileRecord, block: ChangeBlock) -> bool:
    return bool(record.modified_change_tokens.intersection(change_tracking_tokens(block)))


def change_decision_token(block: ChangeBlock) -> str:
    """Return a stable identity for an exact text block at its current location."""
    digest = hashlib.sha256()
    digest.update(block.tag.encode())
    digest.update(
        f"\0{block.old_start}:{block.old_end}:{block.new_start}:{block.new_end}\0".encode()
    )
    digest.update("\n".join(block.old_lines).encode("utf-8", errors="surrogatepass"))
    digest.update(b"\0DEV\0")
    digest.update("\n".join(block.new_lines).encode("utf-8", errors="surrogatepass"))
    return digest.hexdigest()


def change_was_kept(record: FileRecord, block: ChangeBlock) -> bool:
    return change_decision_token(block) in record.kept_change_tokens


def record_handled_change(
    record: FileRecord,
    block: ChangeBlock,
    action: str,
) -> HandledChange:
    """Record one handled block in the current review session."""
    decision_token = change_decision_token(block)
    tracking_tokens = tuple(sorted(change_tracking_tokens(block)))
    context_tokens = tuple(sorted(change_context_tokens(record, block)))

    # Repeating an action for the same exact block replaces the old entry rather
    # than creating duplicate history rows.
    record.handled_changes = [
        item for item in record.handled_changes if item.decision_token != decision_token
    ]
    entry = HandledChange(
        action=action,
        decision_token=decision_token,
        tracking_tokens=tracking_tokens,
        old_start=block.old_start,
        old_end=block.old_end,
        new_start=block.new_start,
        new_end=block.new_end,
        old_lines=tuple(block.old_lines),
        new_lines=tuple(block.new_lines),
        context_tokens=context_tokens,
        order=record.next_handled_order,
    )
    record.next_handled_order += 1
    record.handled_changes.append(entry)
    if action == "KEPT TEST":
        record.kept_change_tokens.add(decision_token)
    else:
        record.modified_change_tokens.update(tracking_tokens)
    return entry


def _block_coordinate_key(block: ChangeBlock) -> tuple[str, int, int, int, int]:
    return (block.tag, block.old_start, block.old_end, block.new_start, block.new_end)


def match_handled_changes(
    record: FileRecord,
    blocks: Sequence[ChangeBlock],
) -> tuple[dict[tuple[str, int, int, int, int], HandledChange], list[HandledChange]]:
    """Match current blocks to prior decisions without guessing.

    Exact identities are preferred. Shifted blocks may be restored from complete
    side hashes, but repeated candidates must be uniquely disambiguated by saved
    surrounding-context hashes. If ambiguity remains, the decision reopens.
    """
    assignments: dict[tuple[str, int, int, int, int], HandledChange] = {}
    unused_entries = list(record.handled_changes)
    unused_blocks = list(blocks)

    # Exact original blocks are only valid handled matches for KEEP TEST.
    # For an applied/edited action, the original exact diff reappearing means
    # the result was undone and must return to the active queue.
    for block in list(unused_blocks):
        token = change_decision_token(block)
        exact_entries = [
            item
            for item in unused_entries
            if item.action == "KEPT TEST" and item.decision_token == token
        ]
        if len(exact_entries) != 1:
            continue
        entry = exact_entries[0]
        _hydrate_handled_entry(entry, record, block)
        assignments[_block_coordinate_key(block)] = entry
        unused_entries.remove(entry)
        unused_blocks.remove(block)

    # After an edit, one complete side may remain identical. Restore only when
    # the best candidate is unique. Saved context hashes break location ties
    # without storing any original configuration text.
    for entry in sorted(unused_entries, key=lambda item: item.order, reverse=True):
        entry_tokens = set(entry.tracking_tokens)
        if entry.action == "KEPT TEST":
            entry_tokens = {token for token in entry_tokens if token.startswith("TEST:")}
        candidates: list[tuple[int, int, ChangeBlock]] = []
        for block in unused_blocks:
            block_tokens = change_tracking_tokens(block)
            if not entry_tokens.intersection(block_tokens):
                continue
            if entry.action != "KEPT TEST" and entry_tokens and entry_tokens.issubset(block_tokens):
                # All original sides are back: this is the unhandled original
                # change, not proof that an earlier apply/edit is still valid.
                continue
            context_overlap = len(
                set(entry.context_tokens).intersection(change_context_tokens(record, block))
            )
            distance = abs(entry.old_start - block.old_start) + abs(
                entry.new_start - block.new_start
            )
            candidates.append((context_overlap, distance, block))
        if not candidates:
            continue

        best_context = max(item[0] for item in candidates)
        context_candidates = [item for item in candidates if item[0] == best_context]
        best_distance = min(item[1] for item in context_candidates)
        finalists = [item for item in context_candidates if item[1] == best_distance]
        if len(finalists) != 1:
            debug(
                "Saved review decision was ambiguous and reopened",
                file=record.relative_path,
                action=entry.action,
                candidate_count=len(finalists),
            )
            continue

        block = finalists[0][2]
        _hydrate_handled_entry(entry, record, block)
        assignments[_block_coordinate_key(block)] = entry
        unused_blocks.remove(block)

    matched_orders = {entry.order for entry in assignments.values()}
    unmatched = [
        entry
        for entry in sorted(record.handled_changes, key=lambda item: item.order)
        if entry.order not in matched_orders
    ]
    return assignments, unmatched


def reconciled_handled_entries(
    record: FileRecord,
    blocks: Sequence[ChangeBlock],
) -> list[HandledChange]:
    """Retain decisions only while their meaning is still defensible.

    KEEP TEST requires a safe current-block match. Applied/edited decisions are
    retained as history while their original exact diff stays absent; if that
    exact diff reappears (for example after git checkout/restore), they reopen.
    """
    assignments, _ = match_handled_changes(record, blocks)
    matched_orders = {entry.order for entry in assignments.values()}
    current_original_tokens = {change_decision_token(block) for block in blocks}
    retained: list[HandledChange] = []
    for entry in record.handled_changes:
        if entry.action == "KEPT TEST":
            if entry.order in matched_orders:
                retained.append(entry)
            continue
        if entry.decision_token not in current_original_tokens:
            retained.append(entry)
    return retained


def exact_change_still_present(
    record: FileRecord,
    original: ChangeBlock,
    *,
    hide_mapping_order: bool = False,
) -> bool:
    current = compute_filter_result(
        record.test_text,
        record.dev_text,
        [],
        record.relative_path,
        hide_mapping_order=hide_mapping_order,
    )
    token = change_decision_token(original)
    return any(change_decision_token(block) == token for block in current.blocks)


def handled_marker_text(entry: HandledChange, block: ChangeBlock | None = None) -> str:
    if block is None:
        test_range = _range_text(entry.old_start, entry.old_end)
        dev_range = _range_text(entry.new_start, entry.new_end)
    else:
        test_range = _range_text(block.old_start, block.old_end)
        dev_range = _range_text(block.new_start, block.new_end)
    return f"✓ {entry.action} · TEST {test_range} · DEV {dev_range} · {entry.preview}"


def _range_text(start: int, end: int) -> str:
    count = end - start
    if count <= 0:
        return str(start + 1)
    if count == 1:
        return str(start + 1)
    return f"{start + 1}-{end}"


def _preview_text(lines: Sequence[str], limit: int = 72) -> str:
    text = next((line.strip() for line in lines if line.strip()), "<blank>")
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text
