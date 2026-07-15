"""Config Review Workbench Plain module.

Part of the modular Config Review Workbench source distribution. Build the portable
``dist/config-review.pyz`` executable with ``python build.py``.
"""

from __future__ import annotations

import subprocess

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
    BROAD_PATTERN_MIN_MATCHES,
    CATEGORY_ALWAYS_REVIEWED,
    CATEGORY_ORDER,
    ChangeBlock,
    DiffPresentation,
    DisplayLine,
    FileRecord,
    MIN_PATTERN_FILES,
    MIN_PATTERN_MATCHES,
    PatternCandidate,
    ReviewMenuResult,
    VERSION,
    WorkbenchError,
    _range_text,
    color,
    exact_change_still_present,
    file_hash,
    file_section,
    init_project_config,
    parse_editor_command,
    selected_diff_body_range,
)
from .rendering import (
    full_unified_diff,
    mapping_order_status_text,
    review_unified_diff,
    selected_change_preview,
)
from .workbench import (
    Workbench,
)
from .tui import (
    _category_members,
    _category_state,
)


def _plain_kind_styles(kind: str, *, mute_non_focused: bool = False) -> tuple[str, ...]:
    if kind in {"remove", "remove_note"}:
        return ("red",)
    if kind in {"add", "add_note"}:
        return ("green",)
    if kind == "filtered_remove":
        return ("red", "dim") if mute_non_focused else ("red",)
    if kind == "filtered_add":
        return ("green", "dim") if mute_non_focused else ("green",)
    if kind in {"hunk", "file_header", "legend", "filter_item", "rule_title"}:
        return ("cyan", "bold")
    if kind in {"title", "section"}:
        return ("magenta", "bold")
    if kind in {"regex_name", "summary", "selector_selected"}:
        return ("yellow", "bold")
    if kind in {"selector_kept", "handled"}:
        return ("magenta", "bold")
    if kind in {"selector", "regex_pattern"}:
        return ("cyan", "dim")
    if kind == "filtered_header":
        return ("magenta", "bold")
    if kind == "filtered_footer":
        return ("magenta", "dim") if mute_non_focused else ("magenta",)
    if kind == "filtered":
        return ("magenta", "bold")
    if kind.startswith("filtered") or kind == "note":
        return ("dim",)
    if kind == "error":
        return ("red", "bold")
    return ()


def format_display_line(
    line: DisplayLine,
    number_width: int,
    *,
    mute_non_focused: bool = False,
    selected_guide: bool = False,
) -> str:
    body = line.text
    if line.test_line is None and line.dev_line is None:
        return color(body, *_plain_kind_styles(line.kind, mute_non_focused=mute_non_focused))

    test_raw = (
        f"{line.test_line:>{number_width}}" if line.test_line is not None else " " * number_width
    )
    dev_raw = (
        f"{line.dev_line:>{number_width}}" if line.dev_line is not None else " " * number_width
    )
    test_part = color(test_raw, "red") if line.test_line is not None else test_raw
    dev_part = color(dev_raw, "green") if line.dev_line is not None else dev_raw
    marker = "  "
    if line.kind in {"remove", "filtered_remove"}:
        marker = "- "
    elif line.kind in {"add", "filtered_add"}:
        marker = "+ "
    rendered_body = color(
        marker + body,
        *_plain_kind_styles(line.kind, mute_non_focused=mute_non_focused),
    )
    guide = color("│ ", "yellow", "bold") if selected_guide else ""
    return f"{guide}{test_part} {dev_part} {color('│', 'cyan', 'dim')} {rendered_body}"


def print_presentation(
    presentation: DiffPresentation,
    *,
    mute_non_focused: bool = False,
) -> None:
    selected_body_range = selected_diff_body_range(presentation)
    for index, line in enumerate(presentation.lines):
        selected_guide = bool(
            selected_body_range is not None
            and selected_body_range[0] <= index < selected_body_range[1]
        )
        print(
            format_display_line(
                line,
                presentation.number_width,
                mute_non_focused=mute_non_focused,
                selected_guide=selected_guide,
            )
        )


def _test_snapshot(record: FileRecord) -> tuple[bool, str | None]:
    exists = record.test_path.exists()
    return exists, file_hash(record.test_path) if exists else None


