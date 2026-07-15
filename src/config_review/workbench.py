"""Config Review Workbench module.

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

from .core import (
    AppSettings,
    ChangeBlock,
    DEFAULT_EXCLUDED_DIRS,
    FileRecord,
    HandledChange,
    PatternCandidate,
    PatternRule,
    ProtectedChangeSummary,
    ReviewCounts,
    SessionStore,
    VERSION,
    WorkbenchError,
    atomic_copy,
    atomic_write_bytes,
    atomic_write_text,
    change_decision_token,
    compute_filter_result,
    discover_always_reviewed_summaries,
    discover_project_pattern_candidates,
    discover_yaml_files,
    file_hash,
    file_record_sort_key,
    find_git_root,
    git_checkout_identity,
    git_uncommitted_paths,
    load_project_config,
    parse_editor_command,
    read_file_metadata,
    read_file_snapshot,
    reconciled_handled_entries,
    record_handled_change,
    save_project_config,
    symlink_component,
)
from .rendering import (
    review_unified_diff,
)

class Workbench:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.git_root = find_git_root(settings.target)
        self.initial_uncommitted = git_uncommitted_paths(self.git_root)
        self.session = SessionStore(settings.source, settings.target, self.git_root)
        self.resumed_session_label: str | None = None
        self.patterns: list[PatternRule] = []
        self.excluded_dirs: set[str] = set(DEFAULT_EXCLUDED_DIRS)
        self.hide_whitespace = True
        self.hide_mapping_order = False
        self.mute_non_focused = False
        self.config_diagnostics: list[str] = []
        self.records: list[FileRecord] = []
        self.records_by_path: dict[str, FileRecord] = {}
        self._pattern_candidate_cache: list[PatternCandidate] | None = None
        self._protected_summary_cache: list[ProtectedChangeSummary] | None = None
        self._review_count_cache: dict[str, tuple[str, ReviewCounts]] = {}
        self.reload_config()
        self.scan(initial=True)

    @property
    def enabled_patterns(self) -> list[PatternRule]:
        return [rule for rule in self.patterns if rule.enabled]

    @property
    def review_filter_signature(self) -> str:
        payload = {
            "enabled_patterns": [
                {
                    "id": rule.id,
                    "test_regex": rule.test_regex,
                    "dev_regex": rule.dev_regex,
                    "files": list(rule.files),
                }
                for rule in sorted(self.enabled_patterns, key=lambda item: item.id)
            ],
            "hide_whitespace": self.hide_whitespace,
            "hide_mapping_order": self.hide_mapping_order,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def checkout_identity(self) -> tuple[str, str]:
        return git_checkout_identity(self.git_root)

    @property
    def session_status_text(self) -> str:
        if self.resumed_session_label:
            return f"Loaded session: {self.resumed_session_label} · saves on exit"
        return "New session · saves on exit"

    def has_review_progress(self) -> bool:
        return any(
            record.handled_changes or record.resolved_mode == "manual"
            for record in self.records
        )

    def saved_session_summary(self) -> dict[str, Any] | None:
        branch, commit = self.checkout_identity
        return self.session.saved_summary(
            self.records_by_path,
            self.review_filter_signature,
            branch,
            commit,
            hide_mapping_order=self.hide_mapping_order,
        )

    def _clear_record_review_fields(self, record: FileRecord) -> None:
        record.modified_change_tokens.clear()
        record.kept_change_tokens.clear()
        record.handled_changes.clear()
        record.next_handled_order = 1
        record.resolved = False
        record.resolved_mode = None

    def _rebuild_record_list(self) -> None:
        self.records = [
            record
            for record in self.records_by_path.values()
            if not record.equal or record.edited or record.resolved or record.handled_changes
        ]
        self.records.sort(key=file_record_sort_key)

    def start_fresh_session(self, *, delete_saved: bool = False) -> None:
        self.session.start_fresh()
        for record in self.records_by_path.values():
            self._clear_record_review_fields(record)
        if delete_saved:
            self.session.delete_saved()
        self.resumed_session_label = None
        self._review_count_cache.clear()
        self._rebuild_record_list()
        self.recalculate_completion_all()

    def resume_saved_session(self) -> dict[str, Any] | None:
        summary = self.saved_session_summary()
        if summary is None:
            return None
        self.session.start_fresh()
        for record in self.records_by_path.values():
            self._clear_record_review_fields(record)
            self.session.restore_record(
                record,
                self.review_filter_signature,
                saved=True,
                hide_mapping_order=self.hide_mapping_order,
            )
        self._review_count_cache.clear()
        self._rebuild_record_list()
        self.recalculate_completion_all()
        metadata = self.session.saved_metadata
        branch = str(metadata.get("branch", "unknown"))
        commit = str(metadata.get("commit", ""))
        short_commit = commit[:10] if commit else "no commit"
        self.resumed_session_label = f"{branch} @ {short_commit}"
        return summary

    def save_session(self) -> Path:
        self.session.start_fresh()
        for record in self.records:
            self.save_review_state(record)
        branch, commit = self.checkout_identity
        reviewed_files = sum(
            1
            for record in self.records
            if record.handled_changes or record.resolved
        )
        metadata = {
            "repository": str(self.git_root or ""),
            "source": str(self.settings.source.resolve()),
            "target": str(self.settings.target.resolve()),
            "branch": branch,
            "commit": commit,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tool_version": VERSION,
            "filter_signature": self.review_filter_signature,
            "progress": {
                "files_reviewed": reviewed_files,
                "files_total": len(self.records),
                "handled_changes": sum(len(record.handled_changes) for record in self.records),
            },
        }
        self.session.save_to_disk(metadata)
        self.resumed_session_label = f"{branch} @ {(commit[:10] if commit else 'no commit')}"
        return self.session.path

    def discard_saved_session(self) -> None:
        self.session.delete_saved()

    def review_counts(self, record: FileRecord) -> ReviewCounts:
        state_digest = hashlib.sha256()
        state_digest.update(record.pair_signature.encode())
        state_digest.update(self.review_filter_signature.encode())
        for entry in sorted(record.handled_changes, key=lambda item: item.order):
            state_digest.update(entry.decision_token.encode())
            state_digest.update(entry.action.encode())
        cache_key = state_digest.hexdigest()
        cached = self._review_count_cache.get(record.relative_path)
        if cached is not None and cached[0] == cache_key:
            return cached[1]

        presentation = review_unified_diff(
            record,
            self.enabled_patterns,
            self.settings.context,
            hide_whitespace=self.hide_whitespace,
            hide_mapping_order=self.hide_mapping_order,
            expand_filtered=False,
            selected_change=0,
        )
        counts = ReviewCounts(
            active=presentation.visible_change_count,
            handled=presentation.handled_count,
            pattern_hidden=presentation.pattern_hidden_count,
            whitespace_hidden=presentation.whitespace_hidden_count,
            mapping_order_hidden=presentation.mapping_order_hidden_count,
        )
        self._review_count_cache[record.relative_path] = (cache_key, counts)
        return counts

    def active_change_blocks(self, record: FileRecord) -> list[ChangeBlock]:
        """Return the canonical active Focused Diff blocks for main-list indexing."""
        presentation = review_unified_diff(
            record,
            self.enabled_patterns,
            self.settings.context,
            hide_whitespace=self.hide_whitespace,
            hide_mapping_order=self.hide_mapping_order,
            expand_filtered=False,
            selected_change=0,
        )
        return list(presentation.change_blocks)

    def reload_config(self) -> None:
        (
            self.patterns,
            self.excluded_dirs,
            self.hide_whitespace,
            self.hide_mapping_order,
            self.mute_non_focused,
            self.config_diagnostics,
        ) = load_project_config(self.settings.config_file)
        self._pattern_candidate_cache = None
        self._protected_summary_cache = None
        self._review_count_cache.clear()

    def save_review_state(self, record: FileRecord) -> None:
        self.session.save_record(record, self.review_filter_signature)

    def update_completion(
        self,
        record: FileRecord,
        *,
        reopen_manual: bool = False,
    ) -> ReviewCounts:
        counts = self.review_counts(record)
        if record.read_error or record.binary:
            if record.resolved or reopen_manual:
                record.resolved = False
                record.resolved_mode = None
        elif counts.active == 0 and counts.handled > 0:
            # Automatic completion means the reviewer actually worked through
            # every visible change. A file whose only differences are hidden by
            # patterns/whitespace is FILTERED ONLY, not completed. Only actual
            # review decisions can produce automatic COMPLETE.
            record.resolved = True
            record.resolved_mode = "auto"
        elif record.resolved_mode == "auto" or reopen_manual:
            record.resolved = False
            record.resolved_mode = None
        self.save_review_state(record)
        return counts

    def recalculate_completion_all(self, *, reopen_manual: bool = False) -> None:
        for record in self.records:
            self.update_completion(record, reopen_manual=reopen_manual)

    def handle_change(
        self,
        record: FileRecord,
        block: ChangeBlock,
        action: str,
    ) -> HandledChange:
        entry = record_handled_change(record, block, action)
        self.update_completion(record)
        return entry

    def mark_complete(self, record: FileRecord, complete: bool) -> ReviewCounts:
        counts = self.review_counts(record)
        if not complete:
            record.resolved = False
            record.resolved_mode = None
        elif counts.active == 0 and counts.handled > 0:
            record.resolved = True
            record.resolved_mode = "auto"
        else:
            record.resolved = True
            record.resolved_mode = "manual"
        self.save_review_state(record)
        return counts

    def file_status(self, record: FileRecord) -> tuple[str, ReviewCounts]:
        """Return a short, user-facing status for the main file list."""
        counts = self.review_counts(record)
        if record.read_error or record.binary:
            return "ERROR", counts
        if record.equal:
            if record.edited or counts.handled or record.resolved:
                return "COMPLETE", counts
            return "NO DIFFS", counts
        if record.resolved_mode == "manual":
            if counts.active:
                label = "DIFF" if counts.active == 1 else "DIFFS"
                return f"DONE MANUALLY · {counts.active} {label}", counts
            hidden_total = counts.pattern_hidden + counts.whitespace_hidden + counts.mapping_order_hidden
            if hidden_total:
                return f"DONE MANUALLY · {hidden_total} HIDDEN", counts
            return "DONE MANUALLY", counts
        if counts.active == 0 and counts.handled:
            return "COMPLETE", counts
        if counts.active == 0 and (counts.pattern_hidden or counts.whitespace_hidden or counts.mapping_order_hidden):
            hidden_total = counts.pattern_hidden + counts.whitespace_hidden + counts.mapping_order_hidden
            return f"FILTERED ONLY · {hidden_total}", counts
        if counts.active == 0:
            # A raw text difference that produced no review block (for example,
            # an end-of-file newline edge case) must never look completed.
            return "TEXT DIFF", counts
        if record.change_kind == "DEV ONLY":
            return "DEV ONLY", counts
        if record.change_kind == "TEST ONLY":
            return "TEST ONLY", counts
        label = "DIFF" if counts.active == 1 else "DIFFS"
        return f"{counts.active} {label}", counts

    def pattern_candidates(self, *, refresh: bool = False) -> list[PatternCandidate]:
        if refresh or self._pattern_candidate_cache is None:
            self._pattern_candidate_cache = discover_project_pattern_candidates(
                self.records,
                self.patterns,
                source_name=self.settings.source.name,
                target_name=self.settings.target.name,
            )
        return self._pattern_candidate_cache

    def protected_summaries(self, *, refresh: bool = False) -> list[ProtectedChangeSummary]:
        if refresh or self._protected_summary_cache is None:
            self._protected_summary_cache = discover_always_reviewed_summaries(self.records)
        return self._protected_summary_cache

    def set_hide_whitespace(self, enabled: bool) -> tuple[bool, str]:
        if self.settings.dry_run:
            return False, "Dry-run mode: project display changes are disabled."
        self.hide_whitespace = bool(enabled)
        try:
            save_project_config(
                self.settings.config_file,
                self.patterns,
                self.excluded_dirs,
                self.hide_whitespace,
                self.hide_mapping_order,
                self.mute_non_focused,
            )
            self.reload_config()
            self.recalculate_completion_all(reopen_manual=True)
        except WorkbenchError as exc:
            return False, str(exc)
        state = "hidden" if self.hide_whitespace else "shown"
        return True, (
            f"Whitespace-only blocks are now {state} in Focused Diff. "
            "Full Diff is unchanged."
        )

    def set_hide_mapping_order(self, enabled: bool) -> tuple[bool, str]:
        if self.settings.dry_run:
            return False, "Dry-run mode: project display changes are disabled."
        self.hide_mapping_order = bool(enabled)
        try:
            save_project_config(
                self.settings.config_file,
                self.patterns,
                self.excluded_dirs,
                self.hide_whitespace,
                self.hide_mapping_order,
                self.mute_non_focused,
            )
            self.reload_config()
            self.recalculate_completion_all(reopen_manual=True)
        except WorkbenchError as exc:
            return False, str(exc)
        state = "hidden" if self.hide_mapping_order else "shown"
        return True, (
            f"YAML mapping-order-only scalar changes are now {state} in Focused Diff. "
            "Full Diff is unchanged."
        )

    def set_mute_non_focused(self, enabled: bool) -> tuple[bool, str]:
        if self.settings.dry_run:
            return False, "Dry-run mode: project display changes are disabled."
        self.mute_non_focused = bool(enabled)
        try:
            save_project_config(
                self.settings.config_file,
                self.patterns,
                self.excluded_dirs,
                self.hide_whitespace,
                self.hide_mapping_order,
                self.mute_non_focused,
            )
            self.reload_config()
        except WorkbenchError as exc:
            return False, str(exc)
        state = "enabled" if self.mute_non_focused else "disabled"
        return True, (
            f"Muted non-focused diff content is now {state}. "
            "Diff contents and filtering are unchanged."
        )

    def set_pattern_enabled(self, candidate: PatternCandidate, enabled: bool) -> tuple[bool, str]:
        if self.settings.dry_run:
            return False, "Dry-run mode: project pattern changes are disabled."
        existing = next((rule for rule in self.patterns if rule.id == candidate.rule.id), None)
        if existing is None:
            existing = PatternRule(
                id=candidate.rule.id,
                name=candidate.rule.name,
                test_regex=candidate.rule.test_regex,
                dev_regex=candidate.rule.dev_regex,
                files=(),
                category=candidate.rule.category,
                enabled=enabled,
                kind=candidate.rule.kind,
                source=str(self.settings.config_file),
            )
            self.patterns.append(existing)
        else:
            existing.enabled = enabled
        try:
            save_project_config(
                self.settings.config_file,
                self.patterns,
                self.excluded_dirs,
                self.hide_whitespace,
                self.hide_mapping_order,
                self.mute_non_focused,
            )
            self.reload_config()
            self.recalculate_completion_all(reopen_manual=True)
        except WorkbenchError as exc:
            return False, str(exc)
        state = "hidden" if enabled else "shown"
        return True, f"Project-wide pattern is now {state} in Focused Diff."

    def set_category_patterns(
        self,
        category: str,
        enabled: bool,
    ) -> tuple[bool, str]:
        if self.settings.dry_run:
            return False, "Dry-run mode: project pattern changes are disabled."
        candidates = [
            candidate
            for candidate in self.pattern_candidates(refresh=True)
            if candidate.rule.category == category
        ]
        if not candidates:
            return False, f"No project-wide suggestions were found in {category}."

        by_id = {rule.id: rule for rule in self.patterns}
        changed = 0
        for candidate in candidates:
            rule = by_id.get(candidate.rule.id)
            if rule is None:
                if not enabled:
                    continue
                rule = PatternRule(
                    id=candidate.rule.id,
                    name=candidate.rule.name,
                    test_regex=candidate.rule.test_regex,
                    dev_regex=candidate.rule.dev_regex,
                    files=(),
                    category=candidate.rule.category,
                    enabled=True,
                    kind=candidate.rule.kind,
                    source=str(self.settings.config_file),
                )
                self.patterns.append(rule)
                by_id[rule.id] = rule
                changed += 1
            elif rule.enabled != enabled:
                rule.enabled = enabled
                changed += 1

        try:
            save_project_config(
                self.settings.config_file,
                self.patterns,
                self.excluded_dirs,
                self.hide_whitespace,
                self.hide_mapping_order,
                self.mute_non_focused,
            )
            self.reload_config()
            self.recalculate_completion_all(reopen_manual=True)
        except WorkbenchError as exc:
            return False, str(exc)
        action = "hidden" if enabled else "shown"
        return True, f"{category}: {changed} pattern(s) now {action} in Focused Diff."

    def _reconcile_changed_record(self, record: FileRecord) -> None:
        """Reopen changed content and retain only safely rematched decisions."""
        record.resolved = False
        record.resolved_mode = None
        if record.handled_changes:
            current = compute_filter_result(
                record.test_text,
                record.dev_text,
                [],
                record.relative_path,
                hide_mapping_order=self.hide_mapping_order,
            )
            record.handled_changes = reconciled_handled_entries(
                record, current.blocks
            )
            SessionStore._rebuild_tracking(record)

    def scan(self, *, initial: bool = False) -> None:
        excluded = set(self.excluded_dirs)
        if self.settings.include_secrets:
            excluded.discard("secrets")
        paths = sorted(
            discover_yaml_files(self.settings.source, excluded)
            | discover_yaml_files(self.settings.target, excluded)
        )
        discovered = set(paths)

        for relative in paths:
            dev_path = self.settings.source / relative
            test_path = self.settings.target / relative
            record = self.records_by_path.get(relative)
            is_new = record is None
            if record is None:
                test_exists, test_mode, test_hash, _metadata_error = read_file_metadata(test_path)
                record = FileRecord(
                    relative_path=relative,
                    dev_path=dev_path,
                    test_path=test_path,
                    initial_test_exists=test_exists,
                    initial_test_hash=test_hash,
                    initial_test_bytes=None,
                    initial_test_mode=test_mode,
                    undo_snapshot_captured=False,
                    last_known_test_exists=test_exists,
                    last_known_test_hash=test_hash,
                    uncommitted=test_path.resolve() in self.initial_uncommitted,
                )
                self.records_by_path[relative] = record
                before_signature = None
            else:
                before_signature = record.pair_signature

            record.refresh()
            self._refresh_symlink_state(record)
            if not (is_new or initial) and before_signature != record.pair_signature:
                self._reconcile_changed_record(record)

        # Retain files touched or completed during this review even after their
        # textual diff disappears. Equal files referenced by an optional saved
        # session remain available in records_by_path until the startup choice.
        saved_files = (
            self.session.saved_data.get("files", {})
            if isinstance(self.session.saved_data, Mapping)
            else {}
        )
        saved_paths = set(saved_files) if isinstance(saved_files, Mapping) else set()
        retained: list[FileRecord] = []
        for relative, record in list(self.records_by_path.items()):
            if relative not in discovered:
                before_signature = record.pair_signature
                record.refresh()
                self._refresh_symlink_state(record)
                if before_signature != record.pair_signature:
                    self._reconcile_changed_record(record)
            if not record.equal or record.edited or record.resolved or record.handled_changes:
                retained.append(record)
            elif initial and relative not in saved_paths:
                self.records_by_path.pop(relative, None)

        self.records = retained
        self._pattern_candidate_cache = None
        self._protected_summary_cache = None
        self._review_count_cache.clear()
        self.recalculate_completion_all()
        self.records.sort(key=file_record_sort_key)

    def refresh_record(self, record: FileRecord) -> None:
        before = record.pair_signature
        record.refresh()
        self._refresh_symlink_state(record)
        self._pattern_candidate_cache = None
        self._protected_summary_cache = None
        if before != record.pair_signature:
            self._reconcile_changed_record(record)
        self.update_completion(record)

    def _refresh_symlink_state(self, record: FileRecord) -> Path | None:
        culprit = symlink_component(record.test_path, self.settings.target)
        record.test_symlink_path = str(culprit) if culprit is not None else None
        return culprit

    def _prepare_test_mutation(self, record: FileRecord) -> tuple[bool, str]:
        """Capture a verified lazy undo snapshot before the first TEST mutation."""
        culprit = self._refresh_symlink_state(record)
        if culprit is not None:
            return (
                False,
                "Refusing to modify this TEST file because its path contains a symlink: "
                f"{culprit}. The diff remains viewable, but write actions are disabled.",
            )

        if record.undo_snapshot_captured:
            current_exists, current_hash = self._test_state(record)
            if (
                current_exists != record.last_known_test_exists
                or current_hash != record.last_known_test_hash
            ):
                return (
                    False,
                    "TEST changed outside the tool after its last known action. Nothing was "
                    "modified. Refresh or restart after reviewing the external change.",
                )
            return True, ""

        current_exists, current_hash = self._test_state(record)
        if (
            current_exists != record.initial_test_exists
            or current_hash != record.initial_test_hash
        ):
            return (
                False,
                "TEST changed after this run started, before a safe undo snapshot could be "
                "captured. Nothing was modified. Rescan or restart the workbench after "
                "reviewing the external change.",
            )

        if record.initial_test_exists:
            snapshot_exists, snapshot_bytes, snapshot_mode, snapshot_hash = read_file_snapshot(
                record.test_path
            )
            if (
                not snapshot_exists
                or snapshot_bytes is None
                or snapshot_hash != record.initial_test_hash
            ):
                return (
                    False,
                    "Could not capture a hash-verified session-start TEST snapshot. "
                    "Nothing was modified.",
                )
            if self._refresh_symlink_state(record) is not None:
                return (
                    False,
                    "TEST became symlinked while its undo snapshot was being captured. "
                    "Nothing was modified.",
                )
            record.initial_test_bytes = snapshot_bytes
            record.initial_test_mode = snapshot_mode
        else:
            if record.test_path.exists() or record.test_path.is_symlink():
                return (
                    False,
                    "TEST appeared after this run started, before a safe undo snapshot could "
                    "be captured. Nothing was modified.",
                )
            record.initial_test_bytes = None
            record.initial_test_mode = None

        record.undo_snapshot_captured = True
        return True, ""

    @staticmethod
    def _test_state(record: FileRecord) -> tuple[bool, str | None]:
        exists = record.test_path.exists()
        return exists, file_hash(record.test_path) if exists else None

    def _note_tool_file_state(self, record: FileRecord) -> None:
        record.last_known_test_exists, record.last_known_test_hash = self._test_state(record)

    def undo_session_changes(
        self,
        record: FileRecord,
        *,
        force: bool = False,
    ) -> tuple[bool, str, bool]:
        """Restore TEST to this run's starting state and clear review progress.

        Returns ``(changed, message, confirmation_required)``. File snapshots
        remain memory-only, so content undo is available only in the current
        process. Project-wide patterns are never changed by this action.
        """
        has_review_progress = bool(
            record.handled_changes
            or record.resolved
            or record.modified_change_tokens
            or record.kept_change_tokens
        )

        current_exists, current_hash = self._test_state(record)
        has_content_changes = not (
            current_exists == record.initial_test_exists
            and current_hash == record.initial_test_hash
        )

        if has_content_changes and self._refresh_symlink_state(record) is not None:
            return (
                False,
                "Refusing to undo through a symlinked TEST path. Nothing was modified.",
                False,
            )
        if has_content_changes and not record.undo_snapshot_captured:
            return (
                False,
                "TEST differs from the session-start state, but this run never captured an "
                "undo snapshot for the file. The change may be external, so nothing was undone.",
                False,
            )

        if not has_content_changes and not has_review_progress:
            return False, "No session changes exist for this file.", False

        if self.settings.dry_run and has_content_changes:
            return False, "Dry-run mode: file content cannot be restored.", False

        externally_changed = has_content_changes and (
            current_exists != record.last_known_test_exists
            or current_hash != record.last_known_test_hash
        )
        if externally_changed and not force:
            return (
                False,
                "TEST changed outside the tool after its last known action. "
                "Nothing was undone.",
                True,
            )

        if has_content_changes:
            try:
                if record.initial_test_exists:
                    if record.initial_test_bytes is None:
                        return (
                            False,
                            "The session-start TEST snapshot is unavailable, so file edits "
                            "cannot be undone.",
                            False,
                        )
                    atomic_write_bytes(
                        record.test_path,
                        record.initial_test_bytes,
                        record.initial_test_mode,
                    )
                elif record.test_path.exists():
                    record.test_path.unlink()
            except OSError as exc:
                return False, f"Could not undo session changes: {exc}", False

        self._clear_record_review_fields(record)
        self.session.clear_record(record.relative_path)
        record.refresh()
        self._note_tool_file_state(record)
        self._pattern_candidate_cache = None
        self._protected_summary_cache = None
        self._review_count_cache.pop(record.relative_path, None)
        self.update_completion(record)

        if has_content_changes:
            message = (
                "Restored TEST to how it was when this run started and cleared "
                "this file's review progress."
            )
        else:
            message = "Cleared this file's review progress; TEST was unchanged."
        return True, message, False

    @staticmethod
    def _preferred_newline(*texts: str) -> str:
        for text in texts:
            match = re.search(r"\r\n|\n|\r", text)
            if match:
                return match.group(0)
        return "\n"

    def apply_dev_block_to_test(
        self,
        record: FileRecord,
        block: ChangeBlock,
    ) -> tuple[bool, str, int]:
        """Apply one exact canonical DEV text block to TEST without semantic guessing."""
        if self.settings.dry_run:
            return False, "Dry-run mode: applying DEV to TEST is disabled.", block.old_start + 1

        prepared, prepare_message = self._prepare_test_mutation(record)
        if not prepared:
            return False, prepare_message, block.old_start + 1

        record.refresh()

        # Recompute the canonical diff immediately before writing. This prevents
        # a stale screen block from applying after another process edits TEST.
        current_result = compute_filter_result(
            record.test_text,
            record.dev_text,
            [],
            record.relative_path,
            hide_mapping_order=self.hide_mapping_order,
        )
        selected_token = change_decision_token(block)
        current_matches = [
            candidate
            for candidate in current_result.blocks
            if change_decision_token(candidate) == selected_token
        ]
        if len(current_matches) != 1:
            return (
                False,
                "Unable to apply this change safely. TEST changed since the diff was "
                "displayed or the match is ambiguous. The file was not modified. "
                "Refresh the diff or use vimdiff.",
                block.old_start + 1,
            )
        block = current_matches[0]

        current_lines = record.test_text.splitlines()
        if current_lines[block.old_start : block.old_end] != block.old_lines:
            return (
                False,
                "Unable to apply this change safely. The selected TEST text no longer "
                "matches the current file. The file was not modified. Refresh the diff "
                "or use vimdiff.",
                block.old_start + 1,
            )

        test_raw = record.test_text.splitlines(keepends=True)
        dev_raw = record.dev_text.splitlines(keepends=True)
        if len(test_raw) != len(current_lines) or len(dev_raw) != len(record.dev_text.splitlines()):
            return (
                False,
                "Unable to apply this change safely because line boundaries changed. "
                "The file was not modified. Refresh the diff or use vimdiff.",
                block.old_start + 1,
            )

        newline = self._preferred_newline(record.test_text, record.dev_text)
        replacement: list[str] = []
        for raw_line in dev_raw[block.new_start : block.new_end]:
            content = raw_line.rstrip("\r\n")
            had_ending = raw_line.endswith(("\n", "\r"))
            replacement.append(content + (newline if had_ending else ""))

        # If incoming content has no final newline but more TEST content follows,
        # add the target file's newline so the next line is not concatenated.
        if replacement and block.old_end < len(test_raw) and not replacement[-1].endswith(("\n", "\r")):
            replacement[-1] += newline

        # Inserting after a non-newline-terminated final TEST line needs a separator.
        if (
            block.old_start == block.old_end
            and block.old_start > 0
            and block.old_start == len(test_raw)
            and test_raw
            and not test_raw[-1].endswith(("\n", "\r"))
            and replacement
        ):
            test_raw[-1] += newline

        updated = "".join(
            test_raw[: block.old_start] + replacement + test_raw[block.old_end :]
        )
        try:
            atomic_write_text(record.test_path, updated)
        except OSError as exc:
            return False, f"Could not apply the selected DEV hunk: {exc}", block.old_start + 1

        record.edited = True
        self.refresh_record(record)
        self._note_tool_file_state(record)
        return True, "Applied the selected DEV hunk to TEST.", block.old_start + 1

    def accept_dev_block(
        self,
        record: FileRecord,
        block: ChangeBlock,
    ) -> tuple[bool, str]:
        """Apply the exact selected DEV block and mark it handled immediately."""
        applied, message, _line = self.apply_dev_block_to_test(record, block)
        if not applied:
            return False, message
        self.handle_change(record, block, "ACCEPTED DEV")
        return True, "Accepted the selected DEV change into TEST."

    def pull_dev_block_and_edit(
        self,
        record: FileRecord,
        block: ChangeBlock,
    ) -> tuple[bool, str]:
        applied, message, line = self.apply_dev_block_to_test(record, block)
        if not applied:
            return False, message
        editor_ok, editor_message = self.edit_test(record, line)
        return editor_ok, f"{message} {editor_message}"

    def edit_test(self, record: FileRecord, line: int | None = None) -> tuple[bool, str]:
        if self.settings.dry_run:
            return False, "Dry-run mode: TEST editing is disabled."
        prepared, prepare_message = self._prepare_test_mutation(record)
        if not prepared:
            return False, prepare_message
        command = parse_editor_command(self.settings.edit_command)
        if not command:
            return False, "No edit command configured."
        record.test_path.parent.mkdir(parents=True, exist_ok=True)
        executable = Path(command[0]).name
        if line is not None and executable in {"vim", "nvim", "vi", "view"}:
            command.append(f"+{max(1, line)}")
            command.append(str(record.test_path))
        elif line is not None and executable == "nano":
            command.append(f"+{max(1, line)},1")
            command.append(str(record.test_path))
        elif line is not None and executable in {"code", "code-insiders", "codium"}:
            command.extend(["-g", f"{record.test_path}:{max(1, line)}:1"])
        else:
            command.append(str(record.test_path))
        before = file_hash(record.test_path) if record.test_path.exists() else None
        before_exists = record.test_path.exists()
        try:
            code = subprocess.run(command, check=False).returncode
        except OSError as exc:
            return False, f"Could not run editor: {exc}"
        after_exists = record.test_path.exists()
        after = file_hash(record.test_path) if after_exists else None
        if before_exists != after_exists or before != after:
            record.edited = True
        self.refresh_record(record)
        if before_exists != after_exists or before != after:
            self._note_tool_file_state(record)
        return code == 0, f"Editor exited with status {code}."

    def vimdiff(
        self,
        record: FileRecord,
        test_line: int | None = None,
        dev_line: int | None = None,
    ) -> tuple[bool, str]:
        if self.settings.dry_run:
            return False, "Dry-run mode: vimdiff is disabled because it can modify TEST."
        prepared, prepare_message = self._prepare_test_mutation(record)
        if not prepared:
            return False, prepare_message
        command = parse_editor_command(self.settings.vimdiff_command)
        if not command:
            return False, "No vimdiff command configured."
        executable = Path(command[0]).name
        if executable in {"vim", "nvim"} and "-d" not in command:
            command.append("-d")
        if executable in {"vim", "nvim", "vi", "view", "vimdiff", "nvimdiff"}:
            jump_line = test_line or dev_line
            if jump_line is not None:
                command.append(f"+{max(1, jump_line)}")
        record.test_path.parent.mkdir(parents=True, exist_ok=True)

        temp_dir: tempfile.TemporaryDirectory[str] | None = None
        dev_arg = record.dev_path
        if not record.dev_exists:
            temp_dir = tempfile.TemporaryDirectory(prefix="config-review-missing-source-")
            dev_arg = Path(temp_dir.name) / "DEV_FILE_DOES_NOT_EXIST.yaml"
            dev_arg.write_text("", encoding="utf-8")
        command.extend([str(record.test_path), str(dev_arg)])
        before = file_hash(record.test_path) if record.test_path.exists() else None
        before_exists = record.test_path.exists()
        try:
            code = subprocess.run(command, check=False).returncode
        except OSError as exc:
            if temp_dir is not None:
                temp_dir.cleanup()
            return False, f"Could not run vimdiff: {exc}"
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()
        after_exists = record.test_path.exists()
        after = file_hash(record.test_path) if after_exists else None
        if before_exists != after_exists or before != after:
            record.edited = True
        self.refresh_record(record)
        if before_exists != after_exists or before != after:
            self._note_tool_file_state(record)
        return code == 0, f"vimdiff exited with status {code}."

    def copy_dev_to_test(self, record: FileRecord) -> tuple[bool, str]:
        if self.settings.dry_run:
            return False, "Dry-run mode: copying DEV to TEST is disabled."
        prepared, prepare_message = self._prepare_test_mutation(record)
        if not prepared:
            return False, prepare_message
        try:
            if record.dev_exists:
                atomic_copy(record.dev_path, record.test_path)
                action = "Copied the complete DEV file to TEST."
            else:
                if record.test_path.exists():
                    record.test_path.unlink()
                action = "Removed the TEST file because it does not exist in DEV."
        except OSError as exc:
            return False, f"Could not copy DEV to TEST: {exc}"
        record.edited = True
        self.refresh_record(record)
        self._note_tool_file_state(record)
        return True, action

