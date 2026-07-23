from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

import pytest

import config_review.web_view as web_view
from config_review.core import AppSettings, load_project_path_settings
from config_review.web_view import (
    LocalWebDiffViewer,
    _ContextGapSnapshot,
    _PrivacyRedactor,
    _context_gap_payload,
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
    assert change["privateOldLines"] == ["value: test"]
    assert change["privateNewLines"] == ["value: dev"]
    assert file_data["privatePath"] == "changed.yaml"
    assert snapshot["privateSource"] == "[SOURCE ROOT]"
    assert snapshot["privateTarget"] == "[TARGET ROOT]"
    assert snapshot["comparison"] == {
        "source": str(source),
        "target": str(target),
        "sourceLabel": "dev",
        "targetLabel": "test",
        "sourceColumnLabel": "DEV",
        "targetColumnLabel": "TEST",
        "sourceRepository": "project",
        "targetRepository": "project",
        "launchDirectory": str(Path.cwd().resolve()),
        "configFile": str(root / ".config-review.yaml"),
        "canPersist": True,
    }
    assert len(snapshot["contextCatalog"]["entries"]) >= 100
    assert snapshot["contextCatalog"]["diagnostics"] == []
    assert change["testRange"] == "1"
    assert change["devRange"] == "1"
    assert len(change["key"]) == 24
    assert change["gitContextId"] == change["key"]
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
        assert hidden["privateOldLines"] == ["environment: [IDENTIFIER-1]"]
        assert hidden["privateNewLines"] == ["environment: [IDENTIFIER-2]"]
        assert hidden["privateLabel"] == "Configuration value"
        assert hidden["key"] == file_data["focusedExpanded"]["hiddenChanges"][0]["key"]
        private_headers = [
            line["privateText"]
            for line in file_data["focusedExpanded"]["lines"]
            if line["kind"] == "filtered_header"
        ]
        assert private_headers
        assert all("test → dev" not in value for value in private_headers)
        assert all("[REDACTED]" in value for value in private_headers)


def test_web_snapshot_uses_selected_directory_names_instead_of_fixed_dev_test_labels(
    tmp_path: Path,
):
    source = tmp_path / "flux-configuration" / "alpha"
    target = tmp_path / "deployment-configurations" / "alpha"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    (source / "values.yaml").write_text("value: incoming\n", encoding="utf-8")
    (target / "values.yaml").write_text("value: current\n", encoding="utf-8")
    settings = AppSettings(
        source=source,
        target=target,
        config_file=tmp_path / ".config-review.yaml",
        context=3,
        include_secrets=False,
        edit_command="",
        vimdiff_command="",
        dry_run=False,
    )

    snapshot = build_web_diff_snapshot(Workbench(settings))

    comparison = snapshot["comparison"]
    assert comparison["sourceLabel"] == "flux-configuration/alpha"
    assert comparison["targetLabel"] == "deployment-configurations/alpha"
    assert comparison["sourceColumnLabel"] == "FLUX-CONFIGURATION/ALPHA"
    assert comparison["targetColumnLabel"] == "DEPLOYMENT-CONFIGURATIONS/ALPHA"
    rendered_text = "\n".join(line["text"] for line in snapshot["files"][0]["raw"]["lines"])
    assert "--- DEPLOYMENT-CONFIGURATIONS/ALPHA/values.yaml" in rendered_text
    assert "+++ FLUX-CONFIGURATION/ALPHA/values.yaml" in rendered_text


def test_context_gap_payload_expands_from_either_edge_and_stops_at_bounds():
    snapshot = _ContextGapSnapshot(
        test_start=4,
        dev_start=9,
        lines=tuple(f"line {index}" for index in range(1, 31)),
    )

    first = _context_gap_payload(snapshot, count=10, edge="start")
    assert first["lines"][0] == {
        "testLine": 5,
        "devLine": 10,
        "text": "line 1",
        "privateText": "line 1",
        "kind": "context",
        "emphasisRanges": [],
        "contextRefs": [],
    }
    assert first["lines"][-1]["text"] == "line 10"
    assert first["hasMore"] is True

    tail = _context_gap_payload(snapshot, count=10, edge="end")
    assert tail["lines"][0]["text"] == "line 21"
    assert tail["lines"][0]["testLine"] == 25
    assert tail["lines"][-1]["text"] == "line 30"
    assert tail["hasMore"] is True

    expanded = _context_gap_payload(snapshot, count=100, edge="start")
    assert expanded["count"] == 30
    assert expanded["hasMore"] is False


def test_privacy_redactor_masks_sensitive_values_and_person_references():
    redactor = _PrivacyRedactor()
    original = [
        'password: "same-secret-123"',
        'backupPassword: "same-secret-123"',
        'url: "https://internal.example.gov/api"',
        "owner: Sam Wetherill",
        "email: sam@example.com",
        "replicas: 3",
        "name: SPRING_PROFILES_ACTIVE",
        'value: "prod,seed"',
        "  - name: DB_PASSWORD",
        '    value: "database-password-456"',
        "      secretKeyRef:",
        "        name: database-secret",
        "        key: password",
    ]

    private = redactor.redact_lines(original)

    assert private[0] == 'password: "[SECRET-1]"'
    assert private[1] == 'backupPassword: "[SECRET-1]"'
    assert private[2] == 'url: "[ENDPOINT-1]"'
    assert private[3] == "owner: [PERSON-1]"
    assert private[4] == "email: [PERSON-2]"
    assert private[5] == "replicas: 3"
    assert private[6] == "name: SPRING_PROFILES_ACTIVE"
    assert private[7] == 'value: "prod,seed"'
    assert private[8] == "  - name: DB_PASSWORD"
    assert private[9] == '    value: "[SECRET-2]"'
    assert private[10] == "      secretKeyRef:"
    assert private[11] == "        name: [REFERENCE-1]"
    assert private[12] == "        key: [REFERENCE-2]"


def test_web_snapshot_precomputes_redacted_diff_text(tmp_path: Path):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "values.yaml").write_text(
        'password: "dev-secret-123"\nowner: Dev Person\nurl: https://dev.example.gov/api\n',
        encoding="utf-8",
    )
    (target / "values.yaml").write_text(
        'password: "test-secret-456"\nowner: Test Person\nurl: https://test.example.gov/api\n',
        encoding="utf-8",
    )

    snapshot = build_web_diff_snapshot(Workbench(_settings(root)))
    file_data = snapshot["files"][0]
    raw_lines = file_data["raw"]["lines"]
    private_text = "\n".join(line["privateText"] for line in raw_lines)
    raw_text = "\n".join(line["text"] for line in raw_lines)

    assert "dev-secret-123" in raw_text
    assert "test-secret-456" in raw_text
    assert "Dev Person" in raw_text
    assert "Test Person" in raw_text
    assert "example.gov" in raw_text
    assert "dev-secret-123" not in private_text
    assert "test-secret-456" not in private_text
    assert "Dev Person" not in private_text
    assert "Test Person" not in private_text
    assert "example.gov" not in private_text
    assert "[SECRET-" in private_text
    assert "[PERSON-" in private_text
    assert "[ENDPOINT-" in private_text
    assert "TEST/values.yaml" in private_text
    assert "DEV/values.yaml" in private_text


