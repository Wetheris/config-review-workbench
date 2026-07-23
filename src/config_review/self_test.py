"""Config Review Workbench Self Test module.

Part of the modular Config Review Workbench source distribution. Build the portable
``dist/config-review.pyz`` executable with ``python build.py``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

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
    DiffPresentation,
    DisplayLine,
    WorkbenchError,
    atomic_write_text,
    compute_filter_result,
    git_repository_commit_url,
    git_repository_merge_request_url,
    maximum_horizontal_offset,
    parse_editor_command,
    selected_diff_body_range,
)
from .rendering import (
    _empty_filter_result,
)
from .workbench import (
    Workbench,
)
from .plain import (
    format_display_line,
)
from .web_view import _PrivacyRedactor, _build_web_diff_snapshot, _render_page


def run_regression_tests() -> int:
    """Run a small, dependency-free hardening suite against real temporary files."""
    passed: list[str] = []

    def check(name: str, operation: Any) -> None:
        operation()
        passed.append(name)
        print(f"PASS  {name}")

    original_cache = os.environ.get("XDG_CACHE_HOME")
    original_home = os.environ.get("HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="config-review-tests-") as temp_name:
            root = Path(temp_name)
            cache = root / "cache"
            home = root / "home"
            home.mkdir()
            os.environ["XDG_CACHE_HOME"] = str(cache)
            os.environ["HOME"] = str(home)

            def test_editor_tilde_expansion() -> None:
                command = parse_editor_command("~/bin/review-editor --wait")
                assert command == [str(home / "bin" / "review-editor"), "--wait"]

            check(
                "editor executable expands ~ without invoking a shell", test_editor_tilde_expansion
            )

            def test_horizontal_bounds() -> None:
                long_lines = [DisplayLine("x" * 120, "add", dev_line=1)]
                maximum = maximum_horizontal_offset(long_lines, 4, 40, x=1)
                assert maximum > 0
                assert min(maximum, maximum + 4) == maximum
                assert maximum_horizontal_offset([DisplayLine("short")], 4, 80, x=1) == 0

            check("horizontal scrolling is bounded by rendered content", test_horizontal_bounds)

            def test_web_privacy_redaction() -> None:
                redactor = _PrivacyRedactor()
                private = redactor.redact_lines(
                    [
                        'password: "release-secret-123"',
                        "owner: Example Person",
                        "url: https://internal.example.gov/api",
                        "replicas: 3",
                    ]
                )
                assert private[0] == 'password: "[SECRET-1]"'
                assert private[1] == "owner: [PERSON-1]"
                assert private[2] == "url: [ENDPOINT-1]"
                assert private[3] == "replicas: 3"

            check(
                "web privacy mode masks sensitive values but preserves ordinary structure",
                test_web_privacy_redaction,
            )

            def test_web_commit_links_prefer_merge_request_context() -> None:
                repository = "https://gitlab.example.gov/group/project"
                assert git_repository_commit_url(repository, "abc123") == (
                    "https://gitlab.example.gov/group/project/-/commit/abc123"
                )
                assert (
                    git_repository_merge_request_url(
                        repository,
                        "group/project!42",
                    )
                    == "https://gitlab.example.gov/group/project/-/merge_requests/42"
                )

            check(
                "web inline Git context can link to a related merge request",
                test_web_commit_links_prefer_merge_request_context,
            )

            def test_web_git_context_is_per_side_and_copy_is_always_available() -> None:
                page = _render_page({"files": []}).decode("utf-8")
                assert "lastChangedLineRow" in page
                assert "Last changed in ${sideLabel} · by " in page
                assert "Hide Git context" in page
                assert 'content: "Git context"' in page
                assert "$('copyDiff').hidden = false" in page
                assert "Copied the displayed diff with original values" in page

            check(
                "web Git context follows grouped source and target changes "
                "and copy is always available",
                test_web_git_context_is_per_side_and_copy_is_always_available,
            )

            def test_web_keyed_list_physical_timeline() -> None:
                web_root = root / "web-keyed-list"
                web_source = web_root / "dev"
                web_target = web_root / "test"
                web_source.mkdir(parents=True)
                web_target.mkdir()
                web_target.joinpath("values.yaml").write_text(
                    """imagePullSecrets:
  - name: example-pull-secret
deployment:
  env:
    - name: SPRING_APPLICATION_NAME
      value: "service"
    - name: SPRING_PROFILES_ACTIVE
      value: "prod"
    - name: SPRING_CONFIG_ADDITIONAL_LOCATION
      value: "file:/app/config/application-extra.yml"
