from __future__ import annotations

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


def test_web_snapshot_contains_only_current_differences_and_both_views(tmp_path: Path):
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


def test_web_page_escapes_configuration_that_looks_like_script_markup():
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
                        "visibleChanges": 1,
                        "handled": 0,
                        "noiseHidden": 1,
                        "whitespaceHidden": 0,
                        "orderHidden": 0,
                        "orderUnavailable": None,
                    },
                    "raw": {
                        "lines": [],
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
    assert "scrollbar-gutter:stable" in page
    assert "hidden-block" in page


def test_web_viewer_is_loopback_tokenized_and_read_only(tmp_path: Path):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "values.yaml").write_text("value: dev\n", encoding="utf-8")
    (target / "values.yaml").write_text("value: test\n", encoding="utf-8")

    viewer = LocalWebDiffViewer()
    try:
        launch = viewer.open(Workbench(_settings(root)), open_browser=False)
        parsed = urlparse(launch.url)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.path not in {"", "/"}
        assert launch.file_count == 1

        with urllib.request.urlopen(launch.url, timeout=2) as response:
            page = response.read().decode("utf-8")
            assert response.headers["Cache-Control"] == "no-store"
            assert "Config Review Web Diff" in page
            assert "values.yaml" in page
            assert "Focused" in page
            assert "Raw" in page

        root_url = f"http://127.0.0.1:{parsed.port}/"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(root_url, timeout=2)
        assert exc_info.value.code == 404
    finally:
        viewer.stop()