def _mark_selected_change_if_edited(
    workbench: Workbench,
    record: FileRecord,
    block: ChangeBlock,
    before: tuple[bool, str | None],
    action: str,
) -> tuple[bool, bool]:
    changed = before != _test_snapshot(record)
    if not changed:
        return False, False
    workbench.refresh_record(record)
    if action != "PULL DEV + EDIT" and exact_change_still_present(
        record, block, hide_mapping_order=workbench.hide_mapping_order
    ):
        return True, False
    handled_action = action
    if action == "PULL DEV + EDIT":
        current_lines = record.test_text.splitlines()
        expected = block.new_lines
        actual = current_lines[block.old_start : block.old_start + len(expected)]
        handled_action = "APPLIED DEV" if actual == expected else "ADAPTED FROM DEV"
    workbench.handle_change(record, block, handled_action)
    return True, True


def plain_pattern_preview(
    workbench: Workbench,
    pattern_id: str,
) -> None:
    while True:
        candidates = workbench.pattern_candidates()
        candidate = next((item for item in candidates if item.rule.id == pattern_id), None)
        if candidate is None:
            print("Noise filter is no longer present after the project changed.")
            return
        print("\n" + "=" * 100)
        print(color(candidate.rule.name, "magenta", "bold"))
        state = "HIDDEN" if candidate.rule.enabled else "VISIBLE"
        print(
            color(
                f"STATE: {state} · MATCHES: {candidate.match_count} · "
                f"FILES: {candidate.file_count} · OVERLAP: {candidate.overlap_count} · "
                f"CATEGORY: {candidate.rule.category} · TYPE: {candidate.rule.kind}",
                "yellow",
                "bold",
            )
        )
        print("Scope: PROJECT-WIDE")
        print(
            color(
                "This is a regex suggestion, not a guarantee of semantic equivalence. "
                "Full Diff is always unfiltered.",
                "yellow",
            )
        )
        if candidate.rule.kind == "environment-fragment":
            print(
                color(
                    "ENVIRONMENT SIGNAL: repeated current-target → incoming-source label "
                    "inside scalar values.",
                    "yellow",
                    "bold",
                )
            )
        if candidate.rule.kind in {"url-shape", "host-shape", "ip-shape"}:
            print(
                color(
                    "BROAD SUGGESTION: review every example before enabling this rule.",
                    "yellow",
                    "bold",
                )
            )
        if candidate.overlap_count:
            print(
                color(
                    f"{candidate.overlap_count} matched change(s) are also covered by another "
                    "pattern. Any enabled matching pattern keeps a block hidden.",
                    "yellow",
                )
            )
        print(color("TEST regex:", "cyan", "bold"))
        print(color(candidate.rule.test_regex, "cyan", "dim"))
        print(color("DEV regex:", "cyan", "bold"))
        print(color(candidate.rule.dev_regex, "cyan", "dim"))
        for index, example in enumerate(candidate.examples, start=1):
            print(color(f"\nExample {index} · {example.relative_path}", "yellow", "bold"))
            if example.old_context_before is not None:
                print(f"  TEST {example.old_line_number - 1}: {example.old_context_before}")
            print(color(f"- TEST {example.old_line_number}: {example.old_line}", "red"))
            if example.old_context_after is not None:
                print(f"  TEST {example.old_line_number + 1}: {example.old_context_after}")
            if example.new_context_before is not None:
                print(f"  DEV  {example.new_line_number - 1}: {example.new_context_before}")
            print(color(f"+ DEV  {example.new_line_number}: {example.new_line}", "green"))
            if example.new_context_after is not None:
                print(f"  DEV  {example.new_line_number + 1}: {example.new_context_after}")
        action = "show" if candidate.rule.enabled else "hide"
        try:
            choice = input(f"\n[t]{action} this project pattern  [b]ack: ").strip().lower()[:1]
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice == "b" or not choice:
            return
        if choice == "t":
            _, message = workbench.set_pattern_enabled(candidate, not candidate.rule.enabled)
            print(message)


def plain_display_filters(workbench: Workbench) -> None:
    while True:
        print("\n" + "=" * 100)
        print(color("DISPLAY OPTIONS", "magenta", "bold"))
        print(
            "Filtering affects Focused Diff only; muting is visual in both views. Full Diff hides nothing."
        )
        print(
            f"[1] {'SHOWN' if not workbench.hide_whitespace else 'HIDDEN':<7} "
            "Show whitespace-only changes"
        )
        print("    Hidden by default; Full Diff always preserves the original whitespace.")
        print(
            f"[2] {'HIDDEN' if workbench.hide_mapping_order else 'VISIBLE':<7} "
            "YAML order-only changes"
        )
        print(
            "    Exact scalar mapping moves and unique name-keyed list moves; "
            "changed named items stay visible as one replacement."
        )
        print(
            f"[3] {'ON' if workbench.mute_non_focused else 'OFF':<7} Mute non-focused diff content"
        )
        print(
            "    Off by default. Keeps the selected change bright and softens surrounding "
            "and expanded filtered diff content."
        )
        print("[b] Back")
        try:
            choice = input("Display filters: ").strip().lower()[:1]
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in {"b", "q", ""}:
            return
        if choice == "1":
            _, message = workbench.set_hide_whitespace(not workbench.hide_whitespace)
            print(message)
        elif choice == "2":
            _, message = workbench.set_hide_mapping_order(not workbench.hide_mapping_order)
            print(message)
        elif choice == "3":
            _, message = workbench.set_mute_non_focused(not workbench.mute_non_focused)
            print(message)


