# Context Dictionary and Web Tooltips

The web diff viewer includes a searchable context dictionary for E-IDS services, shared
libraries, Helm packaging, GitLab CI/CD, GitOps, identity, selected FAA acronyms, and common
operational terms.

Select the `?` button in the web toolbar to turn context help on. When a rendered diff line
matches a documented term or YAML element, it receives a subtle hover indicator. Hover or
keyboard-focus the recognized line for a short explanation. Select the line to open the full
dictionary entry.

The first bundled catalog contains the service and platform definitions gathered during the
initial E-IDS review, including SWIM Relay, Keycloak, MSTL, Mission Support services, Helm
commands, Flux reconciliation, pipeline keywords, and related terminology.

## Opening the dictionary

Turn on the `?` context-help button, then select any recognized line to open its entry. The
dialog supports full-text search across:

- Entry names
- Categories
- Summaries and longer details
- Aliases

The dictionary remains read-only. It explains configuration; it does not decide whether a
change is safe or automatically hide a difference.

Privacy mode turns context help off and disables the `?` button because service names and
architectural descriptions may themselves be sensitive when screenshots or reports are shared.

## Matching behavior

Matching is intentionally conservative. The initial implementation supports:

| Match type | Behavior |
|---|---|
| `term` | Matches a complete term while accepting spaces, hyphens, underscores, dots, or slashes between words |
| `yaml-key` | Matches the exact YAML key before `:` |
| `yaml-value` | Matches the complete scalar value after `:` |
| `env-name` | Matches a Kubernetes-style `- name: VALUE` environment variable declaration |
| `command` | Matches a command or literal pipeline expression inside a line |
| `file-name` | Matches an exact file name such as `Chart.yaml` |
| `path` | Matches a glob against the changed file's relative path |

Rules may include `files` globs. The built-in GitLab keywords use file restrictions so a generic
key such as `include` is documented in pipeline files without appearing on unrelated application
YAML.

The matcher attaches context to displayed lines and logical change records. It does not alter the
Focused Diff, Raw Diff, noise filters, or comparison engine.

## Adding project-specific entries

Create `.config-review-context.yaml` beside the active `.config-review.yaml`, or inside either
selected comparison root:

```yaml
schemaVersion: 1

entries:
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

  - id: custom-environment-variable
    title: Custom service endpoint
    category: Project Context
    summary: Defines the upstream endpoint used by the service.
    matches:
      - type: env-name
        value: CUSTOM_SERVICE_URL
```

A project-local entry with the same `id` as a built-in entry replaces the built-in definition.
This allows the repository to provide a more precise local explanation without modifying the
workbench source.

Invalid local files do not prevent the web viewer from opening. The built-in catalog remains
available, and the dictionary dialog displays a diagnostic describing the invalid file.

## Catalog design guidance

Keep hover summaries short and operationally useful. A good entry answers:

1. What is this service, key, or tool?
2. What does it affect?
3. Why should a reviewer care when it changes?

Use longer `details` for dependencies, expected environment behavior, or promotion concerns.
Avoid putting passwords, tokens, internal credentials, or decrypted Secret values in context
files.
