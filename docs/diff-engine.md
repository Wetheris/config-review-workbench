# Diff Engine and Keyed YAML Lists

Config Review Workbench keeps a literal line diff as its source of truth. Focused
Diff may apply a conservative presentation layer to reduce misleading order noise,
but Full Diff always uses the original text alignment.

## Why named YAML lists need special handling

A normal line diff cannot know that these are the same logical list item:

```yaml
- name: SPRING_PROFILES_ACTIVE
  value: "prod"
```

```yaml
- name: SPRING_PROFILES_ACTIVE
  value: "prod,seed"
```

When that item moves around another `env` entry, a text algorithm can show one large
DEV insertion and one large TEST deletion. That output is textually valid, but it can
look like several variables were removed and recreated when only one value changed.
Git may choose a different anchor, but it has the same general limitation because it
also compares lines.

## Conservative keyed-list reconciliation

When **YAML order-only changes** is enabled, Focused Diff now recognizes a YAML
sequence only when all of the following are true:

1. The file parses successfully with `ruamel.yaml`.
2. Every item in that sequence is a mapping.
3. Every item has a scalar `name` field.
4. Each `name` is unique within that sequence.
5. The raw diff contains one complete TEST deletion and one complete DEV insertion.
6. Those two ranges contain exactly the same set of named items under the same parsed
   parent sequence.

If those checks pass, items are matched by `name` instead of position:

- Identical items that only moved are treated as order-only noise.
- Moved items whose content changed remain visible as one logical replacement.
- Merge actions still use exact TEST and DEV source line ranges.

For the reported example, Focused Diff becomes:

```text
SPRING_PROFILES_ACTIVE
TEST: value: "prod"
DEV:  value: "prod,seed"
```

The unchanged `SPRING_CONFIG_ADDITIONAL_LOCATION` move is omitted from Focused Diff.
It remains visible in Full Diff.

## Safety and fallback behavior

The reconciliation is intentionally all-or-nothing for each candidate range. It does
not guess when the YAML contains:

- duplicate `name` values;
- mixed scalar and mapping list items;
- partial list items in a diff hunk;
- different sets of names on the two sides;
- invalid YAML or template syntax that cannot be parsed;
- ambiguous parent structures.

Any such case falls back to the original literal diff.

When a logical replacement is applied, the workbench recomputes the current diff,
verifies that the selected TEST text still matches exactly, and then replaces only
that concrete TEST range with the corresponding DEV lines. The operation updates the
value without duplicating or moving unrelated entries.

## Comparing with Git

A useful manual sanity check is:

```bash
git diff --no-index -- test-values.yaml dev-values.yaml
```

Git and Python's `difflib` may choose different equal-line anchors, so their hunks do
not need to look identical. The important invariants are:

- Full Diff still exposes every raw changed line.
- Both raw views expose the real scalar transition.
- Focused Diff only collapses moves that passed the strict keyed-list checks above.
- Disabling **YAML order-only changes** restores the literal two-sided text diff.

Regression tests cover the raw fallback, the one-change focused result, duplicate-name
fallback, safe apply behavior, and a Git diff sanity check for the real scalar value.
