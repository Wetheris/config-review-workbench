"""Context dictionary loading and conservative line matching for the web diff viewer."""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from ruamel.yaml import YAML
except ImportError:  # pragma: no cover - handled by the normal build dependency
    YAML = None  # type: ignore[assignment]

_CONTEXT_FILENAME = ".config-review-context.yaml"
_CONTEXT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SUPPORTED_MATCH_TYPES = {
    "command",
    "env-name",
    "file-name",
    "path",
    "path-segment",
    "term",
    "yaml-key",
    "yaml-path",
    "yaml-value",
}
_ENV_NAME_RE = re.compile(r"^\s*-\s*name\s*:\s*(?P<value>.*?)\s*$", re.IGNORECASE)
_YAML_ASSIGNMENT_RE = re.compile(
    r"^(?P<indent>\s*)(?P<dash>-\s*)?(?P<quote>[\"']?)"
    r"(?P<key>[A-Za-z0-9_.$/-]+)(?P=quote)(?P<separator>\s*:\s*)(?P<value>.*)$"
)
_CONTEXT_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*|v\d+(?:alpha\d*|beta\d*)?", re.IGNORECASE)
_CONTEXT_SKIP_TOKENS = {
    "false",
    "no",
    "none",
    "null",
    "off",
    "on",
    "true",
    "yes",
}
_CONTEXT_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_.-])(?:password|passwd|passphrase|token|secret|api.?key|private.?key|"
    r"access.?key|credential|authorization|certificate|keystore|truststore)(?:$|[_.-])",
    re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class ContextMatchRule:
    """One explicit rule describing where a dictionary entry should appear."""

    type: str
    value: str
    files: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "value": self.value,
            "files": list(self.files),
        }


@dataclass(slots=True, frozen=True)
class ContextEntry:
    """One human-readable service, platform, pipeline, or operational definition."""

    id: str
    title: str
    category: str
    summary: str
    details: str = ""
    aliases: tuple[str, ...] = ()
    matches: tuple[ContextMatchRule, ...] = ()
    source: str = "local"

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "summary": self.summary,
            "details": self.details,
            "aliases": list(self.aliases),
            "matches": [rule.payload() for rule in self.matches],
            "source": self.source,
        }


