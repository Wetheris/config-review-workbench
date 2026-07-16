from __future__ import annotations

from typing import Any

import config_review.tui as tui_module
from config_review.core import DisplayLine
from config_review.rendering import apply_intraline_emphasis
from config_review.tui import Tui


def test_intraline_emphasis_marks_only_changed_value_tokens() -> None:
    lines = [
        DisplayLine('value: "iesp-test-east"', "remove", test_line=1),
        DisplayLine('value: "iesp-dev-east"', "add", dev_line=1),
    ]

    apply_intraline_emphasis(lines)

    assert [lines[0].text[start:end] for start, end in lines[0].emphasis_ranges] == ["test"]
    assert [lines[1].text[start:end] for start, end in lines[1].emphasis_ranges] == ["dev"]


def test_terminal_draw_uses_bold_highlight_for_exact_change(monkeypatch: Any) -> None:
    calls: list[tuple[str, int]] = []
    tui = Tui.__new__(Tui)
    tui._kind_attr = lambda _kind: 0  # type: ignore[method-assign]
    tui._muted_kind_attr = lambda _kind: 0  # type: ignore[method-assign]
    tui._test_red_attr = lambda **_kwargs: 0  # type: ignore[method-assign]
    tui._muted_green_attr = lambda: 0  # type: ignore[method-assign]
    tui._muted_text_attr = lambda: 0  # type: ignore[method-assign]
    tui._muted_cyan_attr = lambda: 0  # type: ignore[method-assign]
    tui._color_pair = lambda _number: 0  # type: ignore[method-assign]
    tui._add = lambda _screen, _y, _x, text, attr=0: calls.append((text, attr))  # type: ignore[method-assign]

    monkeypatch.setattr(tui_module.curses, "A_BOLD", 0x10)
    monkeypatch.setattr(tui_module.curses, "A_REVERSE", 0x20)
    line = DisplayLine(
        'value: "iesp-test-east"',
        "remove",
        test_line=1,
        emphasis_ranges=((13, 17),),
    )

    tui._draw_display_line(object(), 0, 0, line, 4, 0)

    emphasized = [(text, attr) for text, attr in calls if text == "test"]
    assert emphasized
    assert emphasized[0][1] & tui_module.curses.A_BOLD
    assert emphasized[0][1] & tui_module.curses.A_REVERSE
