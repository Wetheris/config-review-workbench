"""Config Review Workbench Rendering module.

Part of the modular Config Review Workbench source distribution. Build the portable
``dist/config-review.pyz`` executable with ``python build.py``.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

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
    ChangeBlock,
    DiffPresentation,
    DisplayLine,
    FileRecord,
    FilterResult,
    HandledChange,
    PatternRule,
    WorkbenchError,
    _SENSITIVE_KEY_RE,
    _block_coordinate_key,
    _opcode_coordinate_key,
    _parse_scalar_line,
    _preview_text,
    _range_text,
    compute_filter_result,
    handled_marker_text,
    match_handled_changes,
)


def mapping_order_status_text(
    *,
    enabled: bool,
    hidden_count: int,
    unavailable_reason: str | None,
) -> str:
    """Return one of the three explicit mapping-order analysis states."""
    if not enabled:
        return "mapping order OFF"
    if unavailable_reason:
        return f"mapping order UNAVAILABLE · {unavailable_reason}"
    if hidden_count:
        return f"mapping order ON · {hidden_count} hidden"
    return "mapping order ON · no order-only changes found"


def _line_number_width(old_length: int, new_length: int) -> int:
    return max(4, len(str(max(old_length, new_length, 1))))


def _empty_filter_result() -> FilterResult:
    return FilterResult(opcodes=[], blocks=[], hidden=[], visible=[])


def grouped_refined_opcodes(
    opcodes: Sequence[tuple[str, int, int, int, int]],
    context: int,
) -> list[list[tuple[str, int, int, int, int]]]:
    """Group the already-computed canonical opcodes into context hunks.

    This mirrors ``difflib.get_grouped_opcodes`` but accepts our refined opcode
    list. Crucially, it only trims equal ranges; it never realigns changed text.
    """
    if not opcodes:
        return []

    context = max(0, context)
    codes = list(opcodes)
    if codes[0][0] == "equal":
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = (tag, max(i1, i2 - context), i2, max(j1, j2 - context), j2)
    if codes[-1][0] == "equal":
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = (tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context))

    groups: list[list[tuple[str, int, int, int, int]]] = []
    group: list[tuple[str, int, int, int, int]] = []
    double_context = context * 2

    for tag, i1, i2, j1, j2 in codes:
        if tag == "equal" and i2 - i1 > double_context:
            if context:
                group.append((tag, i1, i1 + context, j1, j1 + context))
            if group:
                groups.append(group)
            group = []
            if context:
                i1, j1 = i2 - context, j2 - context
            else:
                continue
        if tag != "equal" or i1 != i2 or j1 != j2:
            group.append((tag, i1, i2, j1, j2))

    if group and not (len(group) == 1 and group[0][0] == "equal"):
        groups.append(group)
    return groups


def _compact_summary_value(value: str, limit: int = 30) -> str:
    rendered = value.strip() or "<blank>"
    if len(rendered) > limit:
        return rendered[: limit - 1] + "…"
    return rendered


def change_block_summary(block: ChangeBlock) -> str:
    """Return a short deterministic summary without interpreting YAML structure."""
    old_parsed = _parse_scalar_line(block.old_lines[0]) if len(block.old_lines) == 1 else None
    new_parsed = _parse_scalar_line(block.new_lines[0]) if len(block.new_lines) == 1 else None

    if old_parsed and new_parsed and old_parsed[0] == new_parsed[0]:
        key, old_value = old_parsed
        _, new_value = new_parsed
        if key != "<list-item>" and _SENSITIVE_KEY_RE.search(key):
            return f"{key} changed"
        if key == "<list-item>":
            return (
                f"list item: {_compact_summary_value(old_value)} → "
                f"{_compact_summary_value(new_value)}"
            )
        return f"{key}: {_compact_summary_value(old_value)} → {_compact_summary_value(new_value)}"

    if block.tag == "delete" and old_parsed:
        key, _value = old_parsed
        return "list item removed" if key == "<list-item>" else f"{key} removed"
    if block.tag == "insert" and new_parsed:
        key, _value = new_parsed
        return "list item added" if key == "<list-item>" else f"{key} added"

    label = {"replace": "changed", "delete": "removed", "insert": "added"}.get(block.tag, block.tag)
    if block.old_count <= 1 and block.new_count <= 1:
        if block.old_lines and block.new_lines:
            return f"{_preview_text(block.old_lines, 34)} → {_preview_text(block.new_lines, 34)}"
        if block.old_lines:
            return f"removed: {_preview_text(block.old_lines, 58)}"
        return f"added: {_preview_text(block.new_lines, 58)}"
    return f"{label} · {block.old_count} TEST line(s) → {block.new_count} DEV line(s)"


def change_block_location(block: ChangeBlock) -> str:
    return (
        f"TEST {_range_text(block.old_start, block.old_end)} · "
        f"DEV {_range_text(block.new_start, block.new_end)}"
    )


def _brief_filter_reason(hidden_by: Sequence[str], max_length: int = 54) -> str:
    """Return one concise reason for inline filtered-diff labels.

    The full set of matching rules remains available in Filter Details. Inline
    rendering deliberately shows only one readable reason plus an overlap count
    so expanded blocks do not become another verbose diagnostics screen.
    """
    normalized: list[str] = []
    for reason in hidden_by:
        if reason == "Whitespace-only":
            label = "Whitespace only"
        elif reason.startswith("YAML mapping order"):
            label = "YAML mapping order"
        else:
            label = reason.strip()
        if label and label not in normalized:
            normalized.append(label)

    if not normalized:
        return "Filtered by display settings"

    # Prefer the shortest matching rule name because it is generally the most
    # readable inline description. Filter Details still exposes every match.
    primary = min(normalized, key=lambda item: (len(item), item.lower()))
    extra_count = len(normalized) - 1
    suffix = f" +{extra_count} more" if extra_count else ""
    available = max(12, max_length - len(suffix))
    if len(primary) > available:
        primary = primary[: available - 1].rstrip() + "…"
    return primary + suffix


def _filtered_block_lines(
    *,
    tag: str,
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    hidden_by: Sequence[str],
    old_start: int,
    old_end: int,
    new_start: int,
    new_end: int,
    expanded: bool,
) -> list[DisplayLine]:
    """Render a filtered block as one compact marker or an expanded inline diff."""
    if tuple(hidden_by) == ("YAML mapping order continuation",):
        return []

    reason = _brief_filter_reason(hidden_by)
    if expanded:
        # The expanded lines make visibility self-evident, so do not add a
        # redundant "VISIBLE" status. Keep only a concise rule name here; the
        # complete list of matching rules remains available in Filter Details.
        lines = [
            DisplayLine(
                f"▼ FILTERED DIFF · {reason}",
                "filtered_header",
            )
        ]
        for offset, value in enumerate(old_lines):
            lines.append(
                DisplayLine(
                    value,
                    "filtered_remove",
                    test_line=old_start + offset + 1,
                )
            )
        for offset, value in enumerate(new_lines):
            lines.append(
                DisplayLine(
                    value,
                    "filtered_add",
                    dev_line=new_start + offset + 1,
                )
            )
        return lines

    hidden_count = max(len(old_lines), len(new_lines))
    count_text = f"{hidden_count} line" if hidden_count == 1 else f"{hidden_count} lines"
    return [
        DisplayLine(
            f"··· FILTERED DIFF (HIDDEN) · {reason} · {count_text} ···",
            "filtered",
            test_line=old_start + 1 if old_lines else None,
            dev_line=new_start + 1 if new_lines else None,
        )
    ]


def _selector_text(
    index: int,
    total: int,
    block: ChangeBlock,
    selected: bool,
) -> str:
    marker = "▶" if selected else "·"
    return (
        f"{marker} ACTIVE CHANGE {index + 1}/{total} · "
        f"TEST {_range_text(block.old_start, block.old_end)} · "
        f"DEV {_range_text(block.new_start, block.new_end)}"
    )


def _render_collapsed_focused_body(
    *,
    output: list[DisplayLine],
    result: FilterResult,
    old_lines: Sequence[str],
    new_lines: Sequence[str],
    blocks_by_coordinates: Mapping[tuple[str, int, int, int, int], ChangeBlock],
    handled_by_key: Mapping[tuple[Any, ...], HandledChange],
    active_blocks: Sequence[ChangeBlock],
    selected_change: int,
    context: int,
) -> tuple[list[int], list[tuple[int, int]], list[ChangeBlock]]:
    """Render Focused Diff with collapsed hidden blocks as marker-only rows.

    Hidden and handled blocks never inherit unified hunk headers or unchanged
    context. Context is constructed only around active review blocks. This keeps
    approved noise genuinely collapsed even when difflib grouped it into a
    larger hunk with nearby equal lines.
    """

    opcodes = list(result.opcodes)
    active_index_by_key = {
        _block_coordinate_key(block): index for index, block in enumerate(active_blocks)
    }
    active_keys = set(active_index_by_key)
    change_line_indexes: list[int] = []
    change_line_ranges: list[tuple[int, int]] = []
    change_blocks: list[ChangeBlock] = []

    def block_for(opcode: tuple[str, int, int, int, int]) -> ChangeBlock:
        block = blocks_by_coordinates.get(opcode)
        if block is None:
            raise WorkbenchError(
                "Internal diff consistency error: a rendered change block was not "
                "present in the canonical file diff."
            )
        return block

    def status_for(block: ChangeBlock) -> str:
        key = _block_coordinate_key(block)
        if key in handled_by_key:
            return "handled"
        if block.is_hidden:
            return "hidden"
        if key in active_keys:
            return "active"
        return "other"

    def add_spaced(lines: Sequence[DisplayLine]) -> None:
        if not lines:
            return
        if output and output[-1].text:
            output.append(DisplayLine("", "text"))
        output.extend(lines)
        if output[-1].text:
            output.append(DisplayLine("", "text"))

    index = 0
    while index < len(opcodes):
        opcode = opcodes[index]
        tag, i1, i2, j1, j2 = opcode
        if tag == "equal":
            index += 1
            continue

        block = block_for(opcode)
        status = status_for(block)

        if status == "hidden":
            add_spaced(
                _filtered_block_lines(
                    tag=block.tag,
                    old_lines=block.old_lines,
                    new_lines=block.new_lines,
                    hidden_by=block.hidden_by,
                    old_start=block.old_start,
                    old_end=block.old_end,
                    new_start=block.new_start,
                    new_end=block.new_end,
                    expanded=False,
                )
            )
            index += 1
            continue

        if status == "handled":
            add_spaced(
                [
                    DisplayLine(
                        handled_marker_text(handled_by_key[_block_coordinate_key(block)], block),
                        "handled",
                    )
                ]
            )
            index += 1
            continue

        if status != "active":
            index += 1
            continue

        # Merge neighboring active changes only when separated by a short equal
        # region. Hidden/handled changes terminate the segment and remain marker-only.
        segment_active_indexes = [index]
        segment_end = index
        cursor = index
        while True:
            candidate = cursor + 1
            if candidate >= len(opcodes):
                break
            next_opcode = opcodes[candidate]
            if next_opcode[0] == "equal":
                equal_length = next_opcode[2] - next_opcode[1]
                if equal_length > context * 2:
                    break
                candidate += 1
                if candidate >= len(opcodes):
                    break
            candidate_block = block_for(opcodes[candidate])
            if status_for(candidate_block) != "active":
                break
            segment_active_indexes.append(candidate)
            segment_end = candidate
            cursor = candidate

        first_block = block_for(opcodes[segment_active_indexes[0]])
        last_block = block_for(opcodes[segment_active_indexes[-1]])

        pre_count = 0
        if index > 0 and opcodes[index - 1][0] == "equal":
            previous = opcodes[index - 1]
            pre_count = min(context, previous[2] - previous[1])
        post_count = 0
        if segment_end + 1 < len(opcodes) and opcodes[segment_end + 1][0] == "equal":
            following = opcodes[segment_end + 1]
            post_count = min(context, following[2] - following[1])

        old_hunk_start = first_block.old_start - pre_count
        new_hunk_start = first_block.new_start - pre_count
        old_hunk_end = last_block.old_end + post_count
        new_hunk_end = last_block.new_end + post_count
        segment_output_start = len(output)
        output.append(
            DisplayLine(
                f"@@ TEST {old_hunk_start + 1},{old_hunk_end - old_hunk_start} │ "
                f"DEV {new_hunk_start + 1},{new_hunk_end - new_hunk_start} @@",
                "hunk",
            )
        )

        if pre_count:
            for offset in range(pre_count):
                old_line_index = first_block.old_start - pre_count + offset
                new_line_index = first_block.new_start - pre_count + offset
                output.append(
                    DisplayLine(
                        old_lines[old_line_index],
                        "context",
                        test_line=old_line_index + 1,
                        dev_line=new_line_index + 1,
                    )
                )

        active_opcode_set = set(segment_active_indexes)
        for opcode_index in range(index, segment_end + 1):
            current = opcodes[opcode_index]
            current_tag, ci1, ci2, cj1, cj2 = current
            if current_tag == "equal":
                for offset, value in enumerate(old_lines[ci1:ci2]):
                    output.append(
                        DisplayLine(
                            value,
                            "context",
                            test_line=ci1 + offset + 1,
                            dev_line=cj1 + offset + 1,
                        )
                    )
                continue
            if opcode_index not in active_opcode_set:
                continue
            current_block = block_for(current)
            active_position = active_index_by_key[_block_coordinate_key(current_block)]
            marker_index = len(output)
            change_line_indexes.append(marker_index)
            change_blocks.append(current_block)
            output.append(
                DisplayLine(
                    _selector_text(
                        active_position,
                        len(active_blocks),
                        current_block,
                        selected=active_position == selected_change,
                    ),
                    "selector_selected" if active_position == selected_change else "selector",
                )
            )
            for offset, value in enumerate(current_block.old_lines):
                output.append(
                    DisplayLine(
                        value,
                        "remove",
                        test_line=current_block.old_start + offset + 1,
                    )
                )
            for offset, value in enumerate(current_block.new_lines):
                output.append(
                    DisplayLine(
                        value,
                        "add",
                        dev_line=current_block.new_start + offset + 1,
                    )
                )

        if post_count:
            for offset in range(post_count):
                old_line_index = last_block.old_end + offset
                new_line_index = last_block.new_end + offset
                output.append(
                    DisplayLine(
                        old_lines[old_line_index],
                        "context",
                        test_line=old_line_index + 1,
                        dev_line=new_line_index + 1,
                    )
                )

        segment_output_end = len(output)
        for _ in segment_active_indexes:
            change_line_ranges.append((segment_output_start, segment_output_end))
        index = segment_end + 1

    if len(change_blocks) != len(active_blocks):
        raise WorkbenchError(
            "Internal diff consistency error: active change navigation does not match "
            "the canonical file diff."
        )
    return change_line_indexes, change_line_ranges, change_blocks


def _render_text_diff(
    record: FileRecord,
    context: int,
    *,
    patterns: Sequence[PatternRule],
    filter_enabled: bool,
    hide_whitespace: bool,
    hide_mapping_order: bool,
    expand_filtered: bool,
    selected_change: int,
) -> DiffPresentation:
    # One canonical diff calculation drives filtering, rendering, history, line
    # numbers, and active-change navigation.
    result = compute_filter_result(
        record.test_text,
        record.dev_text,
        patterns if filter_enabled else [],
        record.relative_path,
        hide_whitespace=hide_whitespace if filter_enabled else False,
        hide_mapping_order=hide_mapping_order if filter_enabled else False,
    )
    old_lines = record.test_text.splitlines()
    new_lines = record.dev_text.splitlines()
    number_width = _line_number_width(len(old_lines), len(new_lines))

    handled_by_key, unmatched_history = match_handled_changes(record, result.blocks)
    active_blocks = [
        block for block in result.visible if _block_coordinate_key(block) not in handled_by_key
    ]
    active_total = len(active_blocks)
    if active_total:
        selected_change = max(0, min(selected_change, active_total - 1))
    else:
        selected_change = 0

    hidden_unhandled = [
        block for block in result.hidden if _block_coordinate_key(block) not in handled_by_key
    ]
    pattern_hidden_count = sum(
        1
        for block in hidden_unhandled
        if any(
            reason != "Whitespace-only" and not reason.startswith("YAML mapping order")
            for reason in block.hidden_by
        )
    )
    whitespace_hidden_count = sum(
        1 for block in hidden_unhandled if "Whitespace-only" in block.hidden_by
    )
    mapping_order_hidden_count = sum(
        1 for block in hidden_unhandled if "YAML mapping order" in block.hidden_by
    )

    output = [
        DisplayLine(
            "Line columns: TEST/current │ DEV/incoming",
            "legend",
        ),
        DisplayLine(f"--- TEST/{record.relative_path}", "test_file_header"),
        DisplayLine(f"+++ DEV/{record.relative_path}", "dev_file_header"),
        # Separate file metadata from the first diff hunk so the start of the
        # actual file comparison is immediately recognizable.
        DisplayLine("", "text"),
    ]

    if unmatched_history:
        output.append(DisplayLine("SESSION HISTORY", "section"))
        for entry in unmatched_history:
            output.append(DisplayLine(handled_marker_text(entry), "handled"))
        output.append(DisplayLine("", "text"))

    if not result.blocks:
        if not record.handled_changes:
            output.append(DisplayLine("No textual differences remain.", "note"))
        else:
            output.append(
                DisplayLine(
                    "No active textual differences remain; completed session actions are listed above.",
                    "note",
                )
            )
        return DiffPresentation(
            lines=output,
            filter_result=result,
            number_width=number_width,
            handled_count=len(record.handled_changes),
            pattern_hidden_count=pattern_hidden_count,
            whitespace_hidden_count=whitespace_hidden_count,
            mapping_order_hidden_count=mapping_order_hidden_count,
            mapping_order_unavailable_reason=result.mapping_order_unavailable_reason,
        )

    blocks_by_coordinates = {_opcode_coordinate_key(block): block for block in result.blocks}

    if filter_enabled and not expand_filtered:
        (
            change_line_indexes,
            change_line_ranges,
            change_blocks,
        ) = _render_collapsed_focused_body(
            output=output,
            result=result,
            old_lines=old_lines,
            new_lines=new_lines,
            blocks_by_coordinates=blocks_by_coordinates,
            handled_by_key=handled_by_key,
            active_blocks=active_blocks,
            selected_change=selected_change,
            context=context,
        )
        return DiffPresentation(
            lines=output,
            filter_result=result,
            change_line_indexes=change_line_indexes,
            change_line_ranges=change_line_ranges,
            change_blocks=change_blocks,
            selected_change=selected_change if active_total else None,
            number_width=number_width,
            handled_count=len(record.handled_changes),
            pattern_hidden_count=pattern_hidden_count,
            whitespace_hidden_count=whitespace_hidden_count,
            mapping_order_hidden_count=mapping_order_hidden_count,
            mapping_order_unavailable_reason=result.mapping_order_unavailable_reason,
        )

    groups = grouped_refined_opcodes(result.opcodes, context)
    change_line_indexes: list[int] = []
    change_line_ranges: list[tuple[int, int]] = []
    change_blocks: list[ChangeBlock] = []
    active_index = 0

    for group in groups:
        # A collapsed hidden-only hunk should be genuinely collapsed. Context
        # lines and the unified hunk header add noise without helping the user
        # understand a block they explicitly chose not to expand. Mixed hunks
        # still retain their normal context because it may belong to a visible
        # neighboring change.
        group_blocks: list[ChangeBlock] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                continue
            block = blocks_by_coordinates.get((tag, i1, i2, j1, j2))
            if block is None:
                raise WorkbenchError(
                    "Internal diff consistency error: a rendered change block was not "
                    "present in the canonical file diff."
                )
            group_blocks.append(block)

        hidden_only_collapsed = (
            filter_enabled
            and not expand_filtered
            and bool(group_blocks)
            and all(block.is_hidden for block in group_blocks)
        )
        if hidden_only_collapsed:
            for block in group_blocks:
                hidden_lines = _filtered_block_lines(
                    tag=block.tag,
                    old_lines=block.old_lines,
                    new_lines=block.new_lines,
                    hidden_by=block.hidden_by,
                    old_start=block.old_start,
                    old_end=block.old_end,
                    new_start=block.new_start,
                    new_end=block.new_end,
                    expanded=False,
                )
                if not hidden_lines:
                    continue
                if output and output[-1].text:
                    output.append(DisplayLine("", "text"))
                output.extend(hidden_lines)
                output.append(DisplayLine("", "text"))
            continue

        # Hidden-only expanded sections stay compact: one unchanged line of
        # context on each side, the exact hidden diff, and no unified hunk
        # header. Active hunks retain the normal user-configured context.
        hidden_only_expanded = (
            filter_enabled
            and expand_filtered
            and bool(group_blocks)
            and all(block.is_hidden for block in group_blocks)
        )
        if hidden_only_expanded:
            for block in group_blocks:
                hidden_lines = _filtered_block_lines(
                    tag=block.tag,
                    old_lines=block.old_lines,
                    new_lines=block.new_lines,
                    hidden_by=block.hidden_by,
                    old_start=block.old_start,
                    old_end=block.old_end,
                    new_start=block.new_start,
                    new_end=block.new_end,
                    expanded=True,
                )
                if not hidden_lines:
                    continue
                if output and output[-1].text:
                    output.append(DisplayLine("", "text"))

                before_old = block.old_start - 1
                before_new = block.new_start - 1
                if (
                    before_old >= 0
                    and before_new >= 0
                    and old_lines[before_old] == new_lines[before_new]
                ):
                    output.append(
                        DisplayLine(
                            old_lines[before_old],
                            "filtered_context",
                            test_line=before_old + 1,
                            dev_line=before_new + 1,
                        )
                    )

                output.extend(hidden_lines)

                after_old = block.old_end
                after_new = block.new_end
                if (
                    after_old < len(old_lines)
                    and after_new < len(new_lines)
                    and old_lines[after_old] == new_lines[after_new]
                ):
                    output.append(
                        DisplayLine(
                            old_lines[after_old],
                            "filtered_context",
                            test_line=after_old + 1,
                            dev_line=after_new + 1,
                        )
                    )
                output.append(DisplayLine("", "text"))
            continue

        old_start = group[0][1]
        old_end = group[-1][2]
        new_start = group[0][3]
        new_end = group[-1][4]
        output.append(
            DisplayLine(
                f"@@ TEST {old_start + 1},{old_end - old_start} │ "
                f"DEV {new_start + 1},{new_end - new_start} @@",
                "hunk",
            )
        )

        for tag, i1, i2, j1, j2 in group:
            old_block = old_lines[i1:i2]

            if tag == "equal":
                for offset, value in enumerate(old_block):
                    output.append(
                        DisplayLine(
                            value,
                            "context",
                            test_line=i1 + offset + 1,
                            dev_line=j1 + offset + 1,
                        )
                    )
                continue

            key = (tag, i1, i2, j1, j2)
            block = blocks_by_coordinates.get(key)
            if block is None:
                raise WorkbenchError(
                    "Internal diff consistency error: a rendered change block was not "
                    "present in the canonical file diff."
                )

            handled = handled_by_key.get(_block_coordinate_key(block))
            if handled is not None:
                output.append(DisplayLine(handled_marker_text(handled, block), "handled"))
                # Full Diff remains literal: the status marker is followed by the
                # actual current TEST/DEV lines. Focused Diff keeps it collapsed.
                if not filter_enabled:
                    for offset, value in enumerate(block.old_lines):
                        output.append(
                            DisplayLine(
                                value,
                                "remove",
                                test_line=block.old_start + offset + 1,
                            )
                        )
                    for offset, value in enumerate(block.new_lines):
                        output.append(
                            DisplayLine(
                                value,
                                "add",
                                dev_line=block.new_start + offset + 1,
                            )
                        )
                continue

            if block.is_hidden:
                hidden_lines = _filtered_block_lines(
                    tag=block.tag,
                    old_lines=block.old_lines,
                    new_lines=block.new_lines,
                    hidden_by=block.hidden_by,
                    old_start=block.old_start,
                    old_end=block.old_end,
                    new_start=block.new_start,
                    new_end=block.new_end,
                    expanded=expand_filtered,
                )
                if hidden_lines and not expand_filtered:
                    if output and output[-1].text:
                        output.append(DisplayLine("", "text"))
                    output.extend(hidden_lines)
                    output.append(DisplayLine("", "text"))
                else:
                    output.extend(hidden_lines)
                continue

            marker_index = len(output)
            change_line_indexes.append(marker_index)
            change_blocks.append(block)
            output.append(
                DisplayLine(
                    _selector_text(
                        active_index,
                        active_total,
                        block,
                        selected=active_index == selected_change,
                    ),
                    "selector_selected" if active_index == selected_change else "selector",
                )
            )
            for offset, value in enumerate(block.old_lines):
                output.append(
                    DisplayLine(
                        value,
                        "remove",
                        test_line=block.old_start + offset + 1,
                    )
                )
            for offset, value in enumerate(block.new_lines):
                output.append(
                    DisplayLine(
                        value,
                        "add",
                        dev_line=block.new_start + offset + 1,
                    )
                )
            change_line_ranges.append((marker_index, len(output)))
            active_index += 1

    if active_index != active_total:
        raise WorkbenchError(
            "Internal diff consistency error: active change navigation does not match "
            "the canonical file diff."
        )

    # Keep the selected change's nearby unchanged context at full brightness.
    # Stop at neighboring changes/filtered markers so unrelated hunks remain muted.
    expanded_ranges: list[tuple[int, int]] = []
    for start, end in change_line_ranges:
        bright_start = start
        while bright_start > 0 and output[bright_start - 1].kind in {"context", "hunk"}:
            bright_start -= 1
        bright_end = end
        while bright_end < len(output) and output[bright_end].kind == "context":
            bright_end += 1
        expanded_ranges.append((bright_start, bright_end))
    change_line_ranges = expanded_ranges

    return DiffPresentation(
        lines=output,
        filter_result=result,
        change_line_indexes=change_line_indexes,
        change_line_ranges=change_line_ranges,
        change_blocks=change_blocks,
        selected_change=selected_change if active_total else None,
        number_width=number_width,
        handled_count=len(record.handled_changes),
        pattern_hidden_count=pattern_hidden_count,
        whitespace_hidden_count=whitespace_hidden_count,
        mapping_order_hidden_count=mapping_order_hidden_count,
        mapping_order_unavailable_reason=result.mapping_order_unavailable_reason,
    )


def full_unified_diff(
    record: FileRecord,
    context: int,
    *,
    selected_change: int = 0,
) -> DiffPresentation:
    if record.read_error and record.binary:
        return DiffPresentation(
            lines=[DisplayLine(record.read_error, "error")],
            filter_result=_empty_filter_result(),
        )
    return _render_text_diff(
        record,
        context,
        patterns=[],
        filter_enabled=False,
        hide_whitespace=False,
        hide_mapping_order=False,
        expand_filtered=True,
        selected_change=selected_change,
    )


def review_unified_diff(
    record: FileRecord,
    patterns: Sequence[PatternRule],
    context: int,
    *,
    hide_whitespace: bool = False,
    hide_mapping_order: bool = False,
    expand_filtered: bool = False,
    selected_change: int = 0,
) -> DiffPresentation:
    return _render_text_diff(
        record,
        context,
        patterns=patterns,
        filter_enabled=True,
        hide_whitespace=hide_whitespace,
        hide_mapping_order=hide_mapping_order,
        expand_filtered=expand_filtered,
        selected_change=selected_change,
    )


def selected_change_preview(
    record: FileRecord,
    block: ChangeBlock,
    *,
    context: int = 1,
) -> DiffPresentation:
    """Render only one canonical change block with small, real source context."""
    old_lines = record.test_text.splitlines()
    new_lines = record.dev_text.splitlines()
    number_width = _line_number_width(len(old_lines), len(new_lines))
    context = max(0, context)
    output: list[DisplayLine] = [
        DisplayLine(
            f"@@ TEST {_range_text(block.old_start, block.old_end)} │ "
            f"DEV {_range_text(block.new_start, block.new_end)} @@",
            "hunk",
        )
    ]

    before = min(context, block.old_start, block.new_start)
    for offset in range(before, 0, -1):
        old_index = block.old_start - offset
        new_index = block.new_start - offset
        if old_lines[old_index] == new_lines[new_index]:
            output.append(
                DisplayLine(
                    old_lines[old_index],
                    "context",
                    test_line=old_index + 1,
                    dev_line=new_index + 1,
                )
            )
        else:
            output.append(DisplayLine(old_lines[old_index], "remove", test_line=old_index + 1))
            output.append(DisplayLine(new_lines[new_index], "add", dev_line=new_index + 1))

    for offset, value in enumerate(block.old_lines):
        output.append(DisplayLine(value, "remove", test_line=block.old_start + offset + 1))
    for offset, value in enumerate(block.new_lines):
        output.append(DisplayLine(value, "add", dev_line=block.new_start + offset + 1))

    after = min(
        context,
        max(0, len(old_lines) - block.old_end),
        max(0, len(new_lines) - block.new_end),
    )
    for offset in range(after):
        old_index = block.old_end + offset
        new_index = block.new_end + offset
        if old_lines[old_index] == new_lines[new_index]:
            output.append(
                DisplayLine(
                    old_lines[old_index],
                    "context",
                    test_line=old_index + 1,
                    dev_line=new_index + 1,
                )
            )
        else:
            output.append(DisplayLine(old_lines[old_index], "remove", test_line=old_index + 1))
            output.append(DisplayLine(new_lines[new_index], "add", dev_line=new_index + 1))

    return DiffPresentation(
        lines=output,
        filter_result=_empty_filter_result(),
        change_line_indexes=[0],
        change_line_ranges=[(0, len(output))],
        change_blocks=[block],
        selected_change=0,
        number_width=number_width,
    )
