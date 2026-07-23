from __future__ import annotations

from pathlib import Path

from config_review.context_help import load_context_catalog


def _catalog(root: Path):
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    return load_context_catalog(root / ".config-review.yaml", source, target)


def test_built_in_context_catalog_contains_initial_dictionary(tmp_path: Path):
    catalog = _catalog(tmp_path)

    assert not catalog.diagnostics
    assert len(catalog.entries) >= 100
    by_id = catalog.by_id
    assert by_id["mstl"].title == "MSTL — Mission Support Tool Suite"
    assert by_id["swim-relay"].category == "Services & Libraries"
    assert by_id["helm-chart"].category == "Helm & Packaging"
    assert by_id["gitlab-rules"].category == "GitLab CI/CD"
    assert by_id["fluxcd"].category == "Security & GitOps"


def test_context_catalog_matches_yaml_services_and_pipeline_keywords(tmp_path: Path):
    catalog = _catalog(tmp_path)

    assert catalog.match_line("values.yaml", "swim-relay:") == ["swim-relay"]
    assert "allow-insecure-images" in catalog.match_line(
        "values.keycloak.yaml", "allowInsecureImages: true"
    )
    assert "eids-mstl-realm" in catalog.match_line("values.keycloak.yaml", "realm: eids-mstl")
    assert catalog.match_line(".gitlab-ci.yml", "  include:") == ["gitlab-include"]
    assert "gitlab-include" not in catalog.match_line("values.yaml", "include:")
    assert "helm-dependency-update" in catalog.match_line(
        "pipeline.sh", "helm dependency update ./charts/eids"
    )


def test_project_context_file_adds_and_overrides_entries(tmp_path: Path):
    root = tmp_path
    source = root / "dev"
    target = root / "test"
    source.mkdir()
    target.mkdir()
    (root / ".config-review-context.yaml").write_text(
        """\
schemaVersion: 1
entries:
  - id: swim-relay
    title: SWIM Relay — Local Description
    category: Project Context
    summary: Local project-specific explanation.
    matches:
      - type: term
        value: swim-relay
  - id: custom-setting
    title: Custom Setting
    category: Project Context
    summary: Explains one local setting.
    matches:
      - type: yaml-key
        value: customSetting
""",
        encoding="utf-8",
    )

    catalog = load_context_catalog(root / ".config-review.yaml", source, target)

    assert catalog.by_id["swim-relay"].title == "SWIM Relay — Local Description"
    assert catalog.by_id["swim-relay"].source == str(root / ".config-review-context.yaml")
    assert catalog.match_line("values.yaml", "customSetting: true") == ["custom-setting"]


def test_invalid_project_context_file_does_not_break_built_in_catalog(tmp_path: Path):
    root = tmp_path
    source = root / "dev"
    target = root / "test"
    source.mkdir()
    target.mkdir()
    (root / ".config-review-context.yaml").write_text(
        "schemaVersion: 2\nentries: []\n", encoding="utf-8"
    )

    catalog = load_context_catalog(root / ".config-review.yaml", source, target)

    assert "swim-relay" in catalog.by_id
    assert len(catalog.diagnostics) == 1
    assert "schemaVersion must be 1" in catalog.diagnostics[0]


def test_path_segments_match_independently_without_leaking_into_yaml_lines(tmp_path: Path):
    catalog = _catalog(tmp_path)

    assert catalog.match_path_segment("ms/config/values.yaml", "alpha") == ["alpha-environment"]
    assert catalog.match_path_segment("ms/config/values.yaml", "ms") == ["mission-support-path"]
    assert catalog.match_path_segment("ms/config/values.yaml", "config") == ["config-directory"]
    assert catalog.match_path_segment("ms/config/values.yaml", "values.yaml", is_filename=True) == [
        "values-yaml-file"
    ]
    assert "values-yaml-file" not in catalog.match_line("ms/config/values.yaml", "unrelated: true")


def test_context_entry_editor_creates_and_updates_project_dictionary(tmp_path: Path):
    from config_review.context_help import upsert_context_entry

    config_file = tmp_path / ".config-review.yaml"
    entry, path = upsert_context_entry(
        config_file,
        {
            "id": "custom-folder",
            "title": "Custom folder",
            "category": "Project Context",
            "summary": "Explains this folder.",
            "matches": [
                {
                    "type": "path-segment",
                    "value": "custom",
                    "files": [],
                }
            ],
        },
    )

    assert entry.id == "custom-folder"
    assert path == tmp_path / ".config-review-context.yaml"
    catalog = _catalog(tmp_path)
    assert catalog.match_path_segment("custom/values.yaml", "custom") == ["custom-folder"]

    upsert_context_entry(
        config_file,
        {
            "id": "custom-folder",
            "title": "Updated custom folder",
            "category": "Project Context",
            "summary": "Updated explanation.",
            "matches": [
                {
                    "type": "path-segment",
                    "value": "custom",
                    "files": [],
                }
            ],
        },
    )
    catalog = _catalog(tmp_path)
    assert catalog.by_id["custom-folder"].title == "Updated custom folder"
    assert [entry.id for entry in catalog.entries].count("custom-folder") == 1
