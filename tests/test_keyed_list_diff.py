from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from config_review.core import AppSettings, compute_filter_result, refined_opcodes
from config_review.workbench import Workbench


TEST_YAML = """\
env:
  - name: BEFORE
    value: x
  - name: SPRING_APPLICATION_NAME
    value: "dnc-cutover-service"
  - name: SPRING_PROFILES_ACTIVE
    value: "prod"
  - name: SPRING_CONFIG_ADDITIONAL_LOCATION
    value: "file:/app/config/application-extra.yml"
  - name: AFTER
    value: y
"""

DEV_YAML = """\
env:
  - name: BEFORE
    value: x
  - name: SPRING_PROFILES_ACTIVE
    value: "prod,seed"
  - name: SPRING_CONFIG_ADDITIONAL_LOCATION
    value: "file:/app/config/application-extra.yml"
  - name: SPRING_APPLICATION_NAME
    value: "dnc-cutover-service"
  - name: AFTER
    value: y
"""


def test_full_diff_remains_the_literal_text_alignment():
    result = compute_filter_result(TEST_YAML, DEV_YAML, [], "values.yaml")

    assert result.opcodes == refined_opcodes(TEST_YAML.splitlines(), DEV_YAML.splitlines())
    assert [block.tag for block in result.blocks] == ["insert", "delete"]
    assert result.hidden == []


def test_named_list_move_becomes_one_logical_value_change():
    result = compute_filter_result(
        TEST_YAML,
        DEV_YAML,
        [],
        "values.yaml",
        hide_mapping_order=True,
    )

    assert len(result.visible) == 1
    block = result.visible[0]
    assert block.tag == "replace"
    assert block.old_lines == [
        "  - name: SPRING_PROFILES_ACTIVE",
        '    value: "prod"',
    ]
    assert block.new_lines == [
        "  - name: SPRING_PROFILES_ACTIVE",
        '    value: "prod,seed"',
    ]
    assert all(
        "SPRING_CONFIG_ADDITIONAL_LOCATION" not in line
        for line in block.old_lines + block.new_lines
    )


def test_identical_named_item_move_is_hidden_as_order_only():
    test_text = """\
env:
  - name: A
    value: one
  - name: B
    value: two
  - name: C
    value: three
"""
    dev_text = """\
env:
  - name: B
    value: two
  - name: A
    value: one
  - name: C
    value: three
"""

    result = compute_filter_result(
        test_text,
        dev_text,
        [],
        "order-only.yaml",
        hide_mapping_order=True,
    )

    assert result.visible == []
    assert any("YAML keyed-list order" in block.hidden_by for block in result.hidden)


def test_accepting_logical_change_updates_value_without_reordering_or_duplication(tmp_path: Path):
    source = tmp_path / "dev"
    target = tmp_path / "test"
    source.mkdir()
    target.mkdir()
    (source / "values.yaml").write_text(DEV_YAML, encoding="utf-8")
    (target / "values.yaml").write_text(TEST_YAML, encoding="utf-8")

    workbench = Workbench(
        AppSettings(
            source=source,
            target=target,
            config_file=tmp_path / ".config-review.yaml",
            context=3,
            include_secrets=False,
            edit_command="",
            vimdiff_command="",
            dry_run=False,
        )
    )
    workbench.hide_mapping_order = True
    record = workbench.records_by_path["values.yaml"]
    blocks = workbench.active_change_blocks(record)

    assert len(blocks) == 1
    accepted, message = workbench.accept_dev_block(record, blocks[0])
    assert accepted, message

    updated = (target / "values.yaml").read_text(encoding="utf-8")
    assert 'value: "prod,seed"' in updated
    assert updated.count("SPRING_PROFILES_ACTIVE") == 1
    assert updated.count("SPRING_CONFIG_ADDITIONAL_LOCATION") == 1
    assert updated.index("SPRING_APPLICATION_NAME") < updated.index("SPRING_PROFILES_ACTIVE")


def test_duplicate_name_items_fall_back_to_literal_diff():
    test_text = """\
items:
  - name: duplicate
    value: one
  - name: duplicate
    value: two
"""
    dev_text = """\
items:
  - name: duplicate
    value: two
  - name: duplicate
    value: three
"""

    result = compute_filter_result(
        test_text,
        dev_text,
        [],
        "duplicates.yaml",
        hide_mapping_order=True,
    )

    assert result.visible
    assert not any(
        reason.startswith("YAML keyed-list order")
        for block in result.blocks
        for reason in block.hidden_by
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is unavailable")
def test_git_diff_and_raw_tool_both_expose_the_real_scalar_change(tmp_path: Path):
    test_file = tmp_path / "test.yaml"
    dev_file = tmp_path / "dev.yaml"
    test_file.write_text(TEST_YAML, encoding="utf-8")
    dev_file.write_text(DEV_YAML, encoding="utf-8")

    completed = subprocess.run(
        [
            "git",
            "diff",
            "--no-index",
            "--no-color",
            "--unified=0",
            "--",
            str(test_file),
            str(dev_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1

    # Git and difflib can choose different moved-line anchors, so their full
    # add/remove sets are not required to be identical. Both raw views must
    # still expose the actual value transition that prompted the review.
    git_removed = {
        line[1:]
        for line in completed.stdout.splitlines()
        if line.startswith("-") and not line.startswith("---")
    }
    git_added = {
        line[1:]
        for line in completed.stdout.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    }
    raw = compute_filter_result(TEST_YAML, DEV_YAML, [], "values.yaml")
    tool_removed = {line for block in raw.blocks for line in block.old_lines}
    tool_added = {line for block in raw.blocks for line in block.new_lines}

    assert '    value: "prod"' in git_removed
    assert '    value: "prod,seed"' in git_added
    assert '    value: "prod"' in tool_removed
    assert '    value: "prod,seed"' in tool_added
