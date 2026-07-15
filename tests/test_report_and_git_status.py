from __future__ import annotations

import subprocess
from pathlib import Path

from config_review.core import AppSettings, git_repository_status
from config_review.tui import detail_footer_lines
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
        assert "n/p" in combined
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

    assert "Visible differences:** 1" in report
    assert "Environment variable · SPRING_PROFILES_ACTIVE" in report
    assert 'value: "prod"' in report
    assert 'value: "prod,seed"' in report
    assert "SPRING_CONFIG_ADDITIONAL_LOCATION" not in report

    saved = workbench.save_file_report(
        record,
        mode="focused",
        include_context_labels=True,
        include_git_context=False,
    )
    assert saved.parent.name == ".config-review-reports"
    saved_text = saved.read_text(encoding="utf-8")
    assert "Environment variable · SPRING_PROFILES_ACTIVE" in saved_text
    assert "Visible differences:** 1" in saved_text


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

    assert "Environment variable · LOGGING_LEVEL_ROOT" in report
    assert "Set environment logging defaults" in report
    assert "TEST (line)" in report
    assert "DEV (line)" in report


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
