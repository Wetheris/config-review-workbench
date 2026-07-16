"""Local browser companion for reviewing DEV-to-TEST differences.

The viewer serves a snapshot generated from the workbench's existing Focused Diff
and Full Diff presentations. It binds only to loopback, uses a random URL token,
and never modifies DEV, TEST, Git, or workbench configuration. Git context is
loaded lazily through a read-only endpoint, while reviewer notes remain in the
browser until the reviewer explicitly exports a plaintext review file.
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
from typing import TYPE_CHECKING, Any, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from .core import (
    ChangeBlock,
    DiffPresentation,
    DisplayLine,
    FileRecord,
    GitCommitContext,
    WorkbenchError,
    _is_yaml_order_continuation,
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


ContextLookup = dict[str, _ContextGapSnapshot]


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
) -> list[dict[str, Any]]:
    private_lines = redactor.redact_lines([line.text for line in lines])
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
    return [
        {
            "text": line.text,
            "privateText": private_text,
            "kind": line.kind,
            "testLine": line.test_line,
            "devLine": line.dev_line,
            "emphasisRanges": [list(item) for item in line.emphasis_ranges],
        }
        for line, private_text in zip(lines, private_lines, strict=True)
    ]


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
            hidden=True,
        )
        if payload["key"] in active_keys:
            continue
        hidden_changes.append(payload)

    lines = _display_lines_payload(presentation.lines, redactor)
    context_gaps = _timeline_context_gaps(
        record,
        lines,
        context_lookup,
        redactor,
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
) -> list[dict[str, Any]]:
    """Find omitted aligned unchanged ranges in a physical web timeline."""
    test_lines = record.test_text.splitlines()
    dev_lines = record.dev_text.splitlines()
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
    files: list[dict[str, Any]] = []
    git_lookup: GitLookup = {}
    context_lookup: ContextLookup = {}
    redactor = _PrivacyRedactor()
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
            )
            focused_expanded_payload = _physical_semantic_presentation_payload(
                workbench,
                record,
                focused_expanded,
                semantic_focused_expanded,
                git_lookup,
                context_lookup,
                redactor,
            )
        else:
            focused_payload = _presentation_payload(
                workbench,
                record,
                focused,
                git_lookup,
                context_lookup,
                redactor,
            )
            focused_expanded_payload = _presentation_payload(
                workbench,
                record,
                focused_expanded,
                git_lookup,
                context_lookup,
                redactor,
            )
        files.append(
            {
                "path": record.relative_path,
                "privatePath": redactor.redact_path(record.relative_path),
                "status": status,
                "states": list(record.states),
                "focused": focused_payload,
                "focusedExpanded": focused_expanded_payload,
                "raw": _presentation_payload(
                    workbench,
                    record,
                    full,
                    git_lookup,
                    context_lookup,
                    redactor,
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
        )

    if not files:
        raise WorkbenchError("No current DEV/TEST differences are available for the web viewer.")

    snapshot = {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(workbench.settings.source),
        "target": str(workbench.settings.target),
        "privateSource": "[DEV ROOT]",
        "privateTarget": "[TEST ROOT]",
        "gitStatus": workbench.git_status.summary,
        "privateGitStatus": "Git context hidden in privacy mode",
        "gitLinks": {
            "available": bool(workbench.git_repository_url and workbench.git_link_commit),
            "repositoryUrl": workbench.git_repository_url,
            "source": workbench.git_repository_url_source,
            "commit": workbench.git_link_commit,
            "status": workbench.git_link_status_text,
        },
        "files": files,
    }
    return snapshot, git_lookup, context_lookup


def build_web_diff_snapshot(workbench: Workbench) -> dict[str, Any]:
    """Build the public browser snapshot without exposing private server objects."""
    snapshot, _git_lookup, _context_lookup = _build_web_diff_snapshot(workbench)
    return snapshot


def _commit_payload(context: GitCommitContext) -> dict[str, str]:
    return {
        "source": context.source,
        "hash": context.short_hash,
        "author": context.author,
        "date": context.date,
        "subject": context.subject,
    }


def _git_context_payload(
    workbench: Workbench,
    record: FileRecord,
    block: ChangeBlock,
) -> dict[str, Any]:
    test_context, dev_context = workbench._block_git_context(record, block)

    def newest_first(items: list[GitCommitContext]) -> list[GitCommitContext]:
        return sorted(items, key=lambda item: item.date, reverse=True)

    return {
        # DEV is the incoming side of the comparison and is intentionally first.
        "dev": [_commit_payload(item) for item in newest_first(dev_context)],
        "test": [_commit_payload(item) for item in newest_first(test_context)],
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
            }
            for index, text in enumerate(selected)
        ],
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
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.controls { display: flex; gap: 6px; align-items: center; }
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
.hidden-row, .git-row { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; margin-top: 6px; }
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
  user-select: none;
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
.git-context { border-bottom: 1px solid var(--border); }
.git-context summary { cursor: pointer; padding: 8px 12px; color: var(--accent); user-select: none; }
.git-context[open] summary { border-bottom: 1px solid var(--border); }
.git-content { padding: 10px 12px; color: var(--text); }
.git-side + .git-side { margin-top: 10px; }
.git-side-title { color: var(--muted); font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; }
.commit {
  display: grid;
  grid-template-columns: 78px minmax(180px, 1fr) auto;
  gap: 8px;
  padding: 5px 0;
  border-bottom: 1px solid var(--border);
}
.commit:last-child { border-bottom: 0; }
.commit-hash { color: var(--accent); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
.commit-subject { overflow-wrap: anywhere; }
.commit-meta { color: var(--muted); font-size: 12px; white-space: nowrap; }
.no-context { color: var(--muted); padding: 4px 0; }
.note-wrap { padding: 10px 12px 12px; }
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
@media (max-width: 800px) {
  .app { grid-template-columns: 240px minmax(0, 1fr); }
  .line { grid-template-columns: 46px 46px 20px minmax(max-content, 1fr); }
  .view-menu-panel { right: -4px; }
  .review-panel { margin-left: 115px; }
  .context-grid { grid-template-columns: 1fr; }
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
        <button id="hideFile" title="Temporarily remove this file from the active tree">Hide file</button>
        <button id="reviewFile" title="Mark this file reviewed for the temporary review report">Mark reviewed</button>
        <button id="saveReview" title="Save all current-view changes and reviewer notes as plaintext">Save review…</button>
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
            <div class="menu-separator"></div>
            <div class="menu-label">Git context</div>
            <div class="git-row">
              <button id="expandGit" type="button">Expand all</button>
              <button id="collapseGit" type="button">Collapse all</button>
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
const gitContextCache = new Map();
const gapStateById = new Map();
const hiddenFiles = new Set();
const reviewedFiles = new Set();
const reviewedAtByFile = new Map();
const $ = id => document.getElementById(id);
const systemTheme = window.matchMedia('(prefers-color-scheme: light)');
const prefixFor = kind => kind.includes('remove') || kind === 'remove_note' ? '-' : kind.includes('add') || kind === 'add_note' ? '+' : kind === 'context' || kind === 'filtered_context' ? ' ' : '';

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
  const file = currentFile();
  const tl = lineNumberElement(
    line.testLine,
    privacyMode ? null : file?.remote?.testFileUrl,
    'TEST',
  );
  const dl = lineNumberElement(
    line.devLine,
    privacyMode ? null : file?.remote?.devFileUrl,
    'DEV',
  );
  const prefix = document.createElement('div');
  prefix.className = 'prefix';
  prefix.textContent = prefixFor(line.kind);
  const code = document.createElement('div');
  code.className = 'code';
  appendHighlightedText(
    code,
    displayLineText(line),
    privacyMode ? [] : (line.emphasisRanges ?? []),
  );
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


function commitElement(context) {
  const row = document.createElement('div');
  row.className = 'commit';
  const hash = document.createElement('span');
  hash.className = 'commit-hash';
  hash.textContent = context.hash || '—';
  hash.title = context.source === 'line' ? 'Commit blamed for the changed line range' : 'Latest commit touching the file';
  const subject = document.createElement('span');
  subject.className = 'commit-subject';
  subject.textContent = context.subject || 'No commit subject';
  const meta = document.createElement('span');
  meta.className = 'commit-meta';
  meta.textContent = [context.author, context.date, context.source].filter(Boolean).join(' · ');
  row.append(hash, subject, meta);
  return row;
}

function gitSide(label, contexts) {
  const section = document.createElement('div');
  section.className = 'git-side';
  const title = document.createElement('div');
  title.className = 'git-side-title';
  title.textContent = label;
  section.append(title);
  if (!contexts.length) {
    const empty = document.createElement('div');
    empty.className = 'no-context';
    empty.textContent = 'No tracked commit context was available.';
    section.append(empty);
  } else {
    contexts.forEach(context => section.append(commitElement(context)));
  }
  return section;
}

async function loadGitContext(details, change) {
  const content = details.querySelector('.git-content');
  if (content.dataset.loaded === 'true') return;
  content.textContent = 'Loading local Git context…';
  const context = await getGitContext(change);
  content.replaceChildren();
  if (context.error) {
    const error = document.createElement('div');
    error.className = 'no-context';
    error.textContent = context.error;
    content.append(error);
  }
  content.append(gitSide('Incoming DEV', context.dev ?? []));
  content.append(gitSide('Current TEST', context.test ?? []));
  content.dataset.loaded = 'true';
  const newest = context.dev?.[0];
  if (newest) details.querySelector('summary').textContent = `Git context · DEV ${newest.hash} · ${newest.subject}`;
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
  ranges.textContent = `TEST ${change.testRange} → DEV ${change.devRange}`;
  const remoteLinks = document.createElement('span');
  remoteLinks.className = 'review-remote-links';
  for (const [side, url] of [['TEST', change.testRemoteUrl], ['DEV', change.devRemoteUrl]]) {
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

  let splitNotice = null;
  if (change.splitPhysical) {
    splitNotice = document.createElement('div');
    splitNotice.className = 'split-change-note';
    splitNotice.textContent = 'This is one keyed-list value change. Its TEST and DEV rows appear at separate physical YAML positions.';
  }

  let gitDetails;
  if (privacyMode) {
    gitDetails = document.createElement('div');
    gitDetails.className = 'privacy-omitted';
    gitDetails.textContent = 'Git context and remote links are hidden in privacy mode.';
  } else {
    gitDetails = document.createElement('details');
    gitDetails.className = 'git-context';
    const gitSummary = document.createElement('summary');
    gitSummary.textContent = 'Git context · show latest incoming commit message';
    const gitContent = document.createElement('div');
    gitContent.className = 'git-content';
    gitDetails.append(gitSummary, gitContent);
    gitDetails.addEventListener('toggle', () => {
      if (gitDetails.open) loadGitContext(gitDetails, change);
    });
  }


  const noteWrap = document.createElement('div');
  noteWrap.className = 'note-wrap';
  const noteLabel = document.createElement('label');
  noteLabel.className = 'note-label';
  const noteTitle = document.createElement('span');
  noteTitle.textContent = 'Deployment note';
  const noteHelp = document.createElement('span');
  noteHelp.className = 'note-help';
  noteHelp.textContent = privacyMode
    ? 'hidden and omitted from privacy-mode exports'
    : 'kept in this browser until Save review';
  noteLabel.append(noteTitle, noteHelp);
  const textarea = document.createElement('textarea');
  textarea.className = 'review-note';
  if (privacyMode) {
    textarea.disabled = true;
    textarea.placeholder = 'Reviewer notes are hidden in privacy mode.';
    textarea.value = (notesByChange.get(change.key) ?? '').trim()
      ? '[Reviewer note hidden in privacy mode]'
      : '';
  } else {
    textarea.placeholder = 'Add context, a question, or a deployment follow-up for this change…';
    textarea.value = notesByChange.get(change.key) ?? '';
    textarea.addEventListener('input', () => {
      notesByChange.set(change.key, textarea.value);
      notesDirty = true;
      renderTree();
      setStatus('Unsaved reviewer notes · use Save review… to export them', 'busy');
    });
  }
  noteWrap.append(noteLabel, textarea);

  panel.append(heading);
  if (splitNotice) panel.append(splitNotice);
  panel.append(gitDetails, noteWrap);
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
  const shownPath = displayFilePath(file);
  $('path').textContent = stateSuffix ? `${shownPath} · ${stateSuffix}` : shownPath;
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

function setAllGit(open) {
  if (privacyMode) {
    $('viewMenu').open = false;
    setStatus('Git context stays hidden while privacy mode is enabled.', 'busy');
    return;
  }
  document.querySelectorAll('.git-context').forEach(details => {
    details.open = open;
    if (open) {
      const key = details.closest('.review-panel')?.dataset.changeKey;
      const file = snapshot.files[selected];
      const view = mode === 'focused' ? (file.focusedExpanded ?? file.focused) : file.raw;
      const change = allReviewChanges(view).find(item => item.key === key);
      if (change) loadGitContext(details, change);
    }
  });
  $('viewMenu').open = false;
}

function exportFilename() {
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');
  const privacy = privacyMode ? '-private' : '';
  return `config-review-${mode}${privacy}-${stamp}.txt`;
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

function formatCommitLines(side, contexts) {
  const lines = [`  ${side}:`];
  if (!contexts?.length) {
    lines.push('    No tracked commit context was available.');
    return lines;
  }
  for (const context of contexts) {
    const metadata = [context.hash, context.author, context.date, context.source].filter(Boolean).join(' · ');
    lines.push(`    ${metadata}`);
    lines.push(`    ${context.subject || 'No commit subject'}`);
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
    `TEST:      ${privacyMode ? snapshot.privateTarget : snapshot.target}`,
    `DEV:       ${privacyMode ? snapshot.privateSource : snapshot.source}`,
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
      lines.push(`TEST ${change.testRange} -> DEV ${change.devRange}`);
      lines.push('');
      const oldLines = privacyMode ? (change.privateOldLines ?? []) : change.oldLines;
      const newLines = privacyMode ? (change.privateNewLines ?? []) : change.newLines;
      for (const value of oldLines) lines.push(`- ${value}`);
      for (const value of newLines) lines.push(`+ ${value}`);
      if (!change.oldLines.length && !change.newLines.length) lines.push('  (No literal lines available for this logical change.)');
      lines.push('');
      lines.push('Git context:');
      if (privacyMode) {
        lines.push('  (omitted in privacy mode)');
      } else {
        const context = await getGitContext(change);
        lines.push(...formatCommitLines('Incoming DEV', context.dev));
        lines.push(...formatCommitLines('Current TEST', context.test));
        if (context.error) lines.push(`    Warning: ${context.error}`);
      }
      lines.push('');
      const note = (notesByChange.get(change.key) ?? '').trim();
      lines.push('Reviewer note:');
      if (privacyMode) lines.push('  (omitted in privacy mode)');
      else if (note) lines.push(...note.split(/\r?\n/).map(value => `  ${value}`));
      else lines.push('  (none)');
      lines.push('');
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
      : 'Collecting Git context and building plaintext review…',
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
      : 'Collecting Git context and preparing reviewed-files printout…',
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
  button.classList.toggle('active', privacyMode);
  button.setAttribute('aria-pressed', privacyMode ? 'true' : 'false');
  button.textContent = privacyMode ? 'Show original values' : 'Hide sensitive values';
  $('expandGit').disabled = privacyMode;
  $('collapseGit').disabled = privacyMode;
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
$('hideFile').onclick = toggleCurrentHidden;
$('reviewFile').onclick = toggleCurrentReviewed;
$('saveReview').onclick = saveReview;
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
$('expandGit').onclick = () => setAllGit(true);
$('collapseGit').onclick = () => setAllGit(false);
$('privacyToggle').onclick = togglePrivacyMode;
document.querySelectorAll('[data-theme-choice]').forEach(button => {
  button.onclick = () => {
    themeChoice = button.dataset.themeChoice;
    applyTheme();
  };
});
document.addEventListener('keydown', event => {
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
            self._send_bytes(200, self.server.page, "text/html; charset=utf-8")
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
        self.send_error(405)

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