""",
                    encoding="utf-8",
                )
                web_source.joinpath("values.yaml").write_text(
                    """imagePullSecrets:
  - name: example-pull-secret
deployment:
  env:
    - name: SPRING_PROFILES_ACTIVE
      value: "prod,seed"
    - name: SPRING_CONFIG_ADDITIONAL_LOCATION
      value: "file:/app/config/application-extra.yml"
    - name: SPRING_APPLICATION_NAME
      value: "service"
""",
                    encoding="utf-8",
                )
                web_settings = AppSettings(
                    source=web_source,
                    target=web_target,
                    config_file=web_root / ".config-review.yaml",
                    context=3,
                    include_secrets=False,
                    edit_command="vim",
                    vimdiff_command="vimdiff",
                    dry_run=False,
                )
                snapshot, _git_lookup, _context_lookup = _build_web_diff_snapshot(
                    Workbench(web_settings)
                )
                view = snapshot["files"][0]["focusedExpanded"]
                assert view["visibleChanges"] == 1
                assert len(view["changes"]) == 1
                assert view["changes"][0]["splitPhysical"] is True
                assert all(
                    line["kind"].startswith("filtered_")
                    for line in view["lines"]
                    if "SPRING_CONFIG_ADDITIONAL_LOCATION" in line["text"]
                )
                test_numbers = [line["testLine"] for line in view["lines"] if line["testLine"]]
                dev_numbers = [line["devLine"] for line in view["lines"] if line["devLine"]]
                assert test_numbers == sorted(set(test_numbers))
                assert dev_numbers == sorted(set(dev_numbers))

            check(
                "web keyed-list changes keep one logical review item on a physical timeline",
                test_web_keyed_list_physical_timeline,
            )

            def test_web_context_gaps_follow_physical_line_order() -> None:
                web_root = root / "web-context-order"
                web_source = web_root / "dev"
                web_target = web_root / "test"
                web_source.mkdir(parents=True)
                web_target.mkdir()
                web_target.joinpath("values.yaml").write_text(
                    """env:
  - name: A
    value: "a"
  - name: B
    value: "old"
  - name: C
    value: "c"
  - name: D
    value: "d"
""",
                    encoding="utf-8",
                )
                web_source.joinpath("values.yaml").write_text(
                    """env:
  - name: A
    value: "a"
  - name: B
    value: "new"
  - name: D
    value: "d"
  - name: C
    value: "c"
