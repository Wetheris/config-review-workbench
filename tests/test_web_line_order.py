from __future__ import annotations

from pathlib import Path
from typing import Any

from config_review.core import AppSettings
from config_review.web_view import _build_web_diff_snapshot, build_web_diff_snapshot
from config_review.workbench import Workbench


def _settings(root: Path) -> AppSettings:
    return AppSettings(
        source=root / "dev",
        target=root / "test",
        config_file=root / ".config-review.yaml",
        context=3,
        include_secrets=False,
        edit_command="",
        vimdiff_command="",
        dry_run=False,
    )


def _named_environment(
    names: list[str],
    *,
    moved_name: str,
    moved_value: str,
) -> str:
    lines = ["env:"]
    for name in names:
        value = moved_value if name == moved_name else f"value-{name.lower()}"
        lines.extend(
            [
                f"  - name: {name}",
                f'    value: "{value}"',
            ]
        )
    return "\n".join(lines) + "\n"


def _line_numbers(presentation: dict[str, Any], side: str) -> list[int]:
    key = "testLine" if side == "TEST" else "devLine"
    return [int(line[key]) for line in presentation["lines"] if line.get(key) is not None]


def _assert_strictly_increasing(numbers: list[int], side: str) -> None:
    backwards = [
        (previous, current)
        for previous, current in zip(numbers, numbers[1:])
        if current <= previous
    ]
    assert not backwards, (
        f"{side} line numbers moved backward or repeated: {backwards[:5]}. "
        f"Rendered sequence: {numbers}"
    )


def _fully_expanded_line_numbers(
    presentation: dict[str, Any],
    context_lookup: dict[str, Any],
    side: str,
) -> list[int]:
    numbers = _line_numbers(presentation, side)
    start_name = "test_start" if side == "TEST" else "dev_start"
    referenced_gap_ids: set[str] = set()
    for change in [
        *presentation.get("changes", []),
        *presentation.get("hiddenChanges", []),
    ]:
        for name in ("beforeGap", "afterGap"):
            gap = change.get(name)
            if gap:
                referenced_gap_ids.add(str(gap["id"]))

    for gap_id in referenced_gap_ids:
        gap = context_lookup[gap_id]
        start = int(getattr(gap, start_name))
        numbers.extend(range(start + 1, start + len(gap.lines) + 1))
    return numbers


def test_raw_web_snapshot_line_numbers_remain_monotonic_for_moved_named_item(
    tmp_path: Path,
) -> None:
    """Raw Diff is the literal baseline and must never reorder either file."""
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()

    ordinary = [f"SETTING_{index:03d}" for index in range(1, 81)]
    moved = "SPRING_PROFILES_ACTIVE"

    test_order = [*ordinary[:65], moved, *ordinary[65:]]
    dev_order = [*ordinary[:5], moved, *ordinary[5:]]

    (target / "values.yaml").write_text(
        _named_environment(
            test_order,
            moved_name=moved,
            moved_value="prod",
        ),
        encoding="utf-8",
    )
    (source / "values.yaml").write_text(
        _named_environment(
            dev_order,
            moved_name=moved,
            moved_value="prod,seed",
        ),
        encoding="utf-8",
    )

    snapshot = build_web_diff_snapshot(Workbench(_settings(root)))
    raw = snapshot["files"][0]["raw"]

    _assert_strictly_increasing(_line_numbers(raw, "TEST"), "TEST")
    _assert_strictly_increasing(_line_numbers(raw, "DEV"), "DEV")


def test_focused_expanded_web_snapshot_never_moves_line_numbers_backward(
    tmp_path: Path,
) -> None:
    """Expanded Focused Diff must preserve each file's physical line order."""
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()

    ordinary = [f"SETTING_{index:03d}" for index in range(1, 81)]
    moved = "SPRING_PROFILES_ACTIVE"

    # The same uniquely named item is moved a long distance and modified.
    # This exercises keyed-list reconciliation plus expanded web context.
    test_order = [*ordinary[:65], moved, *ordinary[65:]]
    dev_order = [*ordinary[:5], moved, *ordinary[5:]]

    (target / "values.yaml").write_text(
        _named_environment(
            test_order,
            moved_name=moved,
            moved_value="prod",
        ),
        encoding="utf-8",
    )
    (source / "values.yaml").write_text(
        _named_environment(
            dev_order,
            moved_name=moved,
            moved_value="prod,seed",
        ),
        encoding="utf-8",
    )

    snapshot, _git_lookup, context_lookup = _build_web_diff_snapshot(Workbench(_settings(root)))
    focused_expanded = snapshot["files"][0]["focusedExpanded"]

    assert focused_expanded["physicalOrderFallback"] is True
    assert focused_expanded["hiddenChanges"] == []

    test_numbers = _line_numbers(focused_expanded, "TEST")
    dev_numbers = _line_numbers(focused_expanded, "DEV")

    _assert_strictly_increasing(test_numbers, "TEST")
    _assert_strictly_increasing(dev_numbers, "DEV")

    assert len(test_numbers) == len(set(test_numbers)), (
        f"TEST line numbers were rendered more than once: {test_numbers}"
    )
    assert len(dev_numbers) == len(set(dev_numbers)), (
        f"DEV line numbers were rendered more than once: {dev_numbers}"
    )

    fully_expanded_test = _fully_expanded_line_numbers(focused_expanded, context_lookup, "TEST")
    fully_expanded_dev = _fully_expanded_line_numbers(focused_expanded, context_lookup, "DEV")
    expected_test = list(range(1, len((target / "values.yaml").read_text().splitlines()) + 1))
    expected_dev = list(range(1, len((source / "values.yaml").read_text().splitlines()) + 1))

    assert sorted(fully_expanded_test) == expected_test
    assert sorted(fully_expanded_dev) == expected_dev
    assert len(fully_expanded_test) == len(set(fully_expanded_test))
    assert len(fully_expanded_dev) == len(set(fully_expanded_dev))
