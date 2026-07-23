"""Local browser companion for reviewing source-to-target differences.

The viewer serves a snapshot generated from the workbench's existing Focused Diff
and Full Diff presentations. It binds only to loopback and uses a random URL
token. Reviewers may switch the source and target directories from the browser;
the selected paths are saved to the project configuration only when explicitly
requested. Git context is loaded only after a reviewer adds it to a change,
while opt-in reviewer notes remain in the browser until the reviewer explicitly
exports a plaintext review file.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import threading
import webbrowser
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from .context_help import (
    ContextCatalog,
    context_edit_path,
    context_line_suggestion,
    context_path_part_payload,
    load_context_catalog,
    yaml_paths_by_line,
    upsert_context_entry,
)
from .core import (
    ChangeBlock,
    DEFAULT_EXCLUDED_DIRS,
    DiffPresentation,
    DisplayLine,
    FileRecord,
    GitCommitContext,
    WorkbenchError,
    _is_yaml_order_continuation,
    discover_yaml_files,
    find_git_root,
    git_repository_commit_url,
    git_repository_merge_request_url,
    save_project_paths,
)
from .rendering import full_unified_diff, review_unified_diff

if TYPE_CHECKING:
    from .workbench import Workbench


@dataclass(slots=True, frozen=True)
class WebViewerLaunch:
    """Details for one started local viewer."""

    url: str
    file_count: int
    browser_opened: bool


GitLookup = dict[str, tuple[FileRecord, ChangeBlock]]


@dataclass(slots=True, frozen=True)
class _ContextGapSnapshot:
    """One immutable aligned unchanged range available for inline expansion."""

    test_start: int
    dev_start: int
    lines: tuple[str, ...]
    private_lines: tuple[str, ...] = ()
    context_refs: tuple[tuple[str, ...], ...] = ()
    context_suggestions: tuple[dict[str, Any] | None, ...] = ()
    context_targets: tuple[tuple[dict[str, Any], ...], ...] = ()


ContextLookup = dict[str, _ContextGapSnapshot]


def _directory_display_label(path: Path, other: Path) -> str:
    """Return a compact label that distinguishes one comparison directory."""
    name = path.name or str(path)
    other_name = other.name or str(other)
    if name.casefold() != other_name.casefold():
        return name

    parent_name = path.parent.name
    other_parent_name = other.parent.name
    if parent_name and parent_name.casefold() != other_parent_name.casefold():
        return f"{parent_name}/{name}"

    try:
        common = Path(os.path.commonpath([str(path), str(other)]))
        relative = path.relative_to(common).as_posix()
    except (ValueError, OSError):
        relative = str(path)
    return relative or name


def _comparison_identity(source: Path, target: Path) -> dict[str, str]:
    """Return user-facing labels for two arbitrary comparison roots."""
    source_label = _directory_display_label(source, target)
    target_label = _directory_display_label(target, source)
    source_git_root = find_git_root(source)
    target_git_root = find_git_root(target)
    return {
        "sourceLabel": source_label,
        "targetLabel": target_label,
        "sourceColumnLabel": source_label.upper(),
        "targetColumnLabel": target_label.upper(),
        "sourceRepository": (
            source_git_root.name if source_git_root is not None else source.parent.name
        ),
        "targetRepository": (
            target_git_root.name if target_git_root is not None else target.parent.name
        ),
    }


_WEB_GENERATED_LABEL_KINDS = {
    "hunk",
    "title",
    "section",
    "selector",
    "selector_selected",
    "selector_continuation",
    "test_file_header",
    "dev_file_header",
    "file_header",
    "handled",
}


def _replace_web_side_labels(
    text: str,
    *,
    source_column_label: str,
    target_column_label: str,
) -> str:
    """Replace legacy DEV/TEST presentation tokens without touching YAML values."""
    text = re.sub(r"\bTEST\b", target_column_label, text)
    return re.sub(r"\bDEV\b", source_column_label, text)


def _web_status_text(
    status: str,
    *,
    source_column_label: str,
    target_column_label: str,
) -> str:
    return _replace_web_side_labels(
        status,
        source_column_label=source_column_label,
        target_column_label=target_column_label,
    )


_PRIVACY_YAML_ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>\s*(?:-\s*)?[\"']?(?P<key>[A-Za-z0-9_.-]+)[\"']?\s*:\s*)(?P<value>.*)$"
)
_PRIVACY_ENV_ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*)(?P<value>.*)$"
)
_PRIVACY_ENV_NAME_RE = re.compile(
    r"^(?P<indent>\s*)-\s*name\s*:\s*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)
_PRIVACY_REF_SECTION_RE = re.compile(
    r"^(?P<indent>\s*)(?:secretKeyRef|configMapKeyRef|secretRef|configMapRef)\s*:\s*$",
    re.IGNORECASE,
)
_PRIVACY_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
)
_PRIVACY_URL_RE = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*:){1,2}//[^\s\"'<>]+")
_PRIVACY_IPV4_RE = re.compile(
    r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?::\d+)?(?![\d.])"
)
_PRIVACY_HOST_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,}|local)(?::\d+)?(?![A-Za-z0-9.-])"
)
_PRIVACY_UUID_RE = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![0-9a-f])"
)
_PRIVACY_LONG_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9+/=_-])(?=[A-Za-z0-9+/=_-]{28,}(?![A-Za-z0-9+/=_-]))"
    r"(?=[A-Za-z0-9+/=_-]*[A-Za-z])(?=[A-Za-z0-9+/=_-]*\d)"
    r"[A-Za-z0-9+/=_-]{28,}(?![A-Za-z0-9+/=_-])"
)
_PRIVACY_AUTH_RE = re.compile(r"(?i)\b(?P<scheme>Bearer|Basic)\s+(?P<value>[A-Za-z0-9._~+/=-]+)")
_PRIVACY_UNIX_USER_PATH_RE = re.compile(r"(?P<prefix>/(?:home|Users)/)(?P<user>[^/\s]+)")
_PRIVACY_WINDOWS_USER_PATH_RE = re.compile(r"(?i)(?P<prefix>\b[A-Z]:\\Users\\)(?P<user>[^\\\s]+)")

_PRIVACY_SECRET_KEY_RE = re.compile(
    r"(?:^|[_.-])(?:password|passwd|passphrase|token|secret|api.?key|private.?key|access.?key|credential|auth(?:entication|orization)?|certificate|cert|keystore|truststore)(?:$|[_.-])",
    re.IGNORECASE,
)
_PRIVACY_PERSON_KEY_RE = re.compile(
    r"(?:^|[_.-])(?:user(?:name)?|owner|author|contact|assignee|requeste[dr]|reviewer|manager|employee|person|principal|first.?name|last.?name|full.?name|display.?name|email|mail|service.?account)(?:$|[_.-])",
    re.IGNORECASE,
)
_PRIVACY_ENDPOINT_KEY_RE = re.compile(
    r"(?:^|[_.-])(?:url|uri|host(?:name)?|domain|endpoint|address|ip|server|proxy|route|ingress)(?:$|[_.-])",
    re.IGNORECASE,
)
_PRIVACY_RESOURCE_KEY_RE = re.compile(
    r"(?:^|[_.-])(?:bucket|database|db|schema|table|topic|queue|index|repository|repo|registry|path|mount|volume|claim)(?:$|[_.-])",
    re.IGNORECASE,
)
_PRIVACY_IDENTITY_KEY_RE = re.compile(
    r"(?:^|[_.-])(?:namespace|cluster|region|environment|profile|site|location|tenant|subscription|account|project|client.?id|application.?id)(?:$|[_.-])",
    re.IGNORECASE,
)


class _PrivacyRedactor:
    """Build stable session-only aliases for sensitive browser text.

    Privacy mode is intentionally a sharing aid rather than a secret scanner or
    security boundary. The unredacted snapshot remains available in the local
    page so the reviewer can toggle the original view back on. Exports generated
    while privacy mode is enabled use only these redacted values.
    """

    def __init__(self) -> None:
        self._aliases: dict[tuple[str, str], str] = {}
        self._counts: defaultdict[str, int] = defaultdict(int)

    def alias(self, kind: str, value: str) -> str:
        normalized = value.strip()
        key = (kind, normalized)
        if key not in self._aliases:
            self._counts[kind] += 1
            self._aliases[key] = f"[{kind}-{self._counts[kind]}]"
        return self._aliases[key]

    @staticmethod
    def _scalar_value(value: str) -> str:
        value = value.strip().rstrip(",")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            return value[1:-1]
        return value

    @staticmethod
    def _key_kind(key: str) -> str | None:
        normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
        if _PRIVACY_SECRET_KEY_RE.search(normalized):
            return "SECRET"
        if _PRIVACY_PERSON_KEY_RE.search(normalized):
            return "PERSON"
        if _PRIVACY_ENDPOINT_KEY_RE.search(normalized):
            return "ENDPOINT"
        if _PRIVACY_RESOURCE_KEY_RE.search(normalized):
            return "RESOURCE"
        if _PRIVACY_IDENTITY_KEY_RE.search(normalized):
            return "IDENTIFIER"
        return None

    def _replace_assignment_value(self, prefix: str, value: str, kind: str) -> str:
        leading = value[: len(value) - len(value.lstrip())]
        stripped = value.strip()
        if not stripped:
            return prefix + value

        comment = ""
        comment_match = re.search(r"\s+#.*$", stripped)
        if comment_match:
            comment = stripped[comment_match.start() :]
            stripped = stripped[: comment_match.start()].rstrip()

        comma = "," if stripped.endswith(",") else ""
        if comma:
            stripped = stripped[:-1].rstrip()
        quote = (
            stripped[0]
            if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}
            else ""
        )
        raw_value = stripped[1:-1] if quote else stripped
        if raw_value in {"", "{}", "[]"}:
            return prefix + value
        alias = self.alias(kind, raw_value)
        replacement = f"{quote}{alias}{quote}{comma}{comment}"
        return prefix + leading + replacement

    def _redact_patterns(self, text: str) -> str:
        text = _PRIVACY_AUTH_RE.sub(
            lambda match: f"{match.group('scheme')} {self.alias('TOKEN', match.group('value'))}",
            text,
        )
        text = _PRIVACY_EMAIL_RE.sub(lambda match: self.alias("EMAIL", match.group(0)), text)
        text = _PRIVACY_URL_RE.sub(lambda match: self.alias("URL", match.group(0)), text)
        text = _PRIVACY_IPV4_RE.sub(lambda match: self.alias("IP", match.group(0)), text)
        text = _PRIVACY_HOST_RE.sub(lambda match: self.alias("HOST", match.group(0)), text)
        text = _PRIVACY_UUID_RE.sub(lambda match: self.alias("ID", match.group(0)), text)
        text = _PRIVACY_LONG_TOKEN_RE.sub(lambda match: self.alias("TOKEN", match.group(0)), text)
        text = _PRIVACY_UNIX_USER_PATH_RE.sub(
            lambda match: match.group("prefix") + self.alias("USER", match.group("user")),
            text,
        )
        return _PRIVACY_WINDOWS_USER_PATH_RE.sub(
            lambda match: match.group("prefix") + self.alias("USER", match.group("user")),
            text,
        )

    def redact_text(self, text: str, *, forced_kind: str | None = None) -> str:
        assignment = _PRIVACY_YAML_ASSIGNMENT_RE.match(text)
        if assignment is None:
            assignment = _PRIVACY_ENV_ASSIGNMENT_RE.match(text)
        if assignment is not None:
            kind = forced_kind or self._key_kind(assignment.group("key"))
            if kind is not None:
                return self._replace_assignment_value(
                    assignment.group("prefix"),
                    assignment.group("value"),
                    kind,
                )
        if forced_kind is not None and text.strip():
            return self.alias(forced_kind, text)
        return self._redact_patterns(text)

    def redact_path(self, path: str) -> str:
        """Redact personal identifiers without treating file extensions as hosts."""
        text = _PRIVACY_EMAIL_RE.sub(lambda match: self.alias("EMAIL", match.group(0)), path)
        text = _PRIVACY_UUID_RE.sub(lambda match: self.alias("ID", match.group(0)), text)
        text = _PRIVACY_LONG_TOKEN_RE.sub(lambda match: self.alias("TOKEN", match.group(0)), text)
        text = _PRIVACY_UNIX_USER_PATH_RE.sub(
            lambda match: match.group("prefix") + self.alias("USER", match.group("user")),
            text,
        )
        return _PRIVACY_WINDOWS_USER_PATH_RE.sub(
            lambda match: match.group("prefix") + self.alias("USER", match.group("user")),
            text,
        )

    def redact_lines(self, lines: Sequence[str]) -> list[str]:
        redacted: list[str] = []
        pending_env: tuple[int, str] | None = None
        reference_indent: int | None = None

        for line in lines:
            indent = len(line) - len(line.lstrip())
            env_name = _PRIVACY_ENV_NAME_RE.match(line)
            if env_name:
                variable = self._scalar_value(env_name.group("value"))
                kind = self._key_kind(variable)
                pending_env = (len(env_name.group("indent")), kind) if kind else None

            reference = _PRIVACY_REF_SECTION_RE.match(line)
            if reference:
                reference_indent = len(reference.group("indent"))
            elif reference_indent is not None and line.strip() and indent <= reference_indent:
                reference_indent = None

            assignment = _PRIVACY_YAML_ASSIGNMENT_RE.match(line)
            forced_kind: str | None = None
            if assignment is not None:
                key = assignment.group("key")
                if (
                    pending_env is not None
                    and indent > pending_env[0]
                    and key.lower() in {"value", "valuefrom"}
                ):
                    forced_kind = pending_env[1]
                elif (
                    reference_indent is not None
                    and indent > reference_indent
                    and key.lower() in {"name", "key", "namespace"}
                ):
                    forced_kind = "REFERENCE"

            redacted.append(self.redact_text(line, forced_kind=forced_kind))

            if (
                pending_env is not None
                and line.strip()
                and indent <= pending_env[0]
                and not env_name
            ):
                pending_env = None

        return redacted


def _running_under_wsl() -> bool:
    """Return True when the process is running inside Windows Subsystem for Linux."""
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    return "microsoft" in platform.release().lower()


def _open_browser_once(url: str) -> bool:
    """Open one browser window without noisy duplicate WSL launcher attempts."""
    if _running_under_wsl():
        command: list[str] | None = None
        windows_cmd = shutil.which("cmd.exe")
        if windows_cmd:
            command = [windows_cmd, "/d", "/c", "start", "", url]
        else:
            wslview = shutil.which("wslview")
            if wslview:
                command = [wslview, url]

        if command is None:
            return False
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return False
        return True

    try:
        return bool(webbrowser.open(url, new=2))
    except (OSError, webbrowser.Error):
        return False


def _display_lines_payload(
    lines: Sequence[DisplayLine],
    redactor: _PrivacyRedactor,
    context_catalog: ContextCatalog,
    record: FileRecord,
) -> list[dict[str, Any]]:
    private_lines = redactor.redact_lines([line.text for line in lines])
    test_paths = yaml_paths_by_line(record.test_text)
    dev_paths = yaml_paths_by_line(record.dev_text)
    for index, line in enumerate(lines):
        if line.kind in {"test_file_header", "dev_file_header", "file_header"}:
            private_lines[index] = redactor.redact_path(line.text)
        elif line.kind == "filtered_header":
            marker, separator, detail = line.text.partition("·")
            category = detail.split(":", 1)[0].strip() if separator else ""
            private_lines[index] = (
                f"{marker.rstrip()} · {category}: [REDACTED]"
                if category
                else "▼ FILTERED DIFF · [REDACTED]"
            )

    result: list[dict[str, Any]] = []
    for line, private_text in zip(lines, private_lines, strict=True):
        yaml_path = None
        if line.dev_line is not None:
            yaml_path = dev_paths.get(line.dev_line)
        if yaml_path is None and line.test_line is not None:
            yaml_path = test_paths.get(line.test_line)
        targets = context_catalog.line_targets(
            record.relative_path,
            line.text,
            yaml_path=yaml_path,
        )
        refs: list[str] = []
        suggestion = None
        for target in targets:
            for entry_id in target.get("contextRefs", []):
                if entry_id not in refs:
                    refs.append(entry_id)
            if suggestion is None and not target.get("contextRefs"):
                suggestion = target.get("contextSuggestion")
        result.append(
            {
                "text": line.text,
                "privateText": private_text,
                "kind": line.kind,
                "testLine": line.test_line,
                "devLine": line.dev_line,
                "emphasisRanges": [list(item) for item in line.emphasis_ranges],
                "yamlPath": yaml_path,
                "contextTargets": targets,
                "contextRefs": refs,
                "contextSuggestion": suggestion
                or context_line_suggestion(
                    record.relative_path,
                    line.text,
                    yaml_path=yaml_path,
                ),
            }
        )
    return result


def _presentation_preserves_physical_order(presentation: DiffPresentation) -> bool:
    """Return whether each file's rendered line numbers only move forward.

    A moved keyed-list item can be represented as one logical replacement whose
    TEST and DEV ranges occur at very different physical locations. That logical
    view is useful in the terminal, but it cannot be placed on a single GitLab-
    style file timeline without making one side's line numbers move backward.
    The web viewer therefore falls back to the literal opcode placement for that
    file while retaining every other Focused Diff filter.
    """
    previous_test = 0
    previous_dev = 0
    for line in presentation.lines:
        if line.test_line is not None:
            if line.test_line <= previous_test:
                return False
            previous_test = line.test_line
        if line.dev_line is not None:
            if line.dev_line <= previous_dev:
                return False
            previous_dev = line.dev_line
    return True


def _change_key(record: FileRecord, block: ChangeBlock) -> str:
    """Return a stable, content-derived key for browser notes and Git lookups."""
    digest = hashlib.sha256()
    values: tuple[object, ...] = (
        record.relative_path,
        block.tag,
        block.old_start,
        block.old_end,
        block.new_start,
        block.new_end,
        *block.old_lines,
        "\0DEV\0",
        *block.new_lines,
    )
    for value in values:
        digest.update(str(value).encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
    return digest.hexdigest()[:24]


def _line_range_text(start: int, end: int) -> str:
    if end <= start:
        return "—"
    first = start + 1
    last = end
    return str(first) if first == last else f"{first}-{last}"


def _change_payload(
    workbench: Workbench,
    record: FileRecord,
    block: ChangeBlock,
    git_lookup: GitLookup,
    redactor: _PrivacyRedactor,
    context_catalog: ContextCatalog,
    *,
    marker_index: int | None = None,
    panel_after: int | None = None,
    hidden: bool = False,
) -> dict[str, Any]:
    key = _change_key(record, block)
    git_lookup.setdefault(key, (record, block))
    label = workbench._change_context_label(record, block)
    private_label = redactor.redact_text(label)
    if private_label == label and ":" in label:
        private_label = f"{label.split(':', 1)[0]}: [REDACTED]"
    return {
        "key": key,
        "gitContextId": key,
        "label": label,
        "privateLabel": private_label,
        "markerIndex": marker_index,
        "panelAfter": panel_after,
        "testRange": _line_range_text(block.old_start, block.old_end),
        "devRange": _line_range_text(block.new_start, block.new_end),
        "testStart": block.old_start,
        "testEnd": block.old_end,
        "devStart": block.new_start,
        "devEnd": block.new_end,
        "oldLines": list(block.old_lines),
        "newLines": list(block.new_lines),
        "privateOldLines": redactor.redact_lines(block.old_lines),
        "privateNewLines": redactor.redact_lines(block.new_lines),
        "contextRefs": context_catalog.match_lines(
            record.relative_path,
            [*block.old_lines, *block.new_lines],
        ),
        "hidden": hidden,
        "testRemoteUrl": (
            workbench.git_file_url(
                record.test_path,
                line_start=block.old_start + 1,
                line_end=block.old_end,
            )
            if block.old_count
            else None
        ),
        "devRemoteUrl": (
            workbench.git_file_url(
                record.dev_path,
                line_start=block.new_start + 1,
                line_end=block.new_end,
            )
            if block.new_count
            else None
        ),
    }


def _gap_key(
    record: FileRecord,
    test_start: int,
    dev_start: int,
    lines: tuple[str, ...],
) -> str:
    digest = hashlib.sha256()
    digest.update(record.relative_path.encode("utf-8", errors="surrogatepass"))
    digest.update(b"\0GAP\0")
    digest.update(str(test_start).encode())
    digest.update(b"\0")
    digest.update(str(dev_start).encode())
    for line in lines:
        digest.update(b"\0")
        digest.update(line.encode("utf-8", errors="surrogatepass"))
    return digest.hexdigest()[:24]


def _presentation_payload(
    workbench: Workbench,
    record: FileRecord,
    presentation: DiffPresentation,
    git_lookup: GitLookup,
    context_lookup: ContextLookup,
    redactor: _PrivacyRedactor,
    context_catalog: ContextCatalog,
    *,
    physical_order_fallback: bool = False,
) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    active_keys: set[str] = set()
    for index, block in enumerate(presentation.change_blocks):
        marker_index = (
            presentation.change_line_indexes[index]
            if index < len(presentation.change_line_indexes)
            else 0
        )
        payload = _change_payload(
            workbench,
            record,
            block,
            git_lookup,
            redactor,
            context_catalog,
            marker_index=marker_index,
            panel_after=min(
                len(presentation.lines),
                marker_index + 1 + block.old_count + block.new_count,
            ),
        )
        active_keys.add(payload["key"])
        changes.append(payload)

    hidden_changes: list[dict[str, Any]] = []
    for block in presentation.filter_result.blocks:
        if not block.is_hidden or _is_yaml_order_continuation(block.hidden_by):
            continue
        payload = _change_payload(
            workbench,
            record,
            block,
            git_lookup,
            redactor,
            context_catalog,
            hidden=True,
        )
        if payload["key"] in active_keys:
            continue
        hidden_changes.append(payload)

    lines = _display_lines_payload(
        presentation.lines,
        redactor,
        context_catalog,
        record,
    )
    context_gaps = _timeline_context_gaps(
        record,
        lines,
        context_lookup,
        redactor,
        context_catalog,
    )

    return {
        "lines": lines,
        "changes": changes,
        "hiddenChanges": hidden_changes,
        "contextGaps": context_gaps,
        "visibleChanges": presentation.visible_change_count,
        "handled": presentation.handled_count,
        "noiseHidden": presentation.pattern_hidden_count,
        "whitespaceHidden": presentation.whitespace_hidden_count,
        "orderHidden": presentation.mapping_order_hidden_count,
        "orderUnavailable": presentation.mapping_order_unavailable_reason,
        "physicalOrderFallback": physical_order_fallback,
    }


def _semantic_line_owners(
    presentation: DiffPresentation,
) -> tuple[
    dict[int, tuple[int, str]],
    dict[int, tuple[int, str]],
    dict[tuple[str, int, str], list[list[int]]],
]:
    """Map physical TEST/DEV lines back to semantic active changes."""
    test_owners: dict[int, tuple[int, str]] = {}
    dev_owners: dict[int, tuple[int, str]] = {}
    emphasis: dict[tuple[str, int, str], list[list[int]]] = {}
    for index, block in enumerate(presentation.change_blocks):
        for offset, text in enumerate(block.old_lines):
            test_owners[block.old_start + offset + 1] = (index, text)
        for offset, text in enumerate(block.new_lines):
            dev_owners[block.new_start + offset + 1] = (index, text)

    for line in presentation.lines:
        if line.kind == "remove" and line.test_line is not None:
            emphasis[("TEST", line.test_line, line.text)] = [
                list(item) for item in line.emphasis_ranges
            ]
        elif line.kind == "add" and line.dev_line is not None:
            emphasis[("DEV", line.dev_line, line.text)] = [
                list(item) for item in line.emphasis_ranges
            ]
    return test_owners, dev_owners, emphasis


def _physical_noise_block(lines: Sequence[dict[str, Any]]) -> ChangeBlock:
    """Create one browser-only YAML-order block from literal physical rows."""
    test_numbers = [int(line["testLine"]) for line in lines if line.get("testLine")]
    dev_numbers = [int(line["devLine"]) for line in lines if line.get("devLine")]
    old_start = min(test_numbers) - 1 if test_numbers else 0
    old_end = max(test_numbers) if test_numbers else old_start
    new_start = min(dev_numbers) - 1 if dev_numbers else 0
    new_end = max(dev_numbers) if dev_numbers else new_start
    return ChangeBlock(
        tag="replace",
        old_start=old_start,
        old_end=old_end,
        new_start=new_start,
        new_end=new_end,
        old_lines=[str(line["text"]) for line in lines if line.get("testLine") is not None],
        new_lines=[str(line["text"]) for line in lines if line.get("devLine") is not None],
        hidden_by=("YAML keyed-list order",),
    )


def _timeline_context_gaps(
    record: FileRecord,
    lines: Sequence[dict[str, Any]],
    context_lookup: ContextLookup,
    redactor: _PrivacyRedactor,
    context_catalog: ContextCatalog,
) -> list[dict[str, Any]]:
    """Find omitted aligned unchanged ranges in a physical web timeline."""
    test_lines = record.test_text.splitlines()
    dev_lines = record.dev_text.splitlines()
    test_paths = yaml_paths_by_line(record.test_text)
    consumed_test = 0
    consumed_dev = 0
    gaps: list[dict[str, Any]] = []

    def add_gap(insert_at: int, test_end: int, dev_end: int) -> None:
        nonlocal consumed_test, consumed_dev
        test_count = test_end - consumed_test
        dev_count = dev_end - consumed_dev
        if test_count <= 0 or test_count != dev_count:
            return
        old_gap = test_lines[consumed_test:test_end]
        new_gap = dev_lines[consumed_dev:dev_end]
        if old_gap != new_gap:
            return
        values = tuple(old_gap)
        gap_id = _gap_key(record, consumed_test, consumed_dev, values)
        context_lookup.setdefault(
            gap_id,
            _ContextGapSnapshot(
                test_start=consumed_test,
                dev_start=consumed_dev,
                lines=values,
                private_lines=tuple(redactor.redact_lines(values)),
                context_refs=tuple(
                    tuple(context_catalog.match_line(record.relative_path, value))
                    for value in values
                ),
                context_suggestions=tuple(
                    context_line_suggestion(
                        record.relative_path,
                        value,
                        yaml_path=test_paths.get(consumed_test + index + 1),
                    )
                    for index, value in enumerate(values)
                ),
                context_targets=tuple(
                    tuple(
                        context_catalog.line_targets(
                            record.relative_path,
                            value,
                            yaml_path=test_paths.get(consumed_test + index + 1),
                        )
                    )
                    for index, value in enumerate(values)
                ),
            ),
        )
        leading = consumed_test == 0 and consumed_dev == 0
        gaps.append(
            {
                "id": gap_id,
                "length": len(values),
                # Leading gaps reveal the lines closest to the first rendered
                # row. Internal and trailing gaps reveal forward from the last
                # rendered row, matching the control's visual placement.
                "edge": "end" if leading else "start",
                "position": "before" if leading else "after",
                "insertAt": insert_at,
            }
        )

    for index, line in enumerate(lines):
        test_line = line.get("testLine")
        dev_line = line.get("devLine")
        if test_line is None and dev_line is None:
            continue
        next_test = int(test_line) - 1 if test_line is not None else consumed_test
        next_dev = int(dev_line) - 1 if dev_line is not None else consumed_dev
        add_gap(index, next_test, next_dev)
        if test_line is not None:
            consumed_test = int(test_line)
        if dev_line is not None:
            consumed_dev = int(dev_line)

    add_gap(len(lines), len(test_lines), len(dev_lines))
    return gaps


def _physical_semantic_presentation_payload(
    workbench: Workbench,
    record: FileRecord,
    physical: DiffPresentation,
    semantic: DiffPresentation,
    git_lookup: GitLookup,
    context_lookup: ContextLookup,
    redactor: _PrivacyRedactor,
    context_catalog: ContextCatalog,
) -> dict[str, Any]:
    """Render semantic active changes on a monotonic physical file timeline.

    Keyed-list reconciliation may pair TEST and DEV entries that live at crossed
    physical positions. The terminal can render that as one compact logical
    replacement, but a GitLab-style web timeline cannot. This adapter keeps the
    physical rows in file order, hides literal move-only rows, and overlays one
    semantic review change across all of its physical segments.
    """
    base = _presentation_payload(
        workbench,
        record,
        physical,
        git_lookup,
        context_lookup,
        redactor,
        context_catalog,
        physical_order_fallback=True,
    )

    test_owners, dev_owners, emphasis = _semantic_line_owners(semantic)
    output: list[dict[str, Any]] = []
    semantic_positions: dict[int, dict[str, Any]] = {
        index: {"marker": None, "last": None, "segmentCount": 0, "continuations": []}
        for index in range(len(semantic.change_blocks))
    }
    existing_hidden = iter(base["hiddenChanges"])
    hidden_changes: list[dict[str, Any]] = []
    noise_group: list[dict[str, Any]] = []
    previous_owner: int | None = None
    previous_was_active = False

    def append_payload(line: dict[str, Any]) -> None:
        output.append(dict(line))

    def flush_noise() -> None:
        nonlocal noise_group, previous_was_active
        if not noise_group:
            return
        header_index = len(output)
        count = len(noise_group)
        header = {
            "text": f"▼ FILTERED DIFF · YAML keyed-list order: {count} physical line(s)",
            "privateText": (f"▼ FILTERED DIFF · YAML keyed-list order: {count} physical line(s)"),
            "kind": "filtered_header",
            "testLine": None,
            "devLine": None,
            "emphasisRanges": [],
        }
        append_payload(header)
        filtered_rows: list[dict[str, Any]] = []
        for line in noise_group:
            filtered = dict(line)
            filtered["kind"] = "filtered_remove" if line["kind"] == "remove" else "filtered_add"
            filtered_rows.append(filtered)
            append_payload(filtered)
        block = _physical_noise_block(filtered_rows)
        payload = _change_payload(
            workbench,
            record,
            block,
            git_lookup,
            redactor,
            context_catalog,
            marker_index=header_index,
            panel_after=len(output),
            hidden=True,
        )
        payload["physicalOrderOnly"] = True
        hidden_changes.append(payload)
        noise_group = []
        previous_was_active = False

    for line in base["lines"]:
        kind = str(line["kind"])
        if kind in {"selector", "selector_selected"}:
            flush_noise()
            previous_was_active = False
            continue

        owner: int | None = None
        side: str | None = None
        line_number: int | None = None
        if kind == "remove" and line.get("testLine") is not None:
            line_number = int(line["testLine"])
            candidate = test_owners.get(line_number)
            if candidate is not None and candidate[1] == line["text"]:
                owner = candidate[0]
                side = "TEST"
        elif kind == "add" and line.get("devLine") is not None:
            line_number = int(line["devLine"])
            candidate = dev_owners.get(line_number)
            if candidate is not None and candidate[1] == line["text"]:
                owner = candidate[0]
                side = "DEV"

        if kind in {"remove", "add"} and owner is None:
            noise_group.append(dict(line))
            previous_was_active = False
            continue

        flush_noise()

        if owner is None:
            append_payload(line)
            if kind == "filtered_header":
                try:
                    hidden_changes.append(next(existing_hidden))
                except StopIteration:
                    pass
            previous_was_active = False
            previous_owner = None
            continue

        position = semantic_positions[owner]
        block = semantic.change_blocks[owner]
        if position["marker"] is None:
            marker_index = len(output)
            selector = {
                "text": "",
                "privateText": "",
                "kind": "selector_selected" if owner == 0 else "selector",
                "testLine": None,
                "devLine": None,
                "emphasisRanges": [],
            }
            append_payload(selector)
            position["marker"] = marker_index
            position["segmentCount"] = 1
        elif not previous_was_active or previous_owner != owner:
            continuation_index = len(output)
            continuation = {
                "text": "",
                "privateText": "",
                "kind": "selector_continuation",
                "testLine": None,
                "devLine": None,
                "emphasisRanges": [],
            }
            append_payload(continuation)
            position["continuations"].append(continuation_index)
            position["segmentCount"] = int(position["segmentCount"]) + 1

        active_line = dict(line)
        if side is not None and line_number is not None:
            active_line["emphasisRanges"] = emphasis.get((side, line_number, str(line["text"])), [])
        append_payload(active_line)
        position["last"] = len(output)
        previous_owner = owner
        previous_was_active = True

    flush_noise()
    for hidden in existing_hidden:
        hidden_changes.append(hidden)

    physical_change_order = sorted(
        range(len(semantic.change_blocks)),
        key=lambda index: int(semantic_positions[index]["marker"]),
    )
    display_number = {owner: index + 1 for index, owner in enumerate(physical_change_order)}
    total_changes = len(semantic.change_blocks)
    for owner, block in enumerate(semantic.change_blocks):
        number = display_number[owner]
        position = semantic_positions[owner]
        selector_text = (
            f"▶ ACTIVE CHANGE {number}/{total_changes} · "
            f"TEST {_line_range_text(block.old_start, block.old_end)} · "
            f"DEV {_line_range_text(block.new_start, block.new_end)}"
        )
        marker_index = int(position["marker"])
        output[marker_index]["text"] = selector_text
        output[marker_index]["privateText"] = selector_text
        output[marker_index]["kind"] = "selector_selected" if number == 1 else "selector"
        continuation_text = (
            f"↳ ACTIVE CHANGE {number}/{total_changes} continues at this physical YAML position"
        )
        for continuation_index in position["continuations"]:
            output[int(continuation_index)]["text"] = continuation_text
            output[int(continuation_index)]["privateText"] = continuation_text

    changes: list[dict[str, Any]] = []
    for index in physical_change_order:
        block = semantic.change_blocks[index]
        position = semantic_positions[index]
        marker_index = position["marker"]
        panel_after = position["last"]
        if marker_index is None or panel_after is None:
            raise WorkbenchError(
                "Internal web diff consistency error: a semantic change could not "
                "be located on the physical file timeline."
            )
        payload = _change_payload(
            workbench,
            record,
            block,
            git_lookup,
            redactor,
            context_catalog,
            marker_index=int(marker_index),
            panel_after=int(panel_after),
        )
        payload["splitPhysical"] = int(position["segmentCount"]) > 1
        changes.append(payload)

    context_gaps = _timeline_context_gaps(
        record,
        output,
        context_lookup,
        redactor,
        context_catalog,
    )
    return {
        "lines": output,
        "changes": changes,
        "hiddenChanges": hidden_changes,
        "contextGaps": context_gaps,
        "visibleChanges": semantic.visible_change_count,
        "handled": semantic.handled_count,
        "noiseHidden": semantic.pattern_hidden_count,
        "whitespaceHidden": semantic.whitespace_hidden_count,
        "orderHidden": len(
            [change for change in hidden_changes if change.get("physicalOrderOnly")]
        ),
        "orderUnavailable": semantic.mapping_order_unavailable_reason,
        "physicalOrderFallback": True,
    }


def _build_web_diff_snapshot(
    workbench: Workbench,
) -> tuple[dict[str, Any], GitLookup, ContextLookup]:
    """Build the browser snapshot and private read-only lookup tables."""
    identity = _comparison_identity(
        workbench.settings.source,
        workbench.settings.target,
    )
    source_column_label = identity["sourceColumnLabel"]
    target_column_label = identity["targetColumnLabel"]
    files: list[dict[str, Any]] = []
    git_lookup: GitLookup = {}
    context_lookup: ContextLookup = {}
    redactor = _PrivacyRedactor()
    context_catalog = load_context_catalog(
        workbench.settings.config_file,
        workbench.settings.source,
        workbench.settings.target,
    )
    for record in workbench.records:
        workbench.refresh_record(record)
        full = full_unified_diff(record, workbench.settings.context, selected_change=0)
        has_current_difference = not record.equal or bool(record.read_error) or record.binary
        if not has_current_difference:
            continue
        focused = review_unified_diff(
            record,
            workbench.enabled_patterns,
            workbench.settings.context,
            hide_whitespace=workbench.hide_whitespace,
            hide_mapping_order=workbench.hide_mapping_order,
            expand_filtered=False,
            selected_change=0,
        )
        focused_expanded = review_unified_diff(
            record,
            workbench.enabled_patterns,
            workbench.settings.context,
            hide_whitespace=workbench.hide_whitespace,
            hide_mapping_order=workbench.hide_mapping_order,
            expand_filtered=True,
            selected_change=0,
        )
        semantic_focused = focused
        semantic_focused_expanded = focused_expanded
        physical_order_fallback = not (
            _presentation_preserves_physical_order(focused)
            and _presentation_preserves_physical_order(focused_expanded)
        )
        if physical_order_fallback:
            # A two-column physical file timeline cannot place one logical
            # replacement at two crossed TEST/DEV locations. Keep the terminal's
            # semantic reconciliation, but render this web file with literal
            # YAML-order placement so line numbers, context, notes, and remote
            # links remain trustworthy. Other noise/whitespace filters stay on.
            focused = review_unified_diff(
                record,
                workbench.enabled_patterns,
                workbench.settings.context,
                hide_whitespace=workbench.hide_whitespace,
                hide_mapping_order=False,
                expand_filtered=False,
                selected_change=0,
            )
            focused_expanded = review_unified_diff(
                record,
                workbench.enabled_patterns,
                workbench.settings.context,
                hide_whitespace=workbench.hide_whitespace,
                hide_mapping_order=False,
                expand_filtered=True,
                selected_change=0,
            )
        status, counts = workbench.file_status(record)
        if physical_order_fallback:
            focused_payload = _physical_semantic_presentation_payload(
                workbench,
                record,
                focused,
                semantic_focused,
                git_lookup,
                context_lookup,
                redactor,
                context_catalog,
            )
            focused_expanded_payload = _physical_semantic_presentation_payload(
                workbench,
                record,
                focused_expanded,
                semantic_focused_expanded,
                git_lookup,
                context_lookup,
                redactor,
                context_catalog,
            )
        else:
            focused_payload = _presentation_payload(
                workbench,
                record,
                focused,
                git_lookup,
                context_lookup,
                redactor,
                context_catalog,
            )
            focused_expanded_payload = _presentation_payload(
                workbench,
                record,
                focused_expanded,
                git_lookup,
                context_lookup,
                redactor,
                context_catalog,
            )
        file_payload = {
            "path": record.relative_path,
            "privatePath": redactor.redact_path(record.relative_path),
            "status": _web_status_text(
                status,
                source_column_label=source_column_label,
                target_column_label=target_column_label,
            ),
            "states": list(record.states),
            "contextRefs": context_catalog.match_path(record.relative_path),
            "contextPath": {
                "sourceEnvironment": context_path_part_payload(
                    context_catalog,
                    record.relative_path,
                    workbench.settings.source.name,
                ),
                "targetEnvironment": context_path_part_payload(
                    context_catalog,
                    record.relative_path,
                    workbench.settings.target.name,
                ),
                "parts": [
                    context_path_part_payload(
                        context_catalog,
                        record.relative_path,
                        part,
                        is_filename=index == len(Path(record.relative_path).parts) - 1,
                    )
                    for index, part in enumerate(Path(record.relative_path).parts)
                ],
            },
            "focused": focused_payload,
            "focusedExpanded": focused_expanded_payload,
            "raw": _presentation_payload(
                workbench,
                record,
                full,
                git_lookup,
                context_lookup,
                redactor,
                context_catalog,
            ),
            "counts": {
                "active": counts.active,
                "handled": counts.handled,
                "noiseHidden": counts.pattern_hidden,
                "whitespaceHidden": counts.whitespace_hidden,
                "orderHidden": counts.mapping_order_hidden,
            },
            "remote": {
                "testFileUrl": workbench.git_file_url(record.test_path),
                "devFileUrl": workbench.git_file_url(record.dev_path),
            },
        }
        for view_name in ("focused", "focusedExpanded", "raw"):
            view = file_payload[view_name]
            for line in view["lines"]:
                if line["kind"] not in _WEB_GENERATED_LABEL_KINDS:
                    continue
                line["text"] = _replace_web_side_labels(
                    line["text"],
                    source_column_label=source_column_label,
                    target_column_label=target_column_label,
                )
                line["privateText"] = _replace_web_side_labels(
                    line["privateText"],
                    source_column_label=source_column_label,
                    target_column_label=target_column_label,
                )
        files.append(file_payload)

    if not files:
        raise WorkbenchError(
            "No current source/target differences are available for the web viewer."
        )

    snapshot = {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(workbench.settings.source),
        "target": str(workbench.settings.target),
        "comparison": {
            "source": str(workbench.settings.source),
            "target": str(workbench.settings.target),
            **identity,
            "launchDirectory": str(Path.cwd().resolve()),
            "configFile": str(workbench.settings.config_file),
            "canPersist": not workbench.settings.dry_run,
        },
        "privateSource": "[SOURCE ROOT]",
        "privateTarget": "[TARGET ROOT]",
        "gitStatus": workbench.git_status.summary,
        "privateGitStatus": "Git context hidden in privacy mode",
        "gitLinks": {
            "available": bool(workbench.git_repository_url and workbench.git_link_commit),
            "repositoryUrl": workbench.git_repository_url,
            "source": workbench.git_repository_url_source,
            "commit": workbench.git_link_commit,
            "status": workbench.git_link_status_text,
        },
        "contextCatalog": {
            **context_catalog.payload(),
            "editable": not workbench.settings.dry_run,
            "editFile": str(context_edit_path(workbench.settings.config_file)),
        },
        "files": files,
    }
    return snapshot, git_lookup, context_lookup


def build_web_diff_snapshot(workbench: Workbench) -> dict[str, Any]:
    """Build the public browser snapshot without exposing private server objects."""
    snapshot, _git_lookup, _context_lookup = _build_web_diff_snapshot(workbench)
    return snapshot


def _commit_payload(
    context: GitCommitContext,
    repository_url: str | None,
) -> dict[str, str | None]:
    merge_request_url = (
        git_repository_merge_request_url(repository_url, context.merge_request_ref)
        if repository_url
        else None
    )
    commit_url = (
        git_repository_commit_url(repository_url, context.commit_hash) if repository_url else None
    )
    return {
        "source": context.source,
        "fullHash": context.commit_hash,
        "hash": context.short_hash,
        "author": context.author,
        "date": context.date,
        "subject": context.subject,
        "url": merge_request_url or commit_url,
        "linkKind": "merge request" if merge_request_url else "commit",
    }


def _git_context_payload(
    workbench: Workbench,
    record: FileRecord,
    block: ChangeBlock,
) -> dict[str, Any]:
    # The browser annotates the first red TEST line and first green DEV line.
    # Restrict blame to those exact physical lines so a newer commit elsewhere
    # in a multi-line block cannot be shown beside the wrong row.
    first_line_block = ChangeBlock(
        tag=block.tag,
        old_start=block.old_start,
        old_end=min(block.old_start + 1, block.old_end),
        new_start=block.new_start,
        new_end=min(block.new_start + 1, block.new_end),
        old_lines=list(block.old_lines[:1]),
        new_lines=list(block.new_lines[:1]),
    )
    test_context, dev_context = workbench._block_git_context(record, first_line_block)

    def newest_first(items: list[GitCommitContext]) -> list[GitCommitContext]:
        return sorted(items, key=lambda item: item.date, reverse=True)

    return {
        # DEV is the incoming side of the comparison and is intentionally first.
        "dev": [
            _commit_payload(item, workbench.git_repository_url)
            for item in newest_first(dev_context)
        ],
        "test": [
            _commit_payload(item, workbench.git_repository_url)
            for item in newest_first(test_context)
        ],
    }


def _bounded_query_int(values: list[str] | None, default: int) -> int:
    if not values:
        return default
    try:
        value = int(values[0])
    except (TypeError, ValueError):
        return default
    return max(0, min(value, 100_000))


def _context_gap_payload(
    snapshot: _ContextGapSnapshot,
    *,
    count: int,
    edge: str,
) -> dict[str, Any]:
    count = max(0, min(count, len(snapshot.lines)))
    if edge == "end":
        offset = len(snapshot.lines) - count
    else:
        offset = 0
    selected = snapshot.lines[offset : offset + count]
    private_selected = (
        snapshot.private_lines[offset : offset + count]
        if snapshot.private_lines
        else tuple(selected)
    )
    context_selected = (
        snapshot.context_refs[offset : offset + count]
        if snapshot.context_refs
        else tuple(() for _item in selected)
    )
    suggestion_selected = (
        snapshot.context_suggestions[offset : offset + count]
        if snapshot.context_suggestions
        else tuple(None for _item in selected)
    )
    targets_selected = (
        snapshot.context_targets[offset : offset + count]
        if snapshot.context_targets
        else tuple(() for _item in selected)
    )
    return {
        "edge": edge,
        "count": count,
        "total": len(snapshot.lines),
        "hasMore": count < len(snapshot.lines),
        "lines": [
            {
                "testLine": snapshot.test_start + offset + index + 1,
                "devLine": snapshot.dev_start + offset + index + 1,
                "text": text,
                "privateText": private_selected[index],
                "kind": "context",
                "emphasisRanges": [],
                "contextRefs": list(context_selected[index]),
                **(
                    {"contextTargets": list(targets_selected[index])}
                    if targets_selected[index]
                    else {}
                ),
                **(
                    {"contextSuggestion": suggestion_selected[index]}
                    if suggestion_selected[index] is not None
                    else {}
                ),
            }
            for index, text in enumerate(selected)
        ],
    }


def _resolve_browser_directory(value: str) -> Path:
    """Resolve one browser-supplied directory without requiring shell expansion."""
    cleaned = value.strip().strip('"').strip("'")
    if not cleaned:
        raise WorkbenchError("Choose both a source directory and a target directory.")
    expanded = os.path.expandvars(os.path.expanduser(cleaned))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise WorkbenchError(f"Could not resolve directory {cleaned!r}: {exc}") from exc
    if not resolved.is_dir():
        raise WorkbenchError(f"Directory does not exist: {resolved}")
    return resolved


def _directory_listing_payload(workbench: Workbench, requested: str | None) -> dict[str, Any]:
    """Return a small local-only directory listing for the comparison picker."""
    if requested:
        current = _resolve_browser_directory(requested)
    else:
        try:
            common = Path(
                os.path.commonpath(
                    [
                        str(workbench.settings.source.resolve()),
                        str(workbench.settings.target.resolve()),
                    ]
                )
            )
        except ValueError:
            common = workbench.settings.source.parent
        current = common if common.is_dir() else Path.cwd().resolve()

    directories: list[dict[str, str]] = []
    truncated = False
    try:
        children = sorted(
            (item for item in current.iterdir() if item.is_dir()),
            key=lambda item: item.name.casefold(),
        )
        if len(children) > 500:
            children = children[:500]
            truncated = True
        directories = [{"name": item.name, "path": str(item.resolve())} for item in children]
    except OSError as exc:
        raise WorkbenchError(f"Could not list directory {current}: {exc}") from exc

    parent = current.parent if current.parent != current else current
    shortcuts: list[dict[str, str]] = []
    seen: set[Path] = set()
    for label, candidate in (
        ("Current comparison", workbench.settings.source.parent),
        ("Launch directory", Path.cwd()),
        ("Home", Path.home()),
        ("Filesystem root", Path(current.anchor or "/")),
    ):
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        shortcuts.append({"label": label, "path": str(resolved)})

    return {
        "path": str(current),
        "parent": str(parent),
        "directories": directories,
        "shortcuts": shortcuts,
        "truncated": truncated,
    }


def _environment_listing_payload(root_value: str) -> dict[str, Any]:
    """Return direct child directories that contain comparable YAML files."""
    root = _resolve_browser_directory(root_value)
    environments: list[dict[str, Any]] = []
    truncated = False
    try:
        children = sorted(
            (item for item in root.iterdir() if item.is_dir() and not item.name.startswith(".")),
            key=lambda item: item.name.casefold(),
        )
    except OSError as exc:
        raise WorkbenchError(f"Could not inspect repository directory {root}: {exc}") from exc

    if len(children) > 250:
        children = children[:250]
        truncated = True
    for child in children:
        yaml_files = discover_yaml_files(child, set(DEFAULT_EXCLUDED_DIRS))
        if not yaml_files:
            continue
        environments.append(
            {
                "name": child.name,
                "path": str(child.resolve()),
                "yamlFiles": len(yaml_files),
            }
        )
    return {
        "root": str(root),
        "repository": (find_git_root(root) or root).name,
        "environments": environments,
        "truncated": truncated,
    }


def _comparison_preview_payload(
    current: Workbench,
    source_value: str,
    target_value: str,
) -> dict[str, Any]:
    """Build a read-only summary before replacing the active comparison."""
    preview = _build_replacement_workbench(current, source_value, target_value)
    source_only = 0
    target_only = 0
    modified = 0
    identical = 0
    matched = 0
    for record in preview.records:
        if record.dev_exists and record.test_exists:
            matched += 1
            if record.equal:
                identical += 1
            else:
                modified += 1
        elif record.dev_exists:
            source_only += 1
        elif record.test_exists:
            target_only += 1

    identity = _comparison_identity(preview.settings.source, preview.settings.target)
    return {
        "source": str(preview.settings.source),
        "target": str(preview.settings.target),
        **identity,
        "totalFiles": len(preview.records),
        "matchedFiles": matched,
        "modifiedFiles": modified,
        "identicalFiles": identical,
        "sourceOnlyFiles": source_only,
        "targetOnlyFiles": target_only,
        "differentFiles": modified + source_only + target_only,
    }


def _build_replacement_workbench(
    current: Workbench,
    source_value: str,
    target_value: str,
) -> Workbench:
    """Create a fresh workbench using browser-selected source and target roots."""
    source = _resolve_browser_directory(source_value)
    target = _resolve_browser_directory(target_value)
    if source == target:
        raise WorkbenchError("Source and target must be different directories.")

    settings_type = type(current.settings)
    settings = settings_type(
        source=source,
        target=target,
        config_file=current.settings.config_file,
        context=current.settings.context,
        include_secrets=current.settings.include_secrets,
        edit_command=current.settings.edit_command,
        vimdiff_command=current.settings.vimdiff_command,
        dry_run=current.settings.dry_run,
    )
    # Imported lazily so web_view remains usable from the workbench's UI modules
    # without creating an import cycle at module import time.
    from .workbench import Workbench as WorkbenchImplementation

    return WorkbenchImplementation(settings)


def _replace_server_comparison(
    server: _ViewerServer,
    *,
    source_value: str,
    target_value: str,
    persist: bool,
) -> dict[str, Any]:
    """Build and atomically install a new comparison on the existing server."""
    current = server.workbench
    replacement = _build_replacement_workbench(current, source_value, target_value)
    snapshot, git_lookup, context_lookup = _build_web_diff_snapshot(replacement)
    page = _render_page(snapshot)

    if persist:
        if replacement.settings.dry_run:
            raise WorkbenchError("Cannot save comparison paths while dry-run mode is enabled.")
        save_project_paths(
            replacement.settings.config_file,
            replacement.settings.source,
            replacement.settings.target,
        )

    with server.state_lock:
        server.page = page
        server.workbench = replacement
        server.git_lookup = git_lookup
        server.context_lookup = context_lookup
        server.git_cache = {}

    return {
        "ok": True,
        "source": str(replacement.settings.source),
        "target": str(replacement.settings.target),
        **_comparison_identity(replacement.settings.source, replacement.settings.target),
        "fileCount": len(snapshot["files"]),
        "persisted": persist,
    }


def _save_server_context_entry(
    server: _ViewerServer,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Persist one local definition and rebuild the active browser snapshot."""
    if server.workbench.settings.dry_run:
        raise WorkbenchError("Context definitions cannot be saved in dry-run mode.")
    raw_entry = payload.get("entry")
    if not isinstance(raw_entry, dict):
        raise WorkbenchError("Context definition request must include an entry object.")
    try:
        entry, path = upsert_context_entry(
            server.workbench.settings.config_file,
            raw_entry,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkbenchError(str(exc)) from exc

    snapshot, git_lookup, context_lookup = _build_web_diff_snapshot(server.workbench)
    page = _render_page(snapshot)
    with server.state_lock:
        server.page = page
        server.git_lookup = git_lookup
        server.context_lookup = context_lookup
        server.git_cache = {}
    return {
        "ok": True,
        "entryId": entry.id,
        "path": str(path),
    }


def _render_page(snapshot: dict[str, Any]) -> bytes:
    snapshot = dict(snapshot)
    snapshot.setdefault(
        "gitLinks",
        {
            "available": False,
            "repositoryUrl": None,
            "source": "unavailable",
            "commit": "",
            "status": "Git links unavailable",
        },
    )
    snapshot.setdefault("contextCatalog", {"entries": [], "diagnostics": []})
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    # Prevent configuration text containing </script> from ending the data block.
    encoded = encoded.replace("</", r"<\/")
    page = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Config Review Web Diff</title>
<style>
:root {
  color-scheme: dark;
  --bg: #0d1117;
  --panel: #161b22;
  --panel2: #21262d;
  --border: #30363d;
  --text: #e6edf3;
  --muted: #8b949e;
  --accent: #58a6ff;
  --accentbg: #1f6feb33;
  --add: #aff5b4;
  --addbg: #033a16;
  --del: #ffdcd7;
  --delbg: #67060c;
  --hidden: #d2a8ff;
  --hiddenbg: #8957e522;
  --hover: #ffffff08;
  --gutter: #21262d;
  --scroll-thumb: #484f58;
  --scroll-track: #161b22;
  --note: #f2cc60;
  --notebg: #bb800926;
  --reviewed: #3fb950;
  --reviewedbg: #23863626;
}
:root[data-theme="light"] {
  color-scheme: light;
  --bg: #ffffff;
  --panel: #f6f8fa;
  --panel2: #ffffff;
  --border: #d0d7de;
  --text: #1f2328;
  --muted: #59636e;
  --accent: #0969da;
  --accentbg: #ddf4ff;
  --add: #116329;
  --addbg: #dafbe1;
  --del: #82071e;
  --delbg: #ffebe9;
  --hidden: #6639ba;
  --hiddenbg: #fbefff;
  --hover: #818b981a;
  --gutter: #d8dee4;
  --scroll-thumb: #afb8c1;
  --scroll-track: #f6f8fa;
  --note: #7d4e00;
  --notebg: #fff8c5;
  --reviewed: #1a7f37;
  --reviewedbg: #dafbe1;
}
* { box-sizing: border-box; }
html, body { height: 100%; min-height: 0; margin: 0; }
body {
  font: 14px/1.45 system-ui, -apple-system, Segoe UI, sans-serif;
  background: var(--bg);
  color: var(--text);
  overflow: hidden;
}
button, input, select, textarea { font: inherit; color: inherit; }
.app {
  height: 100vh;
  height: 100dvh;
  min-height: 0;
  overflow: hidden;
  display: grid;
  grid-template-columns: 330px minmax(0, 1fr);
}
.sidebar {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  background: var(--panel);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
}
.brand { padding: 18px 16px 12px; border-bottom: 1px solid var(--border); }
.brand h1 { font-size: 16px; margin: 0 0 4px; }
.brand p { margin: 0; color: var(--muted); font-size: 12px; }
.search { padding: 12px; border-bottom: 1px solid var(--border); }
.search input {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
}
.tree, .diff {
  overflow: auto;
  scrollbar-gutter: stable;
  scrollbar-color: var(--scroll-thumb) var(--scroll-track);
  scrollbar-width: thin;
}
.tree::-webkit-scrollbar, .diff::-webkit-scrollbar { width: 12px; height: 12px; }
.tree::-webkit-scrollbar-track, .diff::-webkit-scrollbar-track { background: var(--scroll-track); }
.tree::-webkit-scrollbar-thumb, .diff::-webkit-scrollbar-thumb {
  background: var(--scroll-thumb);
  border: 3px solid var(--scroll-track);
  border-radius: 999px;
}
.tree { padding: 8px; flex: 1 1 0; min-height: 0; overscroll-behavior: contain; }
.tree details { margin: 1px 0; }
.tree summary { cursor: pointer; color: var(--muted); padding: 4px 6px; user-select: none; }
.tree .children { padding-left: 14px; }
.file {
  width: 100%;
  display: flex;
  gap: 8px;
  align-items: center;
  text-align: left;
  border: 0;
  border-radius: 6px;
  padding: 6px 8px;
  color: var(--text);
  background: transparent;
  cursor: pointer;
}
.file:hover { background: var(--panel2); }
.file.active { background: var(--accentbg); color: var(--text); }
.file .name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
.badge {
  font-size: 10px;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 1px 6px;
}
.badge.notes { color: var(--note); border-color: var(--note); }
.badge.reviewed { color: var(--reviewed); border-color: var(--reviewed); }
.badge.hidden { color: var(--hidden); border-color: var(--hidden); }
.main { min-width: 0; min-height: 0; overflow: hidden; display: flex; flex-direction: column; }
.toolbar {
  min-height: 70px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--panel);
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.path {
  font: 600 14px ui-monospace, SFMono-Regular, Consolas, monospace;
  min-width: 240px;
  flex: 1;
  overflow-x: auto;
  overflow-y: hidden;
  text-overflow: clip;
  white-space: nowrap;
  scrollbar-width: thin;
}
.controls { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.controls button, .view-menu summary, .view-menu button {
  border: 1px solid var(--border);
  background: var(--panel2);
  color: var(--text);
  padding: 6px 10px;
  border-radius: 6px;
  cursor: pointer;
}
.controls button:hover, .view-menu summary:hover, .view-menu button:hover { border-color: var(--muted); }
.controls button.active { background: var(--accent); border-color: var(--accent); color: #fff; }
.view-menu { position: relative; }
.view-menu summary { list-style: none; user-select: none; }
.view-menu summary::-webkit-details-marker { display: none; }
.view-menu[open] summary { border-color: var(--accent); }
.view-menu-panel {
  position: absolute;
  right: 0;
  top: calc(100% + 6px);
  z-index: 20;
  width: 250px;
  padding: 10px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 12px 30px #0005;
}
.menu-label {
  font-size: 11px;
  font-weight: 700;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .05em;
  margin: 2px 0 6px;
}
.theme-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 5px; }
.hidden-row { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; margin-top: 6px; }
.privacy-row { display: grid; grid-template-columns: 1fr; gap: 5px; }
.menu-help { color: var(--muted); font-size: 11px; line-height: 1.35; margin-top: 6px; }
.privacy-omitted {
  padding: 10px 12px;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
  background: var(--hiddenbg);
}
.view-menu button { padding: 5px 7px; font-size: 12px; }
.view-menu button.active { background: var(--accent); border-color: var(--accent); color: #fff; }
.menu-separator { height: 1px; background: var(--border); margin: 10px 0; }
.review-summary {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 5px;
  margin-bottom: 8px;
}
.review-summary div {
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: 6px;
  text-align: center;
  font-size: 11px;
}
.file-state-list { max-height: 180px; overflow: auto; margin-bottom: 6px; }
.file-state-empty { color: var(--muted); font-size: 12px; padding: 5px 2px; }
.file-state-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 6px;
  align-items: center;
  padding: 4px 0;
  border-bottom: 1px solid var(--border);
}
.file-state-row:last-child { border-bottom: 0; }
.file-state-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font: 11px/1.35 ui-monospace, SFMono-Regular, Consolas, monospace;
}
.file-state-actions { display: flex; gap: 4px; }
.file-state-actions button, .review-action-row button { padding: 4px 6px; font-size: 11px; }
.review-action-row { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; margin-top: 6px; }
.meta { width: 100%; color: var(--muted); font-size: 12px; display: flex; gap: 12px; flex-wrap: wrap; }
.diff {
  flex: 1 1 0;
  min-height: 0;
  overscroll-behavior: contain;
  font: 13px/1.45 ui-monospace, SFMono-Regular, Consolas, Liberation Mono, monospace;
}
.line {
  display: grid;
  grid-template-columns: 60px 60px 24px minmax(max-content, 1fr);
  min-height: 20px;
  border-left: 3px solid transparent;
}
.line:hover { background: var(--hover); }
.ln {
  padding: 1px 8px;
  text-align: right;
  color: var(--muted);
  user-select: text;
  border-right: 1px solid var(--gutter);
}
.ln a {
  color: inherit;
  text-decoration: none;
  display: block;
  margin: -1px -8px;
  padding: 1px 8px;
}
.ln a:hover, .ln a:focus-visible {
  color: var(--accent);
  background: var(--accentbg);
  outline: none;
}
.ln a::after { content: ' ↗'; font-size: 9px; opacity: .65; }
.prefix { padding: 1px 6px; text-align: center; color: var(--muted); user-select: none; }
.code { padding: 1px 10px; white-space: pre; }
.context-help-button {
  width: 32px;
  min-width: 32px;
  padding-left: 0 !important;
  padding-right: 0 !important;
  font-weight: 800;
  font-size: 16px;
}
.context-help-button.active {
  background: var(--accent) !important;
  border-color: var(--accent) !important;
  color: #fff !important;
}
.context-available, .context-missing { position: relative; }
.context-token {
  display: inline;
  border-radius: 3px;
  box-decoration-break: clone;
  -webkit-box-decoration-break: clone;
}
.context-help-mode .context-available { cursor: help; }
.context-help-mode .context-missing { cursor: copy; }
.context-help-mode .context-token.context-available:hover,
.context-help-mode .context-token.context-available:focus-visible,
.context-help-mode .path-part.context-available:hover,
.context-help-mode .path-part.context-available:focus-visible {
  outline: 1px dotted var(--accent);
  outline-offset: 1px;
  background: var(--accentbg);
}
.context-help-mode .context-token.context-missing:hover,
.context-help-mode .context-token.context-missing:focus-visible,
.context-help-mode .path-part.context-missing:hover,
.context-help-mode .path-part.context-missing:focus-visible {
  outline: 1px dashed var(--muted);
  outline-offset: 1px;
  background: var(--hover);
}
.path-breadcrumb { display: inline-flex; align-items: center; gap: 3px; min-width: 0; }
.path-part { display: inline-block; border-radius: 3px; padding: 0 2px; }
.path-arrow { color: var(--accent); padding: 0 3px; }
.path-separator { color: var(--muted); }
.path-state { color: var(--muted); margin-left: 7px; font: 600 11px/1.4 system-ui, sans-serif; }
.context-tooltip[hidden] { display: none; }
.context-tooltip {
  position: fixed;
  z-index: 130;
  width: min(390px, calc(100vw - 24px));
  max-height: min(420px, calc(100vh - 24px));
  overflow: auto;
  padding: 11px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
  color: var(--text);
  box-shadow: 0 14px 38px #0008;
  white-space: normal;
  font: 12px/1.45 system-ui, -apple-system, Segoe UI, sans-serif;
}
.context-tooltip-entry + .context-tooltip-entry {
  margin-top: 9px;
  padding-top: 9px;
  border-top: 1px solid var(--border);
}
.context-tooltip-title { font-weight: 800; color: var(--accent); }
.context-tooltip-category { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: .04em; }
.context-tooltip-summary { margin-top: 4px; }
.context-tooltip-hint { margin-top: 8px; color: var(--muted); font-size: 10px; }
.context-tooltip-missing { color: var(--muted); }
.intraline {
  font-weight: 800;
  border-radius: 2px;
  padding: 0 1px;
}
.remove .intraline, .remove_note .intraline, .filtered_remove .intraline { background: #f8514955; }
.add .intraline, .add_note .intraline, .filtered_add .intraline { background: #3fb95055; }
.remove, .remove_note, .filtered_remove { background: var(--delbg); color: var(--del); border-left-color: #f85149; }
.add, .add_note, .filtered_add { background: var(--addbg); color: var(--add); border-left-color: #3fb950; }
.hunk, .title, .section, .selector, .selector_selected, .selector_continuation, .test_file_header, .dev_file_header, .file_header {
  background: var(--accentbg);
  color: var(--accent);
  font-weight: 600;
}
.selector_continuation { color: var(--muted); font-style: italic; }
.filtered, .filtered_header, .handled { background: var(--hiddenbg); color: var(--hidden); }
.error { background: var(--delbg); color: var(--del); font-weight: 700; }
.empty { padding: 48px; text-align: center; color: var(--muted); }
.hidden-block {
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  background: var(--hiddenbg);
}
.hidden-block summary { cursor: pointer; list-style: none; user-select: none; }
.hidden-block summary::-webkit-details-marker { display: none; }
.hidden-block summary .line { background: transparent; }
.hidden-block summary .code::before { content: '▶ '; display: inline-block; width: 18px; }
.hidden-block[open] summary .code::before { content: '▼ '; }
.hidden-block-body { overflow: visible; }
.hidden-block .filtered_header .code { font-weight: 700; }
.review-panel {
  margin: 8px 12px 14px 147px;
  min-width: 560px;
  max-width: 980px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
  overflow: hidden;
  font: 13px/1.45 system-ui, -apple-system, Segoe UI, sans-serif;
}
.review-heading {
  display: flex;
  gap: 10px;
  align-items: baseline;
  justify-content: space-between;
  padding: 9px 12px;
  background: var(--panel2);
  border-bottom: 1px solid var(--border);
}
.review-label { font-weight: 700; }
.review-ranges { color: var(--muted); font-size: 12px; white-space: nowrap; }
.review-remote-links { display: inline-flex; gap: 7px; white-space: nowrap; }
.review-remote-links a { color: var(--accent); text-decoration: none; font-size: 12px; }
.review-remote-links a:hover, .review-remote-links a:focus-visible { text-decoration: underline; }
.split-change-note {
  padding: 8px 12px;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
  background: var(--gutter);
}
.review-actions {
  display: flex;
  gap: 8px;
  padding: 9px 12px 10px;
  border-top: 1px solid var(--border);
  background: var(--panel2);
}
.review-actions button {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 5px 9px;
  background: var(--panel);
  color: var(--text);
  cursor: pointer;
}
.review-actions button:hover { border-color: var(--muted); }
.review-actions button.active { border-color: var(--accent); color: var(--accent); }
.line-git-context {
  grid-column: 4 / -1;
  margin-left: 18px;
  padding: 3px 10px 6px;
  border-left: 2px solid var(--border);
  color: var(--muted);
  font: 11px/1.4 system-ui, -apple-system, Segoe UI, sans-serif;
  white-space: normal;
  overflow-wrap: anywhere;
}
.line-git-context::before {
  content: "Git context";
  display: inline-block;
  margin-right: 7px;
  color: var(--muted);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .04em;
  text-transform: uppercase;
}
.line-git-context.no-context { color: var(--muted); }
.inline-git-prefix { color: var(--muted); }
.inline-git-author { color: var(--muted); font-weight: 600; }
.inline-git-subject { color: var(--muted); }
.inline-git-hash {
  color: var(--accent);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  text-decoration: none;
}
.inline-git-hash:hover, .inline-git-hash:focus-visible { text-decoration: underline; }
.note-wrap { padding: 10px 12px 12px; border-bottom: 1px solid var(--border); }
.note-label { display: flex; justify-content: space-between; gap: 10px; color: var(--note); font-weight: 700; margin-bottom: 6px; }
.note-help { color: var(--muted); font-weight: 400; font-size: 12px; }
.review-note {
  display: block;
  width: 100%;
  min-height: 72px;
  resize: vertical;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
  line-height: 1.4;
}
.review-note:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.context-gap {
  min-width: max-content;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  background: var(--gutter);
}
.context-gap-lines .line { background: var(--panel2); }
.context-gap-button {
  display: block;
  width: 100%;
  min-height: 26px;
  border: 0;
  background: var(--gutter);
  color: var(--muted);
  cursor: pointer;
  font: 12px/1.3 system-ui, -apple-system, Segoe UI, sans-serif;
}
.context-gap-button:hover { color: var(--accent); background: var(--accentbg); }
.context-gap-button:disabled { cursor: default; color: var(--muted); opacity: .65; }
.print-report { display: none; white-space: pre-wrap; font: 11pt/1.4 ui-monospace, Consolas, monospace; }
.footer {
  padding: 6px 12px;
  border-top: 1px solid var(--border);
  background: var(--panel);
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.footer.success { color: var(--add); }
.footer.error { color: var(--del); }
.footer.busy { color: var(--accent); }
.modal-backdrop[hidden] { display: none; }
.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: grid;
  place-items: center;
  padding: 24px;
  background: #0009;
}
.modal-dialog {
  width: min(780px, 100%);
  max-height: min(760px, calc(100vh - 48px));
  max-height: min(760px, calc(100dvh - 48px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--panel);
  box-shadow: 0 24px 70px #0008;
}
.modal-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--panel2);
}
.modal-heading h2 { margin: 0; font-size: 16px; }
.modal-close {
  width: 32px;
  height: 32px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--panel);
  cursor: pointer;
}
.modal-body { min-height: 0; overflow: auto; padding: 16px; }
.comparison-help { margin: 0 0 14px; color: var(--muted); }
.comparison-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.comparison-side {
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
}
.comparison-side-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}
.comparison-side-heading strong { font-size: 13px; }
.comparison-side-heading span { color: var(--muted); font-size: 11px; }
.comparison-field label { display: block; margin-bottom: 5px; font-weight: 700; }
.comparison-field + .comparison-field { margin-top: 10px; }
.path-entry { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 7px; }
.path-entry input, .comparison-field select {
  width: 100%;
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
}
.environment-entry { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 7px; }
.environment-status { min-height: 18px; margin-top: 4px; color: var(--muted); font-size: 11px; }
.exact-directory-label { color: var(--muted); font-size: 11px; }
.comparison-preview {
  margin-top: 14px;
  padding: 11px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel2);
}
.comparison-preview[hidden] { display: none; }
.comparison-preview-title { font-weight: 800; margin-bottom: 7px; }
.comparison-preview-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
}
.comparison-preview-grid div {
  padding: 7px;
  border: 1px solid var(--border);
  border-radius: 6px;
  text-align: center;
  font-size: 11px;
}
.comparison-preview-grid strong { display: block; font-size: 15px; }
.path-entry button, .comparison-actions button, .folder-toolbar button, .shortcut-button,
.folder-row {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 7px 10px;
  background: var(--panel2);
  color: var(--text);
  cursor: pointer;
}
.path-entry button:hover, .comparison-actions button:hover, .folder-toolbar button:hover,
.shortcut-button:hover, .folder-row:hover { border-color: var(--muted); }
.comparison-options {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 4px;
}
.persist-option { display: inline-flex; gap: 7px; align-items: center; color: var(--muted); }
.comparison-actions { display: flex; justify-content: flex-end; gap: 7px; margin-top: 16px; }
.comparison-actions .primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.comparison-error {
  min-height: 20px;
  margin-top: 10px;
  color: var(--del);
  white-space: pre-wrap;
}
.folder-browser[hidden] { display: none; }
.folder-browser {
  margin-top: 16px;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}