def test_snapshot_marks_exact_intraline_value_changes(tmp_path: Path):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "values.yaml").write_text('value: "iesp-dev-east"\n', encoding="utf-8")
    (target / "values.yaml").write_text('value: "iesp-test-east"\n', encoding="utf-8")

    snapshot = build_web_diff_snapshot(Workbench(_settings(root)))
    lines = snapshot["files"][0]["raw"]["lines"]
    removed = next(line for line in lines if line["kind"] == "remove")
    added = next(line for line in lines if line["kind"] == "add")

    assert [removed["text"][start:end] for start, end in removed["emphasisRanges"]] == ["test"]
    assert [added["text"][start:end] for start, end in added["emphasisRanges"]] == ["dev"]


def test_snapshot_attaches_context_references_to_matching_yaml_lines(tmp_path: Path):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "values.keycloak.yaml").write_text(
        "keycloakx:\n  realm: eids-mstl\n  allowInsecureImages: true\n",
        encoding="utf-8",
    )
    (target / "values.keycloak.yaml").write_text(
        "keycloakx:\n  realm: old\n  allowInsecureImages: false\n",
        encoding="utf-8",
    )

    snapshot = build_web_diff_snapshot(Workbench(_settings(root)))
    lines = snapshot["files"][0]["raw"]["lines"]

    realm = next(line for line in lines if line["text"] == "  realm: eids-mstl")
    insecure = next(line for line in lines if line["text"] == "  allowInsecureImages: true")
    assert "eids-mstl-realm" in realm["contextRefs"]
    assert insecure["contextRefs"] == ["allow-insecure-images"]


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
    assert "Add note" in page
    assert "Add Git context" in page
    assert "Copy displayed diff" in page
    assert 'id="contextHelp"' in page
    assert "context-help-button" in page
    assert "context-help-mode" in page
    assert "contextTooltip" in page
    assert "contextSearch" in page
    assert 'id="contextEntryCategory"' in page
    assert "+ Create new category…" in page
    assert 'id="contextLimitFiles"' in page
    assert "Limit this definition to specific files or paths" in page
    assert "Browse changed files…" in page
    assert 'id="contextFilePickerModal"' in page
    assert "dictionary-match" in page
    assert "scrollbar-gutter: stable" in page
    assert "renderContextFilePicker" in page
    assert "updateContextFileScope" in page
    assert "decorateContextTarget" in page
    assert "toggleContextHelpMode" in page
    assert "showContextTooltip" in page
    assert "context-ref-button" not in page
    assert "displayedDiffText" in page
    assert "Copied the displayed redacted diff, including line numbers." in page
    assert "Copied the displayed diff with original values and line numbers." in page
    assert "context-gap" in page
    assert "Show 10 more lines" in page
    assert "intraline" in page
    assert "Hide file" in page
    assert "Mark reviewed" in page
    assert "Review ▾" in page
    assert "Hidden files" in page
    assert "Reviewed files" in page
    assert "Save reviewed report…" in page
    assert "Print reviewed report…" in page
    assert "reviewedFiles = new Set()" in page
    assert "hiddenFiles = new Set()" in page
    assert "gapStateById.clear()" in page
    assert "window.print()" in page
    assert "hiddenChanges" in page
    assert "included because it has a note" in page
    assert "fetch(`git/${encodeURIComponent(change.gitContextId)}`" in page
    assert "Last changed in ${sideLabel} · by " in page
    assert "lastChangedLineRow" in page
    assert "line-git-context" in page
    assert 'content: "Git context"' in page
    assert "Hide Git context" in page
    assert "Open the related merge request for this commit" in page
    assert "createWritable" in page
    assert "lineNumberElement" in page
    assert "Open ${label} line" in page
    assert "review-remote-links" in page
    assert "Hide sensitive values" in page
    assert "Show original values" in page
    assert "privacyMode = false" in page
    assert "display and exports are redacted" in page
    assert "original snapshot still exists inside this local page" in page
    assert "privateOldLines" in page
    assert "$('copyDiff').hidden = false" in page
    assert "Copy the currently displayed diff with original values" in page
    assert "noteEditorsOpen = new Set()" in page
    assert "inlineGitContextKeys = new Set()" in page
    assert "if (!privacyMode && noteEditorsOpen.has(change.key))" in page
    assert "renderOpenGitContexts(view)" in page
    assert "if (privacyMode || !inlineGitContextKeys.has(change.key)) return" in page
    assert "Git context · show latest incoming commit message" not in page


