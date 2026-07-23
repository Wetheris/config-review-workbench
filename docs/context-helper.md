# Context Dictionary and Web Tooltips

The web diff viewer includes a searchable context dictionary for E-IDS services, shared
libraries, Helm packaging, GitLab CI/CD, GitOps, identity, selected FAA acronyms, and common
operational terms.

Select the `?` button in the web toolbar to turn context help on. Recognized YAML lines and
individual file-path segments receive a subtle hover indicator. Hover or keyboard-focus an item
for a short explanation. Select it to open the full dictionary entry.

The current file is shown as a segmented breadcrumb, for example:

```text
alpha → test-ot / ms / config / values.yaml
```

Each environment, directory, and file-name segment can have its own definition. This lets a new
reviewer learn what `alpha`, `ms`, `config`, or `values.yaml` means without treating the entire
path as one glossary term.

## Adding and editing definitions in the web viewer

When context help is enabled, an undocumented YAML key or path segment uses a dashed hover
indicator. Select it to open an **Add context definition** form with a suggested matching rule.

For an existing entry, open its dictionary details and select **Edit definition**. Built-in
entries use **Override definition** because bundled definitions are not modified directly. The
project-specific replacement is written to:

```text
.config-review-context.yaml
```

The editor supports definitions for path segments, file names, YAML keys and values, environment
variable names, commands, terms, and path patterns. Saving reloads the current browser snapshot
so the new matching rule is immediately applied. Unsaved reviewer notes are protected by a
confirmation prompt before that reload.

Context editing is unavailable in dry-run mode. Privacy mode turns context help off because
service names and architecture descriptions may themselves be sensitive in screenshots.

## Opening the dictionary

Turn on the `?` context-help button, then select any recognized item to open its entry. The dialog
supports full-text search across:

- Entry names
- Categories
- Summaries and longer details
- Aliases

The dictionary explains configuration; it does not decide whether a change is safe or
automatically hide a difference.

## Matching behavior

Matching is intentionally conservative. The implementation supports:

| Match type | Behavior |
|---|---|
| `term` | Matches a complete term while accepting spaces, hyphens, underscores, dots, or slashes between words |
| `yaml-key` | Matches the exact YAML key before `:` |
| `yaml-value` | Matches the complete scalar value after `:` |
| `env-name` | Matches a Kubernetes-style `- name: VALUE` environment variable declaration |
| `command` | Matches a command or literal pipeline expression inside a line |
| `file-name` | Matches an exact file name such as `Chart.yaml` or `values.yaml` |
| `path-segment` | Matches one directory or environment breadcrumb segment such as `alpha`, `ms`, or `config` |
| `path` | Matches a glob against the changed file's relative path |

Rules may include `files` globs. The built-in GitLab keywords use file restrictions so a generic
key such as `include` is documented in pipeline files without appearing on unrelated application
YAML.

Path-only definitions are intentionally not attached to every YAML line inside that path. They
appear only on the relevant breadcrumb segment, which keeps context highlighting precise.

The matcher does not alter the Focused Diff, Raw Diff, noise filters, or comparison engine.

## Adding project-specific entries manually

Definitions can also be maintained directly in `.config-review-context.yaml` beside the active
`.config-review.yaml`, or inside either selected comparison root:

```yaml
schemaVersion: 1

entries:
  - id: mission-support-directory
    title: MS — Mission Support
    category: Repository Layout
    summary: Contains Mission Support services and configuration.
    matches:
      - type: path-segment
        value: ms

  - id: swim-routing-destination
    title: SWIM routing destination
    category: Project Context
    summary: >
      Defines the internal messaging destination used by SWIM Relay.
    details: >
      An incorrect destination can prevent downstream services from receiving
      national SWIM updates.
    aliases:
      - swim topic
    matches:
      - type: yaml-key
        value: destination
        files:
          - "**/swim-relay/**"
```

A project-local entry with the same `id` as a built-in entry replaces the built-in definition.
Invalid local files do not prevent the viewer from opening. The built-in catalog remains
available, and the dictionary displays a diagnostic describing the invalid file.

## Catalog design guidance

Keep hover summaries short and operationally useful. A good entry answers:

1. What is this service, key, path segment, or tool?
2. What does it affect?
3. Why should a reviewer care when it changes?

Use longer `details` for dependencies, expected environment behavior, or promotion concerns.
Avoid putting passwords, tokens, internal credentials, or decrypted Secret values in context
files.