def plain_pattern_manager(workbench: Workbench) -> None:
    while True:
        candidates = workbench.pattern_candidates()
        protected = workbench.protected_summaries()
        print("\n" + "=" * 100)
        print(color("NOISE FILTERS", "magenta", "bold"))
        print(
            f"Scanned {len(workbench.records)} changed file(s); noise filters apply project-wide."
        )
        print(
            color(
                "Noise suggestions are hidden by default. Full Diff always remains literal.",
                "yellow",
            )
        )
        print(
            "Display filters: whitespace "
            f"{'HIDDEN' if workbench.hide_whitespace else 'VISIBLE'} · YAML order "
            f"{'HIDDEN' if workbench.hide_mapping_order else 'VISIBLE'} · background "
            f"{'MUTED' if workbench.mute_non_focused else 'FULL BRIGHTNESS'}."
        )

        pattern_numbers: dict[int, PatternCandidate] = {}
        category_numbers: dict[int, str] = {}
        pattern_index = 1
        category_index = 1
        for category in CATEGORY_ORDER:
            members = _category_members(candidates, category)
            if not members:
                continue
            state = _category_state(members)
            match_count = sum(candidate.match_count for candidate in members)
            files = len({path for candidate in members for path in candidate.affected_files})
            print(
                color(
                    f"\n[C{category_index}] {state:<7} {category.upper()} · "
                    f"{len(members)} pattern(s) · {match_count} pattern matches · {files} file(s)",
                    "magenta",
                    "bold",
                )
            )
            category_numbers[category_index] = category
            category_index += 1
            for candidate in members:
                state = "HIDDEN" if candidate.rule.enabled else "VISIBLE"
                source = "saved" if candidate.persisted else "suggested"
                overlap = f" · overlap {candidate.overlap_count}" if candidate.overlap_count else ""
                print(
                    f"  [{pattern_index:>2}] {state:<8} {candidate.match_count:>3} change(s) · "
                    f"{candidate.file_count:>2} file(s){overlap} · "
                    f"{candidate.rule.name} · {source}"
                )
                pattern_numbers[pattern_index] = candidate
                pattern_index += 1

        if protected:
            print(color(f"\n{CATEGORY_ALWAYS_REVIEWED.upper()} · cannot be hidden", "red", "bold"))
            for summary in protected:
                print(
                    f"  VISIBLE  {summary.match_count:>3} change(s) · "
                    f"{summary.file_count:>2} file(s) · {summary.name}"
                )

        if not candidates and not protected:
            print(
                f"No repeated project-wide noise filters found. Narrow suggestions require "
                f"{MIN_PATTERN_MATCHES}+ matches; broad suggestions require "
                f"{BROAD_PATTERN_MIN_MATCHES}+ unless they span {MIN_PATTERN_FILES}+ files."
            )

        print("\nEnter a noise-filter number to preview it.")
        print("[cN] toggle category N  [f] display options  [b] back")
        try:
            choice = input("Noise filters: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in {"b", "q", ""}:
            return
        if choice == "f":
            plain_display_filters(workbench)
            continue
        if choice.startswith("c") and choice[1:].isdigit():
            category = category_numbers.get(int(choice[1:]))
            if category is not None:
                members = _category_members(candidates, category)
                enable = not members or not all(candidate.rule.enabled for candidate in members)
                _, message = workbench.set_category_patterns(category, enable)
                print(message)
            continue
        try:
            index = int(choice)
        except ValueError:
            continue
        candidate = pattern_numbers.get(index)
        if candidate is not None:
            plain_pattern_preview(workbench, candidate.rule.id)


def plain_filters(workbench: Workbench) -> None:
    while True:
        print("\nFILTERS")
        print("[1] Noise filters — repeated environment, domain, endpoint, and project noise")
        print("[2] Display options — whitespace, safe YAML order noise, and focused contrast")
        print("[b] Back")
        try:
            choice = input("Filters: ").strip().lower()[:1]
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in {"b", "q", ""}:
            return
        if choice == "1":
            plain_pattern_manager(workbench)
        elif choice == "2":
            plain_display_filters(workbench)


def plain_report_options(workbench: Workbench, record: FileRecord, *, mode: str) -> None:
    if workbench.report_change_count(record, mode) == 0:
        print("No visible differences are available in the current view; report was not generated.")
        return
    include_context_labels = True
    include_git_context = True
    while True:
        presentation = workbench._report_presentation(record, mode)
        print("\nVISIBLE-DIFF REPORT")
        print(
            f"{record.relative_path} · {'Full Diff' if mode == 'full' else 'Focused Diff'} · "
            f"{presentation.visible_change_count} selectable difference(s)"
        )
        print(f"[1] [{'x' if include_context_labels else ' '}] Context labels")
        print(f"[2] [{'x' if include_git_context else ' '}] Git commit context")
        print("[o] Open report in editor")
        print("[s] Save report under .config-review-reports/")
        print("[p] Print report to terminal")
        print("[b] Back")
        try:
            choice = input("Report: ").strip().lower()[:1]
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in {"b", "q", ""}:
            return
        if choice == "1":
            include_context_labels = not include_context_labels
            continue
        if choice == "2":
            include_git_context = not include_git_context
            continue
        if choice == "p":
            try:
                report = workbench.generate_file_report(
                    record,
                    mode=mode,
                    include_context_labels=include_context_labels,
                    include_git_context=include_git_context,
                )
            except WorkbenchError as exc:
                print(exc)
                return
            print("\n" + report)
            continue
        if choice in {"o", "s"}:
            try:
                path = workbench.save_file_report(
                    record,
                    mode=mode,
                    include_context_labels=include_context_labels,
                    include_git_context=include_git_context,
                )
            except (OSError, WorkbenchError) as exc:
                print(f"Could not create report: {exc}")
                continue
            if choice == "s":
                print(f"Saved report to {path}")
                continue
            command = parse_editor_command(workbench.settings.edit_command)
            if not command:
                print(f"No edit command configured. Report was saved to {path}")
                continue
            try:
                code = subprocess.run([*command, str(path)], check=False).returncode
                print(f"Report editor exited with status {code}; saved to {path}")
            except OSError as exc:
                print(f"Could not open report editor: {exc}")


def plain_file_actions(workbench: Workbench, record: FileRecord, *, mode: str) -> bool:
    while True:
        _status, counts = workbench.file_status(record)
        copy_label = (
            "Copy the complete DEV file to TEST"
            if record.dev_exists
            else "Delete TEST because DEV is absent"
        )
        completion_label = (
            "Reopen file for review"
            if record.resolved_mode == "manual"
            else f"Mark file complete ({counts.active} active difference(s))"
        )
        print("\nFILE ACTIONS")
        print(f"[m] {completion_label}")
        print("[u] Undo this run's file changes")
        report_count = workbench.report_change_count(record, mode)
        if report_count:
            print(f"[r] Visible-diff report ({report_count})")
        else:
            print("[r] No visible differences to report")
        print(f"[c] {copy_label}")
        print("[b] Back")
        try:
            choice = input("Action: ").strip().lower()[:1]
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if choice in {"b", "q", ""}:
            return False
        if choice == "m":
            if record.resolved_mode == "manual":
                workbench.mark_complete(record, False)
                print("Reopened the file for review.")
                return True
            if counts.active:
                updated = workbench.mark_complete(record, True)
                print(f"Marked done manually with {updated.active} active diff(s).")
                return True
            print("No active diffs remain; this file is already complete.")
            continue
        if choice == "u":
            answer = (
                input(
                    "Undo this run's file edits and review progress? Noise filters stay unchanged. [y/N]: "
                )
                .strip()
                .lower()
            )
            if answer != "y":
                continue
            changed, message, needs_confirmation = workbench.undo_session_changes(record)
            if needs_confirmation:
                answer = (
                    input(
                        "TEST changed outside the tool. Restore the session-start copy anyway? [y/N]: "
                    )
                    .strip()
                    .lower()
                )
                if answer == "y":
                    changed, message, _ = workbench.undo_session_changes(record, force=True)
            print(message)
            return changed
        if choice == "r":
            if report_count == 0:
                print(
                    "No visible differences are available in the current view; "
                    "report was not generated."
                )
                continue
            plain_report_options(workbench, record, mode=mode)
            continue
        if choice != "c":
            continue
        question = "Replace TEST with the complete DEV file?"
        if not record.dev_exists:
            question = "DEV is absent. Delete TEST?"
        if record.uncommitted:
            question += " TEST had pre-existing uncommitted changes."
        answer = input(question + " [y/N]: ").strip().lower()
        if answer != "y":
            continue
        _, message = workbench.copy_dev_to_test(record)
        print(message)
        return True


def plain_review_actions(
    workbench: Workbench,
    record: FileRecord,
    *,
    mode: str,
    selected_change: int,
) -> ReviewMenuResult:
    changed_any = False
    while True:
        workbench.refresh_record(record)
        if mode == "focused":
            presentation = review_unified_diff(
                record,
                workbench.enabled_patterns,
                workbench.settings.context,
                hide_whitespace=workbench.hide_whitespace,
                hide_mapping_order=workbench.hide_mapping_order,
                expand_filtered=False,
                selected_change=selected_change,
            )
        else:
            presentation = full_unified_diff(
                record,
                workbench.settings.context,
                selected_change=selected_change,
            )
        count = presentation.visible_change_count
        block = presentation.selected_block
        if not count or block is None:
            print("No reviewable changes remain in this view.")
            return ReviewMenuResult(0, changed=changed_any)

        selected_change = presentation.selected_change or 0
        print("\n" + "=" * 100)
        print(
            color(
                f"REVIEW ACTIONS · ACTIVE CHANGE {selected_change + 1}/{count}", "magenta", "bold"
            )
        )
        print(
            color(
                f"{record.relative_path} · TEST {_range_text(block.old_start, block.old_end)} · "
                f"DEV {_range_text(block.new_start, block.new_end)}",
                "yellow",
                "bold",
            )
        )
        print("Only this selected block is shown with one nearby context line.")
        print_presentation(
            selected_change_preview(record, block, context=1),
            mute_non_focused=workbench.mute_non_focused,
        )
        print("Resolve: [a]ccept DEV  [s]keep TEST")
        print("Edit: [l]pull DEV + edit  [e]dit TEST  [v]vimdiff")
        print("Navigate: [j/k] next/previous diff  [ / ] Previous/next file  [b]ack  [q]uit")
        try:
            choice = input("Action: ").strip()[:1]
        except (EOFError, KeyboardInterrupt):
            print()
            return ReviewMenuResult(selected_change, changed=changed_any, quit=True)
        lowered = choice.lower()
        if lowered == "q":
            return ReviewMenuResult(selected_change, changed=changed_any, quit=True)
        if lowered == "b" or not choice:
            return ReviewMenuResult(selected_change, changed=changed_any)
        if lowered in {"n", "j"}:
            selected_change = (selected_change + 1) % count
            continue
        if lowered in {"p", "k"}:
            selected_change = (selected_change - 1) % count
            continue
        if choice == "[":
            return ReviewMenuResult(selected_change, changed=changed_any, file_delta=-1)
        if choice == "]":
            return ReviewMenuResult(selected_change, changed=changed_any, file_delta=1)
        if lowered == "s":
            workbench.handle_change(record, block, "KEPT TEST")
            print("Kept TEST; the change moved to session history.")
            continue
        if lowered == "a":
            accepted, message = workbench.accept_dev_block(record, block)
            print(message)
            changed_any = changed_any or accepted
            continue

        before = _test_snapshot(record)
        if lowered == "l":
            action = "PULL DEV + EDIT"
            _, message = workbench.pull_dev_block_and_edit(record, block)
        elif lowered == "e":
            action = "EDITED TEST"
            _, message = workbench.edit_test(record, block.old_start + 1)
        elif lowered == "v":
            action = "EDITED VIA VIMDIFF"
            _, message = workbench.vimdiff(record, block.old_start + 1, block.new_start + 1)
        else:
            continue
        print(message)
        file_changed, handled = _mark_selected_change_if_edited(
            workbench, record, block, before, action
        )
        if handled:
            print("Moved the selected change to session history.")
        elif file_changed:
            print("TEST changed, but the selected change remains active.")
        changed_any = changed_any or file_changed


def plain_detail(workbench: Workbench, record: FileRecord) -> str:
    expand_filtered = False
    selected_change = 0
    mode = "focused"
    while True:
        workbench.refresh_record(record)
        if mode == "focused":
            presentation = review_unified_diff(
                record,
                workbench.enabled_patterns,
                workbench.settings.context,
                hide_whitespace=workbench.hide_whitespace,
                hide_mapping_order=workbench.hide_mapping_order,
                expand_filtered=expand_filtered,
                selected_change=selected_change,
            )
            selected_change = presentation.selected_change or 0
            pattern_hidden = presentation.pattern_hidden_count
            whitespace_hidden = presentation.whitespace_hidden_count
            mapping_hidden = presentation.mapping_order_hidden_count
            mapping_state = mapping_order_status_text(
                enabled=workbench.hide_mapping_order,
                hidden_count=mapping_hidden,
                unavailable_reason=presentation.mapping_order_unavailable_reason,
            )
            mode_title = (
                f"FOCUSED DIFF — {presentation.visible_change_count} active · "
                f"{presentation.handled_count} handled · "
                f"{pattern_hidden} noise-hidden · {whitespace_hidden} whitespace-hidden · "
                f"{'expanded' if expand_filtered else 'collapsed'}"
            )
            mapping_note = mapping_state
        else:
            presentation = full_unified_diff(
                record,
                workbench.settings.context,
                selected_change=selected_change,
            )
            selected_change = presentation.selected_change or 0
            mode_title = (
                f"FULL DIFF — {presentation.visible_change_count} active · "
                f"{presentation.handled_count} handled · NOISE FILTERING DISABLED"
            )
            mapping_note = "Display filters are disabled in Full Diff."

        print("\n" + "=" * 100)
        print(color(record.relative_path, "magenta", "bold"))
        file_status, focused_counts = workbench.file_status(record)
        status_style = ("yellow", "bold")
        print(color(f"{file_status} | {' · '.join(record.states) or '—'}", *status_style))
        print(color(mode_title, "yellow", "bold"))
        mapping_styles = ("red", "bold") if "UNAVAILABLE" in mapping_note else ("cyan",)
        print(color(mapping_note, *mapping_styles))
        selected_block = presentation.selected_block
        if presentation.visible_change_count and selected_block is not None:
            print(
                color(
                    f"ACTIVE CHANGE {selected_change + 1}/{presentation.visible_change_count}; "
                    "use j/k to step through active differences.",
                    "yellow",
                )
            )
        else:
            if mode == "focused" and presentation.handled_count:
                note = "No active diffs remain. This file is complete."
            elif mode == "focused" and (
                presentation.pattern_hidden_count
                or presentation.whitespace_hidden_count
                or presentation.mapping_order_hidden_count
            ):
                note = "No active diffs; remaining differences are FILTERED ONLY."
            elif mode == "focused":
                note = "No active review blocks were produced; inspect Full Diff."
            else:
                note = "No active changes remain in Full Diff."
            print(color(note, "yellow"))
        print_presentation(
            presentation,
            mute_non_focused=workbench.mute_non_focused,
        )
        print()
        if mode == "focused":
            hidden_action = "[h]collapse hidden" if expand_filtered else "[h]expand hidden"
            print(
                f"[j/k] next/previous diff  [Enter]review  [d]full diff  "
                f"{hidden_action}  [f]filters"
            )
        else:
            print("[j/k] next/previous diff  [Enter]review  [d]focused diff  [f]filters")
        print("[a]file actions  [ / ] Previous/next file  [b]back  [q]quit")
        try:
            raw_choice = input("Action: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "quit"
        choice = raw_choice[:1]
        if choice == "q":
            return "quit"
        if choice == "b":
            return "back"
        if raw_choice == "[":
            return "prev_file"
        if raw_choice == "]":
            return "next_file"
        if choice == "d":
            mode = "full" if mode == "focused" else "focused"
            selected_change = 0
            continue
        if choice == "h" and mode == "focused":
            expand_filtered = not expand_filtered
            continue
        if choice == "f":
            plain_filters(workbench)
            selected_change = 0
            continue
        if choice == "g":
            # Backward-compatible shortcut for the former Patterns command.
            plain_pattern_manager(workbench)
            selected_change = 0
            continue
        if choice == "m":
            if record.resolved_mode == "manual":
                workbench.mark_complete(record, False)
                print("Reopened the file for review.")
            elif focused_counts.active:
                counts = workbench.mark_complete(record, True)
                print(f"Marked done manually with {counts.active} active diff(s).")
            else:
                print("No active diffs remain; this file is already automatically complete.")
            continue
        if choice == "u":
            answer = (
                input(
                    "Undo this run's file edits and review progress? Noise filters stay unchanged. [y/N]: "
                )
                .strip()
                .lower()
            )
            if answer != "y":
                continue
            changed, message, needs_confirmation = workbench.undo_session_changes(record)
            if needs_confirmation:
                answer = (
                    input(
                        "TEST changed outside the tool. Restore the session-start copy anyway? [y/N]: "
                    )
                    .strip()
                    .lower()
                )
                if answer == "y":
                    changed, message, _ = workbench.undo_session_changes(record, force=True)
            print(message)
            if changed:
                selected_change = 0
            continue
        if choice in {"a", "x"}:
            changed = plain_file_actions(workbench, record, mode=mode)
            if changed:
                selected_change = 0
            continue
        if choice in {"n", "p", "j", "k"}:
            count = presentation.visible_change_count
            if count:
                selected_change = (
                    (selected_change + 1) % count
                    if choice in {"n", "j"}
                    else (selected_change - 1) % count
                )
            continue
        if raw_choice == "":
            if selected_block is None:
                print("No selected change is available for actions.")
                continue
            result = plain_review_actions(
                workbench,
                record,
                mode=mode,
                selected_change=selected_change,
            )
            if result.quit:
                return "quit"
            if result.file_delta < 0:
                return "prev_file"
            if result.file_delta > 0:
                return "next_file"
            selected_change = result.selected_change


def plain_startup_session_prompt(workbench: Workbench) -> bool:
    if not workbench.session.has_saved:
        return True
    while True:
        summary = workbench.saved_session_summary()
        if summary is None:
            workbench.start_fresh_session(delete_saved=True)
            return True
        commit = str(summary["commit"])
        short_commit = commit[:10] if commit else "no commit"
        print("\n" + "=" * 100)
        print(color("LOAD LAST REVIEW SESSION?", "magenta", "bold"))
        print(f"Saved from: {summary['branch']} @ {short_commit}")
        print(f"Saved:      {summary['saved_at']}")
        print(
            f"Progress:   {summary['files_reviewed']}/{summary['files_total']} files reviewed · "
            f"{summary['total_handled']} handled changes"
        )
        if summary["exact"]:
            print(
                color("The saved review exactly matches the current checkout and filters.", "green")
            )
        else:
            print(
                color(
                    "The checkout or comparison changed since this review was saved.",
                    "yellow",
                    "bold",
                )
            )
            print(
                color(
                    f"{summary['verified_handled']}/{summary['total_handled']} handled changes "
                    "can still be verified; uncertain progress returns to review.",
                    "yellow",
                )
            )
        try:
            choice = input("Load the last session? [y]es / [n]o: ").strip().lower()[:1]
        except (EOFError, KeyboardInterrupt):
            print()
            choice = "n"
        if choice == "y":
            restored = workbench.resume_saved_session()
            if restored is not None:
                print(
                    f"Loaded last review; verified "
                    f"{restored['verified_handled']}/{restored['total_handled']} handled changes."
                )
            return True
        if choice == "n":
            workbench.start_fresh_session(delete_saved=True)
            print("Deleted the last review session and started fresh.")
            return True


def plain_exit_session_prompt(workbench: Workbench) -> bool:
    try:
        path = workbench.save_session()
    except WorkbenchError as exc:
        print(f"Could not save review session: {exc}")
        return False
    print(f"Saved review session to {path}")
    return True


def run_plain(workbench: Workbench) -> int:
    if not plain_startup_session_prompt(workbench):
        return 0
    selected = 0
    while True:
        workbench.scan()
        records = workbench.records
        print("\n" + "=" * 100)
        print(color(f"CONFIG REVIEW WORKBENCH v{VERSION}", "magenta", "bold"))
        print(f"DEV:  {workbench.settings.source}")
        print(f"TEST: {workbench.settings.target}")
        git_style = ("yellow", "bold") if workbench.git_status.warning else ("green",)
        print(color(workbench.git_status.summary, *git_style))
        print(color(workbench.session_status_text, "cyan"))
        print(
            color(
                f"Noise filters: {len(workbench.enabled_patterns)} hidden · "
                f"display options: whitespace "
                f"{'hidden' if workbench.hide_whitespace else 'visible'}, YAML order "
                f"{'hidden' if workbench.hide_mapping_order else 'visible'}, background "
                f"{'muted' if workbench.mute_non_focused else 'full brightness'}. "
                "New suggestions are hidden by default.",
                "yellow",
            )
        )
        if not records:
            print(color("No remaining DEV/TEST YAML differences or review history.", "green"))
            return 0
        status_rows = [(record, *workbench.file_status(record)) for record in records]
        remaining = sum(counts.active for _, _, counts in status_rows)
        complete = sum(status == "COMPLETE" for _, status, _ in status_rows)
        filtered_only = sum(status.startswith("FILTERED ONLY") for _, status, _ in status_rows)
        remaining_text = "NO ACTIVE DIFFS" if remaining == 0 else f"Active diffs: {remaining}"
        print(
            f"Files: {len(records)} · {remaining_text} · Complete: {complete} · "
            f"Filtered only: {filtered_only}"
        )
        current_section: str | None = None
        for index, (record, status_text, _counts) in enumerate(status_rows, start=1):
            section = file_section(record.relative_path)
            if section != current_section:
                print(color(f"\n── {section} " + "─" * max(1, 82 - len(section)), "cyan", "bold"))
                current_section = section
            states = " · ".join(record.states) or "—"
            if status_text == "ERROR" or status_text == "TEST ONLY":
                styles = ("red", "bold")
            elif status_text == "DEV ONLY":
                styles = ("cyan", "bold")
            elif status_text.startswith("DONE MANUALLY"):
                styles = ("magenta", "bold")
            elif status_text == "COMPLETE":
                styles = ("green", "bold")
            elif status_text.startswith("FILTERED ONLY") or status_text == "NO DIFFS":
                styles = ("dim",)
            elif "DIFF" in status_text:
                styles = ("yellow", "bold")
            else:
                styles = ()
            padded_states = f"{states:<24}"
            state_text = (
                color(padded_states, "red", "bold") if record.test_symlink_path else padded_states
            )
            print(
                f"[{index:>2}] {color(f'{status_text:<30}', *styles)} "
                f"{state_text} {record.relative_path}"
            )
        print("[f]filters  [s]rescan/Git check  [x]project config  [q]uit")
        try:
            raw_choice = input("Open file number: ").strip()
            choice = raw_choice.lower()
        except (EOFError, KeyboardInterrupt):
            print()
            if plain_exit_session_prompt(workbench):
                return 0
            continue
        if choice == "q":
            if plain_exit_session_prompt(workbench):
                return 0
            continue
        if choice == "s":
            workbench.refresh_git_status(fetch_remote=True)
            print(workbench.git_status.summary)
            continue
        if choice.startswith("u"):
            index_text = choice[1:]
            if not index_text:
                index_text = input("File number: ").strip()
            try:
                undo_index = int(index_text) - 1
            except ValueError:
                continue
            if 0 <= undo_index < len(records):
                record = records[undo_index]
                answer = (
                    input(
                        "Undo this run's file edits and review progress? Noise filters stay unchanged. [y/N]: "
                    )
                    .strip()
                    .lower()
                )
                if answer != "y":
                    continue
                changed, message, needs_confirmation = workbench.undo_session_changes(record)
                if needs_confirmation:
                    answer = (
                        input(
                            "TEST changed outside the tool. Restore the session-start copy anyway? [y/N]: "
                        )
                        .strip()
                        .lower()
                    )
                    if answer == "y":
                        changed, message, _ = workbench.undo_session_changes(record, force=True)
                print(message)
            continue
        if choice == "p":
            plain_pattern_manager(workbench)
            continue
        if choice == "f":
            plain_filters(workbench)
            continue
        if choice == "x":
            if workbench.settings.dry_run:
                print("Dry-run mode: project-config editing is disabled.")
                continue
            path = workbench.settings.config_file
            if not path.exists():
                answer = input(f"Create project configuration {path}? [y/N]: ").strip().lower()
                if answer != "y":
                    continue
                try:
                    init_project_config(path)
                except WorkbenchError as exc:
                    print(f"Could not create project config: {exc}")
                    continue
            command = parse_editor_command(workbench.settings.edit_command)
            if not command:
                print("No edit command configured.")
                continue
            command.append(str(path))
            try:
                code = subprocess.run(command, check=False).returncode
                workbench.reload_config()
                workbench.recalculate_completion_all(reopen_manual=True)
                workbench.scan()
                print(
                    f"Project config editor exited with status {code}; "
                    f"loaded {len(workbench.patterns)} noise filter(s)."
                )
            except (OSError, WorkbenchError) as exc:
                print(f"Could not edit/reload project config: {exc}")
            continue
        try:
            selected = int(choice) - 1
        except ValueError:
            continue
        if 0 <= selected < len(records):
            while records:
                result = plain_detail(workbench, records[selected])
                if result == "quit" and plain_exit_session_prompt(workbench):
                    return 0
                if result == "next_file":
                    workbench.scan()
                    records = workbench.records
                    if not records:
                        break
                    selected = (selected + 1) % len(records)
                    continue
                if result == "prev_file":
                    workbench.scan()
                    records = workbench.records
                    if not records:
                        break
                    selected = (selected - 1) % len(records)
                    continue
                break