@dataclass(slots=True, frozen=True)
class ContextCatalog:
    """Validated dictionary entries and diagnostics for optional local additions."""

    entries: tuple[ContextEntry, ...]
    diagnostics: tuple[str, ...] = ()

    @property
    def by_id(self) -> dict[str, ContextEntry]:
        return {entry.id: entry for entry in self.entries}

    def payload(self) -> dict[str, Any]:
        return {
            "entries": [entry.payload() for entry in self.entries],
            "diagnostics": list(self.diagnostics),
        }

    def match_line(self, relative_path: str, text: str, *, limit: int = 4) -> list[str]:
        """Return conservative, ordered context matches for one rendered line."""
        matches: list[str] = []
        for entry in self.entries:
            if any(
                rule.type not in {"path", "path-segment", "file-name"}
                and _rule_matches(rule, relative_path, text)
                for rule in entry.matches
            ):
                matches.append(entry.id)
                if len(matches) >= limit:
                    break
        return matches

    def match_lines(
        self,
        relative_path: str,
        lines: Iterable[str],
        *,
        limit: int = 6,
    ) -> list[str]:
        """Return unique context matches across a logical change or range."""
        found: list[str] = []
        for line in lines:
            for entry_id in self.match_line(relative_path, line, limit=limit):
                if entry_id not in found:
                    found.append(entry_id)
                    if len(found) >= limit:
                        return found
        return found

    def match_path_segment(
        self,
        relative_path: str,
        segment: str,
        *,
        is_filename: bool = False,
        limit: int = 4,
    ) -> list[str]:
        """Return definitions attached to one visible path breadcrumb segment."""
        matches: list[str] = []
        for entry in self.entries:
            for rule in entry.matches:
                if not _file_allowed(rule.files, relative_path):
                    continue
                if rule.type == "path-segment" and rule.value.casefold() == segment.casefold():
                    matches.append(entry.id)
                    break
                if (
                    is_filename
                    and rule.type == "file-name"
                    and rule.value.casefold() == segment.casefold()
                ):
                    matches.append(entry.id)
                    break
            if len(matches) >= limit:
                break
        return matches

    def match_path(self, relative_path: str, *, limit: int = 4) -> list[str]:
        """Return entries with explicit path or file-name rules for a changed file."""
        matches: list[str] = []
        for entry in self.entries:
            if any(
                rule.type in {"path", "file-name"} and _rule_matches(rule, relative_path, "")
                for rule in entry.matches
            ):
                matches.append(entry.id)
                if len(matches) >= limit:
                    break
        return matches

    def line_targets(
        self,
        relative_path: str,
        text: str,
        *,
        yaml_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return non-overlapping context targets for one rendered line."""
        return _context_targets_for_text(
            self,
            relative_path,
            text,
            yaml_path=yaml_path,
            path_part=False,
            is_filename=False,
        )

    def path_part_targets(
        self,
        relative_path: str,
        text: str,
        *,
        is_filename: bool = False,
    ) -> list[dict[str, Any]]:
        """Return independent targets inside one visible path breadcrumb part."""
        return _context_targets_for_text(
            self,
            relative_path,
            text,
            yaml_path=None,
            path_part=True,
            is_filename=is_filename,
        )


def _safe_yaml() -> Any:
    if YAML is None:
        raise RuntimeError("ruamel.yaml is required to load the context dictionary")
    yaml = YAML(typ="safe")
    yaml.allow_duplicate_keys = False
    return yaml


def _as_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _as_strings(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a list of strings")
    result: list[str] = []
    for item in value:
        result.append(_as_string(item, field=field))
    return tuple(result)


def _parse_match(value: object, *, entry_id: str) -> ContextMatchRule:
    if not isinstance(value, Mapping):
        raise ValueError(f"entry {entry_id!r} match rules must be mappings")
    match_type = _as_string(value.get("type"), field=f"entry {entry_id!r} match.type")
    if match_type not in _SUPPORTED_MATCH_TYPES:
        supported = ", ".join(sorted(_SUPPORTED_MATCH_TYPES))
        raise ValueError(
            f"entry {entry_id!r} uses unsupported match type {match_type!r}; "
            f"supported types: {supported}"
        )
    match_value = _as_string(value.get("value"), field=f"entry {entry_id!r} match.value")
    files = _as_strings(value.get("files"), field=f"entry {entry_id!r} match.files")
    return ContextMatchRule(match_type, match_value, files)


def _parse_entry(value: object, *, source: str) -> ContextEntry:
    if not isinstance(value, Mapping):
        raise ValueError("context entries must be mappings")
    entry_id = _as_string(value.get("id"), field="entry.id")
    if not _CONTEXT_ID_RE.fullmatch(entry_id):
        raise ValueError(
            "entry.id may contain only letters, numbers, dots, underscores, and hyphens"
        )
    title = _as_string(value.get("title"), field=f"entry {entry_id!r} title")
    category = _as_string(value.get("category"), field=f"entry {entry_id!r} category")
    summary = _as_string(value.get("summary"), field=f"entry {entry_id!r} summary")
    details_value = value.get("details", "")
    details = "" if details_value is None else str(details_value).strip()
    aliases = _as_strings(value.get("aliases"), field=f"entry {entry_id!r} aliases")
    raw_matches = value.get("matches", ())
    if not isinstance(raw_matches, Sequence) or isinstance(raw_matches, (str, bytes)):
        raise ValueError(f"entry {entry_id!r} matches must be a list")
    matches = tuple(_parse_match(item, entry_id=entry_id) for item in raw_matches)
    return ContextEntry(
        id=entry_id,
        title=title,
        category=category,
        summary=summary,
        details=details,
        aliases=aliases,
        matches=matches,
        source=source,
    )


def _load_document(text: str, *, source: str) -> list[ContextEntry]:
    raw = _safe_yaml().load(text)
    if not isinstance(raw, Mapping):
        raise ValueError("context dictionary root must be a mapping")
    if raw.get("schemaVersion") != 1:
        raise ValueError("context dictionary schemaVersion must be 1")
    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)):
        raise ValueError("context dictionary entries must be a list")
    return [_parse_entry(item, source=source) for item in raw_entries]


def context_override_paths(config_file: Path, source: Path, target: Path) -> tuple[Path, ...]:
    """Return unique, ordered local catalog locations relevant to a comparison."""
    candidates = [
        config_file.parent / _CONTEXT_FILENAME,
        source / _CONTEXT_FILENAME,
        target / _CONTEXT_FILENAME,
    ]
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return tuple(result)


def load_context_catalog(config_file: Path, source: Path, target: Path) -> ContextCatalog:
    """Load and merge optional local context dictionaries by entry id.

    No catalog is bundled with the application. A missing local dictionary is a
    valid empty state; users can copy ``.config-review-context.example.yaml`` to
    ``.config-review-context.yaml`` or create definitions from the web editor.
    """
    diagnostics: list[str] = []
    ordered_ids: list[str] = []
    entries_by_id: dict[str, ContextEntry] = {}

    for path in context_override_paths(config_file, source, target):
        if not path.is_file():
            continue
        try:
            additions = _load_document(path.read_text(encoding="utf-8"), source=str(path))
        except (OSError, ValueError, RuntimeError) as exc:
            diagnostics.append(f"Could not load context dictionary {path}: {exc}")
            continue
        for entry in additions:
            if entry.id not in entries_by_id:
                ordered_ids.append(entry.id)
            entries_by_id[entry.id] = entry

    return ContextCatalog(
        tuple(entries_by_id[entry_id] for entry_id in ordered_ids),
        tuple(diagnostics),
    )


def context_edit_path(config_file: Path) -> Path:
    """Return the project-local dictionary file used by the web editor."""
    return config_file.expanduser().resolve().parent / _CONTEXT_FILENAME


def context_line_suggestion(
    relative_path: str,
    text: str,
    *,
    yaml_path: str | None = None,
) -> dict[str, Any] | None:
    """Suggest a precise rule for an undocumented rendered YAML key."""
    env_name = _ENV_NAME_RE.match(text)
    if env_name is not None:
        value = _strip_scalar(env_name.group("value"))
        if value:
            return _context_suggestion(
                match_type="env-name",
                value=value,
                title=value,
                relative_path=relative_path,
                clicked_type="Environment variable name",
                clicked_value=value,
                yaml_path=yaml_path,
            )

    assignment = _YAML_ASSIGNMENT_RE.match(text)
    if assignment is None:
        return None
    key = assignment.group("key")
    return _context_suggestion(
        match_type="yaml-path" if yaml_path else "yaml-key",
        value=yaml_path or key,
        title=key,
        relative_path=relative_path,
        clicked_type="YAML key",
        clicked_value=key,
        yaml_path=yaml_path,
    )


def context_path_part_payload(
    catalog: ContextCatalog,
    relative_path: str,
    segment: str,
    *,
    is_filename: bool = False,
) -> dict[str, Any]:
    """Build one path breadcrumb with independently clickable targets."""
    match_type = "file-name" if is_filename else "path-segment"
    targets = catalog.path_part_targets(
        relative_path,
        segment,
        is_filename=is_filename,
    )
    refs = _unique(ref for target in targets for ref in target.get("contextRefs", []))
    suggestion = _context_suggestion(
        match_type=match_type,
        value=segment,
        title=segment,
        relative_path=relative_path,
        clicked_type="File name" if is_filename else "Path segment",
        clicked_value=segment,
    )
    suggestion["files"] = []
    return {
        "text": segment,
        "contextRefs": refs,
        "contextSuggestion": suggestion,
        "contextTargets": targets,
    }


def yaml_paths_by_line(text: str) -> dict[int, str]:
    """Return a conservative dotted YAML path for mapping-key lines."""
    result: dict[int, str] = {}
    stack: list[tuple[int, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assignment = _YAML_ASSIGNMENT_RE.match(line)
        if assignment is None:
            continue
        indent = len(assignment.group("indent").replace("\t", "    "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = assignment.group("key")
        path = ".".join([item[1] for item in stack] + [key])
        result[line_number] = path
        value = assignment.group("value").strip()
        if not value or value in {"|", ">", "|-", "|+", ">-", ">+"}:
            stack.append((indent, key))
    return result


def _entry_document(entry: ContextEntry) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": entry.id,
        "title": entry.title,
        "category": entry.category,
        "summary": entry.summary,
    }
    if entry.details:
        result["details"] = entry.details
    if entry.aliases:
        result["aliases"] = list(entry.aliases)
    result["matches"] = [rule.payload() for rule in entry.matches]
    return result


def upsert_context_entry(
    config_file: Path,
    value: Mapping[str, object],
) -> tuple[ContextEntry, Path]:
    """Add or override one definition in the project-local context dictionary."""
    path = context_edit_path(config_file)
    entry = _parse_entry(value, source=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlinked context dictionary: {path}")
    file_mode = path.stat().st_mode & 0o777 if path.exists() else 0o644

    if path.is_file():
        existing_text = path.read_text(encoding="utf-8")
        # Validate the complete file before attempting to preserve and modify it.
        _load_document(existing_text, source=str(path))
        yaml = YAML()
        document = yaml.load(existing_text)
    else:
        yaml = YAML()
        document = {"schemaVersion": 1, "entries": []}

    if not isinstance(document, Mapping) or document.get("schemaVersion") != 1:
        raise ValueError("context dictionary schemaVersion must be 1")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("context dictionary entries must be a list")

    replacement = _entry_document(entry)
    replaced = False
    for index, raw_entry in enumerate(raw_entries):
        if isinstance(raw_entry, Mapping) and raw_entry.get("id") == entry.id:
            raw_entries[index] = replacement
            replaced = True
            break
    if not replaced:
        raw_entries.append(replacement)

    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 100
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        yaml.dump(document, stream)
    try:
        os.chmod(temporary, file_mode)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return entry, path


def _file_allowed(patterns: tuple[str, ...], relative_path: str) -> bool:
    if not patterns:
        return True
    normalized = relative_path.replace("\\", "/")
    name = Path(normalized).name
    return any(
        fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(name, pattern)
        for pattern in patterns
    )


def _strip_scalar(value: str) -> str:
    stripped = value.strip().rstrip(",")
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def _term_pattern(value: str) -> re.Pattern[str]:
    pieces = [re.escape(item) for item in re.split(r"[^A-Za-z0-9]+", value) if item]
    if not pieces:
        return re.compile(r"(?!x)x")
    body = r"[-_.\s/]+".join(pieces)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def _rule_matches(
    rule: ContextMatchRule,
    relative_path: str,
    text: str,
    *,
    yaml_path: str | None = None,
) -> bool:
    if not _file_allowed(rule.files, relative_path):
        return False

    if rule.type == "path":
        normalized = relative_path.replace("\\", "/")
        return fnmatch.fnmatch(normalized.lower(), rule.value.lower())
    if rule.type == "path-segment":
        normalized = relative_path.replace("\\", "/")
        return any(part.casefold() == rule.value.casefold() for part in Path(normalized).parts)
    if rule.type == "file-name":
        return Path(relative_path).name.lower() == rule.value.lower()
    if rule.type == "yaml-path":
        return yaml_path is not None and fnmatch.fnmatch(
            yaml_path.casefold(), rule.value.casefold()
        )
    if rule.type == "term":
        return bool(_term_pattern(rule.value).search(text))
    if rule.type == "command":
        return rule.value.lower() in text.lower()

    assignment = _YAML_ASSIGNMENT_RE.match(text)
    if rule.type == "yaml-key":
        return assignment is not None and assignment.group("key").lower() == rule.value.lower()
    if rule.type == "yaml-value":
        return (
            assignment is not None
            and _strip_scalar(assignment.group("value")).lower() == rule.value.lower()
        )
    if rule.type == "env-name":
        env_name = _ENV_NAME_RE.match(text)
        return (
            env_name is not None
            and _strip_scalar(env_name.group("value")).lower() == rule.value.lower()
        )
    return False


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _context_suggestion(
    *,
    match_type: str,
    value: str,
    title: str,
    relative_path: str,
    clicked_type: str,
    clicked_value: str,
    yaml_path: str | None = None,
) -> dict[str, Any]:
    return {
        "type": match_type,
        "value": value,
        "files": [relative_path] if relative_path else [],
        "title": title,
        "clickedType": clicked_type,
        "clickedValue": clicked_value,
        "yamlPath": yaml_path,
        "file": relative_path,
    }


def _matching_ids(
    catalog: ContextCatalog,
    relative_path: str,
    text: str,
    *,
    rule_types: set[str],
    yaml_path: str | None = None,
) -> list[str]:
    result: list[str] = []
    for entry in catalog.entries:
        if any(
            rule.type in rule_types
            and _rule_matches(
                rule,
                relative_path,
                text,
                yaml_path=yaml_path,
            )
            for rule in entry.matches
        ):
            result.append(entry.id)
    return result


def _term_candidates(
    catalog: ContextCatalog,
    relative_path: str,
    text: str,
    *,
    start: int = 0,
    end: int | None = None,
) -> list[tuple[int, int, str]]:
    end = len(text) if end is None else end
    window = text[start:end]
    candidates: list[tuple[int, int, str]] = []
    for entry in catalog.entries:
        for rule in entry.matches:
            if rule.type not in {"term", "command"}:
                continue
            if not _file_allowed(rule.files, relative_path):
                continue
            if rule.type == "term":
                found = _term_pattern(rule.value).finditer(window)
                for match in found:
                    candidates.append((start + match.start(), start + match.end(), entry.id))
            else:
                needle = rule.value.casefold()
                folded = window.casefold()
                offset = 0
                while True:
                    index = folded.find(needle, offset)
                    if index < 0:
                        break
                    candidates.append((start + index, start + index + len(rule.value), entry.id))
                    offset = index + max(1, len(rule.value))
    return candidates


def _select_term_spans(
    candidates: Iterable[tuple[int, int, str]],
) -> list[tuple[int, int, list[str]]]:
    grouped: dict[tuple[int, int], list[str]] = {}
    for start, end, entry_id in candidates:
        grouped.setdefault((start, end), []).append(entry_id)
    ordered = sorted(
        grouped.items(),
        key=lambda item: (-(item[0][1] - item[0][0]), item[0][0]),
    )
    selected: list[tuple[int, int, list[str]]] = []
    for (start, end), ids in ordered:
        overlaps = any(
            start < chosen_end and end > chosen_start for chosen_start, chosen_end, _ in selected
        )
        if overlaps:
            continue
        selected.append((start, end, _unique(ids)))
    selected.sort(key=lambda item: item[0])
    return selected


def _scalar_content_span(
    text: str,
    assignment: re.Match[str],
) -> tuple[int, int] | None:
    raw_start = assignment.start("value")
    raw = assignment.group("value")
    leading = len(raw) - len(raw.lstrip())
    stripped = raw.strip()
    if not stripped:
        return None
    comment = re.search(r"\s+#", stripped)
    if comment is not None:
        stripped = stripped[: comment.start()].rstrip()
    if stripped.endswith(","):
        stripped = stripped[:-1].rstrip()
    quote = (
        stripped[0]
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}
        else ""
    )
    start = raw_start + leading + (1 if quote else 0)
    end = start + len(stripped) - (2 if quote else 0)
    return (start, end) if end > start else None


def _generic_token_targets(
    text: str,
    *,
    start: int,
    end: int,
    occupied: Sequence[tuple[int, int]],
    relative_path: str,
    yaml_path: str | None,
    clicked_type: str,
    match_type: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for match in _CONTEXT_TOKEN_RE.finditer(text, start, end):
        token = match.group(0)
        if token.casefold() in _CONTEXT_SKIP_TOKENS:
            continue
        if len(token) < 2 and not re.fullmatch(r"v\d+", token, re.IGNORECASE):
            continue
        token_start, token_end = match.span()
        overlaps = any(
            token_start < used_end and token_end > used_start for used_start, used_end in occupied
        )
        if overlaps:
            continue
        result.append(
            {
                "start": token_start,
                "end": token_end,
                "text": text[token_start:token_end],
                "contextRefs": [],
                "contextSuggestion": _context_suggestion(
                    match_type=match_type,
                    value=token,
                    title=token,
                    relative_path=relative_path,
                    clicked_type=clicked_type,
                    clicked_value=token,
                    yaml_path=yaml_path,
                ),
            }
        )
    return result


def _target(
    text: str,
    start: int,
    end: int,
    refs: Sequence[str],
    suggestion: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "text": text[start:end],
        "contextRefs": list(refs),
        "contextSuggestion": dict(suggestion),
    }


def _path_part_targets(
    catalog: ContextCatalog,
    relative_path: str,
    text: str,
    *,
    is_filename: bool,
) -> list[dict[str, Any]]:
    exact_refs = catalog.match_path_segment(
        relative_path,
        text,
        is_filename=is_filename,
        limit=max(4, len(catalog.entries)),
    )
    exact_type = "file-name" if is_filename else "path-segment"
    clicked_type = "File name" if is_filename else "Path segment"
    if exact_refs:
        suggestion = _context_suggestion(
            match_type=exact_type,
            value=text,
            title=text,
            relative_path=relative_path,
            clicked_type=clicked_type,
            clicked_value=text,
        )
        suggestion["files"] = []
        return [_target(text, 0, len(text), exact_refs, suggestion)]

    targets: list[dict[str, Any]] = []
    term_spans = _select_term_spans(_term_candidates(catalog, relative_path, text))
    for start, end, refs in term_spans:
        suggestion = _context_suggestion(
            match_type="term",
            value=text[start:end],
            title=text[start:end],
            relative_path=relative_path,
            clicked_type="File-name term" if is_filename else "Path term",
            clicked_value=text[start:end],
        )
        targets.append(_target(text, start, end, refs, suggestion))

    occupied = [(item["start"], item["end"]) for item in targets]
    targets.extend(
        _generic_token_targets(
            text,
            start=0,
            end=len(text),
            occupied=occupied,
            relative_path=relative_path,
            yaml_path=None,
            clicked_type="File-name term" if is_filename else "Path segment",
            match_type="term" if is_filename else "path-segment",
        )
    )
    return sorted(targets, key=lambda item: item["start"])


def _unstructured_targets(
    catalog: ContextCatalog,
    relative_path: str,
    text: str,
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for start, end, refs in _select_term_spans(_term_candidates(catalog, relative_path, text)):
        suggestion = _context_suggestion(
            match_type="term",
            value=text[start:end],
            title=text[start:end],
            relative_path=relative_path,
            clicked_type="Term",
            clicked_value=text[start:end],
        )
        targets.append(_target(text, start, end, refs, suggestion))
    return targets


def _yaml_key_targets(
    catalog: ContextCatalog,
    relative_path: str,
    text: str,
    assignment: re.Match[str],
    yaml_path: str | None,
) -> list[dict[str, Any]]:
    key = assignment.group("key")
    key_start, key_end = assignment.span("key")
    exact_refs = _unique(
        [
            *_matching_ids(
                catalog,
                relative_path,
                text,
                rule_types={"yaml-path"},
                yaml_path=yaml_path,
            ),
            *_matching_ids(
                catalog,
                relative_path,
                text,
                rule_types={"yaml-key"},
            ),
        ]
    )
    exact_suggestion = _context_suggestion(
        match_type="yaml-path" if yaml_path else "yaml-key",
        value=yaml_path or key,
        title=key,
        relative_path=relative_path,
        clicked_type="YAML key",
        clicked_value=key,
        yaml_path=yaml_path,
    )
    if exact_refs:
        return [_target(text, key_start, key_end, exact_refs, exact_suggestion)]

    targets: list[dict[str, Any]] = []
    candidates = _term_candidates(
        catalog,
        relative_path,
        text,
        start=key_start,
        end=key_end,
    )
    for start, end, refs in _select_term_spans(candidates):
        suggestion = _context_suggestion(
            match_type="term",
            value=text[start:end],
            title=text[start:end],
            relative_path=relative_path,
            clicked_type="YAML key term",
            clicked_value=text[start:end],
            yaml_path=yaml_path,
        )
        targets.append(_target(text, start, end, refs, suggestion))

    if not targets:
        return [_target(text, key_start, key_end, [], exact_suggestion)]

    occupied = [(item["start"], item["end"]) for item in targets]
    targets.extend(
        _generic_token_targets(
            text,
            start=key_start,
            end=key_end,
            occupied=occupied,
            relative_path=relative_path,
            yaml_path=yaml_path,
            clicked_type="YAML key term",
            match_type="term",
        )
    )
    return sorted(targets, key=lambda item: item["start"])


def _exact_yaml_value_target(
    catalog: ContextCatalog,
    relative_path: str,
    text: str,
    value_start: int,
    value_end: int,
    yaml_path: str | None,
) -> dict[str, Any] | None:
    refs = _matching_ids(
        catalog,
        relative_path,
        text,
        rule_types={"yaml-value", "env-name"},
    )
    if not refs:
        return None
    scalar = text[value_start:value_end]
    suggestion = _context_suggestion(
        match_type="yaml-value",
        value=scalar,
        title=scalar,
        relative_path=relative_path,
        clicked_type="YAML value",
        clicked_value=scalar,
        yaml_path=yaml_path,
    )
    return _target(text, value_start, value_end, refs, suggestion)


def _yaml_value_term_targets(
    catalog: ContextCatalog,
    relative_path: str,
    text: str,
    *,
    value_start: int,
    value_end: int,
    yaml_path: str | None,
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    candidates = _term_candidates(
        catalog,
        relative_path,
        text,
        start=value_start,
        end=value_end,
    )
    for start, end, refs in _select_term_spans(candidates):
        suggestion = _context_suggestion(
            match_type="term",
            value=text[start:end],
            title=text[start:end],
            relative_path=relative_path,
            clicked_type="YAML value term",
            clicked_value=text[start:end],
            yaml_path=yaml_path,
        )
        targets.append(_target(text, start, end, refs, suggestion))
    return targets


def _yaml_targets(
    catalog: ContextCatalog,
    relative_path: str,
    text: str,
    assignment: re.Match[str],
    yaml_path: str | None,
) -> list[dict[str, Any]]:
    key = assignment.group("key")
    targets = _yaml_key_targets(
        catalog,
        relative_path,
        text,
        assignment,
        yaml_path,
    )
    scalar_span = _scalar_content_span(text, assignment)
    if scalar_span is None:
        return targets

    value_start, value_end = scalar_span
    exact_value = _exact_yaml_value_target(
        catalog,
        relative_path,
        text,
        value_start,
        value_end,
        yaml_path,
    )
    if exact_value is not None:
        targets.append(exact_value)
        return sorted(targets, key=lambda item: item["start"])

    targets.extend(
        _yaml_value_term_targets(
            catalog,
            relative_path,
            text,
            value_start=value_start,
            value_end=value_end,
            yaml_path=yaml_path,
        )
    )
    if not _CONTEXT_SENSITIVE_KEY_RE.search(key):
        occupied = [(item["start"], item["end"]) for item in targets]
        targets.extend(
            _generic_token_targets(
                text,
                start=value_start,
                end=value_end,
                occupied=occupied,
                relative_path=relative_path,
                yaml_path=yaml_path,
                clicked_type="YAML value term",
                match_type="term",
            )
        )
    return sorted(targets, key=lambda item: item["start"])


def _context_targets_for_text(
    catalog: ContextCatalog,
    relative_path: str,
    text: str,
    *,
    yaml_path: str | None,
    path_part: bool,
    is_filename: bool,
) -> list[dict[str, Any]]:
    if path_part:
        return _path_part_targets(
            catalog,
            relative_path,
            text,
            is_filename=is_filename,
        )

    assignment = _YAML_ASSIGNMENT_RE.match(text)
    if assignment is None:
        return _unstructured_targets(catalog, relative_path, text)
    return _yaml_targets(
        catalog,
        relative_path,
        text,
        assignment,
        yaml_path,
    )