""",
                    encoding="utf-8",
                )
                web_settings = AppSettings(
                    source=web_source,
                    target=web_target,
                    config_file=web_root / ".config-review.yaml",
                    context=3,
                    include_secrets=False,
                    edit_command="vim",
                    vimdiff_command="vimdiff",
                    dry_run=False,
                )
                snapshot, _git_lookup, context_lookup = _build_web_diff_snapshot(
                    Workbench(web_settings)
                )
                view = snapshot["files"][0]["focused"]
                gaps_by_index = {int(gap["insertAt"]): gap for gap in view.get("contextGaps", [])}

                def expanded(side: str) -> list[int]:
                    key = "testLine" if side == "TEST" else "devLine"
                    start_name = "test_start" if side == "TEST" else "dev_start"
                    numbers: list[int] = []
                    for index, line in enumerate(view["lines"]):
                        gap = gaps_by_index.get(index)
                        if gap is not None:
                            stored = context_lookup[str(gap["id"])]
                            start = int(getattr(stored, start_name))
                            numbers.extend(range(start + 1, start + len(stored.lines) + 1))
                        if line.get(key) is not None:
                            numbers.append(int(line[key]))
                    trailing = gaps_by_index.get(len(view["lines"]))
                    if trailing is not None:
                        stored = context_lookup[str(trailing["id"])]
                        start = int(getattr(stored, start_name))
                        numbers.extend(range(start + 1, start + len(stored.lines) + 1))
                    return numbers

                assert expanded("TEST") == list(range(1, 10))
                assert expanded("DEV") == list(range(1, 10))

            check(
                "web context expansion stays at physical line boundaries",
                test_web_context_gaps_follow_physical_line_order,
            )

            def test_selected_diff_guide_range() -> None:
                source_lines = [DisplayLine("▶ ACTIVE CHANGE 1/1", "selector_selected")]
                source_lines.extend(
                    [
                        DisplayLine("old", "remove", test_line=1),
                        DisplayLine("new", "add", dev_line=1),
                        DisplayLine("context", "context", test_line=2, dev_line=2),
                    ]
                )
                block = ChangeBlock(
                    tag="replace",
                    old_start=0,
                    old_end=1,
                    new_start=0,
                    new_end=1,
                    old_lines=["old"],
                    new_lines=["new"],
                )
                presentation = DiffPresentation(
                    lines=source_lines,
                    filter_result=_empty_filter_result(),
                    change_line_indexes=[0],
                    change_line_ranges=[(0, 4)],
                    change_blocks=[block],
                    selected_change=0,
                )
                assert selected_diff_body_range(presentation) == (1, 3)
                plain_old = format_display_line(source_lines[1], 3, selected_guide=True)
                assert "│ " in plain_old

            check(
                "selected diff body receives an exact yellow guide range",
                test_selected_diff_guide_range,
            )

            def test_atomic_symlink_refusal() -> None:
                actual = root / "actual.yaml"
                link = root / "linked.yaml"
                actual.write_text("value: original\n", encoding="utf-8")
                link.symlink_to(actual)
                try:
                    atomic_write_text(link, "value: changed\n")
                except OSError as exc:
                    assert "symlink" in str(exc).lower()
                else:
                    raise AssertionError("atomic_write_text unexpectedly replaced a symlink")
                assert link.is_symlink()
                assert actual.read_text(encoding="utf-8") == "value: original\n"

            check("atomic TEST writes refuse direct symlink targets", test_atomic_symlink_refusal)

            source = root / "dev"
            target = root / "test"
            source.mkdir()
            target.mkdir()
            (source / "a.yaml").write_text("key: incoming-one\n", encoding="utf-8")
            (target / "a.yaml").write_text("key: current-one\n", encoding="utf-8")
            (source / "crlf.yaml").write_text("other: incoming-two\n", encoding="utf-8")
            (target / "crlf.yaml").write_bytes(b"other: current-two\r\n")
            settings = AppSettings(
                source=source,
                target=target,
                config_file=root / ".config-review.yaml",
                context=2,
                include_secrets=False,
                edit_command="vim",
                vimdiff_command="vimdiff",
                dry_run=False,
            )
            workbench = Workbench(settings)

            def test_lazy_snapshot_and_undo() -> None:
                record = workbench.records_by_path["a.yaml"]
                assert record.initial_test_bytes is None
                assert not record.undo_snapshot_captured
                blocks = workbench.active_change_blocks(record)
                assert len(blocks) == 1
                accepted, message = workbench.accept_dev_block(record, blocks[0])
                assert accepted, message
                assert record.undo_snapshot_captured
                assert record.initial_test_bytes == b"key: current-one\n"
                assert (target / "a.yaml").read_bytes() == b"key: incoming-one\n"
                changed, undo_message, confirmation = workbench.undo_session_changes(record)
                assert changed, undo_message
                assert not confirmation
                assert (target / "a.yaml").read_bytes() == b"key: current-one\n"

            check(
                "undo bytes are captured lazily and restore exact startup content",
                test_lazy_snapshot_and_undo,
            )

            def test_external_change_after_tool_action_is_not_overwritten() -> None:
                record = workbench.records_by_path["a.yaml"]
                (target / "a.yaml").write_text("key: external-after-undo\n", encoding="utf-8")
                copied, message = workbench.copy_dev_to_test(record)
                assert not copied
                assert "outside the tool" in message
                assert (target / "a.yaml").read_text(
                    encoding="utf-8"
                ) == "key: external-after-undo\n"

            check(
                "later write actions refuse external changes after a snapshot exists",
                test_external_change_after_tool_action_is_not_overwritten,
            )

            def test_startup_hash_verification() -> None:
                path = source / "external.yaml"
                test_path = target / "external.yaml"
                path.write_text("key: dev\n", encoding="utf-8")
                test_path.write_text("key: test\n", encoding="utf-8")
                workbench.scan()
                record = workbench.records_by_path["external.yaml"]
                assert not record.undo_snapshot_captured
                test_path.write_text("key: external\n", encoding="utf-8")
                block = workbench.active_change_blocks(record)[0]
                accepted, message = workbench.accept_dev_block(record, block)
                assert not accepted
                assert "before a safe undo snapshot" in message
                assert test_path.read_text(encoding="utf-8") == "key: external\n"

            check(
                "first mutation refuses when TEST changed after startup",
                test_startup_hash_verification,
            )

            def test_crlf_replacement() -> None:
                record = workbench.records_by_path["crlf.yaml"]
                block = workbench.active_change_blocks(record)[0]
                accepted, message = workbench.accept_dev_block(record, block)
                assert accepted, message
                assert (target / "crlf.yaml").read_bytes() == b"other: incoming-two\r\n"

            check("accepted DEV lines use the TEST newline style", test_crlf_replacement)

            def test_workbench_symlink_badge_and_block() -> None:
                actual = root / "shared.yaml"
                actual.write_text("key: test\n", encoding="utf-8")
                (source / "linked.yaml").write_text("key: dev\n", encoding="utf-8")
                (target / "linked.yaml").symlink_to(actual)
                workbench.scan()
                record = workbench.records_by_path["linked.yaml"]
                assert "SYMLINK" in record.states
                block = workbench.active_change_blocks(record)[0]
                accepted, message = workbench.accept_dev_block(record, block)
                assert not accepted
                assert "symlink" in message.lower()
                assert (target / "linked.yaml").is_symlink()
                assert actual.read_text(encoding="utf-8") == "key: test\n"

            check(
                "symlinked TEST records are badged and all write actions are blocked",
                test_workbench_symlink_badge_and_block,
            )

            def test_duplicate_identity_ranges_do_not_overlap() -> None:
                old = (
                    "items:\n"
                    "  - name: duplicate\n"
                    "    value: one\n"
                    "  - name: duplicate\n"
                    "    value: two\n"
                )
                new = (
                    "items:\n"
                    "  - name: duplicate\n"
                    "    value: two\n"
                    "  - name: duplicate\n"
                    "    value: three\n"
                )
                blocks = compute_filter_result(old, new, [], "duplicates.yaml").blocks
                previous_old_end = 0
                previous_new_end = 0
                for block in blocks:
                    assert block.old_start >= previous_old_end
                    assert block.new_start >= previous_new_end
                    previous_old_end = block.old_end
                    previous_new_end = block.new_end

            check(
                "duplicate YAML-like scalar identities never create overlapping blocks",
                test_duplicate_identity_ranges_do_not_overlap,
            )

            def test_named_list_move_is_one_logical_change() -> None:
                old = (
                    "env:\n"
                    "  - name: SPRING_APPLICATION_NAME\n"
                    "    value: app\n"
                    "  - name: SPRING_PROFILES_ACTIVE\n"
                    "    value: prod\n"
                    "  - name: SPRING_CONFIG_ADDITIONAL_LOCATION\n"
                    "    value: file:/app/config/application-extra.yml\n"
                )
                new = (
                    "env:\n"
                    "  - name: SPRING_PROFILES_ACTIVE\n"
                    "    value: prod,seed\n"
                    "  - name: SPRING_CONFIG_ADDITIONAL_LOCATION\n"
                    "    value: file:/app/config/application-extra.yml\n"
                    "  - name: SPRING_APPLICATION_NAME\n"
                    "    value: app\n"
                )
                result = compute_filter_result(
                    old,
                    new,
                    [],
                    "named-list.yaml",
                    hide_mapping_order=True,
                )
                assert len(result.visible) == 1
                block = result.visible[0]
                assert block.old_lines == [
                    "  - name: SPRING_PROFILES_ACTIVE",
                    "    value: prod",
                ]
                assert block.new_lines == [
                    "  - name: SPRING_PROFILES_ACTIVE",
                    "    value: prod,seed",
                ]

            check(
                "moved unique name-keyed YAML items become one logical value change",
                test_named_list_move_is_one_logical_change,
            )

            def test_visible_diff_report_scope() -> None:
                (source / "report.yaml").write_text(
                    "env:\n  - name: LOGGING_LEVEL_ROOT\n    value: DEBUG\n",
                    encoding="utf-8",
                )
                (target / "report.yaml").write_text(
                    "env:\n  - name: LOGGING_LEVEL_ROOT\n    value: INFO\n",
                    encoding="utf-8",
                )
                workbench.scan()
                record = workbench.records_by_path["report.yaml"]
                report = workbench.generate_file_report(
                    record,
                    mode="focused",
                    include_context_labels=True,
                    include_git_context=False,
                )
                assert "1 visible change" in report
                assert "Environment variable · `LOGGING_LEVEL_ROOT`" in report
                assert "value: INFO" in report
                assert "value: DEBUG" in report

            check(
                "visible-diff report exports the current file's focused changes",
                test_visible_diff_report_scope,
            )

    except (AssertionError, OSError, WorkbenchError) as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        print(f"{len(passed)} targeted regression test(s) passed before failure.", file=sys.stderr)
        return 1
    finally:
        if original_cache is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = original_cache
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home

    print(f"All {len(passed)} targeted regression tests passed.")
    return 0