.folder-toolbar {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 9px;
  border-bottom: 1px solid var(--border);
  background: var(--panel2);
}
.folder-location {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font: 12px/1.4 ui-monospace, SFMono-Regular, Consolas, monospace;
}
.folder-shortcuts { display: flex; gap: 6px; flex-wrap: wrap; padding: 9px; border-bottom: 1px solid var(--border); }
.shortcut-button { padding: 4px 7px; font-size: 11px; }
.folder-list { max-height: 270px; overflow: auto; padding: 7px; }
.folder-row { display: block; width: 100%; margin-bottom: 5px; text-align: left; }
.folder-row::before { content: "📁 "; }
.folder-empty { padding: 20px; text-align: center; color: var(--muted); }
.folder-truncated { padding: 6px 9px; color: var(--muted); font-size: 11px; }
.dictionary-dialog { width: min(1040px, 100%); }
.dictionary-dialog .modal-body {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.dictionary-toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
}
.dictionary-toolbar input {
  width: 100%;
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
}
.dictionary-count { color: var(--muted); white-space: nowrap; font-size: 12px; }
.dictionary-diagnostics {
  margin-bottom: 10px;
  padding: 8px 10px;
  border: 1px solid var(--note);
  border-radius: 6px;
  background: var(--notebg);
  color: var(--note);
  white-space: pre-wrap;
}
.dictionary-diagnostics:empty { display: none; }
.dictionary-layout {
  flex: 1 1 auto;
  display: grid;
  grid-template-columns: minmax(260px, .85fr) minmax(360px, 1.4fr);
  min-height: 0;
  height: min(620px, calc(100vh - 210px));
  height: min(620px, calc(100dvh - 210px));
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}
.dictionary-list {
  min-width: 0;
  min-height: 0;
  height: 100%;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  padding: 7px;
  border-right: 1px solid var(--border);
}
.dictionary-category {
  padding: 8px 7px 4px;
  color: var(--muted);
  font-size: 10px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: .05em;
}
.dictionary-item {
  display: block;
  width: 100%;
  padding: 8px 9px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text);
  text-align: left;
  cursor: pointer;
}
.dictionary-item:hover { background: var(--hover); }
.dictionary-item.active { background: var(--accentbg); color: var(--accent); }
.dictionary-item-title { display: block; font-weight: 700; }
.dictionary-item-summary {
  display: block;
  margin-top: 2px;
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dictionary-details {
  min-width: 0;
  min-height: 0;
  height: 100%;
  overflow: auto;
  overscroll-behavior: contain;
  padding: 18px;
}
.dictionary-details h3 { margin: 0 0 4px; font-size: 20px; }
.dictionary-detail-category { color: var(--accent); font-weight: 700; }
.dictionary-detail-summary { margin: 14px 0; font-size: 14px; }
.dictionary-detail-more { color: var(--muted); white-space: pre-wrap; }
.dictionary-aliases { margin-top: 16px; color: var(--muted); font-size: 12px; }
.dictionary-matches { margin-top: 16px; }
.dictionary-matches h4 { margin: 0 0 6px; font-size: 12px; }
.dictionary-match {
  margin-top: 5px;
  padding: 7px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  font: 11px/1.4 ui-monospace, SFMono-Regular, Consolas, monospace;
  overflow-wrap: anywhere;
}
.dictionary-source { margin-top: 12px; color: var(--muted); font-size: 11px; }
.dictionary-detail-actions { display: flex; gap: 7px; margin-top: 16px; }
.dictionary-detail-actions button, .dictionary-toolbar button, .context-editor-actions button {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 9px;
  background: var(--panel2);
  color: var(--text);
  cursor: pointer;
}
.dictionary-detail-actions button:hover, .dictionary-toolbar button:hover,
.context-editor-actions button:hover { border-color: var(--muted); }
.context-editor-dialog { width: min(720px, 100%); }
.context-editor-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.context-editor-field { display: grid; gap: 5px; }
.context-editor-field.full { grid-column: 1 / -1; }
.context-editor-field label { color: var(--muted); font-size: 12px; font-weight: 700; }
.context-editor-field input, .context-editor-field select, .context-editor-field textarea {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
}
.context-category-row,
.context-scope-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 7px;
}
.context-category-row input[hidden],
.context-scope-row[hidden] { display: none; }
.context-scope-option {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
}
.context-scope-option input { width: auto; }
.context-scope-row button,
.context-category-row button {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 9px;
  background: var(--panel2);
  color: var(--text);
  cursor: pointer;
}
.context-scope-row button:hover,
.context-category-row button:hover { border-color: var(--muted); }
.context-file-picker-dialog { width: min(680px, 100%); }
.context-file-picker-list {
  max-height: min(520px, calc(100vh - 220px));
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 7px;
}
.context-file-picker-item {
  display: block;
  width: 100%;
  padding: 8px 9px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text);
  text-align: left;
  font: 12px/1.4 ui-monospace, SFMono-Regular, Consolas, monospace;
  cursor: pointer;
}
.context-file-picker-item:hover,
.context-file-picker-item:focus-visible { background: var(--accentbg); color: var(--accent); outline: none; }
.context-editor-field textarea { min-height: 90px; resize: vertical; }
.context-editor-help { color: var(--muted); font-size: 11px; margin-top: 4px; }
.context-editor-error { min-height: 20px; color: var(--del); white-space: pre-wrap; margin-top: 10px; }
.context-editor-actions { display: flex; justify-content: flex-end; gap: 7px; margin-top: 14px; }
.context-editor-actions .primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.dictionary-empty { padding: 32px; text-align: center; color: var(--muted); }
@media (max-width: 800px) {
  .app { grid-template-columns: 240px minmax(0, 1fr); }
  .line { grid-template-columns: 46px 46px 20px minmax(max-content, 1fr); }
  .view-menu-panel { right: -4px; }
  .review-panel { margin-left: 115px; }
  .context-grid { grid-template-columns: 1fr; }
  .modal-backdrop { padding: 8px; }
  .comparison-grid { grid-template-columns: 1fr; }
  .path-entry { grid-template-columns: 1fr; }
  .environment-entry { grid-template-columns: 1fr; }
  .comparison-preview-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .dictionary-layout {
    grid-template-columns: 1fr;
    height: calc(100vh - 185px);
    height: calc(100dvh - 185px);
  }
  .dictionary-list { max-height: 250px; border-right: 0; border-bottom: 1px solid var(--border); }
}
@media print {
  body { overflow: visible; background: #fff; color: #000; }
  .app { display: none !important; }
  .print-report { display: block; color: #000; }
}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand"><h1>Config Review Web Diff</h1><p id="fileCount"></p></div>
    <div class="search"><input id="search" type="search" placeholder="Filter changed files…" autocomplete="off"></div>
    <div id="tree" class="tree"></div>
  </aside>
  <main class="main">
    <div class="toolbar">
      <div id="path" class="path"></div>
      <div class="controls">
        <button id="prev" title="Previous file ([)">← File</button>
        <button id="next" title="Next file (])">File →</button>
        <button id="focused" class="active">Focused</button>
        <button id="raw">Raw</button>
        <button id="changeComparison" type="button" title="Choose different repositories, environments, or directories">Comparison ▾</button>
        <button id="contextHelp" class="context-help-button" type="button" aria-pressed="false" title="Turn context help on">?</button>
        <button id="hideFile" title="Temporarily remove this file from the active tree">Hide file</button>
        <button id="reviewFile" title="Mark this file reviewed for the temporary review report">Mark reviewed</button>
        <button id="saveReview" title="Save all current-view changes and reviewer notes as plaintext">Save review…</button>
        <button id="copyDiff" type="button" title="Copy the currently displayed diff, including source and target line numbers">Copy displayed diff</button>
        <details id="reviewMenu" class="view-menu">
          <summary>Review ▾</summary>
          <div class="view-menu-panel">
            <div id="reviewSummary" class="review-summary"></div>
            <div class="menu-label">Hidden files</div>
            <div id="hiddenFileList" class="file-state-list"></div>
            <button id="restoreHidden" type="button">Show all hidden</button>
            <div class="menu-separator"></div>
            <div class="menu-label">Reviewed files</div>
            <div id="reviewedFileList" class="file-state-list"></div>
            <div class="review-action-row">
              <button id="saveReviewed" type="button">Save reviewed report…</button>
              <button id="printReviewed" type="button">Print reviewed report…</button>
            </div>
          </div>
        </details>
        <details id="viewMenu" class="view-menu">
          <summary>View ▾</summary>
          <div class="view-menu-panel">
            <div class="menu-label">Theme</div>
            <div class="theme-row">
              <button type="button" data-theme-choice="system" class="active">System</button>
              <button type="button" data-theme-choice="dark">Dark</button>
              <button type="button" data-theme-choice="light">Light</button>
            </div>
            <div class="menu-separator"></div>
            <div class="menu-label">Privacy</div>
            <div class="privacy-row">
              <button id="privacyToggle" type="button" aria-pressed="false" title="Redact sensitive values, personal references, Git context, remote links, and reviewer notes in the display and exported reports">Hide sensitive values</button>
            </div>
            <div class="menu-help">Redacts the display and exports. The original snapshot still exists inside this local page, so share only a privacy-mode export or screenshot—not the HTML file.</div>
            <div class="menu-separator"></div>
            <div class="menu-label">Hidden differences</div>
            <div class="hidden-row">
              <button id="expandHidden" type="button">Expand all</button>
              <button id="collapseHidden" type="button">Collapse all</button>
            </div>
          </div>
        </details>
      </div>
      <div id="meta" class="meta"></div>
    </div>
    <div id="diff" class="diff"></div>
    <div id="footer" class="footer"></div>
  </main>
</div>
<div id="comparisonModal" class="modal-backdrop" hidden>
  <section class="modal-dialog" role="dialog" aria-modal="true" aria-labelledby="comparisonTitle">
    <div class="modal-heading">
      <h2 id="comparisonTitle">Change comparison</h2>
      <button id="closeComparison" class="modal-close" type="button" aria-label="Close">×</button>
    </div>
    <div class="modal-body">
      <p class="comparison-help">Choose any two repository environments or exact directories. The left side is incoming/source; the right side is the current target. Applying a new comparison reloads this browser view and clears temporary notes, hidden files, and reviewed-file state.</p>
      <div class="comparison-grid">
        <section class="comparison-side">
          <div class="comparison-side-heading">
            <strong>Incoming / source</strong>
            <span id="sourceSideLabel"></span>
          </div>
          <div class="comparison-field">
            <label for="sourceRepositoryRoot">Repository or environment parent</label>
            <div class="path-entry">
              <input id="sourceRepositoryRoot" type="text" spellcheck="false" autocomplete="off">
              <button id="browseSourceRoot" type="button">Browse…</button>
            </div>
          </div>
          <div class="comparison-field">
            <label for="sourceEnvironment">Environment</label>
            <div class="environment-entry">
              <select id="sourceEnvironment"></select>
              <button id="refreshSourceEnvironments" type="button">Find</button>
            </div>
            <div id="sourceEnvironmentStatus" class="environment-status"></div>
          </div>
          <div class="comparison-field">
            <label class="exact-directory-label" for="sourceDirectory">Exact comparison directory</label>
            <div class="path-entry">
              <input id="sourceDirectory" type="text" spellcheck="false" autocomplete="off">
              <button id="browseSource" type="button">Browse…</button>
            </div>
          </div>
        </section>
        <section class="comparison-side">
          <div class="comparison-side-heading">
            <strong>Current / target</strong>
            <span id="targetSideLabel"></span>
          </div>
          <div class="comparison-field">
            <label for="targetRepositoryRoot">Repository or environment parent</label>
            <div class="path-entry">
              <input id="targetRepositoryRoot" type="text" spellcheck="false" autocomplete="off">
              <button id="browseTargetRoot" type="button">Browse…</button>
            </div>
          </div>
          <div class="comparison-field">
            <label for="targetEnvironment">Environment</label>
            <div class="environment-entry">
              <select id="targetEnvironment"></select>
              <button id="refreshTargetEnvironments" type="button">Find</button>
            </div>
            <div id="targetEnvironmentStatus" class="environment-status"></div>
          </div>
          <div class="comparison-field">
            <label class="exact-directory-label" for="targetDirectory">Exact comparison directory</label>
            <div class="path-entry">
              <input id="targetDirectory" type="text" spellcheck="false" autocomplete="off">
              <button id="browseTarget" type="button">Browse…</button>
            </div>
          </div>
        </section>
      </div>
      <div class="comparison-options">
        <button id="swapComparison" type="button">Swap source and target</button>
        <label class="persist-option">
          <input id="persistComparison" type="checkbox">
          Save these paths as the project default
        </label>
      </div>
      <div id="comparisonPreview" class="comparison-preview" hidden>
        <div id="comparisonPreviewTitle" class="comparison-preview-title"></div>
        <div id="comparisonPreviewGrid" class="comparison-preview-grid"></div>
      </div>
      <div id="folderBrowser" class="folder-browser" hidden>
        <div class="folder-toolbar">
          <button id="folderUp" type="button">↑ Up</button>
          <div id="folderLocation" class="folder-location"></div>
          <button id="useFolder" type="button">Use this folder</button>
        </div>
        <div id="folderShortcuts" class="folder-shortcuts"></div>
        <div id="folderList" class="folder-list"></div>
        <div id="folderTruncated" class="folder-truncated" hidden>Only the first 500 folders are shown.</div>
      </div>
      <div id="comparisonError" class="comparison-error" role="alert"></div>
      <div class="comparison-actions">
        <button id="cancelComparison" type="button">Cancel</button>
        <button id="previewComparison" type="button">Preview files</button>
        <button id="applyComparison" class="primary" type="button">Start comparison</button>
      </div>
    </div>
  </section>
</div>
<div id="contextModal" class="modal-backdrop" hidden>
  <section class="modal-dialog dictionary-dialog" role="dialog" aria-modal="true" aria-labelledby="contextTitle">
    <div class="modal-heading">
      <h2 id="contextTitle">Context dictionary</h2>
      <button id="closeContext" class="modal-close" type="button" aria-label="Close">×</button>
    </div>
    <div class="modal-body">
      <div class="dictionary-toolbar">
        <input id="contextSearch" type="search" placeholder="Search services, Helm, GitLab, GitOps, acronyms…" autocomplete="off">
        <button id="newContextEntry" type="button">New definition</button>
        <span id="contextCount" class="dictionary-count"></span>
      </div>
      <div id="contextDiagnostics" class="dictionary-diagnostics"></div>
      <div class="dictionary-layout">
        <div id="contextList" class="dictionary-list"></div>
        <article id="contextDetails" class="dictionary-details"></article>
      </div>
    </div>
  </section>
</div>
<div id="contextEditorModal" class="modal-backdrop" hidden>
  <section class="modal-dialog context-editor-dialog" role="dialog" aria-modal="true" aria-labelledby="contextEditorTitle">
    <div class="modal-heading">
      <h2 id="contextEditorTitle">Add context definition</h2>
      <button id="closeContextEditor" class="modal-close" type="button" aria-label="Close">×</button>
    </div>
    <div class="modal-body">
      <div class="context-editor-grid">
        <div class="context-editor-field">
          <label for="contextEntryId">Definition ID</label>
          <input id="contextEntryId" type="text" spellcheck="false">
        </div>
        <div class="context-editor-field">
          <label for="contextEntryCategory">Category</label>
          <select id="contextEntryCategory"></select>
          <input id="contextEntryNewCategory" type="text" placeholder="New category name" hidden>
        </div>
        <div class="context-editor-field full">
          <label for="contextEntryTitle">Title</label>
          <input id="contextEntryTitle" type="text">
        </div>
        <div class="context-editor-field full">
          <label for="contextEntrySummary">Short definition</label>
          <textarea id="contextEntrySummary"></textarea>
        </div>
        <div class="context-editor-field full">
          <label for="contextEntryDetails">More details (optional)</label>
          <textarea id="contextEntryDetails"></textarea>
        </div>
        <div class="context-editor-field full">
          <label for="contextEntryAliases">Aliases (comma separated)</label>
          <input id="contextEntryAliases" type="text">
        </div>
        <div class="context-editor-field">
          <label for="contextMatchType">Match type</label>
          <select id="contextMatchType">
            <option value="path-segment">Path segment</option>
            <option value="file-name">File name</option>
            <option value="yaml-path">Exact YAML path</option>
            <option value="yaml-key">YAML key</option>
            <option value="yaml-value">YAML value</option>
            <option value="env-name">Environment variable name</option>
            <option value="term">Term</option>
            <option value="command">Command</option>
            <option value="path">Path pattern</option>
          </select>
        </div>
        <div class="context-editor-field">
          <label for="contextMatchValue">Match value</label>
          <input id="contextMatchValue" type="text" spellcheck="false">
        </div>
        <div class="context-editor-field full">
          <label class="context-scope-option" for="contextLimitFiles">
            <input id="contextLimitFiles" type="checkbox">
            Limit this definition to specific files or paths
          </label>
          <div id="contextScopeRow" class="context-scope-row" hidden>
            <input id="contextMatchFiles" type="text" spellcheck="false" placeholder="Example: **/values.yaml, ms/config/*" disabled>
            <button id="browseContextFiles" type="button" disabled>Browse changed files…</button>
          </div>
          <div id="contextMatchContext" class="context-editor-help"></div>
          <div class="context-editor-help">Definitions are saved to <span id="contextEditFile"></span>. Editing a built-in entry creates a project-local override.</div>
        </div>
      </div>
      <div id="contextEditorError" class="context-editor-error" role="alert"></div>
      <div class="context-editor-actions">
        <button id="cancelContextEditor" type="button">Cancel</button>
        <button id="saveContextEntry" class="primary" type="button">Save definition</button>
      </div>
    </div>
  </section>
</div>
<div id="contextFilePickerModal" class="modal-backdrop" hidden>
  <section class="modal-dialog context-file-picker-dialog" role="dialog" aria-modal="true" aria-labelledby="contextFilePickerTitle">
    <div class="modal-heading">
      <h2 id="contextFilePickerTitle">Choose a comparison file</h2>
      <button id="closeContextFilePicker" class="modal-close" type="button" aria-label="Close">×</button>
    </div>
    <div class="modal-body">
      <div class="dictionary-toolbar">
        <input id="contextFileSearch" type="search" placeholder="Filter changed files…" autocomplete="off">
        <span id="contextFileCount" class="dictionary-count"></span>
      </div>
      <div id="contextFilePickerList" class="context-file-picker-list"></div>
    </div>
  </section>
</div>
<div id="contextTooltip" class="context-tooltip" role="tooltip" hidden></div>
<pre id="printReport" class="print-report"></pre>
<script id="snapshot" type="application/json">__SNAPSHOT__</script>
<script>
'use strict';
const snapshot = JSON.parse(document.getElementById('snapshot').textContent);
let mode = 'focused';
let selected = 0;
let visible = snapshot.files.slice();
let themeChoice = 'system';
let privacyMode = false;
let notesDirty = false;
const notesByChange = new Map();
const noteEditorsOpen = new Set();
const gitContextCache = new Map();
const inlineGitContextKeys = new Set();
const gapStateById = new Map();
const hiddenFiles = new Set();
const reviewedFiles = new Set();
const reviewedAtByFile = new Map();
const contextEntries = snapshot.contextCatalog?.entries ?? [];
const contextById = new Map(contextEntries.map(entry => [entry.id, entry]));
const contextCategories = [...new Set([
  'Project Context',
  ...contextEntries.map(entry => entry.category).filter(Boolean),
])].sort((left, right) => {
  if (left === 'Project Context') return -1;
  if (right === 'Project Context') return 1;
  return left.localeCompare(right);
});
const contextEditable = Boolean(snapshot.contextCatalog?.editable);
const contextEditFilePath = snapshot.contextCatalog?.editFile ?? '.config-review-context.yaml';
let selectedContextId = contextEntries[0]?.id ?? null;
let contextTooltipTimer = null;
let contextHelpMode = false;
let editingContextEntry = null;
let browseInput = null;
let browseAfterSelect = null;
let browsePath = '';
let browseParent = '';
const $ = id => document.getElementById(id);
const systemTheme = window.matchMedia('(prefers-color-scheme: light)');
const prefixFor = kind => kind.includes('remove') || kind === 'remove_note' ? '-' : kind.includes('add') || kind === 'add_note' ? '+' : kind === 'context' || kind === 'filtered_context' ? ' ' : '';

function comparisonSettings() {
  return snapshot.comparison ?? {
    source: snapshot.source,
    target: snapshot.target,
    launchDirectory: '',
    configFile: '',
    canPersist: true,
  };
}

function sourceLabel() {
  return comparisonSettings().sourceLabel ?? 'source';
}

function targetLabel() {
  return comparisonSettings().targetLabel ?? 'target';
}

function sourceColumnLabel() {
  return comparisonSettings().sourceColumnLabel ?? sourceLabel().toUpperCase();
}

function targetColumnLabel() {
  return comparisonSettings().targetColumnLabel ?? targetLabel().toUpperCase();
}

function parentDirectory(path) {
  const normalized = String(path ?? '').replace(/[\\/]+$/, '');
  const slash = Math.max(normalized.lastIndexOf('/'), normalized.lastIndexOf('\\'));
  return slash > 0 ? normalized.slice(0, slash) : normalized;
}

function basename(path) {
  const normalized = String(path ?? '').replace(/[\\/]+$/, '');
  const slash = Math.max(normalized.lastIndexOf('/'), normalized.lastIndexOf('\\'));
  return slash >= 0 ? normalized.slice(slash + 1) : normalized;
}

function updateComparisonButton() {
  const comparison = comparisonSettings();
  const button = $('changeComparison');
  button.textContent = `${comparison.sourceLabel ?? basename(comparison.source)} → ${comparison.targetLabel ?? basename(comparison.target)} ▾`;
  button.title = [
    `Source: ${comparison.source}`,
    `Target: ${comparison.target}`,
    'Click to compare different repositories, environments, or directories.',
  ].join('\n');
}

function openComparisonModal() {
  const comparison = comparisonSettings();
  $('sourceDirectory').value = comparison.source ?? snapshot.source ?? '';
  $('targetDirectory').value = comparison.target ?? snapshot.target ?? '';
  $('sourceRepositoryRoot').value = parentDirectory($('sourceDirectory').value);
  $('targetRepositoryRoot').value = parentDirectory($('targetDirectory').value);
  $('sourceSideLabel').textContent = comparison.sourceLabel ?? basename(comparison.source);
  $('targetSideLabel').textContent = comparison.targetLabel ?? basename(comparison.target);
  $('persistComparison').checked = false;
  $('persistComparison').disabled = !comparison.canPersist;
  $('persistComparison').closest('label').title = comparison.canPersist
    ? `Optionally save to ${comparison.configFile}`
    : 'Unavailable while the workbench is running in dry-run mode';
  $('comparisonError').textContent = '';
  $('comparisonPreview').hidden = true;
  $('folderBrowser').hidden = true;
  $('comparisonModal').hidden = false;
  loadEnvironments('source', basename($('sourceDirectory').value));
  loadEnvironments('target', basename($('targetDirectory').value));
  $('sourceRepositoryRoot').focus();
  $('sourceRepositoryRoot').select();
}

function closeComparisonModal() {
  $('comparisonModal').hidden = true;
  $('folderBrowser').hidden = true;
  browseInput = null;
  browseAfterSelect = null;
}

function environmentElements(side) {
  const prefix = side === 'source' ? 'source' : 'target';
  return {
    root: $(`${prefix}RepositoryRoot`),
    select: $(`${prefix}Environment`),
    directory: $(`${prefix}Directory`),
    status: $(`${prefix}EnvironmentStatus`),
    sideLabel: $(`${prefix}SideLabel`),
  };
}

async function loadEnvironments(side, preferred = '') {
  const elements = environmentElements(side);
  const root = elements.root.value.trim();
  elements.select.replaceChildren();
  const loading = document.createElement('option');
  loading.textContent = 'Loading environments…';
  loading.value = '';
  elements.select.append(loading);
  elements.select.disabled = true;
  elements.status.textContent = '';
  if (!root) {
    loading.textContent = 'Choose a repository or parent directory';
    return;
  }
  try {
    const response = await fetch(`environments?root=${encodeURIComponent(root)}`, {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {'Accept': 'application/json'},
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Environment request failed (${response.status})`);
    elements.root.value = payload.root;
    elements.select.replaceChildren();
    for (const environment of payload.environments ?? []) {
      const option = document.createElement('option');
      option.value = environment.path;
      option.textContent = `${environment.name} (${environment.yamlFiles} YAML)`;
      option.dataset.name = environment.name;
      elements.select.append(option);
    }
    if (!elements.select.options.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = 'No environment folders found — use exact directory';
      elements.select.append(option);
      elements.status.textContent = 'No direct child folder with YAML files was found.';
      return;
    }
    elements.select.disabled = false;
    const wanted = [...elements.select.options].find(option => {
      return option.dataset.name?.toLowerCase() === String(preferred).toLowerCase();
    });
    if (wanted) elements.select.value = wanted.value;
    else if (elements.directory.value) {
      const exact = [...elements.select.options].find(option => option.value === elements.directory.value);
      if (exact) elements.select.value = exact.value;
    }
    if (!elements.select.value) elements.select.selectedIndex = 0;
    selectEnvironment(side);
    elements.status.textContent = `${payload.environments.length} environment folder${payload.environments.length === 1 ? '' : 's'} found in ${payload.repository}.`;
    if (payload.truncated) elements.status.textContent += ' Results were limited.';
  } catch (error) {
    elements.select.replaceChildren();
    const option = document.createElement('option');
    option.value = '';
    option.textContent = 'Could not load environments';
    elements.select.append(option);
    elements.status.textContent = error.message;
  }
}

function selectEnvironment(side) {
  const elements = environmentElements(side);
  const selectedOption = elements.select.selectedOptions[0];
  if (!selectedOption?.value) return;
  elements.directory.value = selectedOption.value;
  elements.sideLabel.textContent = selectedOption.dataset.name ?? basename(selectedOption.value);
  $('comparisonPreview').hidden = true;
}

function contextMatches(ids = []) {
  return ids.map(id => contextById.get(id)).filter(Boolean);
}

function selectContextEntry(entryId) {
  const entry = contextById.get(entryId);
  if (!entry) return;
  selectedContextId = entryId;
  $('contextList').querySelectorAll('.dictionary-item').forEach(button => {
    button.classList.toggle('active', button.dataset.entryId === entryId);
  });
  renderContextDetails(entry);
}

function renderContextDetails(entry) {
  const host = $('contextDetails');
  host.replaceChildren();
  if (!entry) {
    const empty = document.createElement('div');
    empty.className = 'dictionary-empty';
    empty.textContent = 'No matching dictionary entries.';
    host.append(empty);
    return;
  }
  const title = document.createElement('h3');
  title.textContent = entry.title;
  const category = document.createElement('div');
  category.className = 'dictionary-detail-category';
  category.textContent = entry.category;
  const summary = document.createElement('p');
  summary.className = 'dictionary-detail-summary';
  summary.textContent = entry.summary;
  host.append(title, category, summary);
  if (entry.details) {
    const details = document.createElement('div');
    details.className = 'dictionary-detail-more';
    details.textContent = entry.details;
    host.append(details);
  }
  if (entry.aliases?.length) {
    const aliases = document.createElement('div');
    aliases.className = 'dictionary-aliases';
    aliases.textContent = `Also recognized as: ${entry.aliases.join(', ')}`;
    host.append(aliases);
  }
  if (entry.matches?.length) {
    const matches = document.createElement('section');
    matches.className = 'dictionary-matches';
    const heading = document.createElement('h4');
    heading.textContent = 'Matching rules';
    matches.append(heading);
    for (const rule of entry.matches) {
      const row = document.createElement('div');
      row.className = 'dictionary-match';
      const fileScope = rule.files?.length ? ` · files: ${rule.files.join(', ')}` : '';
      row.textContent = `${rule.type}: ${rule.value}${fileScope}`;
      matches.append(row);
    }
    host.append(matches);
  }
  const source = document.createElement('div');
  source.className = 'dictionary-source';
  source.textContent = entry.source === 'built-in'
    ? 'Source: built-in Config Review context catalog'
    : `Source: ${entry.source}`;
  host.append(source);
  if (contextEditable) {
    const actions = document.createElement('div');
    actions.className = 'dictionary-detail-actions';
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.textContent = entry.source === 'built-in' ? 'Override definition' : 'Edit definition';
    edit.onclick = () => openContextEditor({entry});
    actions.append(edit);
    host.append(actions);
  }
}

function filteredContextEntries() {
  const query = $('contextSearch').value.trim().toLowerCase();
  if (!query) return contextEntries.slice();
  return contextEntries.filter(entry => {
    const searchText = [
      entry.title,
      entry.category,
      entry.summary,
      entry.details,
      ...(entry.aliases ?? []),
    ].join('\n').toLowerCase();
    return searchText.includes(query);
  });
}

function renderContextDictionary() {
  const entries = filteredContextEntries();
  $('contextCount').textContent = `${entries.length} of ${contextEntries.length}`;
  $('contextDiagnostics').textContent = (snapshot.contextCatalog?.diagnostics ?? []).join('\n');
  if (!entries.some(entry => entry.id === selectedContextId)) {
    selectedContextId = entries[0]?.id ?? null;
  }
  const host = $('contextList');
  host.replaceChildren();
  let previousCategory = null;
  for (const entry of entries) {
    if (entry.category !== previousCategory) {
      previousCategory = entry.category;
      const label = document.createElement('div');
      label.className = 'dictionary-category';
      label.textContent = entry.category;
      host.append(label);
    }
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'dictionary-item' + (entry.id === selectedContextId ? ' active' : '');
    button.dataset.entryId = entry.id;
    button.onclick = () => selectContextEntry(entry.id);
    const title = document.createElement('span');
    title.className = 'dictionary-item-title';
    title.textContent = entry.title;
    const summary = document.createElement('span');
    summary.className = 'dictionary-item-summary';
    summary.textContent = entry.summary;
    button.append(title, summary);
    host.append(button);
  }
  if (!entries.length) {
    const empty = document.createElement('div');
    empty.className = 'dictionary-empty';
    empty.textContent = 'No dictionary entries match this search.';
    host.append(empty);
  }
  const selectedEntry = entries.find(entry => entry.id === selectedContextId) ?? null;
  renderContextDetails(selectedEntry);
}

function openContextModal(entryId = null) {
  if (entryId && contextById.has(entryId)) selectedContextId = entryId;
  $('contextSearch').value = '';
  renderContextDictionary();
  $('contextModal').hidden = false;
  if (entryId) {
    $('contextList').querySelector('.dictionary-item.active')?.scrollIntoView({block: 'nearest'});
  } else {
    $('contextSearch').focus();
  }
}

function closeContextModal() {
  $('contextModal').hidden = true;
}

function contextSlug(value) {
  const slug = String(value ?? '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return slug || `context-${Date.now()}`;
}

function populateContextCategories(selectedCategory = 'Project Context') {
  const select = $('contextEntryCategory');
  select.replaceChildren();
  const categories = contextCategories.includes(selectedCategory)
    ? contextCategories
    : [...contextCategories, selectedCategory].filter(Boolean);
  for (const category of categories) {
    const option = document.createElement('option');
    option.value = category;
    option.textContent = category;
    select.append(option);
  }
  const createOption = document.createElement('option');
  createOption.value = '__new__';
  createOption.textContent = '+ Create new category…';
  select.append(createOption);
  select.value = categories.includes(selectedCategory) ? selectedCategory : 'Project Context';
  $('contextEntryNewCategory').hidden = true;
  $('contextEntryNewCategory').value = '';
}

function updateContextCategoryEditor() {
  const creating = $('contextEntryCategory').value === '__new__';
  $('contextEntryNewCategory').hidden = !creating;
  if (creating) $('contextEntryNewCategory').focus();
}

function currentContextFilePath(match = null) {
  return match?.file || currentFile()?.path || '';
}

function updateContextFileScope({autofill = true} = {}) {
  const enabled = $('contextLimitFiles').checked;
  $('contextScopeRow').hidden = !enabled;
  $('contextMatchFiles').disabled = !enabled;
  $('browseContextFiles').disabled = !enabled;
  if (enabled && autofill && !$('contextMatchFiles').value.trim()) {
    $('contextMatchFiles').value = $('contextMatchFiles').dataset.suggestedFile
      || currentFile()?.path
      || '';
  }
}

function filteredContextFiles() {
  const query = $('contextFileSearch').value.trim().toLowerCase();
  return snapshot.files.filter(file => !query || file.path.toLowerCase().includes(query));
}

function chooseContextFile(path) {
  $('contextMatchFiles').value = path;
  closeContextFilePicker();
}

function renderContextFilePicker() {
  const files = filteredContextFiles();
  $('contextFileCount').textContent = `${files.length} of ${snapshot.files.length}`;
  const host = $('contextFilePickerList');
  host.replaceChildren();
  for (const file of files) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'context-file-picker-item';
    button.textContent = file.path;
    button.onclick = () => chooseContextFile(file.path);
    host.append(button);
  }
  if (!files.length) {
    const empty = document.createElement('div');
    empty.className = 'dictionary-empty';
    empty.textContent = 'No changed files match this search.';
    host.append(empty);
  }
}

function openContextFilePicker() {
  if (!$('contextLimitFiles').checked) return;
  $('contextFileSearch').value = '';
  renderContextFilePicker();
  $('contextFilePickerModal').hidden = false;
  $('contextFileSearch').focus();
}

function closeContextFilePicker() {
  $('contextFilePickerModal').hidden = true;
}

function openContextEditor({entry = null, suggestion = null} = {}) {
  if (!contextEditable) {
    setStatus('Context definitions cannot be edited in dry-run mode.', 'error');
    return;
  }
  editingContextEntry = entry;
  const match = suggestion ?? entry?.matches?.[0] ?? {
    type: 'term',
    value: '',
    files: [],
    title: '',
  };
  const titleValue = entry?.title ?? match.title ?? match.value ?? '';
  $('contextEditorTitle').textContent = entry
    ? (entry.source === 'built-in' ? 'Override context definition' : 'Edit context definition')
    : 'Add context definition';
  $('contextEntryId').value = entry?.id ?? contextSlug(match.value || titleValue);
  $('contextEntryId').readOnly = Boolean(entry);
  populateContextCategories(entry?.category ?? 'Project Context');
  $('contextEntryTitle').value = titleValue;
  $('contextEntrySummary').value = entry?.summary ?? '';
  $('contextEntryDetails').value = entry?.details ?? '';
  $('contextEntryAliases').value = (entry?.aliases ?? []).join(', ');
  $('contextMatchType').value = match.type ?? 'term';
  $('contextMatchValue').value = match.value ?? '';
  const scopedFiles = entry ? (match.files ?? []) : [];
  $('contextLimitFiles').checked = scopedFiles.length > 0;
  $('contextMatchFiles').value = scopedFiles.join(', ');
  $('contextMatchFiles').dataset.suggestedFile = currentContextFilePath(match)
    || match.files?.[0]
    || '';
  updateContextFileScope({autofill: false});
  const matchContext = [
    match.clickedType ? `Type: ${match.clickedType}` : '',
    match.clickedValue ? `Value: ${match.clickedValue}` : '',
    match.yamlPath ? `YAML path: ${match.yamlPath}` : '',
    match.file ? `File: ${match.file}` : '',
  ].filter(Boolean);
  $('contextMatchContext').textContent = matchContext.join(' · ');
  $('contextEditFile').textContent = contextEditFilePath;
  $('contextEditorError').textContent = '';
  $('contextModal').hidden = true;
  $('contextEditorModal').hidden = false;
  $('contextEntryTitle').focus();
}

function closeContextEditor() {
  $('contextEditorModal').hidden = true;
  closeContextFilePicker();
  editingContextEntry = null;
}

function contextEditorEntryPayload() {
  const category = $('contextEntryCategory').value === '__new__'
    ? $('contextEntryNewCategory').value.trim()
    : $('contextEntryCategory').value.trim();
  const files = $('contextLimitFiles').checked
    ? $('contextMatchFiles').value
      .split(',')
      .map(value => value.trim())
      .filter(Boolean)
    : [];
  const primaryRule = {
    type: $('contextMatchType').value,
    value: $('contextMatchValue').value.trim(),
    files,
  };
  const preservedRules = editingContextEntry?.matches?.slice(1) ?? [];
  return {
    id: $('contextEntryId').value.trim(),
    title: $('contextEntryTitle').value.trim(),
    category,
    summary: $('contextEntrySummary').value.trim(),
    details: $('contextEntryDetails').value.trim(),
    aliases: $('contextEntryAliases').value
      .split(',')
      .map(value => value.trim())
      .filter(Boolean),
    matches: [primaryRule, ...preservedRules],
  };
}

async function saveContextEntry() {
  const entry = contextEditorEntryPayload();
  if (!entry.id || !entry.title || !entry.category || !entry.summary) {
    $('contextEditorError').textContent = 'ID, title, category, and short definition are required.';
    return;
  }
  if (!entry.matches[0].value) {
    $('contextEditorError').textContent = 'A match value is required.';
    return;
  }
  if (notesDirty && !window.confirm('Saving this definition reloads the comparison and clears unsaved browser notes. Continue?')) {
    return;
  }
  const button = $('saveContextEntry');
  button.disabled = true;
  button.textContent = 'Saving…';
  $('contextEditorError').textContent = '';
  try {
    const response = await fetch('context-entry', {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
      body: JSON.stringify({entry}),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Definition request failed (${response.status})`);
    notesDirty = false;
    window.location.reload();
  } catch (error) {
    $('contextEditorError').textContent = error.message;
    button.disabled = false;
    button.textContent = 'Save definition';
  }
}

function hideContextTooltip() {
  if (contextTooltipTimer) window.clearTimeout(contextTooltipTimer);
  contextTooltipTimer = null;
  $('contextTooltip').hidden = true;
}

function scheduleContextTooltipHide() {
  if (contextTooltipTimer) window.clearTimeout(contextTooltipTimer);
  contextTooltipTimer = window.setTimeout(hideContextTooltip, 140);
}

function positionContextTooltip(anchor) {
  const tooltip = $('contextTooltip');
  const rect = anchor.getBoundingClientRect();
  const gap = 8;
  tooltip.style.left = `${Math.max(12, Math.min(window.innerWidth - tooltip.offsetWidth - 12, rect.left))}px`;
  const below = rect.bottom + gap;
  const above = rect.top - tooltip.offsetHeight - gap;
  tooltip.style.top = `${below + tooltip.offsetHeight <= window.innerHeight - 12 ? below : Math.max(12, above)}px`;
}

function showContextTooltip(anchor, ids) {
  if (privacyMode) return;
  if (contextTooltipTimer) window.clearTimeout(contextTooltipTimer);
  const entries = contextMatches(ids);
  if (!entries.length) return;
  const tooltip = $('contextTooltip');
  tooltip.replaceChildren();
  for (const entry of entries) {
    const section = document.createElement('section');
    section.className = 'context-tooltip-entry';
    const title = document.createElement('div');
    title.className = 'context-tooltip-title';
    title.textContent = entry.title;
    const category = document.createElement('div');
    category.className = 'context-tooltip-category';
    category.textContent = entry.category;
    const summary = document.createElement('div');
    summary.className = 'context-tooltip-summary';
    summary.textContent = entry.summary;
    section.append(title, category, summary);
    tooltip.append(section);
  }
  const hint = document.createElement('div');
  hint.className = 'context-tooltip-hint';
  hint.textContent = 'Click this item to open the full dictionary entry.';
  tooltip.append(hint);
  tooltip.hidden = false;
  positionContextTooltip(anchor);
}

function showMissingContextTooltip(anchor, suggestion) {
  if (privacyMode || !contextEditable || !suggestion) return;
  if (contextTooltipTimer) window.clearTimeout(contextTooltipTimer);
  const tooltip = $('contextTooltip');
  tooltip.replaceChildren();
  const title = document.createElement('div');
  title.className = 'context-tooltip-title';
  title.textContent = suggestion.title || suggestion.value || 'Undocumented item';
  const summary = document.createElement('div');
  summary.className = 'context-tooltip-summary context-tooltip-missing';
  summary.textContent = 'No context definition exists yet.';
  const hint = document.createElement('div');
  hint.className = 'context-tooltip-hint';
  hint.textContent = 'Click to add a project definition.';
  tooltip.append(title, summary, hint);
  tooltip.hidden = false;
  positionContextTooltip(anchor);
}

function contextIds(ids = []) {
  return ids.filter(id => contextById.has(id));
}

function decorateContextTarget(element, ids, suggestion = null) {
  const available = contextIds(ids);
  const canAdd = !available.length && contextEditable && Boolean(suggestion);
  if (!available.length && !canAdd) return;
  element.classList.add(available.length ? 'context-available' : 'context-missing');
  element.tabIndex = contextHelpMode ? 0 : -1;
  const show = () => {
    if (!contextHelpMode) return;
    if (available.length) showContextTooltip(element, available);
    else showMissingContextTooltip(element, suggestion);
  };
  element.addEventListener('mouseenter', show);
  element.addEventListener('mouseleave', scheduleContextTooltipHide);
  element.addEventListener('focus', show);
  element.addEventListener('blur', scheduleContextTooltipHide);
  element.addEventListener('click', event => {
    if (!contextHelpMode) return;
    event.stopPropagation();
    hideContextTooltip();
    if (available.length) openContextModal(available[0]);
    else openContextEditor({suggestion});
  });
}

function applyContextHelpMode() {
  const button = $('contextHelp');
  const enabled = contextHelpMode && !privacyMode;
  document.body.classList.toggle('context-help-mode', enabled);
  button.classList.toggle('active', enabled);
  button.setAttribute('aria-pressed', enabled ? 'true' : 'false');
  button.textContent = '?';
  button.title = privacyMode
    ? 'Context help is unavailable while privacy mode is active'
    : enabled
      ? 'Turn context help off'
      : 'Turn context help on';
  button.disabled = privacyMode;
  document.querySelectorAll('.context-available, .context-missing').forEach(element => {
    element.tabIndex = enabled ? 0 : -1;
  });
  if (!enabled) hideContextTooltip();
}

function toggleContextHelpMode() {
  if (privacyMode) return;
  contextHelpMode = !contextHelpMode;
  applyContextHelpMode();
  setStatus(
    contextHelpMode
      ? 'CONTEXT HELP ON · hover highlighted keys, values, and path terms · click for full details'
      : defaultStatus(),
    contextHelpMode ? 'busy' : '',
  );
}

async function loadDirectory(path = '') {
  const host = $('folderList');
  host.replaceChildren();
  const loading = document.createElement('div');
  loading.className = 'folder-empty';
  loading.textContent = 'Loading folders…';
  host.append(loading);
  $('comparisonError').textContent = '';
  try {
    const query = path ? `?path=${encodeURIComponent(path)}` : '';
    const response = await fetch(`directories${query}`, {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {'Accept': 'application/json'},
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Directory request failed (${response.status})`);
    browsePath = payload.path;
    browseParent = payload.parent;
    $('folderLocation').textContent = payload.path;
    $('folderLocation').title = payload.path;
    $('folderUp').disabled = payload.parent === payload.path;
    $('folderTruncated').hidden = !payload.truncated;

    const shortcutHost = $('folderShortcuts');
    shortcutHost.replaceChildren();
    for (const shortcut of payload.shortcuts ?? []) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'shortcut-button';
      button.textContent = shortcut.label;
      button.title = shortcut.path;
      button.onclick = () => loadDirectory(shortcut.path);
      shortcutHost.append(button);
    }

    host.replaceChildren();
    for (const directory of payload.directories ?? []) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'folder-row';
      button.textContent = directory.name;
      button.title = directory.path;
      button.onclick = () => loadDirectory(directory.path);
      host.append(button);
    }
    if (!host.childElementCount) {
      const empty = document.createElement('div');
      empty.className = 'folder-empty';
      empty.textContent = 'No child folders.';
      host.append(empty);
    }
  } catch (error) {
    host.replaceChildren();
    const failed = document.createElement('div');
    failed.className = 'folder-empty';
    failed.textContent = error.message;
    host.append(failed);
  }
}

function browseFor(input, afterSelect = null) {
  browseInput = input;
  browseAfterSelect = afterSelect;
  $('folderBrowser').hidden = false;
  loadDirectory(input.value.trim());
}

function renderComparisonPreview(payload) {
  const host = $('comparisonPreviewGrid');
  host.replaceChildren();
  $('comparisonPreviewTitle').textContent = `${payload.sourceLabel} → ${payload.targetLabel}`;
  for (const [label, value] of [
    ['Different', payload.differentFiles],
    ['Modified', payload.modifiedFiles],
    [`Only in ${payload.sourceLabel}`, payload.sourceOnlyFiles],
    [`Only in ${payload.targetLabel}`, payload.targetOnlyFiles],
    ['Matched', payload.matchedFiles],
    ['Identical', payload.identicalFiles],
  ]) {
    const item = document.createElement('div');
    const strong = document.createElement('strong');
    strong.textContent = String(value);
    item.append(strong, label);
    host.append(item);
  }
  $('comparisonPreview').hidden = false;
}

async function previewComparison() {
  const source = $('sourceDirectory').value.trim();
  const target = $('targetDirectory').value.trim();
  if (!source || !target) {
    $('comparisonError').textContent = 'Choose both a source directory and a target directory.';
    return;
  }
  const button = $('previewComparison');
  button.disabled = true;
  button.textContent = 'Checking files…';
  $('comparisonError').textContent = '';
  try {
    const response = await fetch('comparison-preview', {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
      body: JSON.stringify({source, target}),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Preview request failed (${response.status})`);
    renderComparisonPreview(payload);
  } catch (error) {
    $('comparisonPreview').hidden = true;
    $('comparisonError').textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = 'Preview files';
  }
}

async function applyComparison() {
  const source = $('sourceDirectory').value.trim();
  const target = $('targetDirectory').value.trim();
  if (!source || !target) {
    $('comparisonError').textContent = 'Choose both a source directory and a target directory.';
    return;
  }
  if (notesDirty && !window.confirm('Changing comparisons clears unsaved browser notes and temporary review state. Continue?')) return;

  const button = $('applyComparison');
  button.disabled = true;
  button.textContent = 'Building comparison…';
  $('comparisonError').textContent = '';
  try {
    const response = await fetch('comparison', {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
      body: JSON.stringify({
        source,
        target,
        persist: $('persistComparison').checked,
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Comparison request failed (${response.status})`);
    notesDirty = false;
    window.location.reload();
  } catch (error) {
    $('comparisonError').textContent = error.message;
    button.disabled = false;
    button.textContent = 'Start comparison';
  }
}

function applyTheme() {
  const resolved = themeChoice === 'system' ? (systemTheme.matches ? 'light' : 'dark') : themeChoice;
  document.documentElement.dataset.theme = resolved;
  document.querySelectorAll('[data-theme-choice]').forEach(button => {
    button.classList.toggle('active', button.dataset.themeChoice === themeChoice);
  });
}

systemTheme.addEventListener?.('change', () => {
  if (themeChoice === 'system') applyTheme();
});

function setStatus(message, kind = '') {
  const footer = $('footer');
  footer.textContent = message;
  footer.className = 'footer' + (kind ? ` ${kind}` : '');
}

function defaultStatus() {
  if (privacyMode) {
    return 'PRIVACY MODE · display and exports are redacted · original values remain inside this local page';
  }
  if (contextHelpMode) {
    return 'CONTEXT HELP ON · hover highlighted keys, values, and path terms · click for full details';
  }
  return `Snapshot ${snapshot.generatedAt} · ${snapshot.gitStatus} · ${snapshot.gitLinks.status} · review state is temporary until exported`;
}

function displayFilePath(file) {
  if (!file) return '';
  return privacyMode ? (file.privatePath ?? '[REDACTED FILE]') : file.path;
}

function displayLineText(line) {
  return privacyMode ? (line.privateText ?? '[REDACTED]') : line.text;
}

function displayChangeLabel(change) {
  return privacyMode ? (change.privateLabel ?? '[REDACTED CHANGE]') : change.label;
}

function appendHighlightedText(host, text, ranges = []) {
  let cursor = 0;
  for (const item of ranges) {
    const start = Math.max(cursor, Math.min(text.length, Number(item?.[0] ?? 0)));
    const end = Math.max(start, Math.min(text.length, Number(item?.[1] ?? start)));
    if (start > cursor) host.append(document.createTextNode(text.slice(cursor, start)));
    if (end > start) {
      const strong = document.createElement('strong');
      strong.className = 'intraline';
      strong.textContent = text.slice(start, end);
      host.append(strong);
    }
    cursor = end;
  }
  if (cursor < text.length) host.append(document.createTextNode(text.slice(cursor)));
}

function appendHighlightedSlice(host, text, ranges, start, end) {
  const adjusted = [];
  for (const item of ranges ?? []) {
    const rangeStart = Math.max(start, Number(item?.[0] ?? 0));
    const rangeEnd = Math.min(end, Number(item?.[1] ?? 0));
    if (rangeEnd > rangeStart) adjusted.push([rangeStart - start, rangeEnd - start]);
  }
  appendHighlightedText(host, text.slice(start, end), adjusted);
}

function appendContextualText(host, text, ranges = [], targets = []) {
  const ordered = (targets ?? [])
    .map(target => ({
      ...target,
      start: Math.max(0, Math.min(text.length, Number(target?.start ?? 0))),
      end: Math.max(0, Math.min(text.length, Number(target?.end ?? 0))),
    }))
    .filter(target => target.end > target.start)
    .sort((left, right) => left.start - right.start || right.end - left.end);
  let cursor = 0;
  for (const target of ordered) {
    if (target.start < cursor) continue;
    if (target.start > cursor) appendHighlightedSlice(host, text, ranges, cursor, target.start);
    const token = document.createElement('span');
    token.className = 'context-token';
    appendHighlightedSlice(token, text, ranges, target.start, target.end);
    decorateContextTarget(
      token,
      target.contextRefs ?? [],
      target.contextSuggestion ?? null,
    );
    host.append(token);
    cursor = target.end;
  }
  if (cursor < text.length) appendHighlightedSlice(host, text, ranges, cursor, text.length);
}


function lineNumberElement(value, baseUrl, label) {
  const cell = document.createElement('div');
  cell.className = 'ln';
  if (value == null || value === '') return cell;
  if (!baseUrl) {
    cell.textContent = value;
    return cell;
  }
  const link = document.createElement('a');
  link.href = `${baseUrl}#L${value}`;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.textContent = value;
  link.title = `Open ${label} line ${value} at ${snapshot.gitLinks.commit.slice(0, 12)} · ${snapshot.gitLinks.status}`;
  cell.append(link);
  return cell;
}

function lineElement(line) {
  const row = document.createElement('div');
  row.className = 'line ' + line.kind;
  row.dataset.testLine = line.testLine ?? '';
  row.dataset.devLine = line.devLine ?? '';
  row.dataset.copyPrefix = prefixFor(line.kind);
  row.dataset.copyText = displayLineText(line);
  const file = currentFile();
  const tl = lineNumberElement(
    line.testLine,
    privacyMode ? null : file?.remote?.testFileUrl,
    targetColumnLabel(),
  );
  const dl = lineNumberElement(
    line.devLine,
    privacyMode ? null : file?.remote?.devFileUrl,
    sourceColumnLabel(),
  );
  const prefix = document.createElement('div');
  prefix.className = 'prefix';
  prefix.textContent = prefixFor(line.kind);
  const code = document.createElement('div');
  code.className = 'code';
  const shownText = displayLineText(line);
  if (privacyMode) {
    appendHighlightedText(code, shownText, []);
  } else {
    appendContextualText(
      code,
      shownText,
      line.emphasisRanges ?? [],
      line.contextTargets ?? [],
    );
  }
  row.append(tl, dl, prefix, code);
  return row;
}

function treeFrom(files) {
  const root = {folders: new Map(), files: []};
  for (const file of files) {
    const parts = displayFilePath(file).split('/');
    let node = root;
    for (const part of parts.slice(0, -1)) {
      if (!node.folders.has(part)) node.folders.set(part, {folders: new Map(), files: []});
      node = node.folders.get(part);
    }
    node.files.push({file, name: parts.at(-1)});
  }
  return root;
}

function allReviewChanges(view) {
  return [...(view?.changes ?? []), ...(view?.hiddenChanges ?? [])];
}

function fileHasNotes(file) {
  for (const viewName of ['focused', 'raw']) {
    for (const change of allReviewChanges(file[viewName])) {
      if ((notesByChange.get(change.key) ?? '').trim()) return true;
    }
  }
  return false;
}

function fileIsActive(file) {
  return !hiddenFiles.has(file.path) && !reviewedFiles.has(file.path);
}

function currentFile() {
  return snapshot.files[selected] ?? null;
}

function selectFile(file) {
  if (!file) return;
  const previousPath = currentFile()?.path ?? null;
  selected = snapshot.files.indexOf(file);
  if (previousPath !== file.path) {
    gapStateById.clear();
  }
  render();
}

function activeFilesMatchingSearch() {
  const query = $('search').value.trim().toLowerCase();
  return snapshot.files.filter(file => {
    return fileIsActive(file) && displayFilePath(file).toLowerCase().includes(query);
  });
}

function nextActiveAfter(file) {
  const active = snapshot.files.filter(fileIsActive);
  if (!active.length) return null;
  const originalIndex = snapshot.files.indexOf(file);
  return active.find(item => snapshot.files.indexOf(item) > originalIndex) ?? active[0];
}

function renderNode(node, host) {
  for (const [name, child] of [...node.folders].sort((a, b) => a[0].localeCompare(b[0]))) {
    const details = document.createElement('details');
    details.open = true;
    const summary = document.createElement('summary');
    summary.textContent = '▾ ' + name;
    details.append(summary);
    const children = document.createElement('div');
    children.className = 'children';
    renderNode(child, children);
    details.append(children);
    host.append(details);
  }
  for (const item of node.files.sort((a, b) => a.name.localeCompare(b.name))) {
    const button = document.createElement('button');
    button.className = 'file' + (snapshot.files[selected] === item.file ? ' active' : '');
    button.title = displayFilePath(item.file);
    button.onclick = () => selectFile(item.file);
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = item.name;
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = item.file.focused.visibleChanges;
    button.append(name, badge);
    if (fileHasNotes(item.file)) {
      const noteBadge = document.createElement('span');
      noteBadge.className = 'badge notes';
      noteBadge.textContent = 'note';
      button.append(noteBadge);
    }
    if (reviewedFiles.has(item.file.path)) {
      const reviewedBadge = document.createElement('span');
      reviewedBadge.className = 'badge reviewed';
      reviewedBadge.textContent = 'reviewed';
      button.append(reviewedBadge);
    }
    if (hiddenFiles.has(item.file.path)) {
      const hiddenBadge = document.createElement('span');
      hiddenBadge.className = 'badge hidden';
      hiddenBadge.textContent = 'hidden';
      button.append(hiddenBadge);
    }
    host.append(button);
  }
}

function fileStateRow(file, actions) {
  const row = document.createElement('div');
  row.className = 'file-state-row';
  const name = document.createElement('div');
  name.className = 'file-state-name';
  name.textContent = displayFilePath(file);
  name.title = displayFilePath(file);
  const actionHost = document.createElement('div');
  actionHost.className = 'file-state-actions';
  for (const action of actions) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = action.label;
    button.onclick = action.run;
    actionHost.append(button);
  }
  row.append(name, actionHost);
  return row;
}

function renderFileStateList(host, files, rowFactory, emptyText) {
  host.replaceChildren();
  if (!files.length) {
    const empty = document.createElement('div');
    empty.className = 'file-state-empty';
    empty.textContent = emptyText;
    host.append(empty);
    return;
  }
  files.forEach(file => host.append(rowFactory(file)));
}

function renderReviewMenu() {
  const remaining = snapshot.files.filter(fileIsActive).length;
  const summary = $('reviewSummary');
  summary.replaceChildren();
  for (const [label, value] of [
    ['Remaining', remaining],
    ['Reviewed', reviewedFiles.size],
    ['Hidden', hiddenFiles.size],
  ]) {
    const item = document.createElement('div');
    const strong = document.createElement('strong');
    strong.textContent = String(value);
    item.append(strong, document.createElement('br'), label);
    summary.append(item);
  }

  const hidden = snapshot.files.filter(file => hiddenFiles.has(file.path));
  renderFileStateList(
    $('hiddenFileList'),
    hidden,
    file => fileStateRow(file, [{
      label: 'Show',
      run: () => {
        hiddenFiles.delete(file.path);
        selectFile(file);
        setStatus(`Restored hidden file: ${displayFilePath(file)}`, 'success');
      },
    }]),
    'No hidden files.',
  );

  const reviewed = snapshot.files.filter(file => reviewedFiles.has(file.path));
  renderFileStateList(
    $('reviewedFileList'),
    reviewed,
    file => fileStateRow(file, [
      {label: 'Open', run: () => selectFile(file)},
      {
        label: 'Unreview',
        run: () => {
          reviewedFiles.delete(file.path);
          reviewedAtByFile.delete(file.path);
          selectFile(file);
          setStatus(`Marked unreviewed: ${displayFilePath(file)}`, 'success');
        },
      },
    ]),
    'No reviewed files.',
  );

  $('restoreHidden').disabled = hiddenFiles.size === 0;
  $('saveReviewed').disabled = reviewedFiles.size === 0;
  $('printReviewed').disabled = reviewedFiles.size === 0;
}

function renderTree() {
  const host = $('tree');
  host.replaceChildren();
  renderNode(treeFrom(visible), host);
  const noteCount = [...notesByChange.values()].filter(value => value.trim()).length;
  const remaining = snapshot.files.filter(fileIsActive).length;
  $('fileCount').textContent = `${remaining} remaining · ${reviewedFiles.size} reviewed · ${hiddenFiles.size} hidden · ${noteCount} note${noteCount === 1 ? '' : 's'}`;
  renderReviewMenu();
}

async function getGitContext(change) {
  if (gitContextCache.has(change.gitContextId)) return gitContextCache.get(change.gitContextId);
  const promise = fetch(`git/${encodeURIComponent(change.gitContextId)}`, {
    credentials: 'same-origin',
    cache: 'no-store',
    headers: {'Accept': 'application/json'},
  }).then(async response => {
    if (!response.ok) throw new Error(`Git context request failed (${response.status})`);
    return response.json();
  }).catch(error => ({dev: [], test: [], error: error.message}));
  gitContextCache.set(change.gitContextId, promise);
  return promise;
}

async function getGapContext(gap, count) {
  const response = await fetch(
    `context/${encodeURIComponent(gap.id)}?count=${count}&edge=${encodeURIComponent(gap.edge)}`,
    {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {'Accept': 'application/json'},
    },
  );
  if (!response.ok) throw new Error(`Inline context request failed (${response.status})`);
  return response.json();
}

function contextGapElement(gap, position) {
  const host = document.createElement('div');
  host.className = 'context-gap';
  host.dataset.gapId = gap.id;
  const linesHost = document.createElement('div');
  linesHost.className = 'context-gap-lines';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'context-gap-button';

  async function renderCount(count) {
    button.disabled = true;
    button.textContent = 'Loading context…';
    try {
      const payload = await getGapContext(gap, count);
      gapStateById.set(gap.id, payload.count);
      linesHost.replaceChildren(...payload.lines.map(lineElement));
      if (payload.hasMore) {
        const remaining = payload.total - payload.count;
        button.disabled = false;
        button.textContent = `${position === 'before' ? '↑' : '↓'} Show 10 more lines (${remaining} hidden)`;
      } else {
        button.disabled = true;
        button.textContent = `All ${payload.total} omitted context line${payload.total === 1 ? '' : 's'} shown`;
      }
    } catch (error) {
      button.disabled = false;
      button.textContent = `Could not load context: ${error.message}`;
    }
  }

  button.onclick = () => {
    const previous = gapStateById.get(gap.id) ?? 0;
    renderCount(Math.min(gap.length, previous + 10));
  };
  if (position === 'before') host.append(button, linesHost);
  else host.append(linesHost, button);

  const shown = gapStateById.get(gap.id) ?? 0;
  if (shown > 0) {
    renderCount(shown);
  } else {
    button.textContent = `${position === 'before' ? '↑' : '↓'} Show 10 more lines (${gap.length} hidden)`;
  }
  return host;
}


function commitContextForSide(context, side) {
  const values = side === 'SOURCE' ? context.dev : context.test;
  return values?.[0] ?? null;
}

function lastChangedLineRow(change, side) {
  const isTest = side === 'TARGET';
  const start = Number(isTest ? change.testStart : change.devStart) + 1;
  const end = Number(isTest ? change.testEnd : change.devEnd);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  const attribute = isTest ? 'testLine' : 'devLine';
  const expectedKinds = isTest
    ? new Set(['remove', 'remove_note'])
    : new Set(['add', 'add_note']);
  let match = null;
  for (const row of $('diff').querySelectorAll('.line')) {
    if (![...expectedKinds].some(kind => row.classList.contains(kind))) continue;
    const value = Number(row.dataset[attribute]);
    if (Number.isFinite(value) && value >= start && value <= end) match = row;
  }
  return match;
}

function lineGitContextElement(side, context) {
  const host = document.createElement('div');
  host.className = 'line-git-context';
  host.dataset.side = side;
  const sideLabel = side === 'SOURCE' ? sourceColumnLabel() : targetColumnLabel();
  const commit = commitContextForSide(context, side);
  if (!commit) {
    host.classList.add('no-context');
    host.textContent = context.error || `No tracked ${sideLabel} commit context was available.`;
    return host;
  }

  const prefix = document.createElement('span');
  prefix.className = 'inline-git-prefix';
  prefix.textContent = `Last changed in ${sideLabel} · by `;
  const author = document.createElement('span');
  author.className = 'inline-git-author';
  author.textContent = commit.author || 'unknown author';
  const subject = document.createElement('span');
  subject.className = 'inline-git-subject';
  subject.textContent = ` · ${commit.subject || 'No commit subject'} · `;
  const hash = commit.url ? document.createElement('a') : document.createElement('span');
  hash.className = 'inline-git-hash';
  hash.textContent = commit.hash || '—';
  if (commit.url) {
    hash.href = commit.url;
    hash.target = '_blank';
    hash.rel = 'noopener noreferrer';
    hash.title = commit.linkKind === 'merge request'
      ? 'Open the related merge request for this commit'
      : 'Open the commit page; GitLab lists related merge requests when available';
  }
  host.append(prefix, author, subject, hash);
  if (context.error) host.title = context.error;
  return host;
}

async function renderInlineGitContext(change) {
  if (privacyMode || !inlineGitContextKeys.has(change.key)) return;
  const context = await getGitContext(change);
  if (privacyMode || !inlineGitContextKeys.has(change.key)) return;
  for (const side of ['TARGET', 'SOURCE']) {
    const row = lastChangedLineRow(change, side);
    if (!row) continue;
    row.querySelector(`:scope > .line-git-context[data-side="${side}"]`)?.remove();
    row.append(lineGitContextElement(side, context));
  }
}

function renderOpenGitContexts(view) {
  if (privacyMode) return;
  for (const change of allReviewChanges(view)) {
    if (inlineGitContextKeys.has(change.key)) renderInlineGitContext(change);
  }
}

function noteEditor(change) {
  const noteWrap = document.createElement('div');
  noteWrap.className = 'note-wrap';
  const noteLabel = document.createElement('label');
  noteLabel.className = 'note-label';
  const noteTitle = document.createElement('span');
  noteTitle.textContent = 'Deployment note';
  const noteHelp = document.createElement('span');
  noteHelp.className = 'note-help';
  noteHelp.textContent = 'kept in this browser until Save review';
  noteLabel.append(noteTitle, noteHelp);
  const textarea = document.createElement('textarea');
  textarea.className = 'review-note';
  textarea.placeholder = 'Add context, a question, or a deployment follow-up for this change…';
  textarea.value = notesByChange.get(change.key) ?? '';
  textarea.addEventListener('input', () => {
    notesByChange.set(change.key, textarea.value);
    notesDirty = true;
    renderTree();
    setStatus('Unsaved reviewer notes · use Save review… to export them', 'busy');
  });
  noteWrap.append(noteLabel, textarea);
  return noteWrap;
}

function noteButtonLabel(change) {
  if (noteEditorsOpen.has(change.key)) return 'Hide note';
  return (notesByChange.get(change.key) ?? '').trim() ? 'Edit note' : 'Add note';
}

function reviewActionRow(panel, change) {
  const actions = document.createElement('div');
  actions.className = 'review-actions';

  const noteButton = document.createElement('button');
  noteButton.type = 'button';
  noteButton.textContent = noteButtonLabel(change);
  noteButton.classList.toggle('active', noteEditorsOpen.has(change.key));
  noteButton.onclick = () => {
    const existing = panel.querySelector(':scope > .note-wrap');
    if (existing) {
      existing.remove();
      noteEditorsOpen.delete(change.key);
    } else {
      noteEditorsOpen.add(change.key);
      panel.insertBefore(noteEditor(change), actions);
      panel.querySelector(':scope > .note-wrap textarea')?.focus();
    }
    noteButton.textContent = noteButtonLabel(change);
    noteButton.classList.toggle('active', noteEditorsOpen.has(change.key));
  };

  const gitButton = document.createElement('button');
  gitButton.type = 'button';
  gitButton.textContent = inlineGitContextKeys.has(change.key)
    ? 'Hide Git context'
    : 'Add Git context';
  gitButton.classList.toggle('active', inlineGitContextKeys.has(change.key));
  gitButton.onclick = () => {
    if (inlineGitContextKeys.has(change.key)) {
      inlineGitContextKeys.delete(change.key);
      for (const side of ['TARGET', 'SOURCE']) {
        lastChangedLineRow(change, side)
          ?.querySelector(`:scope > .line-git-context[data-side="${side}"]`)
          ?.remove();
      }
    } else {
      inlineGitContextKeys.add(change.key);
      renderInlineGitContext(change);
    }
    gitButton.textContent = inlineGitContextKeys.has(change.key)
      ? 'Hide Git context'
      : 'Add Git context';
    gitButton.classList.toggle('active', inlineGitContextKeys.has(change.key));
  };

  actions.append(noteButton, gitButton);
  return actions;
}

function reviewPanel(change) {
  const panel = document.createElement('section');
  panel.className = 'review-panel';
  panel.dataset.changeKey = change.key;

  const heading = document.createElement('div');
  heading.className = 'review-heading';
  const label = document.createElement('span');
  label.className = 'review-label';
  label.textContent = displayChangeLabel(change);
  const ranges = document.createElement('span');
  ranges.className = 'review-ranges';
  ranges.textContent = `${targetColumnLabel()} ${change.testRange} → ${sourceColumnLabel()} ${change.devRange}`;
  const remoteLinks = document.createElement('span');
  remoteLinks.className = 'review-remote-links';
  for (const [side, url] of [[targetColumnLabel(), change.testRemoteUrl], [sourceColumnLabel(), change.devRemoteUrl]]) {
    if (privacyMode) continue;
    if (!url) continue;
    const link = document.createElement('a');
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = `${side} ↗`;
    link.title = `Open this ${side} change at the exact remote commit`;
    remoteLinks.append(link);
  }
  heading.append(label);
  if (remoteLinks.childElementCount) heading.append(remoteLinks);
  heading.append(ranges);

  panel.append(heading);
  if (change.splitPhysical) {
    const splitNotice = document.createElement('div');
    splitNotice.className = 'split-change-note';
    splitNotice.textContent = `This is one keyed-list value change. Its ${targetColumnLabel()} and ${sourceColumnLabel()} rows appear at separate physical YAML positions.`;
    panel.append(splitNotice);
  }

  if (!privacyMode && noteEditorsOpen.has(change.key)) panel.append(noteEditor(change));
  if (!privacyMode) panel.append(reviewActionRow(panel, change));
  return panel;
}

function panelsByEnd(view) {
  const result = new Map();
  for (const change of view.changes ?? []) {
    const end = change.panelAfter;
    if (!result.has(end)) result.set(end, []);
    result.get(end).push(change);
  }
  return result;
}

function appendPanels(host, byEnd, lineCount) {
  for (const change of byEnd.get(lineCount) ?? []) host.append(reviewPanel(change));
}

function gapsByStart(view) {
  const result = new Map();
  for (const gap of view.contextGaps ?? []) {
    const insertAt = Number(gap.insertAt ?? 0);
    if (!result.has(insertAt)) result.set(insertAt, []);
    result.get(insertAt).push(gap);
  }
  return result;
}

function appendBeforeGaps(host, byStart, lineIndex) {
  for (const gap of byStart.get(lineIndex) ?? []) {
    host.append(contextGapElement(gap, gap.position ?? 'before'));
  }
}

function appendRawLines(host, view) {
  const panelEnd = panelsByEnd(view);
  const gapStart = gapsByStart(view);
  for (let index = 0; index < view.lines.length; index++) {
    appendBeforeGaps(host, gapStart, index);
    host.append(lineElement(view.lines[index]));
    appendPanels(host, panelEnd, index + 1);
  }
  appendBeforeGaps(host, gapStart, view.lines.length);
}

function appendFocusedLines(host, view) {
  const lines = view.lines;
  const panelEnd = panelsByEnd(view);
  const gapStart = gapsByStart(view);
  const hiddenChanges = view.hiddenChanges ?? [];
  let hiddenChangeIndex = 0;
  let pendingContext = null;
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    appendBeforeGaps(host, gapStart, index);
    if (line.kind === 'filtered_context' && lines[index + 1]?.kind === 'filtered_header') {
      pendingContext = line;
      appendPanels(host, panelEnd, index + 1);
      continue;
    }
    if (line.kind !== 'filtered_header') {
      host.append(lineElement(line));
      appendPanels(host, panelEnd, index + 1);
      continue;
    }
    const hiddenChange = hiddenChanges[hiddenChangeIndex++];
    const details = document.createElement('details');
    details.className = 'hidden-block';
    const summary = document.createElement('summary');
    const summaryLine = {
      ...line,
      text: line.text.replace(/^▼\s*/, ''),
      privateText: (line.privateText ?? line.text).replace(/^▼\s*/, ''),
    };
    summary.append(lineElement(summaryLine));
    details.append(summary);
    const body = document.createElement('div');
    body.className = 'hidden-block-body';
    if (pendingContext) {
      body.append(lineElement(pendingContext));
      pendingContext = null;
    }
    while (index + 1 < lines.length && lines[index + 1].kind.startsWith('filtered_')) {
      index++;
      appendBeforeGaps(body, gapStart, index);
      body.append(lineElement(lines[index]));
    }
    if (hiddenChange && !hiddenChange.physicalOrderOnly) body.append(reviewPanel(hiddenChange));
    details.append(body);
    host.append(details);
    appendPanels(host, panelEnd, index + 1);
  }
  appendBeforeGaps(host, gapStart, lines.length);
}


function pathPartElement(part) {
  const element = document.createElement('span');
  element.className = 'path-part';
  const text = part?.text ?? '';
  if (privacyMode) {
    element.textContent = text;
  } else if (part?.contextTargets?.length) {
    appendContextualText(element, text, [], part.contextTargets);
  } else {
    element.textContent = text;
    decorateContextTarget(
      element,
      part?.contextRefs ?? [],
      part?.contextSuggestion ?? null,
    );
  }
  return element;
}

function renderFilePathBreadcrumb(host, file, stateSuffix) {
  host.replaceChildren();
  if (privacyMode || !file.contextPath) {
    host.textContent = stateSuffix
      ? `${displayFilePath(file)} · ${stateSuffix}`
      : displayFilePath(file);
    return;
  }
  const breadcrumb = document.createElement('span');
  breadcrumb.className = 'path-breadcrumb';
  breadcrumb.append(pathPartElement(file.contextPath.sourceEnvironment));
  const arrow = document.createElement('span');
  arrow.className = 'path-arrow';
  arrow.textContent = '→';
  breadcrumb.append(arrow, pathPartElement(file.contextPath.targetEnvironment));
  for (const part of file.contextPath.parts ?? []) {
    const separator = document.createElement('span');
    separator.className = 'path-separator';
    separator.textContent = '/';
    breadcrumb.append(separator, pathPartElement(part));
  }
  host.append(breadcrumb);
  if (stateSuffix) {
    const state = document.createElement('span');
    state.className = 'path-state';
    state.textContent = `· ${stateSuffix}`;
    host.append(state);
  }
}

function renderDiff() {
  const file = currentFile();
  if (!file) {
    $('path').textContent = 'No matching files';
    $('diff').innerHTML = '<div class="empty">No files match the search.</div>';
    return;
  }
  $('hideFile').textContent = hiddenFiles.has(file.path) ? 'Show file' : 'Hide file';
  $('reviewFile').textContent = reviewedFiles.has(file.path) ? 'Mark unreviewed' : 'Mark reviewed';
  $('reviewFile').classList.toggle('active', reviewedFiles.has(file.path));
  const view = mode === 'focused' ? (file.focusedExpanded ?? file.focused) : file.raw;
  const summaryView = mode === 'focused' ? file.focused : file.raw;
  const stateSuffix = [reviewedFiles.has(file.path) ? 'REVIEWED' : '', hiddenFiles.has(file.path) ? 'HIDDEN' : ''].filter(Boolean).join(' · ');
  const pathHost = $('path');
  pathHost.replaceWith(pathHost.cloneNode(false));
  renderFilePathBreadcrumb($('path'), file, stateSuffix);
  $('focused').classList.toggle('active', mode === 'focused');
  $('raw').classList.toggle('active', mode === 'raw');
  const hidden = summaryView.noiseHidden + summaryView.whitespaceHidden + summaryView.orderHidden;
  const physicalOrderNote = summaryView.physicalOrderFallback
    ? ' · logical YAML changes mapped onto physical file positions'
    : '';
  const privacyNote = privacyMode ? ' · PRIVACY MODE' : '';
  $('meta').textContent = `${file.status} · ${summaryView.visibleChanges} visible change${summaryView.visibleChanges === 1 ? '' : 's'}${mode === 'focused' ? ` · ${hidden} hidden (click to expand) · ${summaryView.handled} handled${physicalOrderNote}` : ''}${privacyNote}`;
  if (!notesDirty) setStatus(defaultStatus());

  const host = $('diff');
  host.replaceChildren();
  if (!view.lines.length || (summaryView.visibleChanges === 0 && mode === 'focused' && hidden === 0)) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = mode === 'focused' ? 'No visible Focused Diff changes. Switch to Raw to inspect all literal differences.' : 'No literal differences.';
    host.append(empty);
    return;
  }
  if (mode === 'focused') appendFocusedLines(host, view);
  else appendRawLines(host, view);
  renderOpenGitContexts(view);
  applyContextHelpMode();
  host.scrollTop = 0;
  host.scrollLeft = 0;
}

function render() {
  visible = activeFilesMatchingSearch();
  renderTree();
  renderDiff();
}

function move(delta) {
  if (!visible.length) return;
  const current = visible.indexOf(currentFile());
  const next = visible[(Math.max(0, current) + delta + visible.length) % visible.length];
  selectFile(next);
}

function moveAfterStateChange(file) {
  const next = nextActiveAfter(file);
  if (next) selectFile(next);
  else render();
}

function toggleCurrentHidden() {
  const file = currentFile();
  if (!file) return;
  if (hiddenFiles.has(file.path)) {
    hiddenFiles.delete(file.path);
    setStatus(`Restored hidden file: ${displayFilePath(file)}`, 'success');
    render();
    return;
  }
  hiddenFiles.add(file.path);
  moveAfterStateChange(file);
  setStatus(`Hidden for this browser session: ${displayFilePath(file)}`, 'success');
}

function toggleCurrentReviewed() {
  const file = currentFile();
  if (!file) return;
  if (reviewedFiles.has(file.path)) {
    reviewedFiles.delete(file.path);
    reviewedAtByFile.delete(file.path);
    setStatus(`Marked unreviewed: ${displayFilePath(file)}`, 'success');
    render();
    return;
  }
  reviewedFiles.add(file.path);
  reviewedAtByFile.set(file.path, new Date().toISOString());
  moveAfterStateChange(file);
  setStatus(`Marked reviewed: ${displayFilePath(file)}`, 'success');
}

function setAllHidden(open) {
  document.querySelectorAll('.hidden-block').forEach(details => { details.open = open; });
  $('viewMenu').open = false;
}


function exportFilename() {
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');
  const privacy = privacyMode ? '-private' : '';
  return `config-review-${mode}${privacy}-${stamp}.txt`;
}

function elementIsDisplayed(element) {
  if (element.hidden) return false;
  let current = element;
  while (current && current !== $('diff')) {
    const parent = current.parentElement;
    if (!parent) break;
    if (parent.hidden) return false;
    if (parent.tagName === 'DETAILS' && !parent.open) {
      const summary = parent.querySelector(':scope > summary');
      if (!summary?.contains(element)) return false;
    }
    current = parent;
  }
  return true;
}

function displayedDiffText() {
  const file = currentFile();
  if (!file) return null;
  const elements = [...$('diff').querySelectorAll(
    '.line, .context-gap-button, .review-heading, .split-change-note',
  )].filter(elementIsDisplayed);
  const rows = elements
    .filter(element => element.classList.contains('line'))
    .map(element => ({
      test: element.dataset.testLine ?? '',
      dev: element.dataset.devLine ?? '',
      prefix: element.dataset.copyPrefix ?? '',
      text: element.dataset.copyText ?? element.querySelector('.code')?.textContent ?? '',
    }));
  const testWidth = Math.max(4, ...rows.map(row => row.test.length));
  const devWidth = Math.max(3, ...rows.map(row => row.dev.length));
  const lines = [
    privacyMode ? 'CONFIG REVIEW WEB DIFF — REDACTED' : 'CONFIG REVIEW WEB DIFF',
    '='.repeat(80),
    `FILE: ${displayFilePath(file)}`,
    `VIEW: ${mode === 'focused' ? 'Focused' : 'Raw'}`,
    `LINE COLUMNS: ${targetColumnLabel().padStart(testWidth)} | ${sourceColumnLabel().padStart(devWidth)}`,
    '',
  ];
  for (const element of elements) {
    if (element.classList.contains('context-gap-button')) {
      lines.push(`[${element.textContent.trim()}]`);
      continue;
    }
    if (element.classList.contains('review-heading')) {
      const label = element.querySelector('.review-label')?.textContent.trim() ?? 'Change';
      const ranges = element.querySelector('.review-ranges')?.textContent.trim() ?? '';
      lines.push(`CHANGE: ${label}${ranges ? ` · ${ranges}` : ''}`);
      continue;
    }
    if (element.classList.contains('split-change-note')) {
      lines.push(`NOTE: ${element.textContent.trim()}`);
      continue;
    }
    const test = (element.dataset.testLine ?? '').padStart(testWidth);
    const dev = (element.dataset.devLine ?? '').padStart(devWidth);
    const prefix = element.dataset.copyPrefix ?? ' ';
    const value = element.dataset.copyText ?? element.querySelector('.code')?.textContent ?? '';
    lines.push(`${test} | ${dev} | ${prefix} ${value}`.trimEnd());
    for (const context of element.querySelectorAll(':scope > .line-git-context')) {
      lines.push(`GIT ${context.dataset.side ?? ''}: ${context.textContent.trim()}`.trim());
    }
  }
  return lines.join('\n') + '\n';
}

async function copyDisplayedDiff() {
  const text = displayedDiffText();
  if (!text) {
    setStatus('No displayed diff is available to copy.', 'error');
    return;
  }
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.append(textarea);
      textarea.select();
      if (!document.execCommand('copy')) throw new Error('browser copy command failed');
      textarea.remove();
    }
    setStatus(privacyMode ? 'Copied the displayed redacted diff, including line numbers.' : 'Copied the displayed diff with original values and line numbers.', 'success');
  } catch (error) {
    setStatus(`Could not copy displayed diff: ${error.message}`, 'error');
  }
}

async function chooseDestination(filename) {
  if (typeof window.showSaveFilePicker === 'function') {
    try {
      const handle = await window.showSaveFilePicker({
        suggestedName: filename,
        types: [{description: 'Plain text review', accept: {'text/plain': ['.txt']}}],
      });
      return {kind: 'picker', handle};
    } catch (error) {
      if (error?.name === 'AbortError') return {kind: 'cancelled'};
      throw error;
    }
  }
  return {kind: 'download', filename};
}

function formatInlineCommitLines(context, change) {
  const lines = [];
  for (const [side, hasLines] of [
    ['TARGET', change.oldLines.length > 0],
    ['SOURCE', change.newLines.length > 0],
  ]) {
    if (!hasLines) continue;
    const sideLabel = side === 'SOURCE' ? sourceColumnLabel() : targetColumnLabel();
    const commit = commitContextForSide(context, side);
    if (!commit) {
      lines.push(context.error || `No tracked ${sideLabel} commit context was available.`);
      continue;
    }
    lines.push(`Last changed in ${sideLabel} · by ${commit.author || 'unknown author'} · ${commit.subject || 'No commit subject'} · ${commit.hash || '—'}`);
  }
  return lines;
}

function changesForExport(file) {
  if (mode === 'raw') return file.raw.changes ?? [];
  const active = file.focused.changes ?? [];
  const notedHidden = (file.focused.hiddenChanges ?? []).filter(change => {
    return (notesByChange.get(change.key) ?? '').trim();
  });
  return [...active, ...notedHidden].sort((left, right) => {
    const leftPosition = Math.min(left.testStart ?? Number.MAX_SAFE_INTEGER, left.devStart ?? Number.MAX_SAFE_INTEGER);
    const rightPosition = Math.min(right.testStart ?? Number.MAX_SAFE_INTEGER, right.devStart ?? Number.MAX_SAFE_INTEGER);
    return leftPosition - rightPosition;
  });
}

async function buildPlaintextReview(
  files = snapshot.files,
  {title = 'CONFIG REVIEW WORKBENCH', includeEmptyFiles = false} = {},
) {
  const lines = [
    title,
    '='.repeat(80),
    `Generated: ${new Date().toLocaleString()}`,
    `Snapshot:  ${snapshot.generatedAt}`,
    `View:      ${mode === 'focused' ? 'Focused Diff' : 'Raw Diff'}`,
    `Privacy:   ${privacyMode ? 'ON — sensitive values, personal references, Git context, remote links, and reviewer notes omitted or redacted' : 'OFF'}`,
    `${targetColumnLabel()}:      ${privacyMode ? snapshot.privateTarget : snapshot.target}`,
    `${sourceColumnLabel()}:       ${privacyMode ? snapshot.privateSource : snapshot.source}`,
    `Git:       ${privacyMode ? snapshot.privateGitStatus : snapshot.gitStatus}`,
    '',
  ];
  let exportedChanges = 0;
  let exportedFiles = 0;
  for (const file of files) {
    const view = mode === 'focused' ? file.focused : file.raw;
    const changes = changesForExport(file);
    if (!changes.length && !includeEmptyFiles) continue;
    exportedFiles++;
    lines.push('#'.repeat(80));
    lines.push(`FILE: ${displayFilePath(file)}`);
    lines.push(`STATUS: ${file.status}`);
    if (reviewedFiles.has(file.path)) {
      const reviewedAt = reviewedAtByFile.get(file.path);
      lines.push(`REVIEWED: ${reviewedAt ? new Date(reviewedAt).toLocaleString() : 'yes'}`);
    }
    if (mode === 'focused') {
      const hidden = view.noiseHidden + view.whitespaceHidden + view.orderHidden;
      lines.push(`VISIBLE: ${view.visibleChanges} · HIDDEN: ${hidden} · HANDLED: ${view.handled}`);
    }
    lines.push('');
    if (!changes.length) {
      lines.push('No exportable changes in the current view.');
      lines.push('');
    }
    for (let index = 0; index < changes.length; index++) {
      const change = changes[index];
      exportedChanges++;
      const hiddenSuffix = change.hidden
        ? (privacyMode
          ? ' [hidden in Focused view]'
          : ' [hidden in Focused view; included because it has a note]')
        : '';
      lines.push(`${index + 1}. ${displayChangeLabel(change)}${hiddenSuffix}`);
      lines.push('-'.repeat(80));
      lines.push(`${targetColumnLabel()} ${change.testRange} -> ${sourceColumnLabel()} ${change.devRange}`);
      lines.push('');
      const oldLines = privacyMode ? (change.privateOldLines ?? []) : change.oldLines;
      const newLines = privacyMode ? (change.privateNewLines ?? []) : change.newLines;
      for (const value of oldLines) lines.push(`- ${value}`);
      for (const value of newLines) lines.push(`+ ${value}`);
      if (!change.oldLines.length && !change.newLines.length) lines.push('  (No literal lines available for this logical change.)');
      lines.push('');
      if (!privacyMode && inlineGitContextKeys.has(change.key)) {
        const context = await getGitContext(change);
        lines.push(...formatInlineCommitLines(context, change));
        lines.push('');
      }
      const note = (notesByChange.get(change.key) ?? '').trim();
      if (!privacyMode && note) {
        lines.push('Reviewer note:');
        lines.push(...note.split(/\r?\n/).map(value => `  ${value}`));
        lines.push('');
      }
    }
  }
  if (!exportedFiles) return null;
  lines.push('='.repeat(80));
  lines.push(`Exported ${exportedFiles} file${exportedFiles === 1 ? '' : 's'} and ${exportedChanges} change${exportedChanges === 1 ? '' : 's'} from the ${mode === 'focused' ? 'Focused' : 'Raw'} view.`);
  return lines.join('\n') + '\n';
}

async function writeReview(destination, text, filename) {
  if (destination.kind === 'picker') {
    const writable = await destination.handle.createWritable();
    await writable.write(text);
    await writable.close();
    return destination.handle.name || filename;
  }
  const blob = new Blob([text], {type: 'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = 'none';
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return filename;
}

async function saveReport({
  files,
  filename,
  title,
  includeEmptyFiles = false,
  clearNotesDirty = false,
}) {
  if (!files.length) {
    setStatus('No files are available for this report.', 'error');
    return;
  }
  const hasChanges = files.some(file => changesForExport(file).length);
  if (!hasChanges && !includeEmptyFiles) {
    setStatus('No visible changes in the current view; no review file was created.', 'error');
    return;
  }
  let destination;
  try {
    destination = await chooseDestination(filename);
  } catch (error) {
    setStatus(`Could not open save dialog: ${error.message}`, 'error');
    return;
  }
  if (destination.kind === 'cancelled') {
    setStatus('Save cancelled; temporary review state remains in this browser.', '');
    return;
  }
  setStatus(
    privacyMode
      ? 'Building redacted plaintext review…'
      : 'Building plaintext review…',
    'busy',
  );
  try {
    const text = await buildPlaintextReview(files, {title, includeEmptyFiles});
    if (text === null) {
      setStatus('No files were available for the report.', 'error');
      return;
    }
    const savedName = await writeReview(destination, text, filename);
    if (clearNotesDirty) notesDirty = false;
    setStatus(
      `${privacyMode ? 'Saved redacted plaintext review' : 'Saved plaintext review'}: ${savedName}`,
      'success',
    );
  } catch (error) {
    setStatus(`Could not save review: ${error.message}`, 'error');
  }
}

async function saveReview() {
  await saveReport({
    files: snapshot.files,
    filename: exportFilename(),
    title: 'CONFIG REVIEW WORKBENCH',
    clearNotesDirty: !privacyMode,
  });
}

function reviewedReportFiles() {
  return snapshot.files.filter(file => reviewedFiles.has(file.path));
}

async function saveReviewedReport() {
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');
  const privacy = privacyMode ? '-private' : '';
  await saveReport({
    files: reviewedReportFiles(),
    filename: `config-review-reviewed-${mode}${privacy}-${stamp}.txt`,
    title: 'CONFIG REVIEW WORKBENCH — REVIEWED FILES',
    includeEmptyFiles: true,
  });
}

async function printReviewedReport() {
  const files = reviewedReportFiles();
  if (!files.length) {
    setStatus('No files are marked reviewed; nothing was printed.', 'error');
    return;
  }
  setStatus(
    privacyMode
      ? 'Preparing redacted reviewed-files printout…'
      : 'Preparing reviewed-files printout…',
    'busy',
  );
  try {
    const text = await buildPlaintextReview(files, {
      title: 'CONFIG REVIEW WORKBENCH — REVIEWED FILES',
      includeEmptyFiles: true,
    });
    if (text === null) {
      setStatus('No reviewed files were available for printing.', 'error');
      return;
    }
    $('printReport').textContent = text;
    setStatus(`Opening print dialog for ${files.length} reviewed file${files.length === 1 ? '' : 's'}…`, 'success');
    window.print();
  } catch (error) {
    setStatus(`Could not print reviewed report: ${error.message}`, 'error');
  }
}

function applyPrivacyMode() {
  const button = $('privacyToggle');
  hideContextTooltip();
  if (privacyMode) {
    contextHelpMode = false;
    closeContextModal();
    closeContextEditor();
  }
  button.classList.toggle('active', privacyMode);
  button.setAttribute('aria-pressed', privacyMode ? 'true' : 'false');
  button.textContent = privacyMode ? 'Show original values' : 'Hide sensitive values';
  $('copyDiff').hidden = false;
  $('copyDiff').title = privacyMode
    ? 'Copy the currently displayed redacted diff, including source and target line numbers'
    : 'Copy the currently displayed diff with original values and source and target line numbers';
  applyContextHelpMode();
  $('viewMenu').open = false;
  gapStateById.clear();
  render();
  setStatus(defaultStatus(), privacyMode ? 'busy' : '');
}

function togglePrivacyMode() {
  privacyMode = !privacyMode;
  applyPrivacyMode();
}

$('search').addEventListener('input', render);
$('prev').onclick = () => move(-1);
$('next').onclick = () => move(1);
$('focused').onclick = () => { mode = 'focused'; renderDiff(); };
$('raw').onclick = () => { mode = 'raw'; renderDiff(); };
$('changeComparison').onclick = openComparisonModal;
$('contextHelp').onclick = toggleContextHelpMode;
$('closeContext').onclick = closeContextModal;
$('newContextEntry').onclick = () => openContextEditor();
$('newContextEntry').disabled = !contextEditable;
$('closeContextEditor').onclick = closeContextEditor;
$('cancelContextEditor').onclick = closeContextEditor;
$('saveContextEntry').onclick = saveContextEntry;
$('contextEntryCategory').addEventListener('change', updateContextCategoryEditor);
$('contextLimitFiles').addEventListener('change', () => updateContextFileScope());
$('browseContextFiles').onclick = openContextFilePicker;
$('closeContextFilePicker').onclick = closeContextFilePicker;
$('contextFileSearch').addEventListener('input', renderContextFilePicker);
$('contextFilePickerModal').addEventListener('click', event => {
  if (event.target === $('contextFilePickerModal')) closeContextFilePicker();
});
$('contextSearch').addEventListener('input', renderContextDictionary);
$('contextModal').addEventListener('click', event => {
  if (event.target === $('contextModal')) closeContextModal();
});
$('contextEditorModal').addEventListener('click', event => {
  if (event.target === $('contextEditorModal')) closeContextEditor();
});
$('contextTooltip').addEventListener('mouseenter', () => {
  if (contextTooltipTimer) window.clearTimeout(contextTooltipTimer);
});
$('contextTooltip').addEventListener('mouseleave', scheduleContextTooltipHide);
$('closeComparison').onclick = closeComparisonModal;
$('cancelComparison').onclick = closeComparisonModal;
$('swapComparison').onclick = () => {
  for (const suffix of ['Directory', 'RepositoryRoot']) {
    const source = $(`source${suffix}`).value;
    $(`source${suffix}`).value = $(`target${suffix}`).value;
    $(`target${suffix}`).value = source;
  }
  const sourceLabelText = $('sourceSideLabel').textContent;
  $('sourceSideLabel').textContent = $('targetSideLabel').textContent;
  $('targetSideLabel').textContent = sourceLabelText;
  loadEnvironments('source', basename($('sourceDirectory').value));
  loadEnvironments('target', basename($('targetDirectory').value));
  $('comparisonPreview').hidden = true;
};
$('browseSource').onclick = () => browseFor($('sourceDirectory'));
$('browseTarget').onclick = () => browseFor($('targetDirectory'));
$('browseSourceRoot').onclick = () => browseFor(
  $('sourceRepositoryRoot'),
  () => loadEnvironments('source', basename($('sourceDirectory').value)),
);
$('browseTargetRoot').onclick = () => browseFor(
  $('targetRepositoryRoot'),
  () => loadEnvironments('target', basename($('targetDirectory').value)),
);
$('refreshSourceEnvironments').onclick = () => loadEnvironments(
  'source',
  basename($('sourceDirectory').value),
);
$('refreshTargetEnvironments').onclick = () => loadEnvironments(
  'target',
  basename($('targetDirectory').value),
);
$('sourceEnvironment').addEventListener('change', () => selectEnvironment('source'));
$('targetEnvironment').addEventListener('change', () => selectEnvironment('target'));
$('sourceDirectory').addEventListener('input', () => {
  $('sourceSideLabel').textContent = basename($('sourceDirectory').value);
  $('comparisonPreview').hidden = true;
});
$('targetDirectory').addEventListener('input', () => {
  $('targetSideLabel').textContent = basename($('targetDirectory').value);
  $('comparisonPreview').hidden = true;
});
$('folderUp').onclick = () => loadDirectory(browseParent);
$('useFolder').onclick = () => {
  if (browseInput && browsePath) browseInput.value = browsePath;
  $('folderBrowser').hidden = true;
  if (browseAfterSelect) browseAfterSelect();
  browseAfterSelect = null;
};
$('previewComparison').onclick = previewComparison;
$('applyComparison').onclick = applyComparison;
$('comparisonModal').addEventListener('click', event => {
  if (event.target === $('comparisonModal')) closeComparisonModal();
});
$('hideFile').onclick = toggleCurrentHidden;
$('reviewFile').onclick = toggleCurrentReviewed;
$('saveReview').onclick = saveReview;
$('copyDiff').onclick = copyDisplayedDiff;
$('saveReviewed').onclick = saveReviewedReport;
$('printReviewed').onclick = printReviewedReport;
$('restoreHidden').onclick = () => {
  hiddenFiles.clear();
  $('reviewMenu').open = false;
  render();
  setStatus('Restored all hidden files.', 'success');
};
$('expandHidden').onclick = () => setAllHidden(true);
$('collapseHidden').onclick = () => setAllHidden(false);
$('privacyToggle').onclick = togglePrivacyMode;
document.querySelectorAll('[data-theme-choice]').forEach(button => {
  button.onclick = () => {
    themeChoice = button.dataset.themeChoice;
    applyTheme();
  };
});
document.addEventListener('keydown', event => {
  if (!$('contextFilePickerModal').hidden) {
    if (event.key === 'Escape') {
      closeContextFilePicker();
      event.preventDefault();
    }
    return;
  }
  if (!$('contextEditorModal').hidden) {
    if (event.key === 'Escape') {
      closeContextEditor();
      event.preventDefault();
    } else if (event.key === 'Enter' && event.ctrlKey) {
      saveContextEntry();
      event.preventDefault();
    }
    return;
  }
  if (!$('contextModal').hidden) {
    if (event.key === 'Escape') {
      closeContextModal();
      event.preventDefault();
    }
    return;
  }
  if (!$('comparisonModal').hidden) {
    if (event.key === 'Escape') {
      closeComparisonModal();
      event.preventDefault();
    } else if (event.key === 'Enter' && event.ctrlKey) {
      applyComparison();
      event.preventDefault();
    }
    return;
  }
  if (event.target === $('search') || event.target?.tagName === 'TEXTAREA') return;
  if (event.key === '[') {
    move(-1);
    event.preventDefault();
  } else if (event.key === ']') {
    move(1);
    event.preventDefault();
  } else if (event.key === 'f') {
    mode = 'focused';
    renderDiff();
  } else if (event.key === 'r') {
    mode = 'raw';
    renderDiff();
  } else if (event.key.toLowerCase() === 'p') {
    togglePrivacyMode();
  } else if (event.key === '?') {
    toggleContextHelpMode();
    event.preventDefault();
  } else if (event.key === '/') {
    $('search').focus();
    event.preventDefault();
  } else if (event.key.toLowerCase() === 'e' && mode === 'focused') {
    const blocks = [...document.querySelectorAll('.hidden-block')];
    const open = blocks.some(block => !block.open);
    setAllHidden(open);
  }
});
window.addEventListener('beforeunload', event => {
  if (!notesDirty) return;
  event.preventDefault();
  event.returnValue = '';
});
applyTheme();
updateComparisonButton();
applyPrivacyMode();
</script>
</body>
</html>"""
    return page.replace("__SNAPSHOT__", encoded).encode("utf-8")


class _ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    page: bytes
    token: str
    workbench: Workbench
    git_lookup: GitLookup
    context_lookup: ContextLookup
    git_cache: dict[str, bytes]
    git_cache_lock: threading.Lock
    state_lock: threading.Lock


class _ViewerHandler(BaseHTTPRequestHandler):
    server: _ViewerServer
    server_version = "ConfigReviewWebViewer"
    sys_version = ""

    def _send_security_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(self._response_body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self._response_body = body
        self.send_response(status)
        self._send_security_headers(content_type)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        path = parsed.path
        page_paths = {f"/{self.server.token}", f"/{self.server.token}/"}
        if path in page_paths:
            with self.server.state_lock:
                page = self.server.page
            self._send_bytes(200, page, "text/html; charset=utf-8")
            return

        directories_path = f"/{self.server.token}/directories"
        if path == directories_path:
            requested = (parse_qs(parsed.query).get("path") or [None])[0]
            try:
                payload = _directory_listing_payload(self.server.workbench, requested)
            except WorkbenchError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, payload)
            return

        environments_path = f"/{self.server.token}/environments"
        if path == environments_path:
            root = (parse_qs(parsed.query).get("root") or [""])[0]
            try:
                payload = _environment_listing_payload(root)
            except WorkbenchError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, payload)
            return

        context_prefix = f"/{self.server.token}/context/"
        if path.startswith(context_prefix):
            context_id = unquote(path[len(context_prefix) :])
            lookup = self.server.context_lookup.get(context_id)
            if lookup is None or not context_id or "/" in context_id:
                self.send_error(404)
                return
            query = parse_qs(parsed.query)
            count = _bounded_query_int(query.get("count"), 10)
            edge = (query.get("edge") or ["start"])[0]
            if edge not in {"start", "end"}:
                self.send_error(400)
                return
            self._send_json(200, _context_gap_payload(lookup, count=count, edge=edge))
            return

        prefix = f"/{self.server.token}/git/"
        if not path.startswith(prefix):
            self.send_error(404)
            return
        context_id = unquote(path[len(prefix) :])
        lookup = self.server.git_lookup.get(context_id)
        if lookup is None or not context_id or "/" in context_id:
            self.send_error(404)
            return

        with self.server.git_cache_lock:
            cached = self.server.git_cache.get(context_id)
        if cached is not None:
            self._send_bytes(200, cached, "application/json; charset=utf-8")
            return

        record, block = lookup
        try:
            payload = _git_context_payload(self.server.workbench, record, block)
        except (OSError, WorkbenchError) as exc:
            payload = {"dev": [], "test": [], "error": str(exc)}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self.server.git_cache_lock:
            self.server.git_cache[context_id] = body
        self._send_bytes(200, body, "application/json; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        comparison_path = f"/{self.server.token}/comparison"
        preview_path = f"/{self.server.token}/comparison-preview"
        context_entry_path = f"/{self.server.token}/context-entry"
        if parsed.path not in {comparison_path, preview_path, context_entry_path}:
            self.send_error(405)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Invalid request length."})
            return
        if length <= 0 or length > 131_072:
            self._send_json(400, {"error": "Invalid request."})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "Comparison request must be valid JSON."})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "Request must be a JSON object."})
            return
        if parsed.path == context_entry_path:
            try:
                result = _save_server_context_entry(self.server, payload)
            except (OSError, WorkbenchError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, result)
            return

        source = payload.get("source")
        target = payload.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            self._send_json(400, {"error": "Source and target paths must be strings."})
            return
        if parsed.path == preview_path:
            try:
                result = _comparison_preview_payload(
                    self.server.workbench,
                    source,
                    target,
                )
            except (OSError, WorkbenchError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, result)
            return

        persist = payload.get("persist", False)
        if not isinstance(persist, bool):
            self._send_json(400, {"error": "Persist must be true or false."})
            return
        try:
            result = _replace_server_comparison(
                self.server,
                source_value=source,
                target_value=target,
                persist=persist,
            )
        except (OSError, WorkbenchError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, result)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class LocalWebDiffViewer:
    """Own one loopback-only browser review server thread."""

    def __init__(self) -> None:
        self._server: _ViewerServer | None = None
        self._thread: threading.Thread | None = None
        self.url: str | None = None

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        self.url = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def open(
        self,
        workbench: Workbench,
        *,
        open_browser: bool = True,
    ) -> WebViewerLaunch:
        """Start a fresh snapshot server and optionally open the default browser."""
        snapshot, git_lookup, context_lookup = _build_web_diff_snapshot(workbench)
        page = _render_page(snapshot)
        self.stop()
        token = secrets.token_urlsafe(18)
        server = _ViewerServer(("127.0.0.1", 0), _ViewerHandler)
        server.page = page
        server.token = token
        server.workbench = workbench
        server.git_lookup = git_lookup
        server.context_lookup = context_lookup
        server.git_cache = {}
        server.git_cache_lock = threading.Lock()
        server.state_lock = threading.Lock()
        thread = threading.Thread(
            target=server.serve_forever,
            name="config-review-web-viewer",
            daemon=True,
        )
        thread.start()
        port = int(server.server_address[1])
        url = f"http://127.0.0.1:{port}/{token}/"
        self._server = server
        self._thread = thread
        self.url = url
        browser_opened = _open_browser_once(url) if open_browser else False
        return WebViewerLaunch(
            url=url,
            file_count=len(snapshot["files"]),
            browser_opened=bool(browser_opened),
        )
