"""Context dictionary loading and conservative line matching for the web diff viewer."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from ruamel.yaml import YAML
except ImportError:  # pragma: no cover - handled by the normal build dependency
    YAML = None  # type: ignore[assignment]

_CONTEXT_FILENAME = ".config-review-context.yaml"
_SUPPORTED_MATCH_TYPES = {
    "command",
    "env-name",
    "file-name",
    "path",
    "term",
    "yaml-key",
    "yaml-value",
}
_ENV_NAME_RE = re.compile(r"^\s*-\s*name\s*:\s*(?P<value>.*?)\s*$", re.IGNORECASE)
_YAML_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:-\s*)?[\"']?(?P<key>[A-Za-z0-9_.-]+)[\"']?\s*:\s*(?P<value>.*)$"
)


@dataclass(slots=True, frozen=True)
class ContextMatchRule:
    """One explicit rule describing where a dictionary entry should appear."""

    type: str
    value: str
    files: tuple[str, ...] = ()


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
    source: str = "built-in"

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "summary": self.summary,
            "details": self.details,
            "aliases": list(self.aliases),
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
            if any(_rule_matches(rule, relative_path, text) for rule in entry.matches):
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


def _built_in_text() -> str:
    return (
        resources.files("config_review")
        .joinpath("context_catalog.yaml")
        .read_text(encoding="utf-8")
    )


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
    """Load the bundled catalog and merge optional project-local entries by id."""
    diagnostics: list[str] = []
    try:
        built_in = _load_document(_built_in_text(), source="built-in")
    except (OSError, RuntimeError, ValueError) as exc:
        return ContextCatalog((), (f"Built-in context dictionary could not be loaded: {exc}",))

    ordered_ids = [entry.id for entry in built_in]
    entries_by_id = {entry.id: entry for entry in built_in}
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


def _rule_matches(rule: ContextMatchRule, relative_path: str, text: str) -> bool:
    if not _file_allowed(rule.files, relative_path):
        return False

    if rule.type == "path":
        normalized = relative_path.replace("\\", "/")
        return fnmatch.fnmatch(normalized.lower(), rule.value.lower())
    if rule.type == "file-name":
        return Path(relative_path).name.lower() == rule.value.lower()
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
