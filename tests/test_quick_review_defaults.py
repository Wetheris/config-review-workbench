from __future__ import annotations

from pathlib import Path

from config_review.core import (
    CATEGORY_ALWAYS_REVIEWED,
    FileRecord,
    PatternRule,
    discover_project_pattern_candidates,
    load_project_config,
    save_project_config,
    save_project_paths,
)
from config_review.tui import build_pattern_manager_rows


def _record(tmp_path: Path, name: str, test_text: str, dev_text: str) -> FileRecord:
    return FileRecord(
        relative_path=name,
        dev_path=tmp_path / "dev" / name,
        test_path=tmp_path / "test" / name,
        initial_test_exists=True,
        initial_test_hash=None,
        dev_exists=True,
        test_exists=True,
        dev_text=dev_text,
        test_text=test_text,
        equal=False,
    )


def test_new_project_defaults_hide_whitespace_and_safe_yaml_order(tmp_path: Path):
    source = tmp_path / "project" / "dev"
    target = tmp_path / "project" / "test"
    source.mkdir(parents=True)
    target.mkdir()
    config = tmp_path / ".config-review.yaml"

    save_project_paths(config, source, target)
    patterns, _excluded, hide_whitespace, hide_mapping_order, muted, diagnostics = (
        load_project_config(config)
    )

    assert patterns == []
    assert hide_whitespace is True
    assert hide_mapping_order is True
    assert muted is False
    assert diagnostics == []


def test_existing_explicit_display_choice_is_not_overridden(tmp_path: Path):
    config = tmp_path / ".config-review.yaml"
    config.write_text(
        """\
version: 8
custom:
  keep_me: true
display:
  show_whitespace: true
  hide_mapping_order: false
  mute_non_focused: true
patterns: []
""",
        encoding="utf-8",
    )

    _patterns, excluded, hide_whitespace, hide_mapping_order, muted, _diagnostics = (
        load_project_config(config)
    )
    assert hide_whitespace is False
    assert hide_mapping_order is False
    assert muted is True

    save_project_config(
        config,
        patterns=[],
        excluded_dirs=excluded,
        hide_whitespace=hide_whitespace,
        hide_mapping_order=hide_mapping_order,
        mute_non_focused=muted,
    )
    saved = config.read_text(encoding="utf-8")
    assert "keep_me: true" in saved
    assert "hide_mapping_order: false" in saved


def test_new_pattern_suggestions_start_hidden_but_saved_visible_choice_wins(tmp_path: Path):
    records = [
        _record(tmp_path, "one.yaml", "environment: test\n", "environment: dev\n"),
        _record(tmp_path, "two.yaml", "environment: test\n", "environment: dev\n"),
    ]

    generated = discover_project_pattern_candidates(
        records, [], source_name="dev", target_name="test"
    )
    assert generated
    assert all(candidate.rule.enabled for candidate in generated)

    first = generated[0].rule
    saved_visible = PatternRule(
        id=first.id,
        name=first.name,
        test_regex=first.test_regex,
        dev_regex=first.dev_regex,
        files=(),
        category=first.category,
        enabled=False,
        kind=first.kind,
        source="test",
    )
    overridden = discover_project_pattern_candidates(
        records, [saved_visible], source_name="dev", target_name="test"
    )
    matching = next(candidate for candidate in overridden if candidate.rule.id == first.id)
    assert matching.persisted
    assert matching.rule.enabled is False


def test_pattern_manager_categories_start_collapsed(tmp_path: Path):
    records = [
        _record(tmp_path, "one.yaml", "environment: test\n", "environment: dev\n"),
        _record(tmp_path, "two.yaml", "environment: test\n", "environment: dev\n"),
    ]
    candidates = discover_project_pattern_candidates(
        records, [], source_name="dev", target_name="test"
    )

    collapsed = build_pattern_manager_rows(candidates, [])
    assert collapsed
    assert all(row.kind == "category" for row in collapsed)

    category = collapsed[0].category
    expanded = build_pattern_manager_rows(candidates, [], {category})
    assert any(row.kind == "pattern" and row.category == category for row in expanded)
    assert all(row.category != CATEGORY_ALWAYS_REVIEWED for row in expanded)


def test_workbench_quick_view_hides_generated_patterns_and_can_save_visible_override(
    tmp_path: Path,
):
    from config_review.core import AppSettings
    from config_review.workbench import Workbench

    source = tmp_path / "dev"
    target = tmp_path / "test"
    source.mkdir()
    target.mkdir()
    for name in ("one.yaml", "two.yaml"):
        (source / name).write_text("environment: dev\n", encoding="utf-8")
        (target / name).write_text("environment: test\n", encoding="utf-8")

    config = tmp_path / ".config-review.yaml"
    workbench = Workbench(
        AppSettings(
            source=source,
            target=target,
            config_file=config,
            context=3,
            include_secrets=False,
            edit_command="",
            vimdiff_command="",
            dry_run=False,
        )
    )

    assert workbench.enabled_patterns
    assert sum(workbench.review_counts(record).active for record in workbench.records) == 0
    assert sum(workbench.review_counts(record).pattern_hidden for record in workbench.records) > 0

    categories = {candidate.rule.category for candidate in workbench.pattern_candidates()}
    for category in categories:
        changed, _message = workbench.set_category_patterns(category, False)
        assert changed

    assert workbench.enabled_patterns == []
    assert sum(workbench.review_counts(record).active for record in workbench.records) > 0
    assert config.exists()
    assert "enabled: false" in config.read_text(encoding="utf-8")
