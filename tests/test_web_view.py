from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pytest

import config_review.web_view as web_view
from config_review.core import AppSettings, ChangeBlock
from config_review.web_view import (
    LocalWebDiffViewer,
    _ChangeContextSnapshot,
    _context_payload,
    _open_browser_once,
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
    assert change["contextId"] == change["key"]
    assert change["testStart"] == 0
    assert change["testEnd"] == 1
    assert change["devStart"] == 0
    assert change["devEnd"] == 1
    assert change["panelAfter"] > change["markerIndex"]
    assert file_data["raw"]["changes"][0]["key"] == change["key"]


def test_focused_snapshot_exposes_note_targets_for_hidden_differences(tmp_path: Path):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    for name in ("one.yaml", "two.yaml"):
        (source / name).write_text("environment: dev\n", encoding="utf-8")
        (target / name).write_text("environment: test\n", encoding="utf-8")

    snapshot = build_web_diff_snapshot(Workbench(_settings(root)))

    for file_data in snapshot["files"]:
        assert file_data["focused"]["visibleChanges"] == 0
        assert len(file_data["focused"]["hiddenChanges"]) == 1
        hidden = file_data["focused"]["hiddenChanges"][0]
        assert hidden["hidden"] is True
        assert hidden["oldLines"] == ["environment: test"]
        assert hidden["newLines"] == ["environment: dev"]
        assert hidden["key"] == file_data["focusedExpanded"]["hiddenChanges"][0]["key"]


def test_context_payload_expands_in_chunks_and_stops_at_file_bounds():
    lines = tuple(f"line {index}" for index in range(1, 31))
    snapshot = _ChangeContextSnapshot(
        test_lines=lines,
        dev_lines=lines,
        block=ChangeBlock(
            tag="replace",
            old_start=14,
            old_end=16,
            new_start=14,
            new_end=16,
            old_lines=["line 15", "line 16"],
            new_lines=["line 15", "line 16"],
        ),
    )

    first = _context_payload(snapshot, before=10, after=10)
    assert first["test"]["lines"][0]["number"] == 5
    assert first["test"]["lines"][-1]["number"] == 26
    assert first["test"]["moreAbove"] is True
    assert first["test"]["moreBelow"] is True
    assert [line["number"] for line in first["test"]["lines"] if line["changed"]] == [15, 16]

    expanded = _context_payload(snapshot, before=20, after=20)
    assert expanded["test"]["lines"][0]["number"] == 1
    assert expanded["test"]["lines"][-1]["number"] == 30
    assert expanded["test"]["moreAbove"] is False
    assert expanded["test"]["moreBelow"] is False


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
    assert "File context · show nearby TEST and DEV lines" in page
    assert "Show 10 more above" in page
    assert "Show 10 more below" in page
    assert "Hide file" in page
    assert "Mark reviewed" in page
    assert "Review ▾" in page
    assert "Hidden files" in page
    assert "Reviewed files" in page
    assert "Save reviewed report…" in page
    assert "Print reviewed report…" in page
    assert "reviewedFiles = new Set()" in page
    assert "hiddenFiles = new Set()" in page
    assert "contextStateByChange.clear()" in page
    assert "window.print()" in page
    assert "hiddenChanges" in page
    assert "included because it has a note" in page
    assert "Git context · show latest incoming commit message" in page
    assert "fetch(`git/${encodeURIComponent(change.gitContextId)}`" in page
    assert "createWritable" in page


def test_wsl_browser_launcher_uses_one_windows_command(
    monkeypatch: pytest.MonkeyPatch,
):
    url = "http://127.0.0.1:43127/token/"
    calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")
    monkeypatch.setattr(
        web_view.shutil,
        "which",
        lambda name: "/mnt/c/Windows/System32/cmd.exe" if name == "cmd.exe" else None,
    )

    def fake_popen(command: list[str], **kwargs: object) -> object:
        calls.append((command, kwargs))
        return object()

    monkeypatch.setattr(web_view.subprocess, "Popen", fake_popen)

    def unexpected_webbrowser(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("generic webbrowser launcher should not run under WSL")

    monkeypatch.setattr(web_view.webbrowser, "open", unexpected_webbrowser)

    assert _open_browser_once(url) is True
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == [
        "/mnt/c/Windows/System32/cmd.exe",
        "/d",
        "/c",
        "start",
        "",
        url,
    ]
    assert kwargs["stderr"] is web_view.subprocess.DEVNULL
    assert kwargs["stdout"] is web_view.subprocess.DEVNULL


def test_non_wsl_browser_launcher_uses_python_webbrowser_once(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[tuple[str, int]] = []
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setattr(web_view.platform, "release", lambda: "6.8.0-linux")
    monkeypatch.setattr(
        web_view.webbrowser,
        "open",
        lambda url, new=0: calls.append((url, new)) or True,
    )

    url = "http://127.0.0.1:43127/token/"
    assert _open_browser_once(url) is True
    assert calls == [(url, 2)]


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

        file_context_url = launch.url + "context/" + context_id + "?before=10&after=10"
        with urllib.request.urlopen(file_context_url, timeout=5) as response:
            payload = json.load(response)
            assert response.headers["Content-Type"].startswith("application/json")
            assert payload["test"]["lines"] == [
                {"number": 1, "text": "value: test", "changed": True}
            ]
            assert payload["dev"]["lines"] == [{"number": 1, "text": "value: dev", "changed": True}]
            assert payload["test"]["moreAbove"] is False
            assert payload["test"]["moreBelow"] is False

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

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(launch.url + "context/not-a-change", timeout=2)
        assert exc_info.value.code == 404
    finally:
        viewer.stop()
