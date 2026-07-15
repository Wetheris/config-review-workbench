from __future__ import annotations

import curses
import subprocess

import pytest
from pathlib import Path

from config_review.core import AppSettings, WorkbenchError, git_repository_status
from config_review.tui import Tui, detail_footer_lines
from config_review.workbench import Workbench


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


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


def test_detail_footer_is_responsive_and_uses_clear_navigation_labels():
    for width in (28, 42, 48, 70, 105, 140):
        lines = detail_footer_lines(width, mode="focused", expand_filtered=False)
        assert len(lines) >= 2
        assert all(len(line) <= width for line in lines)
        combined = " ".join(lines)
        assert "j/k" in combined
        assert "file" in combined.lower()
        assert "filters" in combined
        assert "actions" in combined


def test_visible_report_uses_focused_blocks_and_context_labels(tmp_path: Path):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "values.yaml").write_text(
        """\
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
    (target / "values.yaml").write_text(
        """\
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

    workbench = Workbench(_settings(root))
    record = workbench.records[0]
    report = workbench.generate_file_report(
        record,
        mode="focused",
        include_context_labels=True,
        include_git_context=False,
    )

    assert "# Config Review — `values.yaml`" in report
    assert "1 visible change" in report
    assert "## 1. Environment variable · `SPRING_PROFILES_ACTIVE`" in report
    assert "### Difference (TEST → DEV)" in report
    assert "| TEST | `test/values.yaml` |" in report
    assert "| DEV | `dev/values.yaml` |" in report
    assert str(root) not in report
    assert 'value: "prod"' in report
    assert 'value: "prod,seed"' in report
    assert "SPRING_CONFIG_ADDITIONAL_LOCATION" not in report

    saved = workbench.save_file_report(
        record,
        mode="focused",
        include_context_labels=True,
        include_git_context=False,
    )
    assert saved.parent.name == "reports"
    saved_text = saved.read_text(encoding="utf-8")
    assert "Environment variable · `SPRING_PROFILES_ACTIVE`" in saved_text
    assert "1 visible change" in saved_text


def test_report_includes_line_commit_context_when_available(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.com")
    (root / "dev").mkdir()
    (root / "test").mkdir()
    (root / "dev" / "values.yaml").write_text(
        "env:\n  - name: LOGGING_LEVEL_ROOT\n    value: DEBUG\n", encoding="utf-8"
    )
    (root / "test" / "values.yaml").write_text(
        "env:\n  - name: LOGGING_LEVEL_ROOT\n    value: INFO\n", encoding="utf-8"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Set environment logging defaults")

    workbench = Workbench(_settings(root))
    report = workbench.generate_file_report(
        workbench.records[0],
        mode="focused",
        include_context_labels=True,
        include_git_context=True,
    )

    assert "Environment variable · `LOGGING_LEVEL_ROOT`" in report
    assert "### Git context" in report
    assert "| Side | Attribution | Commit | Date | Author | Commit message |" in report
    assert "Set environment logging defaults" in report
    assert "| TEST | Line |" in report
    assert "| DEV | Line |" in report


def test_git_status_fetch_detects_branch_behind_remote(tmp_path: Path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    publisher = tmp_path / "publisher"
    publisher.mkdir()
    _git(publisher, "init", "-b", "main")
    _git(publisher, "config", "user.name", "Publisher")
    _git(publisher, "config", "user.email", "publisher@example.com")
    (publisher / "values.yaml").write_text("value: one\n", encoding="utf-8")
    _git(publisher, "add", ".")
    _git(publisher, "commit", "-m", "Initial")
    _git(publisher, "remote", "add", "origin", str(remote))
    _git(publisher, "push", "-u", "origin", "main")

    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(checkout)],
        check=True,
        capture_output=True,
    )

    (publisher / "values.yaml").write_text("value: two\n", encoding="utf-8")
    _git(publisher, "add", ".")
    _git(publisher, "commit", "-m", "Update value")
    _git(publisher, "push")

    status = git_repository_status(checkout, fetch_remote=True)

    assert status.fetch_ok
    assert status.upstream == "origin/main"
    assert status.behind == 1
    assert status.ahead == 0
    assert "1 behind origin/main" in status.summary


def test_blank_focused_report_is_not_generated(tmp_path: Path):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "values.yaml").write_text("key: value\n", encoding="utf-8")
    (target / "values.yaml").write_text("key: value  \n", encoding="utf-8")

    workbench = Workbench(_settings(root))
    record = workbench.records[0]

    assert workbench.report_change_count(record, "focused") == 0
    with pytest.raises(WorkbenchError, match="No visible differences"):
        workbench.generate_file_report(record, mode="focused")
    with pytest.raises(WorkbenchError, match="No visible differences"):
        workbench.save_file_report(record, mode="focused")
    assert not (root / "reports").exists()


class _FakeScreen:
    def __init__(self, keys: list[int], *, height: int = 24, width: int = 120) -> None:
        self.keys = iter(keys)
        self.height = height
        self.width = width

    def erase(self) -> None:
        pass

    def refresh(self) -> None:
        pass

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def getch(self) -> int:
        return next(self.keys)

    def addnstr(self, *_args) -> None:
        pass


def test_detail_scrolling_reuses_cached_file_and_presentation(tmp_path: Path, monkeypatch):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "values.yaml").write_text(
        "settings:\n" + "".join(f"  key_{index}: dev-{index}\n" for index in range(40)),
        encoding="utf-8",
    )
    (target / "values.yaml").write_text(
        "settings:\n" + "".join(f"  key_{index}: test-{index}\n" for index in range(40)),
        encoding="utf-8",
    )

    workbench = Workbench(_settings(root))
    refresh_calls = 0
    original_refresh = workbench.refresh_record

    def counted_refresh(record):
        nonlocal refresh_calls
        refresh_calls += 1
        return original_refresh(record)

    monkeypatch.setattr(workbench, "refresh_record", counted_refresh)

    import config_review.tui as tui_module

    render_calls = 0
    original_render = tui_module.review_unified_diff

    def counted_render(*args, **kwargs):
        nonlocal render_calls
        render_calls += 1
        return original_render(*args, **kwargs)

    layout_calls = 0
    original_layout = tui_module.maximum_horizontal_offset

    def counted_layout(*args, **kwargs):
        nonlocal layout_calls
        layout_calls += 1
        return original_layout(*args, **kwargs)

    monkeypatch.setattr(tui_module, "review_unified_diff", counted_render)
    monkeypatch.setattr(tui_module, "maximum_horizontal_offset", counted_layout)

    tui = Tui(workbench)
    monkeypatch.setattr(tui, "_color_pair", lambda _number: 0)
    screen = _FakeScreen([curses.KEY_DOWN, curses.KEY_RIGHT, ord("b")])

    assert tui.detail_screen(screen, 0) == "back"
    assert refresh_calls == 1
    assert render_calls == 1
    assert layout_calls == 1