def test_web_git_context_uses_first_changed_line_on_each_side(tmp_path: Path):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    original = "one: base\ntwo: base\n"
    (source / "values.yaml").write_text(original, encoding="utf-8")
    (target / "values.yaml").write_text(original, encoding="utf-8")

    _git(root, "init")
    _git(root, "config", "user.name", "Initial Author")
    _git(root, "config", "user.email", "initial@example.test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Initial configuration")

    (target / "values.yaml").write_text("one: red-first\ntwo: base\n", encoding="utf-8")
    _git(root, "add", "test/values.yaml")
    _git(root, "commit", "-m", "Update TEST first line")
    (target / "values.yaml").write_text("one: red-first\ntwo: red-second\n", encoding="utf-8")
    _git(root, "add", "test/values.yaml")
    _git(root, "commit", "-m", "Update TEST second line")

    (source / "values.yaml").write_text("one: green-first\ntwo: base\n", encoding="utf-8")
    _git(root, "add", "dev/values.yaml")
    _git(root, "commit", "-m", "Update DEV first line")
    (source / "values.yaml").write_text("one: green-first\ntwo: green-second\n", encoding="utf-8")
    _git(root, "add", "dev/values.yaml")
    _git(root, "commit", "-m", "Update DEV second line")

    workbench = Workbench(_settings(root))
    snapshot, git_lookup, _context_lookup = web_view._build_web_diff_snapshot(workbench)
    change = snapshot["files"][0]["focused"]["changes"][0]
    record, block = git_lookup[change["gitContextId"]]

    payload = web_view._git_context_payload(workbench, record, block)

    assert len(payload["test"]) == 1
    assert payload["test"][0]["subject"] == "Update TEST first line"
    assert len(payload["dev"]) == 1
    assert payload["dev"][0]["subject"] == "Update DEV first line"


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

    base_lines = [f"line {index}" for index in range(1, 31)]
    base_lines[14] = "value: test"
    (source / "values.yaml").write_text("\n".join(base_lines) + "\n", encoding="utf-8")
    (target / "values.yaml").write_text("\n".join(base_lines) + "\n", encoding="utf-8")
    _git(root, "add", "dev/values.yaml", "test/values.yaml")
    _git(root, "commit", "-m", "Initial configuration")

    dev_lines = base_lines.copy()
    dev_lines[14] = "value: dev"
    (source / "values.yaml").write_text("\n".join(dev_lines) + "\n", encoding="utf-8")
    _git(root, "add", "dev/values.yaml")
    _git(
        root,
        "commit",
        "-m",
        "Enable incoming DEV value",
        "-m",
        "See merge request group/project!42",
    )
    incoming_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "remote", "add", "origin", "git@gitlab.example.gov:group/project.git")

    workbench = Workbench(_settings(root))
    snapshot = build_web_diff_snapshot(workbench)
    change = snapshot["files"][0]["focused"]["changes"][0]
    context_id = change["gitContextId"]
    gap = snapshot["files"][0]["focused"]["contextGaps"][0]

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
            assert payload["dev"][0]["fullHash"] == incoming_commit
            assert payload["dev"][0]["linkKind"] == "merge request"
            assert payload["dev"][0]["url"] == (
                "https://gitlab.example.gov/group/project/-/merge_requests/42"
            )
            assert payload["test"][0]["linkKind"] == "commit"
            assert "/-/commit/" in payload["test"][0]["url"]

        file_context_url = launch.url + "context/" + gap["id"] + "?count=10&edge=end"
        with urllib.request.urlopen(file_context_url, timeout=5) as response:
            payload = json.load(response)
            assert response.headers["Content-Type"].startswith("application/json")
            assert payload["count"] == 10
            assert payload["edge"] == "end"
            assert payload["lines"][-1]["text"] == "line 11"
            assert payload["lines"][-1]["testLine"] == 11
            assert payload["hasMore"] is True

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


