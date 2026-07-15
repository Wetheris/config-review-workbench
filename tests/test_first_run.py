from argparse import Namespace
from pathlib import Path

from config_review.cli import (
    discover_dev_test_pairs,
    interactive_first_run_paths,
    resolve_project_paths,
)
from config_review.core import (
    PatternRule,
    load_project_paths,
    resolve_configured_path,
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


def test_paths_are_saved_relative_and_survive_project_config_writes(tmp_path: Path):
    config = tmp_path / ".config-review.yaml"
    source = tmp_path / "configs" / "dev"
    target = tmp_path / "configs" / "test"
    source.mkdir(parents=True)
    target.mkdir()

    save_project_paths(config, source, target)
    assert load_project_paths(config) == ("configs/dev", "configs/test")
    assert resolve_configured_path(config, "configs/dev") == source.resolve()

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
    assert load_project_paths(config) == ("configs/dev", "configs/test")


def test_explicit_first_run_paths_are_persisted(tmp_path: Path, monkeypatch):
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
    assert load_project_paths(config) == ("dev", "test")


def test_single_discovered_pair_requires_confirmation(tmp_path: Path, monkeypatch):
    source = tmp_path / "application" / "dev"
    target = tmp_path / "application" / "test"
    source.mkdir(parents=True)
    target.mkdir()
    answers = iter(["yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert interactive_first_run_paths(tmp_path) == (source.resolve(), target.resolve())


def test_manual_paths_are_requested_when_discovery_finds_nothing(tmp_path: Path, monkeypatch):
    source = tmp_path / "incoming"
    target = tmp_path / "current"
    source.mkdir()
    target.mkdir()
    answers = iter(["incoming", "current"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert interactive_first_run_paths(tmp_path) == (source.resolve(), target.resolve())
