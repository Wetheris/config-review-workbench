# Context Dictionary and Web Tooltips

The web diff viewer can load a searchable, project-local context dictionary for Kubernetes,
OpenShift, Helm, Flux, GitLab CI/CD, repository paths, services, and any other terms useful to a
reviewer.

No context catalog is bundled with the application. A fresh checkout starts with an empty
dictionary so internal project definitions are never accidentally distributed. A generic,
non-sensitive starter file is committed as:

```text
.config-review-context.example.yaml
```

Copy it to the ignored local filename to enable the examples:

```bash
cp .config-review-context.example.yaml .config-review-context.yaml
```

Select the `?` button in the web toolbar to turn context help on. Recognized YAML keys, value
terms, and individual file-path segments receive a subtle hover indicator. Hover or keyboard-focus
one highlighted term for a short explanation. Select it to open the full dictionary entry.

The current file is shown as a segmented breadcrumb, for example:

```text
dev → test / services / config / values.yaml
```

Each environment, directory, and file-name segment can have its own definition. Compound file
names are split into useful targets while known compound terms are kept together. For example,
`sample-helm-repo.yaml` can expose separate definitions for `sample`, `helm-repo`, and `yaml`.

## Adding and editing definitions in the web viewer

When context help is enabled, undocumented YAML keys, scalar-value terms, or path segments use a
dashed hover indicator. Select one to open an **Add context definition** form. The form shows the
clicked item type, exact value, file, and dotted YAML path when available.

For an existing entry, open its dictionary details and select **Edit definition**. Definitions are
written to:

```text
.config-review-context.yaml
```

The category field uses the existing dictionary categories as a dropdown and defaults to
**Project Context**. Select **Create new category…** when a new grouping is needed.

Definitions are global by default. Enable **Limit this definition to specific files or paths**
only when a generic key or value needs additional scope. The current comparison file is offered
as the default scope, and **Browse changed files…** can select another repository-relative file.
The field also accepts broader glob patterns such as `**/values.yaml` or `services/config/**`.

The editor supports definitions for path segments, file names, exact YAML paths, YAML keys and
values, environment variable names, commands, terms, and path patterns. Saving updates only the
context catalog and re-matches the visible file in place. The diff snapshot, reviewer notes,
reviewed state, and scroll position are preserved; a full comparison rebuild is not required.
Other files are re-matched lazily when they are opened.

Context editing is unavailable in dry-run mode. Privacy mode turns context help off because
service names and architecture descriptions may themselves be sensitive in screenshots.

## Catalog locations and merge order

The viewer checks these optional files in order:

1. `.config-review-context.yaml` beside the active `.config-review.yaml`
2. `.config-review-context.yaml` inside the selected source root
3. `.config-review-context.yaml` inside the selected target root

Later files replace earlier entries that use the same `id`. Missing files are treated as a valid
empty state. An invalid file does not prevent the viewer from opening; the dictionary displays a
diagnostic for that file and continues loading other valid local catalogs.

The root `.config-review-context.yaml` is ignored by Git. Keep internal or project-specific
entries there. The committed `.config-review-context.example.yaml` should contain only generic,
non-sensitive examples.

## Opening the dictionary

Turn on the `?` context-help button, then select any recognized item to open its entry. The dialog
supports full-text search across:

- Entry names
- Categories
- Summaries and longer details
- Aliases

The entry list and detail pane scroll independently. Selecting an entry shows its full
properties, matching rules, source file, and edit action on the right.

The dictionary explains configuration; it does not decide whether a change is safe or
automatically hide a difference.

## Matching behavior

Matching is intentionally conservative. The implementation supports:

| Match type | Behavior |
|---|---|
| `term` | Matches a complete term while accepting spaces, hyphens, underscores, dots, or slashes between words |
| `yaml-path` | Matches a dotted YAML path such as `spec.chart.spec.sourceRef.kind` |
| `yaml-key` | Matches the exact YAML key before `:` |
| `yaml-value` | Matches the complete scalar value after `:` |
| `env-name` | Matches a Kubernetes-style `- name: VALUE` environment variable declaration |
| `command` | Matches a command or literal pipeline expression inside a line |
| `file-name` | Matches an exact file name such as `Chart.yaml` or `values.yaml` |
| `path-segment` | Matches one directory or environment breadcrumb segment such as `dev`, `test`, or `config` |
| `path` | Matches a glob against the changed file's relative path |

Rules may include `files` globs. This lets a generic key such as `script` be documented in pipeline
files without appearing on unrelated application YAML.

A single line can contain multiple independent targets. Matching is applied in this order:

1. Exact YAML path
2. Exact YAML key or complete scalar value
3. Known compound term
4. Known individual term
5. Addable fallback token

For example, `apiVersion: source.toolkit.fluxcd.io/v1` can expose `apiVersion`,
`source.toolkit.fluxcd.io`, and `v1` separately. A broad entry highlights only its matching term
inside a URL or file name instead of claiming the entire string.

Path-only definitions are intentionally not attached to every YAML line inside that path. They
appear only on the relevant breadcrumb segment, which keeps context highlighting precise.

The matcher does not alter the Focused Diff, Raw Diff, noise filters, or comparison engine.

## Adding entries manually

Definitions can be maintained directly in `.config-review-context.yaml`:

```yaml
schemaVersion: 1

entries:
  - id: config-directory
    title: Configuration directory
    category: Repository Layout
    summary: Contains deployment or application configuration files.
    matches:
      - type: path-segment
        value: config

  - id: application-endpoint
    title: Application endpoint
    category: Project Context
    summary: Defines the URL used to reach an application dependency.
    matches:
      - type: yaml-key
        value: endpoint
        files:
          - "**/values.yaml"
```

## Catalog design guidance

Keep hover summaries short and operationally useful. A good entry answers:

1. What is this service, key, path segment, or tool?
2. What does it affect?
3. Why should a reviewer care when it changes?

Use longer `details` for dependencies, expected environment behavior, or promotion concerns.
Avoid putting passwords, tokens, internal credentials, or decrypted Secret values in context
files.
