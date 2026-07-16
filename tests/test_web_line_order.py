from __future__ import annotations

from pathlib import Path
from typing import Any

from config_review.core import AppSettings, compute_filter_result
from config_review.web_view import _build_web_diff_snapshot, build_web_diff_snapshot
from config_review.workbench import Workbench


ADJACENT_MOVED_TEST_YAML = """\
imagePullSecrets:
  - name: oex-eose-gitlab-grpdeploy-pull-secret
deployment:
  env:
    - name: SPRING_APPLICATION_NAME
      value: "dnc-cutover-service"
    - name: SPRING_PROFILES_ACTIVE
      value: "prod"
    - name: SPRING_CONFIG_ADDITIONAL_LOCATION
      value: "file:/app/config/application-extra.yml"
"""

ADJACENT_MOVED_DEV_YAML = """\
imagePullSecrets:
  - name: oex-eose-gitlab-grpdeploy-pull-secret
deployment:
  env:
    - name: SPRING_PROFILES_ACTIVE
      value: "prod,seed"
    - name: SPRING_CONFIG_ADDITIONAL_LOCATION
      value: "file:/app/config/application-extra.yml"
    - name: SPRING_APPLICATION_NAME
      value: "dnc-cutover-service"
"""


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
    for gap in presentation.get("contextGaps", []):
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


def test_engine_reconciles_adjacent_moved_named_items_as_one_value_change() -> None:
    """The semantic engine already understands the real change correctly."""
    result = compute_filter_result(
        ADJACENT_MOVED_TEST_YAML,
        ADJACENT_MOVED_DEV_YAML,
        [],
        "values.yaml",
        hide_mapping_order=True,
    )

    assert len(result.visible) == 1
    change = result.visible[0]
    assert change.old_lines == [
        "    - name: SPRING_PROFILES_ACTIVE",
        '      value: "prod"',
    ]
    assert change.new_lines == [
        "    - name: SPRING_PROFILES_ACTIVE",
        '      value: "prod,seed"',
    ]
    assert all(
        "SPRING_CONFIG_ADDITIONAL_LOCATION" not in line
        for line in [*change.old_lines, *change.new_lines]
    )


def test_web_view_preserves_one_logical_change_for_adjacent_moved_named_items(
    tmp_path: Path,
) -> None:
    """The browser should keep semantic change identity without corrupting line order."""
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (target / "values.yaml").write_text(ADJACENT_MOVED_TEST_YAML, encoding="utf-8")
    (source / "values.yaml").write_text(ADJACENT_MOVED_DEV_YAML, encoding="utf-8")

    snapshot, _git_lookup, context_lookup = _build_web_diff_snapshot(Workbench(_settings(root)))
    focused_expanded = snapshot["files"][0]["focusedExpanded"]

    # The physical file timeline must stay trustworthy.
    _assert_strictly_increasing(_line_numbers(focused_expanded, "TEST"), "TEST")
    _assert_strictly_increasing(_line_numbers(focused_expanded, "DEV"), "DEV")

    fully_expanded_test = _fully_expanded_line_numbers(focused_expanded, context_lookup, "TEST")
    fully_expanded_dev = _fully_expanded_line_numbers(focused_expanded, context_lookup, "DEV")
    assert sorted(fully_expanded_test) == list(
        range(1, len(ADJACENT_MOVED_TEST_YAML.splitlines()) + 1)
    )
    assert sorted(fully_expanded_dev) == list(
        range(1, len(ADJACENT_MOVED_DEV_YAML.splitlines()) + 1)
    )

    # The active-review model should still expose only the real value change.
    assert focused_expanded["visibleChanges"] == 1
    assert len(focused_expanded["changes"]) == 1
    change = focused_expanded["changes"][0]
    assert change["oldLines"] == [
        "    - name: SPRING_PROFILES_ACTIVE",
        '      value: "prod"',
    ]
    assert change["newLines"] == [
        "    - name: SPRING_PROFILES_ACTIVE",
        '      value: "prod,seed"',
    ]
    assert all(
        "SPRING_CONFIG_ADDITIONAL_LOCATION" not in line
        for line in [*change["oldLines"], *change["newLines"]]
    )

    selectors = [
        line["text"]
        for line in focused_expanded["lines"]
        if line["kind"] in {"selector", "selector_selected"}
    ]
    assert selectors == ["▶ ACTIVE CHANGE 1/1 · TEST 7-8 · DEV 5-6"]

    additional_location_rows = [
        line
        for line in focused_expanded["lines"]
        if "SPRING_CONFIG_ADDITIONAL_LOCATION" in line["text"]
        or "file:/app/config/application-extra.yml" in line["text"]
    ]
    assert additional_location_rows
    assert all(line["kind"].startswith("filtered_") for line in additional_location_rows)

    profile_rows = [
        line
        for line in focused_expanded["lines"]
        if "SPRING_PROFILES_ACTIVE" in line["text"]
        or 'value: "prod"' in line["text"]
        or 'value: "prod,seed"' in line["text"]
    ]
    assert {line["kind"] for line in profile_rows} == {"remove", "add"}
    assert any(line["kind"] == "selector_continuation" for line in focused_expanded["lines"])
