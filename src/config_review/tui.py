"""Config Review Workbench Tui module.

Part of the modular Config Review Workbench source distribution. Build the portable
``dist/config-review.pyz`` executable with ``python build.py``.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

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
    CATEGORY_ALWAYS_REVIEWED,
    CATEGORY_ORDER,
    ChangeBlock,
    DisplayLine,
    FileRecord,
    MainListRow,
    PatternCandidate,
    ProtectedChangeSummary,
    ReviewMenuResult,
    VERSION,
    WorkbenchError,
    _range_text,
    exact_change_still_present,
    file_hash,
    file_section,
    init_project_config,
    maximum_horizontal_offset,
    parse_editor_command,
    selected_diff_body_range,
)
from .rendering import (
    change_block_location,
    change_block_summary,
    full_unified_diff,
    mapping_order_status_text,
    review_unified_diff,
    selected_change_preview,
)
from .workbench import (
    Workbench,
)


@dataclass(slots=True)
class PatternManagerRow:
    kind: str  # category | pattern | protected_category | protected
    label: str
    category: str
    candidate: PatternCandidate | None = None
    protected: ProtectedChangeSummary | None = None


def _category_members(
    candidates: Sequence[PatternCandidate], category: str
) -> list[PatternCandidate]:
    return [candidate for candidate in candidates if candidate.rule.category == category]


def _category_state(candidates: Sequence[PatternCandidate]) -> str:
    if not candidates:
        return "EMPTY"
    enabled = sum(1 for candidate in candidates if candidate.rule.enabled)
    if enabled == 0:
        return "VISIBLE"
    if enabled == len(candidates):
        return "HIDDEN"
    return "MIXED"


def build_pattern_manager_rows(
    candidates: Sequence[PatternCandidate],
    protected: Sequence[ProtectedChangeSummary],
    expanded_categories: set[str] | None = None,
) -> list[PatternManagerRow]:
    """Build a compact category-first pattern list.

    Categories start collapsed so the manager works as an at-a-glance summary.
    Expansion is UI-only; hide/show choices remain persisted in project config.
    """
    expanded = expanded_categories or set()
    rows: list[PatternManagerRow] = []
    for category in CATEGORY_ORDER:
        members = _category_members(candidates, category)
        if not members:
            continue
        pattern_matches = sum(candidate.match_count for candidate in members)
        file_names = {path for candidate in members for path in candidate.affected_files}
        rows.append(
            PatternManagerRow(
                kind="category",
                label=(
                    f"{category} · {len(members)} pattern(s) · "
                    f"{pattern_matches} pattern matches · {len(file_names)} file(s)"
                ),
                category=category,
            )
        )
        if category in expanded:
            rows.extend(
                PatternManagerRow(
                    kind="pattern",
                    label=candidate.rule.name,
                    category=category,
                    candidate=candidate,
                )
                for candidate in members
            )

    if protected:
        total_changes = sum(item.match_count for item in protected)
        rows.append(
            PatternManagerRow(
                kind="protected_category",
                label=f"{CATEGORY_ALWAYS_REVIEWED} · {total_changes} change(s)",
                category=CATEGORY_ALWAYS_REVIEWED,
            )
        )
        if CATEGORY_ALWAYS_REVIEWED in expanded:
            rows.extend(
                PatternManagerRow(
                    kind="protected",
                    label=item.name,
                    category=CATEGORY_ALWAYS_REVIEWED,
                    protected=item,
                )
                for item in protected
            )
    return rows


_FOOTER_CATEGORIES = (
    "Resolve:",
    "Edit:",
    "Navigate:",
    "View:",
    "File:",
    "Actions:",
    "Prev file:",
    "Next file:",
)

_FOOTER_TOKEN_RE = re.compile(
    r"(\[[^\]\n:]+\]|(?<!\S)[\[\]](?=\s|$)|"
    + "|".join(re.escape(item) for item in _FOOTER_CATEGORIES)
    + r"|accept DEV|ccept DEV|keep TEST)"
)


def footer_segments(text: str) -> list[tuple[str, str]]:
    """Split footer text into a few deliberately narrow style categories."""
    output: list[tuple[str, str]] = []
    for part in _FOOTER_TOKEN_RE.split(text):
        if not part:
            continue
        if part in _FOOTER_CATEGORIES:
            kind = "category"
        elif part in {"accept DEV", "ccept DEV"}:
            kind = "dev_action"
        elif part == "keep TEST":
            kind = "test_action"
        elif re.fullmatch(r"\[[^\]\n:]+\]|(?<!\S)[\[\]](?=\s|$)", part):
            kind = "hotkey"
        else:
            kind = "text"
        output.append((part, kind))
    return output


def main_footer_lines(available_width: int) -> tuple[str, ...]:
    """Return a non-clipping main-screen footer for the available terminal width."""
    if available_width >= 65:
        return (
            "Navigate: [j/k or ↑/↓]select  [Space]expand/collapse  [Enter]open",
            "Actions: [u]undo  [c]configure  [?]help  [q]quit",
        )
    if available_width >= 49:
        return (
            "Navigate: [j/k]select  [Space]expand  [Enter]open",
            "Actions: [c]configure  [?]help  [q]quit",
        )
    if available_width >= 28:
        return (
            "[j/k]select  [Enter]open",
            "[c]config  [?]help  [q]quit",
        )
    return ("[c]config  [?]help  [q]quit",)


def _directory_input(prompt: str) -> str:
    """Read a directory path with best-effort shell-style Tab completion."""
    try:
        import readline
    except ImportError:  # pragma: no cover - unavailable on some platforms
        return input(prompt)

    previous_completer = readline.get_completer()
    previous_delimiters = readline.get_completer_delims()

    def complete(value: str, state: int) -> str | None:
        expanded = os.path.expanduser(value or "")
        pattern = f"{expanded}*" if expanded else "*"
        matches = [
            match.rstrip(os.sep) + os.sep
            for match in sorted(glob.glob(pattern))
            if Path(match).is_dir()
        ]
        return matches[state] if state < len(matches) else None

    try:
        readline.set_completer(complete)
        readline.set_completer_delims("\t\n")
        readline.parse_and_bind("tab: complete")
        return input(prompt)
    finally:
        readline.set_completer(previous_completer)
        readline.set_completer_delims(previous_delimiters)


def _environment_pairs_under(
    project: Path,
    source_name: str,
    target_name: str,
    *,
    max_depth: int = 5,
) -> list[tuple[Path, Path]]:
    """Find likely sibling source/target directories beneath one selected root."""
    project = project.resolve()
    source_names = tuple(dict.fromkeys((source_name.lower(), "dev")))
    target_names = tuple(dict.fromkeys((target_name.lower(), "test")))
    excluded = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "build",
        "dist",
        "__pycache__",
    }
    found: set[tuple[Path, Path]] = set()
    for root_text, dirnames, _filenames in os.walk(project, followlinks=False):
        root = Path(root_text)
        try:
            depth = len(root.relative_to(project).parts)
        except ValueError:
            continue
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name.lower() not in excluded and not (root / name).is_symlink() and depth < max_depth
        )
        by_lower = {name.lower(): name for name in dirnames}
        source = next((root / by_lower[name] for name in source_names if name in by_lower), None)
        target = next((root / by_lower[name] for name in target_names if name in by_lower), None)
        if source is not None and target is not None and source != target:
            found.add((source.resolve(), target.resolve()))
    return sorted(found, key=lambda pair: (len(pair[0].parts), pair[0].as_posix().lower()))


class Tui:
    def __init__(self, workbench: Workbench) -> None:
        self.workbench = workbench
        self.selected = 0
        self.status = ""
        self.expanded_files: set[str] = set()
        self.main_selection_key: tuple[str, str, int | None] | None = None
        self.pending_change_index: int | None = None
        self.pending_open_review = False
        # True when the terminal supports the additional soft-muted palette.
        # We deliberately avoid curses.A_DIM for diff content because many
        # terminals render it far darker than a useful "background" emphasis.
        self.soft_muted_pairs = False

    def _main_rows(self, records: Sequence[FileRecord]) -> list[MainListRow]:
        rows: list[MainListRow] = []
        current_section: str | None = None
        for record_index, record in enumerate(records):
            section = file_section(record.relative_path)
            if section != current_section:
                rows.append(MainListRow(kind="section", section=section))
                current_section = section
            rows.append(MainListRow(kind="file", record_index=record_index, section=section))
            if record.relative_path not in self.expanded_files:
                continue

            active = self.workbench.active_change_blocks(record)
            for change_index, block in enumerate(active):
                rows.append(
                    MainListRow(
                        kind="change",
                        record_index=record_index,
                        section=section,
                        change_index=change_index,
                        summary=change_block_summary(block),
                        block=block,
                    )
                )
            counts = self.workbench.review_counts(record)
            hidden = counts.pattern_hidden + counts.whitespace_hidden + counts.mapping_order_hidden
            details = [f"{counts.active} active"]
            if counts.handled:
                details.append(f"{counts.handled} handled")
            if hidden:
                details.append(f"{hidden} hidden")
            if not active and not counts.handled and not hidden:
                details = ["No active changes"]
            rows.append(
                MainListRow(
                    kind="summary",
                    record_index=record_index,
                    section=section,
                    summary=" · ".join(details),
                )
            )
        return rows

    @staticmethod
    def _main_row_key(
        row: MainListRow, records: Sequence[FileRecord]
    ) -> tuple[str, str, int | None] | None:
        if row.record_index is None or row.kind not in {"file", "change"}:
            return None
        return (row.kind, records[row.record_index].relative_path, row.change_index)

    def _set_main_row_selection(self, row: MainListRow, records: Sequence[FileRecord]) -> None:
        key = self._main_row_key(row, records)
        if key is not None:
            self.main_selection_key = key
            assert row.record_index is not None
            self.selected = row.record_index

    @staticmethod
    def _add(stdscr: Any, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = stdscr.getmaxyx()
        if y < 0 or y >= height or x >= width:
            return
        try:
            stdscr.addnstr(y, x, text, max(0, width - x - 1), attr)
        except curses.error:
            pass

    @staticmethod
    def _color_pair(number: int) -> int:
        return curses.color_pair(number) if curses is not None and curses.has_colors() else 0

    def _muted_red_attr(self) -> int:
        return self._color_pair(6) if self.soft_muted_pairs else self._color_pair(1)

    def _muted_green_attr(self) -> int:
        return self._color_pair(7) if self.soft_muted_pairs else self._color_pair(2)

    def _muted_text_attr(self) -> int:
        # On limited-color terminals, normal foreground text is preferable to
        # A_DIM, which can become nearly unreadable.
        return self._color_pair(8) if self.soft_muted_pairs else 0

    def _muted_cyan_attr(self) -> int:
        return self._color_pair(9) if self.soft_muted_pairs else self._color_pair(4)

    def _muted_magenta_attr(self) -> int:
        return self._color_pair(10) if self.soft_muted_pairs else self._color_pair(5)

    def _test_red_attr(self, *, bold: bool = False, dim: bool = False) -> int:
        """Return readable TEST red, using a soft muted shade when requested."""
        attr = self._muted_red_attr() if dim else self._color_pair(1)
        if bold:
            attr |= curses.A_BOLD
        return attr

    def _draw_footer(self, stdscr: Any, y: int, x: int, text: str) -> None:
        """Render a footer with restrained, predictable emphasis."""
        attrs = {
            "text": curses.A_BOLD,
            "hotkey": curses.A_BOLD | self._color_pair(4),
            "category": curses.A_BOLD | self._color_pair(5),
            "dev_action": curses.A_BOLD | self._color_pair(2),
            "test_action": self._test_red_attr(),
        }
        _, width = stdscr.getmaxyx()
        cursor = x
        for segment, kind in footer_segments(text):
            if cursor >= width - 1:
                break
            self._add(stdscr, y, cursor, segment, attrs[kind])
            cursor += len(segment)

    @staticmethod
    def _selected_change_banner(text: str, available_width: int) -> str:
        """Keep the selected-change highlight compact instead of spanning the row."""
        del available_width  # The reverse-video highlight should cover only the label.
        if text.startswith("▶ ── "):
            return f"▶ {text[5:]}"
        return text

    @staticmethod
    def _viewport_file_position(
        content: Sequence[DisplayLine],
        scroll: int,
        body_height: int,
        record: FileRecord,
    ) -> tuple[str, str]:
        """Return a Vim-style line/percentage based on the actual viewport.

        The nearest numbered diff row to the viewport center is used. TEST/current
        coordinates are preferred when both sides are present; DEV/incoming is used
        for addition-only rows. This follows manual scrolling rather than the selected
        change index.
        """
        if not content:
            return "FILE 0%", "0%"

        window_start = max(0, min(scroll, len(content) - 1))
        window_end = min(len(content), window_start + max(1, body_height))
        center = window_start + max(0, (window_end - window_start - 1) // 2)

        chosen: DisplayLine | None = None
        max_distance = max(center - window_start, window_end - 1 - center)
        for distance in range(max_distance + 1):
            candidates = (center - distance, center + distance) if distance else (center,)
            for index in candidates:
                if index < window_start or index >= window_end:
                    continue
                line = content[index]
                if line.test_line is not None or line.dev_line is not None:
                    chosen = line
                    break
            if chosen is not None:
                break

        if chosen is None:
            # A viewport containing only labels/spacing still gets a stable nearby
            # source position by searching outward in the rendered diff.
            for distance in range(1, len(content) + 1):
                for index in (window_start - distance, window_end - 1 + distance):
                    if 0 <= index < len(content):
                        line = content[index]
                        if line.test_line is not None or line.dev_line is not None:
                            chosen = line
                            break
                if chosen is not None:
                    break

        if chosen is None:
            return "FILE 0%", "0%"

        test_total = max(1, len(record.test_text.splitlines()))
        dev_total = max(1, len(record.dev_text.splitlines()))
        if chosen.test_line is not None:
            side = "TEST"
            current = chosen.test_line
            total = test_total
        else:
            side = "DEV"
            current = chosen.dev_line or 1
            total = dev_total

        current = max(1, min(current, total))
        percentage = max(0, min(100, round((current / total) * 100)))
        return f"{side} {current}/{total} · {percentage}%", f"{percentage}%"

    def startup_saved_session_screen(self, stdscr: Any) -> bool:
        if not self.workbench.session.has_saved:
            return True
        while True:
            summary = self.workbench.saved_session_summary()
            if summary is None:
                self.workbench.start_fresh_session(delete_saved=True)
                return True
            stdscr.erase()
            saved_commit = str(summary["commit"])
            short_commit = saved_commit[:10] if saved_commit else "no commit"
            self._add(
                stdscr, 1, 2, "LOAD LAST REVIEW SESSION?", curses.A_BOLD | self._color_pair(5)
            )
            self._add(stdscr, 3, 2, f"Saved from: {summary['branch']} @ {short_commit}")
            self._add(stdscr, 4, 2, f"Saved:      {summary['saved_at']}")
            self._add(
                stdscr,
                5,
                2,
                f"Progress:   {summary['files_reviewed']}/{summary['files_total']} files reviewed · "
                f"{summary['total_handled']} handled changes",
            )
            if summary["exact"]:
                self._add(
                    stdscr,
                    7,
                    2,
                    "The saved session exactly matches the current checkout and filters.",
                    self._color_pair(2) | curses.A_BOLD,
                )
            else:
                self._add(
                    stdscr,
                    7,
                    2,
                    "The checkout or comparison changed since this session was saved.",
                    self._color_pair(3) | curses.A_BOLD,
                )
                self._add(
                    stdscr,
                    8,
                    2,
                    f"{summary['verified_handled']}/{summary['total_handled']} handled changes "
                    "can still be verified; uncertain changes return to review.",
                    self._color_pair(3),
                )
            self._draw_footer(stdscr, 10, 2, "[y]es — load the last session")
            self._draw_footer(stdscr, 11, 2, "[n]o  — delete it and start fresh")
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("y"), ord("Y")):
                restored = self.workbench.resume_saved_session()
                if restored is not None:
                    self.status = (
                        f"Loaded last review; verified "
                        f"{restored['verified_handled']}/{restored['total_handled']} handled changes."
                    )
                return True
            if key in (ord("n"), ord("N")):
                self.workbench.start_fresh_session(delete_saved=True)
                self.status = "Deleted the last review session and started fresh."
                return True

    def exit_session_screen(self, stdscr: Any) -> bool:
        try:
            path = self.workbench.save_session()
        except WorkbenchError as exc:
            self.status = f"Could not save review session: {exc}"
            return False
        self.status = f"Saved review session to {path}."
        return True

    def run(self, stdscr: Any) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_RED, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_CYAN, -1)
            curses.init_pair(5, curses.COLOR_MAGENTA, -1)

            # A_DIM is terminal-dependent and often makes content almost
            # invisible. Use medium 256-color shades when available so
            # non-focused and filtered content stays readable while receding.
            if getattr(curses, "COLORS", 0) >= 256 and getattr(curses, "COLOR_PAIRS", 0) > 10:
                curses.init_pair(6, 167, -1)  # soft red
                curses.init_pair(7, 71, -1)  # soft green
                curses.init_pair(8, 245, -1)  # medium gray
                curses.init_pair(9, 73, -1)  # soft cyan
                curses.init_pair(10, 133, -1)  # soft magenta
                self.soft_muted_pairs = True
            elif getattr(curses, "COLOR_PAIRS", 0) > 10:
                gray = 8 if getattr(curses, "COLORS", 0) >= 16 else curses.COLOR_WHITE
                curses.init_pair(6, curses.COLOR_RED, -1)
                curses.init_pair(7, curses.COLOR_GREEN, -1)
                curses.init_pair(8, gray, -1)
                curses.init_pair(9, curses.COLOR_CYAN, -1)
                curses.init_pair(10, curses.COLOR_MAGENTA, -1)
                self.soft_muted_pairs = True
        if not self.startup_saved_session_screen(stdscr):
            return
        while True:
            action = self.main_screen(stdscr)
            if action == "quit":
                if self.exit_session_screen(stdscr):
                    return
                continue
            if action == "open" and self.workbench.records:
                initial_change = self.pending_change_index or 0
                open_review = self.pending_open_review
                self.pending_change_index = None
                self.pending_open_review = False
                result = self.detail_screen(
                    stdscr,
                    self.selected,
                    initial_selected_change=initial_change,
                    open_review=open_review,
                )
                if result == "quit" and self.exit_session_screen(stdscr):
                    return

    def main_screen(self, stdscr: Any) -> str:
        while True:
            records = self.workbench.records
            if records:
                self.selected = max(0, min(self.selected, len(records) - 1))
            else:
                self.selected = 0
                self.main_selection_key = None

            stdscr.erase()
            height, width = stdscr.getmaxyx()
            title = f"CONFIG REVIEW WORKBENCH v{VERSION}"
            self._add(stdscr, 0, 2, title, curses.A_BOLD | self._color_pair(5))
            self._add(stdscr, 1, 2, f"DEV:  {self.workbench.settings.source}")
            self._add(stdscr, 2, 2, f"TEST: {self.workbench.settings.target}")

            enabled_count = len(self.workbench.enabled_patterns)
            whitespace_text = "hidden" if self.workbench.hide_whitespace else "visible"
            mapping_text = "hidden" if self.workbench.hide_mapping_order else "visible"
            self._add(
                stdscr,
                3,
                2,
                f"Patterns hidden: {enabled_count} · display filters: "
                f"whitespace {whitespace_text}, YAML order {mapping_text}",
                self._color_pair(3),
            )
            self._add(
                stdscr,
                4,
                2,
                self.workbench.session_status_text,
                self._color_pair(4),
            )
            if self.workbench.config_diagnostics and not self.status:
                self.status = "Config warning: " + self.workbench.config_diagnostics[0]

            status_rows = [(record, *self.workbench.file_status(record)) for record in records]
            remaining = sum(counts.active for _, _, counts in status_rows)
            complete = sum(status == "COMPLETE" for _, status, _ in status_rows)
            filtered_only = sum(status.startswith("FILTERED ONLY") for _, status, _ in status_rows)
            remaining_text = "NO ACTIVE DIFFS" if remaining == 0 else f"Active diffs: {remaining}"
            summary_attr = self._color_pair(2) | curses.A_BOLD if remaining == 0 else 0
            self._add(
                stdscr,
                5,
                2,
                f"Files: {len(records)} · {remaining_text} · Complete: {complete} · "
                f"Filtered only: {filtered_only}",
                summary_attr,
            )

            self._add(stdscr, 7, 2, "STATUS", curses.A_BOLD)
            self._add(stdscr, 7, 34, "SESSION", curses.A_BOLD)
            self._add(stdscr, 7, 58, "FILE / CHANGE INDEX", curses.A_BOLD)
            self._add(stdscr, 8, 2, "─" * max(1, width - 4), self._color_pair(4))

            list_top = 9
            footer_lines = main_footer_lines(max(1, width - 4))
            list_height = max(1, height - list_top - len(footer_lines) - 1)
            display_rows = self._main_rows(records)
            selectable_positions = [
                index for index, row in enumerate(display_rows) if row.kind in {"file", "change"}
            ]

            selected_display = 0
            if selectable_positions:
                if self.main_selection_key is not None:
                    found = next(
                        (
                            index
                            for index in selectable_positions
                            if self._main_row_key(display_rows[index], records)
                            == self.main_selection_key
                        ),
                        None,
                    )
                else:
                    found = None
                if found is None:
                    found = next(
                        (
                            index
                            for index in selectable_positions
                            if display_rows[index].kind == "file"
                            and display_rows[index].record_index == self.selected
                        ),
                        selectable_positions[0],
                    )
                    self._set_main_row_selection(display_rows[found], records)
                selected_display = found

            start = max(0, selected_display - list_height + 1)
            start = min(start, max(0, len(display_rows) - list_height))
            section_start = selected_display
            while section_start > 0 and display_rows[section_start].kind != "section":
                section_start -= 1
            if selected_display - section_start < list_height:
                start = min(start, section_start)

            for screen_row, row in enumerate(display_rows[start : start + list_height]):
                y = list_top + screen_row
                absolute_index = start + screen_row
                if row.kind == "section":
                    self._add(
                        stdscr,
                        y,
                        2,
                        f"── {row.section} " + "─" * max(1, width - len(row.section) - 8),
                        self._color_pair(4) | curses.A_BOLD,
                    )
                    continue

                if row.record_index is None:
                    continue
                record, status_text, counts = status_rows[row.record_index]
                selected_attr = curses.A_REVERSE if absolute_index == selected_display else 0

                if row.kind == "change":
                    if row.change_index is None or row.block is None:
                        continue
                    block = row.block
                    marker = "▶ " if absolute_index == selected_display else "  "
                    text = (
                        f"{marker}{row.change_index + 1}. {row.summary} · "
                        f"{change_block_location(block)}"
                    )
                    self._add(
                        stdscr,
                        y,
                        58,
                        text,
                        selected_attr | self._color_pair(3),
                    )
                    continue

                if row.kind == "summary":
                    summary_attr = self._color_pair(5) if counts.handled else curses.A_DIM
                    self._add(stdscr, y, 60, f"↳ {row.summary}", summary_attr | curses.A_DIM)
                    continue

                states = " · ".join(record.states) or "—"
                if status_text == "ERROR" or status_text == "TEST ONLY":
                    status_attr = self._color_pair(1) | curses.A_BOLD
                elif status_text == "DEV ONLY":
                    status_attr = self._color_pair(4) | curses.A_BOLD
                elif status_text.startswith("DONE MANUALLY"):
                    status_attr = self._color_pair(5) | curses.A_BOLD
                elif status_text == "COMPLETE":
                    status_attr = self._color_pair(2) | curses.A_BOLD
                elif status_text.startswith("FILTERED ONLY") or status_text == "NO DIFFS":
                    status_attr = curses.A_DIM
                elif "DIFF" in status_text:
                    status_attr = self._color_pair(3) | curses.A_BOLD
                else:
                    status_attr = 0

                expanded = record.relative_path in self.expanded_files
                disclosure = "▾" if expanded else "▸"
                self._add(stdscr, y, 2, f"{status_text:<30}", selected_attr | status_attr)
                states_attr = self._color_pair(1) | curses.A_BOLD if record.test_symlink_path else 0
                self._add(stdscr, y, 34, f"{states:<22}", selected_attr | states_attr)
                self._add(
                    stdscr,
                    y,
                    58,
                    f"{disclosure} {record.relative_path}",
                    selected_attr,
                )

            if not records:
                self._add(
                    stdscr,
                    10,
                    2,
                    "No DEV/TEST differences or review history remain.",
                    self._color_pair(2),
                )

            footer_lines = main_footer_lines(max(1, width - 4))
            footer_top = height - len(footer_lines) - 1
            for footer_row, footer_text in enumerate(footer_lines):
                self._draw_footer(stdscr, footer_top + footer_row, 2, footer_text)
            self._add(stdscr, height - 1, 2, self.status, self._color_pair(3))
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return "quit"

            selected_row = display_rows[selected_display] if display_rows else None
            if key in (curses.KEY_UP, ord("k")) and selectable_positions:
                current = selectable_positions.index(selected_display)
                selected_display = selectable_positions[(current - 1) % len(selectable_positions)]
                self._set_main_row_selection(display_rows[selected_display], records)
            elif key in (curses.KEY_DOWN, ord("j")) and selectable_positions:
                current = selectable_positions.index(selected_display)
                selected_display = selectable_positions[(current + 1) % len(selectable_positions)]
                self._set_main_row_selection(display_rows[selected_display], records)
            elif key == ord("[") and records:
                self.selected = (self.selected - 1) % len(records)
                self.main_selection_key = ("file", records[self.selected].relative_path, None)
            elif key == ord("]") and records:
                self.selected = (self.selected + 1) % len(records)
                self.main_selection_key = ("file", records[self.selected].relative_path, None)
            elif (
                key == ord(" ")
                and selected_row is not None
                and selected_row.record_index is not None
            ):
                record = records[selected_row.record_index]
                if record.relative_path in self.expanded_files:
                    self.expanded_files.remove(record.relative_path)
                    self.main_selection_key = ("file", record.relative_path, None)
                    self.status = f"Collapsed {record.relative_path}."
                else:
                    self.expanded_files.add(record.relative_path)
                    self.main_selection_key = ("file", record.relative_path, None)
                    self.status = f"Expanded {record.relative_path}."
            elif key in (curses.KEY_ENTER, 10, 13) and selected_row is not None:
                if selected_row.kind == "file" and selected_row.record_index is not None:
                    self.selected = selected_row.record_index
                    self.pending_change_index = None
                    self.pending_open_review = False
                    return "open"
                if selected_row.kind == "change" and selected_row.record_index is not None:
                    self.selected = selected_row.record_index
                    self.pending_change_index = selected_row.change_index or 0
                    self.pending_open_review = True
                    return "open"
            elif key in (ord("s"), ord("S")):
                self.workbench.scan()
                self.status = "Rescanned DEV and TEST."
            elif key in (ord("u"), ord("U")) and records:
                record = records[self.selected]
                if not self.confirm(
                    stdscr,
                    "Undo this run's file edits and review progress? Project patterns stay unchanged.",
                ):
                    continue
                changed, message, needs_confirmation = self.workbench.undo_session_changes(record)
                if needs_confirmation and self.confirm(
                    stdscr,
                    "TEST changed outside the tool. Restore the session-start copy anyway?",
                ):
                    changed, message, _ = self.workbench.undo_session_changes(record, force=True)
                self.status = message
            elif key in (ord("p"), ord("P")) and records:
                self.pattern_manager_screen(stdscr)
            elif key in (ord("f"), ord("F")):
                self.display_filters_screen(stdscr)
            elif key in (ord("c"), ord("C")):
                self.configure_screen(stdscr)
            elif key in (ord("x"), ord("X")):
                self.edit_project_config(stdscr)
            elif key == ord("?"):
                self.help_screen(stdscr)

    def _kind_attr(self, kind: str) -> int:
        if kind in {"remove", "remove_note"}:
            return self._test_red_attr()
        if kind in {"add", "add_note"}:
            return self._color_pair(2)
        if kind == "filtered_header":
            return self._color_pair(5) | curses.A_BOLD
        if kind == "filtered_footer":
            return (
                self._muted_magenta_attr()
                if self.workbench.mute_non_focused
                else self._color_pair(5)
            )
        if kind == "filtered_remove":
            return (
                self._muted_red_attr() if self.workbench.mute_non_focused else self._test_red_attr()
            )
        if kind == "filtered_add":
            return (
                self._muted_green_attr() if self.workbench.mute_non_focused else self._color_pair(2)
            )
        if kind == "filtered_context":
            return self._muted_text_attr() if self.workbench.mute_non_focused else 0
        if kind in {"test_file_header", "dev_file_header"}:
            return curses.A_BOLD
        if kind in {"hunk", "legend", "filter_item", "rule_title"}:
            return self._color_pair(4) | curses.A_BOLD
        if kind in {"title", "section"}:
            return self._color_pair(5) | curses.A_BOLD
        if kind in {"regex_name", "summary"}:
            return self._color_pair(3) | curses.A_BOLD
        if kind == "regex_pattern":
            return self._muted_cyan_attr()
        if kind == "selector_selected":
            return self._color_pair(3) | curses.A_BOLD | curses.A_REVERSE
        if kind in {"selector_kept", "handled"}:
            return self._color_pair(5) | curses.A_BOLD
        if kind == "selector":
            return self._muted_cyan_attr()
        if kind == "filtered":
            return self._color_pair(5) | curses.A_BOLD
        if kind.startswith("filtered") or kind == "note":
            return self._muted_text_attr()
        if kind == "error":
            return self._test_red_attr(bold=True)
        return 0

    def _muted_kind_attr(self, kind: str) -> int:
        """Use a readable soft-muted palette for non-selected diff content."""
        if kind in {"remove", "remove_note", "filtered_remove"}:
            return self._muted_red_attr()
        if kind in {"add", "add_note", "filtered_add"}:
            return self._muted_green_attr()
        if kind == "selector_selected":
            return self._kind_attr(kind)
        if kind in {"hunk", "legend", "filter_item", "rule_title", "selector"}:
            return self._muted_cyan_attr()
        if kind in {"filtered_header", "filtered_footer", "filtered", "selector_kept", "handled"}:
            return self._muted_magenta_attr()
        if kind == "filtered_context":
            return self._muted_text_attr()
        if kind in {"test_file_header", "dev_file_header"}:
            return curses.A_BOLD
        return self._muted_text_attr()

    def _draw_display_line(
        self,
        stdscr: Any,
        y: int,
        x: int,
        line: DisplayLine,
        number_width: int,
        horizontal: int,
        *,
        muted: bool = False,
        selected_guide: bool = False,
    ) -> None:
        """Draw fixed TEST/DEV line-number gutters plus a horizontally scrollable body."""
        body_attr = self._muted_kind_attr(line.kind) if muted else self._kind_attr(line.kind)
        if line.test_line is None and line.dev_line is None:
            if line.kind == "legend" and horizontal == 0:
                cursor = x
                label = "Line columns: "
                self._add(
                    stdscr,
                    y,
                    cursor,
                    label,
                    self._color_pair(5) | curses.A_BOLD,
                )
                cursor += len(label)
                test_label = "TEST/current"
                self._add(stdscr, y, cursor, test_label, self._test_red_attr())
                cursor += len(test_label)
                divider = " │ "
                self._add(stdscr, y, cursor, divider, self._muted_cyan_attr())
                cursor += len(divider)
                self._add(stdscr, y, cursor, "DEV/incoming", self._color_pair(2))
                return

            display_text = line.text
            if line.kind == "selector_selected":
                _, width = stdscr.getmaxyx()
                display_text = self._selected_change_banner(
                    display_text,
                    max(1, width - x - 1),
                )
            self._add(stdscr, y, x, display_text[horizontal:], body_attr)
            return

        if selected_guide:
            # The guide sits directly beneath the selected header's arrow and
            # remains yellow even when non-focused content muting is enabled.
            self._add(
                stdscr,
                y,
                x,
                "│",
                self._color_pair(3) | curses.A_BOLD,
            )
            x += 2

        test_text = (
            f"{line.test_line:>{number_width}}"
            if line.test_line is not None
            else " " * number_width
        )
        dev_text = (
            f"{line.dev_line:>{number_width}}" if line.dev_line is not None else " " * number_width
        )
        test_attr = self._test_red_attr(dim=True) if muted else self._test_red_attr()
        self._add(
            stdscr,
            y,
            x,
            test_text,
            test_attr if line.test_line is not None else self._muted_text_attr(),
        )
        cursor = x + number_width
        self._add(stdscr, y, cursor, " ")
        cursor += 1
        dev_attr = self._muted_green_attr() if muted else self._color_pair(2)
        self._add(
            stdscr,
            y,
            cursor,
            dev_text,
            dev_attr if line.dev_line is not None else self._muted_text_attr(),
        )
        cursor += number_width
        self._add(stdscr, y, cursor, " │ ", self._muted_cyan_attr())
        cursor += 3

        if line.kind in {"filtered_remove", "filtered_add", "filtered_context"}:
            guide = "│ "
            if horizontal < len(guide):
                self._add(
                    stdscr,
                    y,
                    cursor,
                    guide[horizontal:],
                    self._color_pair(5) | curses.A_BOLD,
                )
                cursor += len(guide) - horizontal
                body_horizontal = 0
            else:
                body_horizontal = horizontal - len(guide)
            marker = "  "
            if line.kind == "filtered_remove":
                marker = "- "
            elif line.kind == "filtered_add":
                marker = "+ "
            self._add(stdscr, y, cursor, (marker + line.text)[body_horizontal:], body_attr)
            return

        marker = "  "
        if line.kind == "remove":
            marker = "- "
        elif line.kind == "add":
            marker = "+ "
        self._add(stdscr, y, cursor, (marker + line.text)[horizontal:], body_attr)

    def detail_screen(
        self,
        stdscr: Any,
        selected: int,
        *,
        initial_selected_change: int = 0,
        open_review: bool = False,
    ) -> str:
        mode = "focused"
        expand_filtered = False
        scroll = 0
        horizontal = 0
        selected_change = max(0, initial_selected_change)
        jump_to_selected = True
        open_review_once = open_review
        self.selected = selected
        while self.workbench.records:
            self.selected = max(0, min(self.selected, len(self.workbench.records) - 1))
            record = self.workbench.records[self.selected]
            self.workbench.refresh_record(record)

            if mode == "focused":
                presentation = review_unified_diff(
                    record,
                    self.workbench.enabled_patterns,
                    self.workbench.settings.context,
                    hide_whitespace=self.workbench.hide_whitespace,
                    hide_mapping_order=self.workbench.hide_mapping_order,
                    expand_filtered=expand_filtered,
                    selected_change=selected_change,
                )
                selected_change = presentation.selected_change or 0
                pattern_hidden = presentation.pattern_hidden_count
                whitespace_hidden = presentation.whitespace_hidden_count
                mapping_hidden = presentation.mapping_order_hidden_count
                mapping_state = mapping_order_status_text(
                    enabled=self.workbench.hide_mapping_order,
                    hidden_count=mapping_hidden,
                    unavailable_reason=presentation.mapping_order_unavailable_reason,
                )
                mode_label = "FOCUSED DIFF"
                hidden_state = "expanded" if expand_filtered else "collapsed"
                mode_note = (
                    f"{presentation.visible_change_count} active · "
                    f"{presentation.handled_count} handled · "
                    f"{pattern_hidden} pattern-hidden · {whitespace_hidden} whitespace-hidden · "
                    f"{hidden_state}."
                )
                mapping_note = mapping_state
            else:
                presentation = full_unified_diff(
                    record,
                    self.workbench.settings.context,
                    selected_change=selected_change,
                )
                selected_change = presentation.selected_change or 0
                mode_label = "FULL DIFF"
                mode_note = (
                    f"{presentation.visible_change_count} active · "
                    f"{presentation.handled_count} handled · "
                    "actual TEST/DEV text is shown."
                )
                mapping_note = "Display filters are disabled in Full Diff."

            selected_block = presentation.selected_block
            if open_review_once:
                open_review_once = False
                if selected_block is None:
                    self.status = (
                        "That change is no longer active; showing the refreshed file diff."
                    )
                else:
                    result = self.review_action_menu(
                        stdscr,
                        record,
                        mode=mode,
                        selected_change=selected_change,
                    )
                    if result.quit:
                        return "quit"
                    if result.file_delta:
                        self.selected = (self.selected + result.file_delta) % len(
                            self.workbench.records
                        )
                        selected_change = 0
                        scroll = horizontal = 0
                        jump_to_selected = True
                        continue
                    selected_change = result.selected_change
                    scroll = horizontal = 0
                    jump_to_selected = True
                    continue

            if presentation.visible_change_count and selected_block is not None:
                progress_note = (
                    f"ACTIVE CHANGE {selected_change + 1}/{presentation.visible_change_count} · "
                    "[j] next · [k] previous"
                )
            elif mode == "focused":
                if presentation.handled_count:
                    progress_note = (
                        "No active diffs remain. This file is complete; handled changes remain "
                        "in session history."
                    )
                elif (
                    presentation.pattern_hidden_count
                    or presentation.whitespace_hidden_count
                    or presentation.mapping_order_hidden_count
                ):
                    progress_note = (
                        "No active diffs; remaining differences are hidden by approved filters. "
                        "The file is FILTERED ONLY, not complete."
                    )
                else:
                    progress_note = "No active review blocks were produced; inspect Full Diff."
            else:
                progress_note = (
                    "No active changes remain; Full Diff still shows current handled text."
                )

            content = presentation.lines
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            body_top = 7
            body_height = max(1, height - body_top - 4)
            max_scroll = max(0, len(content) - body_height)
            selected_body_range = selected_diff_body_range(presentation)
            max_horizontal = maximum_horizontal_offset(
                content,
                presentation.number_width,
                width,
                x=1,
                selected_body_range=selected_body_range,
            )
            horizontal = max(0, min(horizontal, max_horizontal))
            if jump_to_selected and presentation.selected_line_index is not None:
                scroll = max(0, presentation.selected_line_index - max(1, body_height // 3))
                jump_to_selected = False
            scroll = max(0, min(scroll, max_scroll))
            position_note, compact_position = self._viewport_file_position(
                content, scroll, body_height, record
            )

            self._add(stdscr, 0, 2, record.relative_path, curses.A_BOLD | self._color_pair(5))
            states = " · ".join(record.states) or "—"
            file_status, focused_counts = self.workbench.file_status(record)
            state_attr = (
                self._color_pair(1) | curses.A_BOLD
                if record.test_symlink_path
                else (self._color_pair(3) if record.uncommitted else 0)
            )
            self._add(
                stdscr,
                1,
                2,
                f"{file_status} | {states}",
                state_attr,
            )
            self._add(stdscr, 2, 2, mode_label, curses.A_BOLD | self._color_pair(4))
            full_position_x = width - len(position_note) - 2
            if full_position_x > 2 + len(mode_label) + 3:
                self._add(
                    stdscr,
                    2,
                    full_position_x,
                    position_note,
                    curses.A_BOLD | self._color_pair(4),
                )
            else:
                compact_x = width - len(compact_position) - 2
                if compact_x > 2 + len(mode_label) + 2:
                    self._add(
                        stdscr,
                        2,
                        compact_x,
                        compact_position,
                        curses.A_BOLD | self._color_pair(4),
                    )
            self._add(stdscr, 3, 2, mode_note, self._color_pair(3))
            mapping_attr = (
                self._color_pair(1) | curses.A_BOLD
                if "UNAVAILABLE" in mapping_note
                else self._color_pair(3)
            )
            self._add(stdscr, 4, 2, mapping_note, mapping_attr)
            self._add(stdscr, 5, 2, progress_note, self._color_pair(3) | curses.A_BOLD)
            if record.read_error:
                self._add(stdscr, 6, 2, record.read_error, self._color_pair(1))

            selected_range = presentation.selected_line_range
            for row, line in enumerate(content[scroll : scroll + body_height]):
                absolute_index = scroll + row
                muted = bool(
                    self.workbench.mute_non_focused
                    and selected_range is not None
                    and line.kind not in {"legend", "test_file_header", "dev_file_header"}
                    and not (selected_range[0] <= absolute_index < selected_range[1])
                )
                selected_guide = bool(
                    selected_body_range is not None
                    and selected_body_range[0] <= absolute_index < selected_body_range[1]
                )
                self._draw_display_line(
                    stdscr,
                    body_top + row,
                    1,
                    line,
                    presentation.number_width,
                    horizontal,
                    muted=muted,
                    selected_guide=selected_guide,
                )

            if mode == "focused":
                hidden_action = "[h]collapse hidden" if expand_filtered else "[h]expand hidden"
                view_actions = f"[d]full diff  {hidden_action}  [f]display filters  [g]patterns"
            else:
                view_actions = "[d]focused diff  [f]display filters  [g]patterns"
            if record.resolved_mode == "manual":
                completion_action = "[m]reopen file"
            elif focused_counts.active:
                completion_action = "[m]mark complete"
            else:
                completion_action = ""
            actions = f"Navigate: [j/k]change  [Enter]review  View: {view_actions}"
            navigation_parts = [
                completion_action,
                "[u]undo session changes",
                "[x]file actions",
                "Prev file: [",
                "Next file: ]",
                "[b]ack",
                "[↑/↓]scroll",
                "[←/→]horizontal",
                "[?]help",
                "[q]quit",
            ]
            navigation = "File: " + "  ".join(item for item in navigation_parts if item)
            self._draw_footer(stdscr, height - 3, 1, actions)
            self._draw_footer(stdscr, height - 2, 1, navigation)
            self._add(stdscr, height - 1, 1, self.status, self._color_pair(3))
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return "quit"
            if key in (ord("b"), ord("B"), 27):
                return "back"
            if key in (ord("d"), ord("D")):
                mode = "full" if mode == "focused" else "focused"
                scroll = horizontal = 0
                selected_change = 0
                jump_to_selected = True
            elif key in (ord("h"), ord("H")) and mode == "focused":
                expand_filtered = not expand_filtered
                scroll = horizontal = 0
                jump_to_selected = True
                self.status = (
                    "Hidden blocks expanded inline."
                    if expand_filtered
                    else "Hidden blocks collapsed."
                )
            elif key in (ord("f"), ord("F")):
                self.display_filters_screen(stdscr)
                scroll = horizontal = 0
                selected_change = 0
                jump_to_selected = True
            elif key in (ord("g"), ord("G")):
                self.pattern_manager_screen(stdscr)
                scroll = horizontal = 0
                selected_change = 0
                jump_to_selected = True
            elif key in (10, 13, curses.KEY_ENTER):
                if selected_block is None:
                    self.status = "No selected change is available for actions."
                else:
                    result = self.review_action_menu(
                        stdscr,
                        record,
                        mode=mode,
                        selected_change=selected_change,
                    )
                    if result.quit:
                        return "quit"
                    if result.file_delta:
                        self.selected = (self.selected + result.file_delta) % len(
                            self.workbench.records
                        )
                        selected_change = 0
                        scroll = horizontal = 0
                        jump_to_selected = True
                        continue
                    selected_change = result.selected_change
                    if result.changed:
                        scroll = horizontal = 0
                        jump_to_selected = True
            elif key in (ord("m"), ord("M")):
                if record.resolved_mode == "manual":
                    self.workbench.mark_complete(record, False)
                    self.status = "Reopened the file for review."
                elif focused_counts.active:
                    counts = self.workbench.mark_complete(record, True)
                    self.status = f"Marked done manually with {counts.active} active diff(s)."
                else:
                    self.status = (
                        "No active diffs remain; this file is already automatically complete."
                    )
            elif key in (ord("u"), ord("U")):
                if not self.confirm(
                    stdscr,
                    "Undo this run's file edits and review progress? Project patterns stay unchanged.",
                ):
                    continue
                changed, message, needs_confirmation = self.workbench.undo_session_changes(record)
                if needs_confirmation and self.confirm(
                    stdscr,
                    "TEST changed outside the tool. Restore the session-start copy anyway?",
                ):
                    changed, message, _ = self.workbench.undo_session_changes(record, force=True)
                self.status = message
                if changed:
                    selected_change = 0
                    scroll = horizontal = 0
                    jump_to_selected = True
            elif key in (ord("x"), ord("X")):
                file_result = self.file_actions_menu(stdscr, record)
                if file_result == "quit":
                    return "quit"
                if file_result == "changed":
                    scroll = horizontal = 0
                    selected_change = 0
                    jump_to_selected = True
            elif key in (ord("j"), ord("J")):
                count = presentation.visible_change_count
                if count:
                    selected_change = (selected_change + 1) % count
                    jump_to_selected = True
                else:
                    self.status = "No changes to step through in this view."
            elif key in (ord("k"), ord("K")):
                count = presentation.visible_change_count
                if count:
                    selected_change = (selected_change - 1) % count
                    jump_to_selected = True
                else:
                    self.status = "No changes to step through in this view."
            elif key == curses.KEY_UP:
                scroll = max(0, scroll - 1)
            elif key == curses.KEY_DOWN:
                scroll = min(max_scroll, scroll + 1)
            elif key == curses.KEY_PPAGE:
                scroll = max(0, scroll - body_height)
            elif key == curses.KEY_NPAGE:
                scroll = min(max_scroll, scroll + body_height)
            elif key == curses.KEY_LEFT:
                horizontal = max(0, horizontal - 4)
            elif key == curses.KEY_RIGHT:
                horizontal = min(max_horizontal, horizontal + 4)
            elif key == ord("]"):
                self.selected = (self.selected + 1) % len(self.workbench.records)
                selected_change = 0
                scroll = horizontal = 0
                jump_to_selected = True
            elif key == ord("["):
                self.selected = (self.selected - 1) % len(self.workbench.records)
                selected_change = 0
                scroll = horizontal = 0
                jump_to_selected = True
            elif key == ord("?"):
                self.help_screen(stdscr)

    def display_filters_screen(self, stdscr: Any) -> None:
        selected = 0
        options = (
            (
                "Show whitespace-only changes",
                "Off by default. Enable to show indentation/spacing-only blocks in Focused Diff.",
            ),
            (
                "YAML order-only changes",
                "Hides exact scalar mapping moves and unique name-keyed list moves. "
                "Changed named items become one logical replacement; ambiguous YAML stays visible.",
            ),
            (
                "Mute non-focused diff content",
                "Off by default. Keeps the selected change bright and softens surrounding "
                "and expanded filtered diff content.",
            ),
        )
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            self._add(stdscr, 0, 2, "DISPLAY FILTERS", curses.A_BOLD | self._color_pair(5))
            self._add(
                stdscr,
                1,
                2,
                "Filtering affects Focused Diff only; muting is visual in both views. Full Diff hides nothing.",
                self._color_pair(3),
            )
            states = (
                not self.workbench.hide_whitespace,
                self.workbench.hide_mapping_order,
                self.workbench.mute_non_focused,
            )
            for index, ((label, description), enabled) in enumerate(zip(options, states)):
                y = 3 + index * 4
                selected_attr = curses.A_REVERSE if index == selected else 0
                if index == 0:
                    state = "SHOWN" if enabled else "HIDDEN"
                elif index == 1:
                    state = "HIDDEN" if enabled else "VISIBLE"
                else:
                    state = "ON" if enabled else "OFF"
                state_attr = self._color_pair(3) | curses.A_BOLD if enabled else curses.A_DIM
                self._add(
                    stdscr,
                    y,
                    2,
                    f"[{'x' if enabled else ' '}] {label}",
                    selected_attr | curses.A_BOLD,
                )
                self._add(
                    stdscr, y, max(34, min(width - 18, 48)), state, selected_attr | state_attr
                )
                self._add(stdscr, y + 1, 6, description, curses.A_DIM)

            self._draw_footer(
                stdscr,
                height - 2,
                2,
                "Navigate: [j/k or ↑/↓]select  [Space/Enter]toggle  [b]ack",
            )
            self._add(stdscr, height - 1, 2, self.status, self._color_pair(3))
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("b"), ord("B"), 27, ord("q"), ord("Q")):
                return
            if key in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(options)
                continue
            if key in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(options)
                continue
            if key in (ord(" "), 10, 13, curses.KEY_ENTER):
                if selected == 0:
                    _, self.status = self.workbench.set_hide_whitespace(
                        not self.workbench.hide_whitespace
                    )
                elif selected == 1:
                    _, self.status = self.workbench.set_hide_mapping_order(
                        not self.workbench.hide_mapping_order
                    )
                else:
                    _, self.status = self.workbench.set_mute_non_focused(
                        not self.workbench.mute_non_focused
                    )

    def pattern_manager_screen(self, stdscr: Any) -> None:
        selected = 0
        scroll = 0
        expanded_categories: set[str] = set()
        while True:
            candidates = self.workbench.pattern_candidates()
            protected = self.workbench.protected_summaries()
            rows = build_pattern_manager_rows(candidates, protected, expanded_categories)
            if rows:
                selected = max(0, min(selected, len(rows) - 1))
            else:
                selected = 0

            stdscr.erase()
            height, width = stdscr.getmaxyx()
            self._add(stdscr, 0, 2, "PROJECT PATTERN MANAGER", curses.A_BOLD | self._color_pair(5))
            self._add(
                stdscr,
                1,
                2,
                f"Scanned {len(self.workbench.records)} changed file(s). Patterns apply project-wide.",
                self._color_pair(4) | curses.A_BOLD,
            )
            display_state = (
                f"whitespace {'HIDDEN' if self.workbench.hide_whitespace else 'VISIBLE'} · "
                f"YAML order {'HIDDEN' if self.workbench.hide_mapping_order else 'VISIBLE'} · "
                f"background {'MUTED' if self.workbench.mute_non_focused else 'FULL BRIGHTNESS'}"
            )
            self._add(
                stdscr,
                2,
                2,
                "New noise suggestions start hidden. Expand a category to audit or show individual rules.",
                self._color_pair(3),
            )
            self._add(
                stdscr,
                3,
                2,
                f"Display filters: {display_state}. "
                "ALWAYS REVIEWED changes cannot be hidden by patterns.",
                self._color_pair(3),
            )
            self._add(stdscr, 5, 2, "STATE", curses.A_BOLD)
            self._add(stdscr, 5, 14, "MATCHES", curses.A_BOLD)
            self._add(stdscr, 5, 24, "FILES", curses.A_BOLD)
            self._add(stdscr, 5, 32, "OVERLAP", curses.A_BOLD)
            self._add(stdscr, 5, 42, "CATEGORY / PATTERN", curses.A_BOLD)
            self._add(stdscr, 6, 2, "─" * max(1, width - 4), self._color_pair(4))

            list_top = 7
            list_height = max(1, height - list_top - 4)
            if selected < scroll:
                scroll = selected
            elif selected >= scroll + list_height:
                scroll = selected - list_height + 1
            max_scroll = max(0, len(rows) - list_height)
            scroll = max(0, min(scroll, max_scroll))

            for row_offset, index in enumerate(range(scroll, min(len(rows), scroll + list_height))):
                item = rows[index]
                y = list_top + row_offset
                selected_attr = curses.A_REVERSE if index == selected else 0

                if item.kind == "category":
                    members = _category_members(candidates, item.category)
                    state = _category_state(members)
                    match_count = sum(candidate.match_count for candidate in members)
                    file_count = len(
                        {path for candidate in members for path in candidate.affected_files}
                    )
                    overlap = sum(candidate.overlap_count for candidate in members)
                    attr = selected_attr | curses.A_BOLD | self._color_pair(5)
                    self._add(stdscr, y, 2, f"{state:<10}", attr)
                    self._add(stdscr, y, 14, f"{match_count:<8}", attr)
                    self._add(stdscr, y, 24, f"{file_count:<6}", attr)
                    self._add(stdscr, y, 32, f"{overlap or '—':<8}", attr)
                    marker = "▾" if item.category in expanded_categories else "▸"
                    self._add(stdscr, y, 42, f"{marker} {item.label.upper()}", attr)
                    continue

                if item.kind == "pattern" and item.candidate is not None:
                    candidate = item.candidate
                    state = "HIDDEN" if candidate.rule.enabled else "VISIBLE"
                    attr = selected_attr
                    if index != selected:
                        attr |= (
                            self._color_pair(3) | curses.A_BOLD
                            if candidate.rule.enabled
                            else curses.A_DIM
                        )
                    saved = " · saved" if candidate.persisted else " · suggested"
                    overlap = str(candidate.overlap_count) if candidate.overlap_count else "—"
                    self._add(stdscr, y, 2, f"{state:<10}", attr)
                    self._add(stdscr, y, 14, f"{candidate.match_count:<8}", attr)
                    self._add(stdscr, y, 24, f"{candidate.file_count:<6}", attr)
                    self._add(stdscr, y, 32, f"{overlap:<8}", attr)
                    self._add(stdscr, y, 42, "  ↳ " + candidate.rule.name + saved, attr)
                    continue

                if item.kind == "protected_category":
                    attr = selected_attr | curses.A_BOLD | self._color_pair(1)
                    self._add(stdscr, y, 2, f"{'LOCKED':<10}", attr)
                    marker = "▾" if item.category in expanded_categories else "▸"
                    self._add(stdscr, y, 42, f"{marker} {item.label.upper()}", attr)
                    continue

                if item.kind == "protected" and item.protected is not None:
                    summary = item.protected
                    attr = selected_attr | (self._color_pair(3) if index != selected else 0)
                    self._add(stdscr, y, 2, f"{'VISIBLE':<10}", attr)
                    self._add(stdscr, y, 14, f"{summary.match_count:<8}", attr)
                    self._add(stdscr, y, 24, f"{summary.file_count:<6}", attr)
                    self._add(stdscr, y, 32, f"{'—':<8}", attr)
                    self._add(stdscr, y, 42, "  ↳ " + summary.name, attr)

            if not rows:
                self._add(
                    stdscr,
                    8,
                    2,
                    "No repeated project-wide replacement patterns were found.",
                    self._color_pair(3),
                )

            self._draw_footer(
                stdscr,
                height - 3,
                1,
                "Navigate: [↑/↓]select  [Enter]expand/preview  [Space]hide/show",
            )
            self._draw_footer(
                stdscr,
                height - 2,
                1,
                "Actions: [f]display filters  [x]project config  [b]ack",
            )
            self._add(stdscr, height - 1, 1, self.status, self._color_pair(3))
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("b"), ord("B"), 27, ord("q"), ord("Q")):
                return
            if key in (curses.KEY_UP, ord("k")) and rows:
                selected = (selected - 1) % len(rows)
            elif key in (curses.KEY_DOWN, ord("j")) and rows:
                selected = (selected + 1) % len(rows)
            elif key in (10, 13, curses.KEY_ENTER) and rows:
                item = rows[selected]
                if item.kind == "pattern" and item.candidate is not None:
                    self.pattern_preview_screen(stdscr, item.candidate.rule.id)
                elif item.kind == "protected" and item.protected is not None:
                    self.protected_preview_screen(stdscr, item.protected.name)
                elif item.kind in {"category", "protected_category"}:
                    if item.category in expanded_categories:
                        expanded_categories.remove(item.category)
                        self.status = f"Collapsed {item.category}."
                    else:
                        expanded_categories.add(item.category)
                        self.status = f"Expanded {item.category}."
                else:
                    self.status = "Always-reviewed changes remain visible in Focused Diff."
            elif key == ord(" ") and rows:
                item = rows[selected]
                if item.kind == "pattern" and item.candidate is not None:
                    _, self.status = self.workbench.set_pattern_enabled(
                        item.candidate, not item.candidate.rule.enabled
                    )
                elif item.kind == "category":
                    members = _category_members(candidates, item.category)
                    enable = not members or not all(candidate.rule.enabled for candidate in members)
                    _, self.status = self.workbench.set_category_patterns(item.category, enable)
                else:
                    self.status = "ALWAYS REVIEWED changes cannot be hidden by pattern filters."
            elif key in (ord("f"), ord("F")):
                self.display_filters_screen(stdscr)
            elif key in (ord("x"), ord("X")):
                self.edit_project_config(stdscr)

    def protected_preview_screen(
        self,
        stdscr: Any,
        summary_name: str,
    ) -> None:
        scroll = 0
        while True:
            summary = next(
                (
                    item
                    for item in self.workbench.protected_summaries()
                    if item.name == summary_name
                ),
                None,
            )
            if summary is None:
                self.status = "That always-reviewed group is no longer present."
                return
            lines: list[DisplayLine] = [
                DisplayLine(summary.name, "title"),
                DisplayLine(
                    f"ALWAYS REVIEWED · {summary.match_count} change(s) · "
                    f"{summary.file_count} file(s)",
                    "summary",
                ),
                DisplayLine(
                    "Pattern suggestions never hide these changes. Full Diff and Focused Diff "
                    "both keep them visible.",
                    "summary",
                ),
                DisplayLine("", "text"),
                DisplayLine("SAMPLE CHANGES WITH NEARBY CONTEXT", "section"),
                DisplayLine("", "text"),
            ]
            for index, example in enumerate(summary.examples, start=1):
                lines.append(
                    DisplayLine(f"Example {index} · {example.relative_path}", "filter_item")
                )
                if example.old_context_before is not None:
                    lines.append(
                        DisplayLine(
                            example.old_context_before,
                            "context",
                            test_line=max(1, example.old_line_number - 1),
                        )
                    )
                lines.append(
                    DisplayLine(example.old_line, "remove", test_line=example.old_line_number)
                )
                if example.new_context_before is not None:
                    lines.append(
                        DisplayLine(
                            example.new_context_before,
                            "context",
                            dev_line=max(1, example.new_line_number - 1),
                        )
                    )
                lines.append(DisplayLine(example.new_line, "add", dev_line=example.new_line_number))
                lines.append(DisplayLine("", "text"))

            stdscr.erase()
            height, width = stdscr.getmaxyx()
            self._add(stdscr, 0, 2, "ALWAYS REVIEWED PREVIEW", curses.A_BOLD | self._color_pair(5))
            body_top = 2
            body_height = max(1, height - body_top - 2)
            max_scroll = max(0, len(lines) - body_height)
            scroll = max(0, min(scroll, max_scroll))
            max_line = max(
                [1]
                + [example.old_line_number for example in summary.examples]
                + [example.new_line_number for example in summary.examples]
            )
            number_width = max(3, len(str(max_line)))
            for row, line in enumerate(lines[scroll : scroll + body_height]):
                self._draw_display_line(stdscr, body_top + row, 1, line, number_width, 0)
            self._draw_footer(stdscr, height - 1, 1, "Navigate: [b]ack  [↑/↓]scroll")
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("b"), ord("B"), 27, ord("q"), ord("Q")):
                return
            if key == curses.KEY_UP:
                scroll = max(0, scroll - 1)
            elif key == curses.KEY_DOWN:
                scroll = min(max_scroll, scroll + 1)
            elif key == curses.KEY_PPAGE:
                scroll = max(0, scroll - body_height)
            elif key == curses.KEY_NPAGE:
                scroll = min(max_scroll, scroll + body_height)

    def pattern_preview_screen(
        self,
        stdscr: Any,
        pattern_id: str,
    ) -> None:
        scroll = 0
        while True:
            candidates = self.workbench.pattern_candidates()
            candidate = next((item for item in candidates if item.rule.id == pattern_id), None)
            if candidate is None:
                self.status = "That pattern is no longer present after the project changed."
                return

            lines: list[DisplayLine] = [
                DisplayLine(candidate.rule.name, "title"),
                DisplayLine(
                    f"STATE: {'HIDDEN' if candidate.rule.enabled else 'VISIBLE'} · "
                    f"CATEGORY: {candidate.rule.category} · MATCHES: {candidate.match_count} · "
                    f"FILES: {candidate.file_count} · OVERLAPPING CHANGES: "
                    f"{candidate.overlap_count} · TYPE: {candidate.rule.kind}",
                    "summary",
                ),
                DisplayLine(
                    "SCOPE: PROJECT-WIDE · This is a regex suggestion, not a guarantee of "
                    "semantic equivalence. Full Diff is never filtered.",
                    "summary",
                ),
                DisplayLine("", "text"),
            ]
            if candidate.rule.kind == "environment-fragment":
                lines.extend(
                    [
                        DisplayLine(
                            "ENVIRONMENT SIGNAL: repeated current-target → incoming-source label "
                            "inside scalar values across the project.",
                            "summary",
                        ),
                        DisplayLine("", "text"),
                    ]
                )
            if candidate.rule.kind in {"url-shape", "host-shape", "ip-shape"}:
                lines.extend(
                    [
                        DisplayLine(
                            "BROAD SUGGESTION: this may match unrelated replacements under the "
                            "same YAML key. Review every example before enabling it.",
                            "summary",
                        ),
                        DisplayLine("", "text"),
                    ]
                )
            if candidate.overlap_count:
                lines.extend(
                    [
                        DisplayLine(
                            f"OVERLAP: {candidate.overlap_count} matched change(s) are also covered "
                            "by another pattern. A block stays hidden while any enabled pattern matches.",
                            "summary",
                        ),
                        DisplayLine("", "text"),
                    ]
                )
            lines.extend(
                [
                    DisplayLine("TEST regex", "rule_title"),
                    DisplayLine(candidate.rule.test_regex, "regex_pattern"),
                    DisplayLine("DEV regex", "rule_title"),
                    DisplayLine(candidate.rule.dev_regex, "regex_pattern"),
                    DisplayLine("", "text"),
                    DisplayLine("EXAMPLES WITH NEARBY CONTEXT", "section"),
                    DisplayLine("", "text"),
                ]
            )
            for index, example in enumerate(candidate.examples, start=1):
                lines.append(
                    DisplayLine(
                        f"Example {index} · {example.relative_path}",
                        "filter_item",
                    )
                )
                if example.old_context_before is not None:
                    lines.append(
                        DisplayLine(
                            example.old_context_before,
                            "context",
                            test_line=max(1, example.old_line_number - 1),
                        )
                    )
                lines.append(
                    DisplayLine(example.old_line, "remove", test_line=example.old_line_number)
                )
                if example.old_context_after is not None:
                    lines.append(
                        DisplayLine(
                            example.old_context_after,
                            "context",
                            test_line=example.old_line_number + 1,
                        )
                    )
                if example.new_context_before is not None:
                    lines.append(
                        DisplayLine(
                            example.new_context_before,
                            "context",
                            dev_line=max(1, example.new_line_number - 1),
                        )
                    )
                lines.append(DisplayLine(example.new_line, "add", dev_line=example.new_line_number))
                if example.new_context_after is not None:
                    lines.append(
                        DisplayLine(
                            example.new_context_after,
                            "context",
                            dev_line=example.new_line_number + 1,
                        )
                    )
                lines.append(DisplayLine("", "text"))

            stdscr.erase()
            height, _ = stdscr.getmaxyx()
            self._add(stdscr, 0, 2, "PROJECT PATTERN PREVIEW", curses.A_BOLD | self._color_pair(5))
            body_top = 2
            body_height = max(1, height - body_top - 3)
            max_scroll = max(0, len(lines) - body_height)
            scroll = max(0, min(scroll, max_scroll))
            max_line = max(
                [1]
                + [example.old_line_number for example in candidate.examples]
                + [example.new_line_number for example in candidate.examples]
            )
            number_width = max(3, len(str(max_line)))
            for row, line in enumerate(lines[scroll : scroll + body_height]):
                self._draw_display_line(stdscr, body_top + row, 1, line, number_width, 0)
            action = "show" if candidate.rule.enabled else "hide"
            self._draw_footer(
                stdscr,
                height - 2,
                1,
                f"Actions: [Space]{action} this project pattern  Navigate: [b]ack  [↑/↓]scroll",
            )
            self._add(stdscr, height - 1, 1, self.status, self._color_pair(3))
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("b"), ord("B"), 27, ord("q"), ord("Q")):
                return
            if key == ord(" "):
                _, self.status = self.workbench.set_pattern_enabled(
                    candidate, not candidate.rule.enabled
                )
            elif key == curses.KEY_UP:
                scroll = max(0, scroll - 1)
            elif key == curses.KEY_DOWN:
                scroll = min(max_scroll, scroll + 1)
            elif key == curses.KEY_PPAGE:
                scroll = max(0, scroll - body_height)
            elif key == curses.KEY_NPAGE:
                scroll = min(max_scroll, scroll + body_height)

    def review_action_menu(
        self,
        stdscr: Any,
        record: FileRecord,
        *,
        mode: str,
        selected_change: int,
    ) -> ReviewMenuResult:
        """Review one change at a time without leaving the compact action panel."""
        changed_any = False
        horizontal = 0
        preview_scroll = 0
        while True:
            self.workbench.refresh_record(record)
            if mode == "focused":
                presentation = review_unified_diff(
                    record,
                    self.workbench.enabled_patterns,
                    self.workbench.settings.context,
                    hide_whitespace=self.workbench.hide_whitespace,
                    hide_mapping_order=self.workbench.hide_mapping_order,
                    expand_filtered=False,
                    selected_change=selected_change,
                )
            else:
                presentation = full_unified_diff(
                    record,
                    self.workbench.settings.context,
                    selected_change=selected_change,
                )

            count = presentation.visible_change_count
            if not count or presentation.selected_block is None:
                self.status = "No reviewable changes remain in this view."
                return ReviewMenuResult(0, changed=changed_any)

            selected_change = presentation.selected_change or 0
            block = presentation.selected_block
            preview = selected_change_preview(record, block, context=1)
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            self._add(
                stdscr,
                0,
                2,
                f"REVIEW ACTIONS · ACTIVE CHANGE {selected_change + 1}/{count}",
                curses.A_BOLD | self._color_pair(5),
            )
            self._add(
                stdscr,
                1,
                2,
                f"{record.relative_path} · TEST {_range_text(block.old_start, block.old_end)} · "
                f"DEV {_range_text(block.new_start, block.new_end)}",
                self._color_pair(3) | curses.A_BOLD,
            )
            self._add(
                stdscr,
                2,
                2,
                "Only this selected text block is shown below; one nearby context line is included.",
                curses.A_DIM,
            )

            body_top = 4
            body_height = max(1, height - body_top - 5)
            max_preview_scroll = max(0, len(preview.lines) - body_height)
            preview_scroll = max(0, min(preview_scroll, max_preview_scroll))
            max_horizontal = maximum_horizontal_offset(
                preview.lines, preview.number_width, width, x=1
            )
            horizontal = max(0, min(horizontal, max_horizontal))
            for row, line in enumerate(
                preview.lines[preview_scroll : preview_scroll + body_height]
            ):
                self._draw_display_line(
                    stdscr,
                    body_top + row,
                    1,
                    line,
                    preview.number_width,
                    horizontal,
                )

            self._draw_footer(
                stdscr,
                height - 4,
                1,
                "Resolve: [a]ccept DEV  [s]keep TEST",
            )
            self._draw_footer(
                stdscr,
                height - 3,
                1,
                "Edit: [p]ull DEV + edit  [e]dit TEST  [v]vimdiff",
            )
            self._draw_footer(
                stdscr,
                height - 2,
                1,
                "Navigate: [j/k]change  Prev file: [  Next file: ]  "
                "[↑/↓ or PgUp/PgDn]scroll  [←/→]horizontal  [b]ack  [q]uit",
            )
            self._add(stdscr, height - 1, 1, self.status, self._color_pair(3))
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return ReviewMenuResult(selected_change, changed=changed_any, quit=True)
            if key in (ord("b"), ord("B"), 27):
                return ReviewMenuResult(selected_change, changed=changed_any)
            if key in (ord("j"), ord("J")):
                selected_change = (selected_change + 1) % count
                horizontal = 0
                preview_scroll = 0
                continue
            if key in (ord("k"), ord("K")):
                selected_change = (selected_change - 1) % count
                horizontal = 0
                preview_scroll = 0
                continue
            if key == ord("["):
                return ReviewMenuResult(selected_change, changed=changed_any, file_delta=-1)
            if key == ord("]"):
                return ReviewMenuResult(selected_change, changed=changed_any, file_delta=1)
            if key == curses.KEY_UP:
                preview_scroll = max(0, preview_scroll - 1)
                continue
            if key == curses.KEY_DOWN:
                preview_scroll = min(max_preview_scroll, preview_scroll + 1)
                continue
            if key == curses.KEY_PPAGE:
                preview_scroll = max(0, preview_scroll - body_height)
                continue
            if key == curses.KEY_NPAGE:
                preview_scroll = min(max_preview_scroll, preview_scroll + body_height)
                continue
            if key == curses.KEY_LEFT:
                horizontal = max(0, horizontal - 4)
                continue
            if key == curses.KEY_RIGHT:
                horizontal = min(max_horizontal, horizontal + 4)
                continue
            if key in (ord("a"), ord("A")):
                accepted, message = self.workbench.accept_dev_block(record, block)
                self.status = message
                if not accepted and message.startswith("Unable to apply"):
                    self.blocked_apply_screen(stdscr, record, block, message)
                changed_any = changed_any or accepted
                horizontal = 0
                preview_scroll = 0
                continue
            if key in (ord("p"), ord("P")):
                changed = self.run_change_external(
                    stdscr,
                    record,
                    block,
                    lambda: self.workbench.pull_dev_block_and_edit(record, block),
                    action="PULL DEV + EDIT",
                )
                if not changed and self.status.startswith("Unable to apply"):
                    self.blocked_apply_screen(stdscr, record, block, self.status)
                changed_any = changed_any or changed
                horizontal = 0
                preview_scroll = 0
                continue
            if key in (ord("e"), ord("E")):
                changed = self.run_change_external(
                    stdscr,
                    record,
                    block,
                    lambda: self.workbench.edit_test(record, block.old_start + 1),
                    action="EDITED TEST",
                )
                changed_any = changed_any or changed
                horizontal = 0
                preview_scroll = 0
                continue
            if key in (ord("v"), ord("V")):
                changed = self.run_change_external(
                    stdscr,
                    record,
                    block,
                    lambda: self.workbench.vimdiff(
                        record,
                        block.old_start + 1,
                        block.new_start + 1,
                    ),
                    action="EDITED VIA VIMDIFF",
                )
                changed_any = changed_any or changed
                horizontal = 0
                preview_scroll = 0
                continue
            if key in (ord("s"), ord("S")):
                self.workbench.handle_change(record, block, "KEPT TEST")
                self.status = f"Kept TEST for active change {selected_change + 1}/{count}."
                horizontal = 0
                preview_scroll = 0
                # The handled block leaves the active queue. Keeping the same
                # index naturally selects the next remaining active change.
                continue

    def file_actions_menu(self, stdscr: Any, record: FileRecord) -> str:
        while True:
            stdscr.erase()
            height, _ = stdscr.getmaxyx()
            if record.test_symlink_path:
                copy_label = "[c] Write disabled: TEST path contains a symlink"
            else:
                copy_label = (
                    "[c] Copy the complete DEV file to TEST"
                    if record.dev_exists
                    else "[c] Delete TEST because DEV is absent"
                )
            lines = [
                "FILE ACTIONS",
                "",
                copy_label,
                "[b] Back",
            ]
            top = max(1, (height - len(lines)) // 2)
            self._add(stdscr, top, 4, lines[0], curses.A_BOLD | self._color_pair(5))
            self._draw_footer(stdscr, top + 2, 4, lines[2])
            self._draw_footer(stdscr, top + 3, 4, lines[3])
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return "quit"
            if key in (ord("b"), ord("B"), 27):
                return "back"
            if key in (ord("c"), ord("C")):
                prompt = "Replace TEST with the complete DEV file?"
                if not record.dev_exists:
                    prompt = "DEV file is absent. Delete the TEST file?"
                if record.uncommitted:
                    prompt += " TEST had pre-existing uncommitted changes."
                if not self.confirm(stdscr, prompt):
                    continue
                _, message = self.workbench.copy_dev_to_test(record)
                self.status = message
                return "changed"

    def blocked_apply_screen(
        self,
        stdscr: Any,
        record: FileRecord,
        block: ChangeBlock,
        message: str,
    ) -> None:
        """Explain an all-or-nothing apply refusal without creating file state."""
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            lines = [
                "UNABLE TO APPLY THIS CHANGE SAFELY",
                "",
                message,
                "",
                "TEST was not modified and this change remains active.",
                "",
                "[r] Refresh diff",
                "[v] Open vimdiff",
                "[b] Back",
            ]
            top = max(1, (height - len(lines)) // 2)
            for offset, line in enumerate(lines[:6]):
                attr = self._color_pair(1) | curses.A_BOLD if offset == 0 else 0
                self._add(stdscr, top + offset, 4, line, attr)
            for offset, line in enumerate(lines[6:], start=6):
                self._draw_footer(stdscr, top + offset, 4, line)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("b"), ord("B"), 27, ord("r"), ord("R")):
                self.workbench.refresh_record(record)
                return
            if key in (ord("v"), ord("V")):
                self.run_external(
                    stdscr,
                    lambda: self.workbench.vimdiff(
                        record,
                        block.old_start + 1,
                        block.new_start + 1,
                    ),
                )
                self.workbench.refresh_record(record)
                return

    def run_change_external(
        self,
        stdscr: Any,
        record: FileRecord,
        block: ChangeBlock,
        operation: Any,
        *,
        action: str,
    ) -> bool:
        before = (
            record.test_path.exists(),
            file_hash(record.test_path) if record.test_path.exists() else None,
        )
        self.run_external(stdscr, operation)
        after = (
            record.test_path.exists(),
            file_hash(record.test_path) if record.test_path.exists() else None,
        )
        changed = before != after
        if not changed:
            return False

        self.workbench.refresh_record(record)
        if action != "PULL DEV + EDIT" and exact_change_still_present(
            record, block, hide_mapping_order=self.workbench.hide_mapping_order
        ):
            self.status = (
                f"{self.status} TEST changed elsewhere; the selected change remains active."
            ).strip()
            return True

        handled_action = action
        if action == "PULL DEV + EDIT":
            current_lines = record.test_text.splitlines()
            expected = block.new_lines
            actual = current_lines[block.old_start : block.old_start + len(expected)]
            handled_action = "APPLIED DEV" if actual == expected else "ADAPTED FROM DEV"
        self.workbench.handle_change(record, block, handled_action)
        self.status = f"{self.status} Marked as {handled_action}.".strip()
        return True

    def run_external(self, stdscr: Any, action: Any) -> None:
        curses.def_prog_mode()
        curses.endwin()
        try:
            ok, message = action()
            self.status = message
        finally:
            curses.reset_prog_mode()
            stdscr.refresh()

    def confirm(self, stdscr: Any, message: str) -> bool:
        height, width = stdscr.getmaxyx()
        prompt = message + " Type y to confirm: "
        self._add(stdscr, height - 1, 1, " " * max(1, width - 2))
        self._add(stdscr, height - 1, 1, prompt, self._color_pair(3) | curses.A_BOLD)
        stdscr.refresh()
        return stdscr.getch() in (ord("y"), ord("Y"))

    def _prompt_directory(
        self,
        stdscr: Any,
        prompt: str,
        current: Path,
    ) -> Path | None:
        """Temporarily leave curses and collect one directory with Tab completion."""
        curses.def_prog_mode()
        curses.endwin()
        try:
            print("\nCONFIG REVIEW WORKBENCH — COMPARISON PATHS")
            print("Press Enter to keep the current value. Press Tab to complete paths.")
            print(f"Current: {current}")
            try:
                raw = _directory_input(f"{prompt}: ").strip()
            except (EOFError, KeyboardInterrupt):
                self.status = "Path change canceled."
                return None
        finally:
            curses.reset_prog_mode()
            stdscr.refresh()

        if not raw:
            return current.resolve()
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.resolve()
        if not candidate.is_dir():
            self.status = f"Directory not found: {candidate}"
            return None
        return candidate

    def _switch_comparison_paths(
        self,
        stdscr: Any,
        source: Path,
        target: Path,
    ) -> bool:
        if self.workbench.settings.dry_run:
            self.status = "Dry-run mode: comparison-path changes are disabled."
            return False
        source = source.resolve()
        target = target.resolve()
        if source == target:
            self.status = "DEV/source and TEST/target must be different directories."
            return False
        if (source, target) == (
            self.workbench.settings.source.resolve(),
            self.workbench.settings.target.resolve(),
        ):
            self.status = "Comparison paths are unchanged."
            return False

        if self.workbench.has_review_progress():
            if not self.confirm(
                stdscr,
                "Save the current review session before switching comparison paths?",
            ):
                self.status = "Comparison-path change canceled."
                return False
            try:
                self.workbench.save_session()
            except WorkbenchError as exc:
                self.status = f"Could not save the current review session: {exc}"
                return False

        try:
            disabled_patterns = self.workbench.reconfigure_paths(source, target)
        except WorkbenchError as exc:
            self.status = str(exc)
            return False

        self.selected = 0
        self.main_selection_key = None
        self.expanded_files.clear()
        self.pending_change_index = None
        self.pending_open_review = False
        self.status = f"Switched comparison to {source.name} → {target.name}."
        if disabled_patterns:
            self.status += f" Disabled {disabled_patterns} saved pattern(s); review them again."
        if self.workbench.session.has_saved:
            self.startup_saved_session_screen(stdscr)
        return True

    def change_project_root(self, stdscr: Any) -> bool:
        source = self.workbench.settings.source.resolve()
        target = self.workbench.settings.target.resolve()
        current_root = source.parent if source.parent == target.parent else Path.cwd().resolve()
        project = self._prompt_directory(stdscr, "Project root", current_root)
        if project is None:
            return False

        # Pasting the current environment directory is a common mistake. Search
        # its parent first when that produces one clear sibling pair.
        roots = [project]
        if project.name.lower() in {source.name.lower(), target.name.lower(), "dev", "test"}:
            roots.insert(0, project.parent)

        pairs: list[tuple[Path, Path]] = []
        for root in roots:
            pairs = _environment_pairs_under(root, source.name, target.name)
            if pairs:
                break
        if not pairs:
            self.status = (
                f"No sibling {source.name}/{target.name} or dev/test directories were found "
                f"under {project}. Use 'Set exact DEV and TEST directories' for a custom layout."
            )
            return False
        if len(pairs) > 1:
            self.status = (
                f"Found {len(pairs)} comparison projects under {project}. "
                "Use 'Set exact DEV and TEST directories' to choose one explicitly."
            )
            return False
        return self._switch_comparison_paths(stdscr, *pairs[0])

    def set_exact_comparison_paths(self, stdscr: Any) -> bool:
        source = self._prompt_directory(
            stdscr,
            "DEV/source directory",
            self.workbench.settings.source,
        )
        if source is None:
            return False
        target = self._prompt_directory(
            stdscr,
            "TEST/target directory",
            self.workbench.settings.target,
        )
        if target is None:
            return False
        return self._switch_comparison_paths(stdscr, source, target)

    def comparison_paths_screen(self, stdscr: Any) -> None:
        items = [
            ("Change project root", "Find one sibling DEV/TEST pair beneath a selected root"),
            ("Set exact DEV and TEST directories", "Use this for custom or non-sibling layouts"),
            ("Back", "Return to Configure"),
        ]
        selected = 0
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            self._add(stdscr, 1, 2, "COMPARISON PATHS", curses.A_BOLD | self._color_pair(5))
            self._add(stdscr, 3, 2, f"DEV:  {self.workbench.settings.source}")
            self._add(stdscr, 4, 2, f"TEST: {self.workbench.settings.target}")
            for index, (label, description) in enumerate(items):
                y = 7 + index * 2
                attr = curses.A_REVERSE if index == selected else 0
                self._add(stdscr, y, 2, f"  {label}", attr | curses.A_BOLD)
                if width >= 58:
                    self._add(stdscr, y + 1, 4, description, attr)
            self._draw_footer(
                stdscr,
                height - 1,
                1,
                "Navigate: [↑/↓]select  [Enter]open  [b]ack",
            )
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("b"), ord("B"), 27, ord("q"), ord("Q")):
                return
            if key in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(items)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(items)
            elif key in (curses.KEY_ENTER, 10, 13):
                if selected == 0:
                    if self.change_project_root(stdscr):
                        return
                elif selected == 1:
                    if self.set_exact_comparison_paths(stdscr):
                        return
                else:
                    return

    def configure_screen(self, stdscr: Any) -> None:
        items = [
            (
                "COMPARISON",
                "Comparison paths",
                "Change the project root or enter exact DEV and TEST directories.",
            ),
            (
                "FILTERING",
                "Pattern filters",
                "Review auto-hidden project-wide environment and noise patterns.",
            ),
            (
                "FILTERING",
                "Display filters",
                "Control whitespace, safe YAML order noise, and focused contrast.",
            ),
            (
                "PROJECT",
                "Edit project config",
                "Open the local .config-review.yaml file in the configured editor.",
            ),
            (
                "PROJECT",
                "Rescan",
                "Refresh the current DEV and TEST directories without changing settings.",
            ),
            ("NAVIGATION", "Back", "Return to the main file list."),
        ]
        selected = 0
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            self._add(stdscr, 0, 2, "CONFIGURE", curses.A_BOLD | self._color_pair(5))
            self._add(stdscr, 2, 2, "CURRENT COMPARISON", curses.A_BOLD | self._color_pair(4))
            self._add(stdscr, 3, 4, f"DEV   {self.workbench.settings.source}")
            self._add(stdscr, 4, 4, f"TEST  {self.workbench.settings.target}")
            self._add(stdscr, 5, 2, "─" * max(1, width - 4), self._color_pair(4))

            y = 7
            previous_section = ""
            for index, (section, label, description) in enumerate(items):
                if section != previous_section:
                    self._add(
                        stdscr,
                        y,
                        2,
                        section,
                        curses.A_BOLD | self._color_pair(5),
                    )
                    y += 1
                    previous_section = section
                marker = "▶" if index == selected else " "
                attr = curses.A_REVERSE | curses.A_BOLD if index == selected else curses.A_BOLD
                self._add(stdscr, y, 4, f"{marker} {label}", attr)
                if width >= 82:
                    self._add(stdscr, y, 30, description, curses.A_DIM)
                y += 1

            if width < 82 and height - 3 > y:
                self._add(stdscr, height - 3, 4, items[selected][2], curses.A_DIM)

            self._draw_footer(
                stdscr,
                height - 1,
                1,
                "Navigate: [↑/↓]select  [Enter]open  [b]ack",
            )
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("b"), ord("B"), 27, ord("q"), ord("Q")):
                return
            if key in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(items)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(items)
            elif key in (curses.KEY_ENTER, 10, 13):
                if selected == 0:
                    self.comparison_paths_screen(stdscr)
                elif selected == 1:
                    self.pattern_manager_screen(stdscr)
                elif selected == 2:
                    self.display_filters_screen(stdscr)
                elif selected == 3:
                    self.edit_project_config(stdscr)
                elif selected == 4:
                    self.workbench.scan()
                    self.status = "Rescanned DEV and TEST."
                else:
                    return

    def edit_project_config(self, stdscr: Any) -> None:
        if self.workbench.settings.dry_run:
            self.status = "Dry-run mode: project-config editing is disabled."
            return
        path = self.workbench.settings.config_file
        if not path.exists():
            if not self.confirm(stdscr, f"Create project configuration {path}?"):
                return
            try:
                init_project_config(path)
            except WorkbenchError as exc:
                self.status = str(exc)
                return
        command = parse_editor_command(self.workbench.settings.edit_command)
        if not command:
            self.status = "No edit command configured."
            return
        command.append(str(path))
        curses.def_prog_mode()
        curses.endwin()
        try:
            code = subprocess.run(command, check=False).returncode
            self.workbench.reload_config()
            self.workbench.recalculate_completion_all(reopen_manual=True)
            self.workbench.scan()
            self.status = (
                f"Project config editor exited with status {code}; "
                f"loaded {len(self.workbench.patterns)} saved pattern(s)."
            )
        except (OSError, WorkbenchError) as exc:
            self.status = f"Could not edit/reload project config: {exc}"
        finally:
            curses.reset_prog_mode()
            stdscr.refresh()

    def help_screen(self, stdscr: Any) -> None:
        lines = [
            "CONFIG REVIEW WORKBENCH HELP",
            "",
            "The tool compares YAML as text. It does not infer Kubernetes meaning, moves,",
            "equivalence, safety, priority, or whether a change should be promoted.",
            "",
            "Focused Diff",
            "  Collapses qualifying project noise by default; saved choices override it.",
            "  Press f for Display Filters: whitespace, YAML order, and focused contrast.",
            "  YAML order filtering hides safe scalar mapping moves and unique name-keyed list moves.",
            "  Changed named items, templates, invalid YAML, and ambiguous moves remain visible.",
            "  Collapsed blocks remain visible as one filtered marker line.",
            "  Mute non-focused diff content softens surrounding/expanded filtered content only.",
            "  Press h to expand/collapse hidden blocks.",
            "  TEST/current line numbers are red; DEV/incoming line numbers are green.",
            "",
            "Full Diff",
            "  Always shows the original TEST and DEV text with no pattern or display filtering.",
            "",
            "Pattern Manager",
            "  Groups project-wide suggestions into Environment identity, Application domains,",
            "  Endpoints, Users/references, Storage/data, and Other repeated values.",
            "  Space toggles one pattern or every pattern in the selected category.",
            "  Broad suggestions require more examples; none are hidden without your approval.",
            "  Preview shows affected files, overlaps, TEST/DEV regexes, and nearby context.",
            "  ALWAYS REVIEWED keeps version/image/revision, replica/resource/security, and",
            "  added/removed/structural changes visible even when another regex would match.",
            "",
            "Main screen and Configure",
            "  The footer condenses automatically on narrow terminals; no command is lost.",
            "  Press c to open Configure for paths, patterns, display filters, config editing, and rescan.",
            "  p, f, s, and x remain direct shortcuts for experienced users.",
            "  Comparison Paths can change the project root or set exact DEV/TEST directories.",
            "  Switching paths saves progress, updates .config-review.yaml, and rescans.",
            "  Previously enabled patterns are disabled so the new comparison is never hidden automatically.",
            "",
            "Change navigation",
            "  j/k moves through active changes only in every diff view.",
            "  [ moves to the previous file and ] moves to the next file.",
            "  Enter opens a compact current-change panel with real TEST/DEV line numbers.",
            "  Arrow keys scroll the current screen; they do not change the selected diff.",
            "",
            "File list and states",
            "  Files are grouped generically by their first two parent directories.",
            "  Yellow means active diffs; green COMPLETE means every visible diff was handled.",
            "  Cyan DEV ONLY is an incoming file; red TEST ONLY is absent from incoming DEV.",
            "  Gray FILTERED ONLY means only approved hidden differences remain.",
            "  Magenta DONE MANUALLY means the file was marked done with active diffs remaining.",
            "  UNCOMMITTED: TEST already had Git changes when this run opened.",
            "  EDITED: TEST content changed during this run.",
            "  SYMLINK: the TEST path is viewable, but every TEST write action is blocked.",
            "",
            "Workflows",
            "  Accept DEV applies the exact selected incoming block immediately, with no editor.",
            "  Pull DEV + edit applies that exact block first, then opens TEST for adaptation.",
            "  Both actions refuse safely when the current TEST hunk cannot be revalidated.",
            "  A refused apply leaves TEST untouched and the change active; refresh or use vimdiff.",
            "  Edit TEST and vimdiff open near the selected change when the editor supports it.",
            "  Keep TEST moves the selected block out of the active queue without editing TEST.",
            "  Any selected block changed through pull/edit/vimdiff also moves to session history.",
            "  Focused Diff collapses handled blocks in magenta; Full Diff still shows real text.",
            "  Resolved-away handled blocks remain in SESSION HISTORY for the current run.",
            "  Files become COMPLETE after every visible change is handled; filter-only files stay FILTERED ONLY.",
            "  Undo Session Changes restores TEST to this run's starting state and clears review progress.",
            "  Project-wide pattern settings are not changed by Undo Session Changes.",
            "  Undo is memory-only, preserves pre-existing uncommitted work, and is unavailable after exit.",
            "  Exact undo bytes are captured lazily before the first write and verified against startup SHA-256.",
            "  Review status is saved automatically when the workbench exits.",
            "  The saved review records branch, commit, timestamp, paths, and file fingerprints.",
            "  On launch, answer Yes to load it or No to delete it and start fresh.",
            "  File actions contains only the whole-file DEV→TEST copy/delete operation.",
            "",
            "Press any key to return.",
        ]
        offset = 0
        while True:
            stdscr.erase()
            height, _ = stdscr.getmaxyx()
            for row, line in enumerate(lines[offset : offset + height - 1]):
                self._add(stdscr, row, 1, line, curses.A_BOLD if row == 0 else 0)
            self._draw_footer(
                stdscr, height - 1, 1, "Navigate: [↑/↓]scroll · any other key returns"
            )
            stdscr.refresh()
            key = stdscr.getch()
            if key == curses.KEY_UP:
                offset = max(0, offset - 1)
            elif key == curses.KEY_DOWN:
                offset = min(max(0, len(lines) - height + 1), offset + 1)
            else:
                return
