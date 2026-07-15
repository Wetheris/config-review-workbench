from argparse import Namespace
from pathlib import Path

import config_review.cli as cli
from config_review.cli import (
    discover_dev_test_pairs,
    interactive_first_run_paths,
    resolve_project_paths,
)
from config_review.core import (
    PatternRule,
    load_project_path_settings,
    resolve_configured_project_paths,
    save_project_config,
    save_project_paths,
)


def test_discovers_nested_sibling_dev_test_pair(tmp_path: Path):
    source = tmp_path / "application" / "dev"
    target = tmp_path / "application" / "test"
    source.mkdir(parents=True)
    target.mkdir()
    ignored_dev = tmp_path / "node_modules" / "dev"
    ignored_test = tmp_path / "node_modules" / "test"
    ignored_dev.mkdir(parents=True)
    ignored_test.mkdir()

    assert discover_dev_test_pairs(tmp_path) == [(source.resolve(), target.resolve())]


def test_paths_are_saved_as_one_project_and_survive_config_writes(tmp_path: Path):
    config = tmp_path / ".config-review.yaml"
    source = tmp_path / "configs" / "dev"
    target = tmp_path / "configs" / "test"
    source.mkdir(parents=True)
    target.mkdir()

    save_project_paths(config, source, target)
    assert load_project_path_settings(config) == ("configs", "dev", "test")
    assert resolve_configured_project_paths(config, *load_project_path_settings(config)) == (
        source.resolve(),
        target.resolve(),
    )

    save_project_config(
        config,
        patterns=[
            PatternRule(
                id="test",
                name="Example",
                test_regex="test",
                dev_regex="dev",
                files=(),
            )
        ],
        excluded_dirs={".git"},
        hide_whitespace=True,
        hide_mapping_order=False,
        mute_non_focused=False,
    )
    assert load_project_path_settings(config) == ("configs", "dev", "test")


def test_explicit_paths_are_persisted_as_project(tmp_path: Path, monkeypatch):
    source = tmp_path / "dev"
    target = tmp_path / "test"
    source.mkdir()
    target.mkdir()
    config = tmp_path / ".config-review.yaml"
    monkeypatch.chdir(tmp_path)
    args = Namespace(source=Path("dev"), target=Path("test"))

    resolved_source, resolved_target, saved = resolve_project_paths(args, config, tmp_path)

    assert saved
    assert resolved_source == source.resolve()
    assert resolved_target == target.resolve()
    assert load_project_path_settings(config) == (".", "dev", "test")


def test_single_discovered_pair_requires_confirmation(tmp_path: Path, monkeypatch):
    source = tmp_path / "application" / "dev"
    target = tmp_path / "application" / "test"
    source.mkdir(parents=True)
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "discover_nearby_dev_test_pairs", lambda _base: [(source, target)])
    answers = iter(["yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert interactive_first_run_paths(tmp_path) == (source.resolve(), target.resolve())


def test_manual_setup_asks_for_only_project_directory(tmp_path: Path, monkeypatch):
    project = tmp_path / "configuration-project"
    source = project / "dev"
    target = project / "test"
    source.mkdir(parents=True)
    target.mkdir()
    tool_root = tmp_path / "tool"
    tool_root.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "discover_nearby_dev_test_pairs", lambda _base: [])
    monkeypatch.setattr(cli, "_directory_input", lambda _prompt="": "configuration-project")
    answers = iter(["yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert interactive_first_run_paths(tool_root) == (source.resolve(), target.resolve())


def test_pasting_dev_directory_uses_parent_project(tmp_path: Path, monkeypatch, capsys):
    project = tmp_path / "configuration-project"
    source = project / "dev"
    target = project / "test"
    source.mkdir(parents=True)
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "discover_nearby_dev_test_pairs", lambda _base: [])
    monkeypatch.setattr(cli, "_directory_input", lambda _prompt="": "configuration-project/dev")
    answers = iter(["yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert interactive_first_run_paths(tmp_path) == (source.resolve(), target.resolve())
    assert "Using parent project directory" in capsys.readouterr().out


def test_workbench_can_switch_and_persist_comparison_paths(tmp_path: Path):
    from config_review.core import AppSettings
    from config_review.workbench import Workbench

    first = tmp_path / "first"
    first_source = first / "dev"
    first_target = first / "test"
    second = tmp_path / "second"
    second_source = second / "dev"
    second_target = second / "test"
    for directory in (first_source, first_target, second_source, second_target):
        (directory / "config").mkdir(parents=True)

    (first_source / "config" / "app.yaml").write_text("value: dev-one\n")
    (first_target / "config" / "app.yaml").write_text("value: test-one\n")
    (second_source / "config" / "app.yaml").write_text("value: dev-two\n")
    (second_target / "config" / "app.yaml").write_text("value: test-two\n")

    config = tmp_path / ".config-review.yaml"
    save_project_paths(config, first_source, first_target)
    save_project_config(
        config,
        patterns=[
            PatternRule(
                id="old-project",
                name="Old project environment",
                test_regex="test-one",
                dev_regex="dev-one",
                files=(),
                enabled=True,
            )
        ],
        excluded_dirs={".git"},
        hide_whitespace=True,
        hide_mapping_order=False,
        mute_non_focused=False,
    )
    workbench = Workbench(
        AppSettings(
            source=first_source,
            target=first_target,
            config_file=config,
            context=4,
            include_secrets=False,
            edit_command="",
            vimdiff_command="",
            dry_run=False,
        )
    )

    disabled_patterns = workbench.reconfigure_paths(second_source, second_target)

    assert disabled_patterns == 1
    assert all(not rule.enabled for rule in workbench.patterns)
    assert workbench.settings.source == second_source.resolve()
    assert workbench.settings.target == second_target.resolve()
    assert load_project_path_settings(config) == ("second", "dev", "test")
    assert [record.relative_path for record in workbench.records] == ["config/app.yaml"]
    assert workbench.records[0].dev_text == "value: dev-two\n"
    assert workbench.records[0].test_text == "value: test-two\n"
