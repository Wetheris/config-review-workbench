from __future__ import annotations

from pathlib import Path

from config_review.context_help import load_context_catalog, yaml_paths_by_line

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CATALOG = PROJECT_ROOT / ".config-review-context.example.yaml"


def _write_example(root: Path) -> Path:
    path = root / ".config-review-context.yaml"
    path.write_text(EXAMPLE_CATALOG.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _catalog(root: Path, *, use_example: bool = False):
    source = root / "dev"
    target = root / "test"
    source.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    if use_example:
        _write_example(root)
    return load_context_catalog(root / ".config-review.yaml", source, target)


def test_context_catalog_is_empty_when_no_local_dictionary_exists(tmp_path: Path):
    catalog = _catalog(tmp_path)

    assert catalog.entries == ()
    assert catalog.diagnostics == ()


def test_generic_example_catalog_loads_as_local_dictionary(tmp_path: Path):
    catalog = _catalog(tmp_path, use_example=True)

    assert not catalog.diagnostics
    assert len(catalog.entries) == 9
    by_id = catalog.by_id
    assert by_id["kubernetes-api-version"].category == "Kubernetes Resources"
    assert by_id["flux-source-reference"].category == "Flux / GitOps"
    assert by_id["helm-values-file"].category == "Helm & Packaging"
    assert by_id["gitlab-script"].category == "GitLab CI/CD"
    assert all(
        entry.source == str(tmp_path / ".config-review-context.yaml") for entry in catalog.entries
    )


def test_generic_catalog_matches_yaml_and_pipeline_keywords(tmp_path: Path):
    catalog = _catalog(tmp_path, use_example=True)

    assert catalog.match_line("resource.yaml", "apiVersion: apps/v1") == ["kubernetes-api-version"]
    assert catalog.match_line("resource.yaml", "spec:") == ["kubernetes-spec"]
    assert catalog.match_line("resource.yaml", "sourceRef:") == ["flux-source-reference"]
    assert catalog.match_line(".gitlab-ci.yml", "  script:") == ["gitlab-script"]
    assert "gitlab-script" not in catalog.match_line("values.yaml", "script:")


def test_later_local_catalogs_override_earlier_entries_by_id(tmp_path: Path):
    root = tmp_path
    source = root / "dev"
    target = root / "test"
    source.mkdir()
    target.mkdir()
    _write_example(root)
    (source / ".config-review-context.yaml").write_text(
        """\
schemaVersion: 1
entries:
  - id: kubernetes-spec
    title: Project-specific spec
    category: Project Context
    summary: Local explanation for spec.
    matches:
      - type: yaml-key
        value: spec
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

    assert catalog.by_id["kubernetes-spec"].title == "Project-specific spec"
    assert catalog.by_id["kubernetes-spec"].source == str(source / ".config-review-context.yaml")
    assert catalog.match_line("values.yaml", "customSetting: true") == ["custom-setting"]


def test_invalid_local_context_file_reports_diagnostic_without_crashing(tmp_path: Path):
    root = tmp_path
    source = root / "dev"
    target = root / "test"
    source.mkdir()
    target.mkdir()
    (root / ".config-review-context.yaml").write_text(
        "schemaVersion: 2\nentries: []\n", encoding="utf-8"
    )

    catalog = load_context_catalog(root / ".config-review.yaml", source, target)

    assert catalog.entries == ()
    assert len(catalog.diagnostics) == 1
    assert "schemaVersion must be 1" in catalog.diagnostics[0]


def test_path_segments_match_independently_without_leaking_into_yaml_lines(tmp_path: Path):
    catalog = _catalog(tmp_path, use_example=True)

    assert catalog.match_path_segment("config/values.yaml", "dev") == ["development-environment"]
    assert catalog.match_path_segment("config/values.yaml", "values.yaml", is_filename=True) == [
        "helm-values-file"
    ]
    assert "helm-values-file" not in catalog.match_line("config/values.yaml", "unrelated: true")


def test_context_entry_editor_creates_and_updates_local_dictionary(tmp_path: Path):
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


def test_context_targets_split_yaml_keys_values_and_filename_terms(tmp_path: Path):
    (tmp_path / ".config-review-context.yaml").write_text(
        """\
schemaVersion: 1
entries:
  - id: kubernetes-api-version
    title: apiVersion
    category: Kubernetes Resources
    summary: Identifies the resource API group and version.
    matches:
      - type: yaml-key
        value: apiVersion
  - id: flux-source-api-group
    title: Flux Source Toolkit API Group
    category: Flux / GitOps
    summary: API group for Flux source resources.
    matches:
      - type: term
        value: source.toolkit.fluxcd.io
  - id: kubernetes-api-v1
    title: API version v1
    category: Kubernetes Resources
    summary: Stable API version identifier.
    matches:
      - type: term
        value: v1
        files:
          - "*.yaml"
  - id: kubernetes-kind
    title: kind
    category: Kubernetes Resources
    summary: Identifies the resource type.
    matches:
      - type: yaml-key
        value: kind
  - id: flux-helm-repository
    title: HelmRepository
    category: Flux / GitOps
    summary: Flux resource that points to a Helm repository.
    matches:
      - type: yaml-value
        value: HelmRepository
      - type: term
        value: HelmRepository
      - type: term
        value: helm-repo
  - id: metadata-name
    title: metadata.name
    category: Kubernetes Resources
    summary: Resource name within its namespace.
    matches:
      - type: yaml-path
        value: metadata.name
  - id: yaml-format
    title: YAML
    category: Configuration Formats
    summary: Human-readable configuration format.
    matches:
      - type: term
        value: yaml
""",
        encoding="utf-8",
    )
    catalog = _catalog(tmp_path)

    api_targets = catalog.line_targets(
        "services/sample-helm-repo.yaml",
        "apiVersion: source.toolkit.fluxcd.io/v1",
        yaml_path="apiVersion",
    )
    api_by_text = {target["text"]: target for target in api_targets}
    assert api_by_text["apiVersion"]["contextRefs"] == ["kubernetes-api-version"]
    assert api_by_text["source.toolkit.fluxcd.io"]["contextRefs"] == ["flux-source-api-group"]
    assert api_by_text["v1"]["contextRefs"] == ["kubernetes-api-v1"]

    kind_targets = catalog.line_targets(
        "services/sample-helm-repo.yaml",
        "        kind: HelmRepository",
        yaml_path="spec.chart.spec.sourceRef.kind",
    )
    kind_by_text = {target["text"]: target for target in kind_targets}
    assert kind_by_text["kind"]["contextRefs"] == ["kubernetes-kind"]
    assert kind_by_text["HelmRepository"]["contextRefs"] == ["flux-helm-repository"]
    assert kind_by_text["kind"]["contextSuggestion"]["yamlPath"] == (
        "spec.chart.spec.sourceRef.kind"
    )

    name_targets = catalog.line_targets(
        "services/sample-helm-repo.yaml",
        "  name: sample-stack",
        yaml_path="metadata.name",
    )
    name_by_text = {target["text"]: target for target in name_targets}
    assert name_by_text["name"]["contextRefs"] == ["metadata-name"]
    assert name_by_text["sample"]["contextRefs"] == []
    assert name_by_text["stack"]["contextSuggestion"]["clickedType"] == ("YAML value term")

    filename_targets = catalog.path_part_targets(
        "services/sample-helm-repo.yaml",
        "sample-helm-repo.yaml",
        is_filename=True,
    )
    filename_by_text = {target["text"]: target for target in filename_targets}
    assert filename_by_text["helm-repo"]["contextRefs"] == ["flux-helm-repository"]
    assert filename_by_text["yaml"]["contextRefs"] == ["yaml-format"]


def test_yaml_path_discovery_handles_nested_mapping_keys():
    paths = yaml_paths_by_line(
        "spec:\n  chart:\n    spec:\n      sourceRef:\n        kind: HelmRepository\n"
    )

    assert paths[5] == "spec.chart.spec.sourceRef.kind"
