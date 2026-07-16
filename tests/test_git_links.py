from __future__ import annotations

import subprocess
from pathlib import Path

from config_review.core import (
    AppSettings,
    git_remote_to_web_url,
    git_repository_file_url,
    load_git_repository_url,
    load_project_path_settings,
    save_git_repository_url,
    save_project_paths,
)
from config_review.web_view import build_web_diff_snapshot
from config_review.workbench import Workbench


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )


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


def test_git_remote_web_url_conversion_removes_credentials_and_git_suffix():
    assert (
        git_remote_to_web_url("git@gitlab.example.gov:group/project.git")
        == "https://gitlab.example.gov/group/project"
    )
    assert (
        git_remote_to_web_url("ssh://git@gitlab.example.gov/group/project.git")
        == "https://gitlab.example.gov/group/project"
    )
    assert (
        git_remote_to_web_url("https://token@gitlab.example.gov/group/project.git")
        == "https://gitlab.example.gov/group/project"
    )
    assert git_remote_to_web_url("../local-repository") is None


def test_git_file_urls_use_provider_specific_blob_and_line_ranges():
    assert (
        git_repository_file_url(
            "https://gitlab.example.gov/group/project",
            "abc123",
            "dev/app values.yaml",
            line_start=5,
            line_end=8,
        )
        == "https://gitlab.example.gov/group/project/-/blob/abc123/dev/app%20values.yaml#L5-8"
    )
    assert (
        git_repository_file_url(
            "https://github.com/example/project",
            "abc123",
            "dev/app.yaml",
            line_start=5,
            line_end=8,
        )
        == "https://github.com/example/project/blob/abc123/dev/app.yaml#L5-L8"
    )


def test_git_repository_url_is_local_config_and_preserves_project_paths(tmp_path: Path):
    config = tmp_path / ".config-review.yaml"
    source = tmp_path / "configs" / "dev"
    target = tmp_path / "configs" / "test"
    source.mkdir(parents=True)
    target.mkdir()
    save_project_paths(config, source, target)

    save_git_repository_url(config, "https://gitlab.example.gov/group/project.git/")

    assert load_git_repository_url(config) == "https://gitlab.example.gov/group/project"
    assert load_project_path_settings(config) == ("configs", "dev", "test")

    save_git_repository_url(config, None)
    assert load_git_repository_url(config) is None
    assert load_project_path_settings(config) == ("configs", "dev", "test")


def test_workbench_prefers_configured_url_and_snapshot_contains_exact_line_links(
    tmp_path: Path,
):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "values.yaml").write_text("value: original\n", encoding="utf-8")
    (target / "values.yaml").write_text("value: test\n", encoding="utf-8")

    _git(root, "init")
    _git(root, "config", "user.name", "Test Reviewer")
    _git(root, "config", "user.email", "reviewer@example.test")
    _git(root, "add", "dev/values.yaml", "test/values.yaml")
    _git(root, "commit", "-m", "Initial configuration")
    _git(root, "remote", "add", "origin", "git@auto.example.gov:group/project.git")

    (source / "values.yaml").write_text("value: dev\n", encoding="utf-8")
    save_git_repository_url(
        root / ".config-review.yaml",
        "https://gitlab.configured.gov/team/configuration",
    )

    workbench = Workbench(_settings(root))
    snapshot = build_web_diff_snapshot(workbench)

    assert workbench.git_repository_url_source == "configured"
    assert snapshot["gitLinks"]["repositoryUrl"] == (
        "https://gitlab.configured.gov/team/configuration"
    )
    assert snapshot["gitLinks"]["commit"] == workbench.git_status.commit
    file_data = snapshot["files"][0]
    assert file_data["remote"]["testFileUrl"].startswith(
        "https://gitlab.configured.gov/team/configuration/-/blob/"
    )
    change = file_data["focused"]["changes"][0]
    assert change["testRemoteUrl"].endswith("/test/values.yaml#L1")
    assert change["devRemoteUrl"].endswith("/dev/values.yaml#L1")


def test_workbench_auto_detects_origin_when_no_override_exists(tmp_path: Path):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "values.yaml").write_text("value: dev\n", encoding="utf-8")
    (target / "values.yaml").write_text("value: test\n", encoding="utf-8")

    _git(root, "init")
    _git(root, "config", "user.name", "Test Reviewer")
    _git(root, "config", "user.email", "reviewer@example.test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Initial configuration")
    _git(root, "remote", "add", "origin", "git@gitlab.example.gov:group/project.git")

    workbench = Workbench(_settings(root))

    assert workbench.git_repository_url_source == "origin"
    assert workbench.git_repository_url == "https://gitlab.example.gov/group/project"


def test_git_links_prefer_fetched_upstream_commit(tmp_path: Path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "values.yaml").write_text("value: original\n", encoding="utf-8")
    (target / "values.yaml").write_text("value: test\n", encoding="utf-8")

    _git(root, "init")
    _git(root, "config", "user.name", "Test Reviewer")
    _git(root, "config", "user.email", "reviewer@example.test")
    _git(root, "branch", "-M", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Initial configuration")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-u", "origin", "main")
    remote_commit = _git(root, "rev-parse", "origin/main").stdout.strip()

    (source / "values.yaml").write_text("value: dev\n", encoding="utf-8")
    save_git_repository_url(
        root / ".config-review.yaml",
        "https://gitlab.example.gov/group/project",
    )

    workbench = Workbench(_settings(root))

    assert workbench.git_status.fetch_ok is True
    assert workbench.git_status.upstream_commit == remote_commit
    assert workbench.git_link_commit == remote_commit
    assert "fetched upstream commit" in workbench.git_link_status_text
