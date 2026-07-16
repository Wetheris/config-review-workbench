from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pytest

from config_review.core import AppSettings
from config_review.web_view import (
    LocalWebDiffViewer,
    _render_page,
    build_web_diff_snapshot,
)
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


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )


def test_web_snapshot_contains_only_current_differences_and_review_metadata(
    tmp_path: Path,
):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "changed.yaml").write_text("value: dev\n", encoding="utf-8")
    (target / "changed.yaml").write_text("value: test\n", encoding="utf-8")
    (source / "same.yaml").write_text("value: same\n", encoding="utf-8")
    (target / "same.yaml").write_text("value: same\n", encoding="utf-8")

    snapshot = build_web_diff_snapshot(Workbench(_settings(root)))

    assert [item["path"] for item in snapshot["files"]] == ["changed.yaml"]
    file_data = snapshot["files"][0]
    assert file_data["focused"]["visibleChanges"] == 1
    assert file_data["focusedExpanded"]["visibleChanges"] == 1
    assert file_data["raw"]["visibleChanges"] == 1
    assert any(line["kind"] == "remove" for line in file_data["raw"]["lines"])
    assert any(line["kind"] == "add" for line in file_data["raw"]["lines"])

    change = file_data["focused"]["changes"][0]
    assert change["label"] == "Configuration value"
    assert change["oldLines"] == ["value: test"]
    assert change["newLines"] == ["value: dev"]
    assert change["testRange"] == "1"
    assert change["devRange"] == "1"
    assert len(change["key"]) == 24
    assert change["gitContextId"] == change["key"]
    assert change["panelAfter"] > change["markerIndex"]
    assert file_data["raw"]["changes"][0]["key"] == change["key"]


def test_snapshot_does_not_collect_git_context_eagerly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "values.yaml").write_text("value: dev\n", encoding="utf-8")
    (target / "values.yaml").write_text("value: test\n", encoding="utf-8")

    def fail_if_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Git context should be lazy")

    monkeypatch.setattr(Workbench, "_block_git_context", fail_if_called)
    snapshot = build_web_diff_snapshot(Workbench(_settings(root)))

    assert snapshot["files"][0]["focused"]["changes"]


def test_web_page_escapes_configuration_and_includes_review_controls():
    page = _render_page(
        {
            "generatedAt": "now",
            "source": "/dev",
            "target": "/test",
            "gitStatus": "no git",
            "files": [
                {
                    "path": "values.yaml",
                    "status": "1 DIFF",
                    "states": [],
                    "counts": {},
                    "focused": {
                        "lines": [
                            {
                                "text": "value: </script><script>alert(1)</script>",
                                "kind": "add",
                                "testLine": None,
                                "devLine": 1,
                            }
                        ],
                        "changes": [],
                        "visibleChanges": 1,
                        "handled": 0,
                        "noiseHidden": 0,
                        "whitespaceHidden": 0,
                        "orderHidden": 0,
                        "orderUnavailable": None,
                    },
                    "focusedExpanded": {
                        "lines": [
                            {
                                "text": "▼ FILTERED DIFF · Environment identity",
                                "kind": "filtered_header",
                                "testLine": None,
                                "devLine": None,
                            },
                            {
                                "text": "value: test",
                                "kind": "filtered_remove",
                                "testLine": 1,
                                "devLine": None,
                            },
                            {
                                "text": "value: dev",
                                "kind": "filtered_add",
                                "testLine": None,
                                "devLine": 1,
                            },
                        ],
                        "changes": [],
                        "visibleChanges": 1,
                        "handled": 0,
                        "noiseHidden": 1,
                        "whitespaceHidden": 0,
                        "orderHidden": 0,
                        "orderUnavailable": None,
                    },
                    "raw": {
                        "lines": [],
                        "changes": [],
                        "visibleChanges": 0,
                        "handled": 0,
                        "noiseHidden": 0,
                        "whitespaceHidden": 0,
                        "orderHidden": 0,
                        "orderUnavailable": None,
                    },
                }
            ],
        }
    ).decode("utf-8")

    assert "</script><script>alert(1)</script>" not in page
    assert r"<\/script><script>alert(1)<\/script>" in page
    assert "View ▾" in page
    assert "System" in page
    assert "Dark" in page
    assert "Light" in page
    assert "Expand all" in page
    assert "Collapse all" in page
    assert "scrollbar-gutter: stable" in page
    assert ".main { min-width: 0; min-height: 0; overflow: hidden" in page
    assert "min-height: 0;\n  overscroll-behavior: contain" in page
    assert "hidden-block" in page
    assert "Save review…" in page
    assert "showSaveFilePicker" in page
    assert "Deployment note" in page
    assert "Git context · show latest incoming commit message" in page
    assert "fetch(`git/${encodeURIComponent(change.gitContextId)}`" in page
    assert "createWritable" in page


def test_web_viewer_serves_lazy_git_context_and_rejects_writes(tmp_path: Path):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Test Reviewer")
    _git(root, "config", "user.email", "reviewer@example.test")

    (source / "values.yaml").write_text("value: test\n", encoding="utf-8")
    (target / "values.yaml").write_text("value: test\n", encoding="utf-8")
    _git(root, "add", "dev/values.yaml", "test/values.yaml")
    _git(root, "commit", "-m", "Initial configuration")

    (source / "values.yaml").write_text("value: dev\n", encoding="utf-8")
    _git(root, "add", "dev/values.yaml")
    _git(root, "commit", "-m", "Enable incoming DEV value")

    workbench = Workbench(_settings(root))
    snapshot = build_web_diff_snapshot(workbench)
    context_id = snapshot["files"][0]["focused"]["changes"][0]["gitContextId"]

    viewer = LocalWebDiffViewer()
    try:
        launch = viewer.open(workbench, open_browser=False)
        parsed = urlparse(launch.url)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.path not in {"", "/"}
        assert launch.file_count == 1

        with urllib.request.urlopen(launch.url, timeout=2) as response:
            page = response.read().decode("utf-8")
            assert response.headers["Cache-Control"].startswith("no-store")
            assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
            assert "Config Review Web Diff" in page
            assert "values.yaml" in page
            assert "Focused" in page
            assert "Raw" in page

        context_url = launch.url + "git/" + context_id
        with urllib.request.urlopen(context_url, timeout=5) as response:
            payload = json.load(response)
            assert response.headers["Content-Type"].startswith("application/json")
            assert payload["dev"][0]["subject"] == "Enable incoming DEV value"
            assert payload["test"][0]["subject"] == "Initial configuration"
            assert payload["dev"][0]["source"] in {"line", "file"}

        request = urllib.request.Request(launch.url, data=b"x", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(request, timeout=2)
        assert exc_info.value.code == 405

        root_url = f"http://127.0.0.1:{parsed.port}/"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(root_url, timeout=2)
        assert exc_info.value.code == 404

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(launch.url + "git/not-a-change", timeout=2)
        assert exc_info.value.code == 404
    finally:
        viewer.stop()