def test_web_viewer_can_browse_and_replace_comparison_directories(tmp_path: Path):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    next_source = root / "stage"
    next_target = root / "prod"
    for directory in (source, target, next_source, next_target):
        directory.mkdir(parents=True, exist_ok=True)
    (source / "first.yaml").write_text("value: dev\n", encoding="utf-8")
    (target / "first.yaml").write_text("value: test\n", encoding="utf-8")
    (next_source / "second.yaml").write_text("value: stage\n", encoding="utf-8")
    (next_target / "second.yaml").write_text("value: prod\n", encoding="utf-8")

    viewer = LocalWebDiffViewer()
    try:
        launch = viewer.open(Workbench(_settings(root)), open_browser=False)

        browse_url = launch.url + "directories?path=" + quote(str(root))
        with urllib.request.urlopen(browse_url, timeout=2) as response:
            payload = json.load(response)
            assert payload["path"] == str(root)
            assert {item["name"] for item in payload["directories"]} >= {
                "dev",
                "test",
                "stage",
                "prod",
            }

        environments_url = launch.url + "environments?root=" + quote(str(root))
        with urllib.request.urlopen(environments_url, timeout=2) as response:
            payload = json.load(response)
            assert payload["root"] == str(root)
            assert payload["repository"] == "project"
            assert {item["name"] for item in payload["environments"]} == {
                "dev",
                "test",
                "stage",
                "prod",
            }

        preview_request = urllib.request.Request(
            launch.url + "comparison-preview",
            data=json.dumps(
                {
                    "source": str(next_source),
                    "target": str(next_target),
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(preview_request, timeout=5) as response:
            payload = json.load(response)
            assert payload["sourceLabel"] == "stage"
            assert payload["targetLabel"] == "prod"
            assert payload["matchedFiles"] == 1
            assert payload["modifiedFiles"] == 1
            assert payload["differentFiles"] == 1
            assert payload["sourceOnlyFiles"] == 0
            assert payload["targetOnlyFiles"] == 0

        request = urllib.request.Request(
            launch.url + "comparison",
            data=json.dumps(
                {
                    "source": str(next_source),
                    "target": str(next_target),
                    "persist": False,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
            assert payload == {
                "ok": True,
                "source": str(next_source),
                "target": str(next_target),
                "sourceLabel": "stage",
                "targetLabel": "prod",
                "sourceColumnLabel": "STAGE",
                "targetColumnLabel": "PROD",
                "sourceRepository": "project",
                "targetRepository": "project",
                "fileCount": 1,
                "persisted": False,
            }

        with urllib.request.urlopen(launch.url, timeout=2) as response:
            page = response.read().decode("utf-8")
            assert "second.yaml" in page
            assert str(next_source) in page
            assert str(next_target) in page
            assert "Change comparison" in page
            assert '"sourceLabel":"stage"' in page
            assert '"targetLabel":"prod"' in page
            assert "updateComparisonButton" in page
            assert "STAGE/second.yaml" in page
            assert "PROD/second.yaml" in page

        persist_request = urllib.request.Request(
            launch.url + "comparison",
            data=json.dumps(
                {
                    "source": str(next_source),
                    "target": str(next_target),
                    "persist": True,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(persist_request, timeout=5) as response:
            payload = json.load(response)
            assert payload["persisted"] is True
        assert load_project_path_settings(root / ".config-review.yaml") == (
            ".",
            "stage",
            "prod",
        )

        invalid_request = urllib.request.Request(
            launch.url + "comparison",
            data=json.dumps(
                {
                    "source": str(root / "missing"),
                    "target": str(next_target),
                    "persist": False,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(invalid_request, timeout=2)
        assert exc_info.value.code == 400
        error = json.loads(exc_info.value.read().decode("utf-8"))
        assert "Directory does not exist" in error["error"]
    finally:
        viewer.stop()


def test_snapshot_builds_segmented_context_path_and_undocumented_key_suggestion(
    tmp_path: Path,
):
    root = tmp_path / "project"
    source = root / "alpha"
    target = root / "test-ot"
    source_file = source / "ms" / "config" / "values.yaml"
    target_file = target / "ms" / "config" / "values.yaml"
    source_file.parent.mkdir(parents=True)
    target_file.parent.mkdir(parents=True)
    source_file.write_text("customSetting: alpha\n", encoding="utf-8")
    target_file.write_text("customSetting: test\n", encoding="utf-8")
    settings = AppSettings(
        source=source,
        target=target,
        config_file=root / ".config-review.yaml",
        context=3,
        include_secrets=False,
        edit_command="",
        vimdiff_command="",
        dry_run=False,
    )

    snapshot = build_web_diff_snapshot(Workbench(settings))
    file_data = snapshot["files"][0]
    path = file_data["contextPath"]

    assert path["sourceEnvironment"]["text"] == "alpha"
    assert path["sourceEnvironment"]["contextRefs"] == ["alpha-environment"]
    assert path["sourceEnvironment"]["contextTargets"][0]["text"] == "alpha"
    assert path["targetEnvironment"]["contextRefs"] == ["test-ot-environment"]
    assert [part["text"] for part in path["parts"]] == ["ms", "config", "values.yaml"]
    assert path["parts"][0]["contextRefs"] == ["mission-support-path"]
    assert path["parts"][1]["contextRefs"] == ["config-directory"]
    assert path["parts"][2]["contextRefs"] == ["values-yaml-file"]

    custom_line = next(
        line for line in file_data["raw"]["lines"] if "customSetting:" in line["text"]
    )
    assert custom_line["contextRefs"] == []
    key_target = next(
        target for target in custom_line["contextTargets"] if target["text"] == "customSetting"
    )
    assert key_target["contextSuggestion"] == {
        "type": "yaml-path",
        "value": "customSetting",
        "files": ["ms/config/values.yaml"],
        "title": "customSetting",
        "clickedType": "YAML key",
        "clickedValue": "customSetting",
        "yamlPath": "customSetting",
        "file": "ms/config/values.yaml",
    }


def test_web_context_editor_saves_project_definition_and_refreshes_page(tmp_path: Path):
    root = tmp_path / "project"
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "values.yaml").write_text("customSetting: dev\n", encoding="utf-8")
    (target / "values.yaml").write_text("customSetting: test\n", encoding="utf-8")

    viewer = LocalWebDiffViewer()
    try:
        launch = viewer.open(Workbench(_settings(root)), open_browser=False)
        request = urllib.request.Request(
            launch.url + "context-entry",
            data=json.dumps(
                {
                    "entry": {
                        "id": "custom-setting",
                        "title": "Custom setting",
                        "category": "Project Context",
                        "summary": "Explains the custom setting.",
                        "details": "Saved from the local web viewer.",
                        "aliases": [],
                        "matches": [
                            {
                                "type": "yaml-key",
                                "value": "customSetting",
                                "files": ["values.yaml"],
                            }
                        ],
                    }
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
            assert payload["ok"] is True
            assert payload["entryId"] == "custom-setting"
            assert payload["path"] == str(root / ".config-review-context.yaml")

        context_file = root / ".config-review-context.yaml"
        assert context_file.is_file()
        assert "custom-setting" in context_file.read_text(encoding="utf-8")

        with urllib.request.urlopen(launch.url, timeout=2) as response:
            page = response.read().decode("utf-8")
            assert '"id":"custom-setting"' in page
            assert '"contextRefs":["custom-setting"]' in page
            assert 'id="contextEditorModal"' in page
            assert "No context definition exists yet" in page
    finally:
        viewer.stop()
