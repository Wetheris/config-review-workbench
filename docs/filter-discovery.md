# Filter Discovery Logic

This document describes how Config Review Workbench discovers **candidate project
patterns** from repeated TEST/current to DEV/incoming replacements.

The discovery system is intentionally conservative. It produces suggestions for a
person to review; it does not decide that two values are equivalent, enable a rule,
or hide a change automatically.

## Design goals

Filter discovery is designed to:

- Find repeated environment-specific differences that otherwise obscure meaningful
  review work.
- Keep the discovery process deterministic and explainable.
- Operate on exact text differences even when a file is invalid YAML or contains
  templates.
- Avoid hiding versions, resources, security changes, additions, removals, and
  structural changes.
- Keep Full Diff completely unfiltered.

The implementation is line-oriented rather than a semantic YAML merge system.
YAML-like scalar parsing is used only to recognize conservative candidate shapes.

## High-level pipeline

The Pattern Manager follows this sequence:

1. Build an **unfiltered canonical diff** for every changed text file.
2. Exclude equal files, binary files, and files that could not be read.
3. Mark changes that must always remain reviewed.
4. Inspect eligible single-line replacements for repeated scalar substitutions.
5. Group equivalent substitutions into candidate signatures.
6. Apply minimum match and file-count thresholds.
7. Generate deterministic TEST and DEV regular-expression pairs.
8. Merge generated candidates with patterns already saved in
   `.config-review.yaml`.
9. Calculate examples, affected files, duplicate coverage, and overlap counts.
10. Present every new suggestion as `VISIBLE` until the user explicitly enables it.

The primary implementation entry point is
`discover_project_pattern_candidates()` in `src/config_review/core.py`.

## 1. Building the candidate input set

For each readable, non-binary changed file, the workbench calls the normal diff
engine with no project patterns enabled. This produces the same canonical
`ChangeBlock` objects used elsewhere by the application.

Discovery therefore operates on real diff blocks rather than independently pairing
arbitrary lines from the two trees.

Files are skipped when they are:

- Identical
- Binary
- Unreadable

## 2. Always-reviewed protection runs first

Before a block can become a pattern candidate, the normal diff pipeline assigns an
`always_review_reason()` when appropriate.

The following changes are protected:

- Insertions and deletions
- Multi-line or structural replacements
- Version, image, chart, digest, commit, SHA, revision, Git reference, and dependency
  changes
- Replica, CPU, memory, resource, request, limit, privilege, capability, and related
  security changes

Protected blocks are excluded from automatic candidate generation. Later, when
Focused Diff classifies blocks, enabled project patterns are not applied to any
block that has a protected reason.

Whitespace-only differences are handled separately by Display Filters and are not
considered an always-reviewed category.

## 3. Eligibility for automatic discovery

`_inferred_project_pattern_rules()` only examines blocks meeting all of these
conditions:

- The block is not protected by an always-reviewed rule.
- The diff tag is `replace`.
- TEST/current contains exactly one changed line.
- DEV/incoming contains exactly one changed line.
- Both lines can be conservatively parsed as either `key: scalar` or `- scalar`.
- Both sides use the same parsed key.
- Both values are non-empty and different.
- Neither value is longer than 500 characters.
- The parsed key does not match the sensitive-key exclusion expression.

The automatic sensitive-key exclusion currently recognizes names containing terms
such as:

```text
password, passwd, token, secret, api-key, private-key,
access-key, credential
```

This is a defense-in-depth heuristic, not a complete secret detector. See
[Security considerations](#security-considerations).

The conservative eligibility rules intentionally leave these changes visible:

- Insert-only or delete-only changes
- Multi-line replacements
- Mapping or list structure changes
- Different keys on the two sides
- Empty-value transitions
- Block scalars and other lines that do not look like simple scalar assignments

## 4. Scalar parsing

The lightweight parser recognizes two forms:

```yaml
key: value
```

```yaml
- value
```

It removes matching outer quotes and an unquoted trailing comment for discovery
purposes. It does not construct a YAML object model or claim that the file is valid
YAML.

List entries receive the internal key `<list-item>`. Exact-scalar candidates are not
generated for list entries, but URL, hostname, IP-shape, fragment, and environment
fragment candidates may still be considered when applicable.

## 5. Candidate families

One eligible replacement can contribute to several candidate families. Later steps
remove redundant candidates that cover exactly the same set of blocks.

### Environment fragments

The tool compares fragments from TEST/current and DEV/incoming and treats a pair as
an environment signal when either:

- The old fragment resembles the configured target directory name and the new
  fragment resembles the configured source directory name; or
- Both fragments are in the built-in environment vocabulary.

The built-in vocabulary currently includes:

```text
dev, deve, devw, development, test, test-ocp, test-ms,
sys-test, stage, staging, prod, production
```

Environment-fragment patterns intentionally span YAML keys. This allows one
approved `test` to `dev` relationship to cover repeated occurrences inside URLs,
namespaces, usernames, buckets, and other scalar values.

Category: **Environment identity**

### Application domains

When both values are URLs or hostnames, the tool looks for the `.apps.` marker and
extracts the domain beginning with `apps.`. Different TEST and DEV domains become a
project-wide literal-fragment candidate.

Examples of recognized shapes include:

```text
service.apps.test.example.org
service.apps.dev.example.org
```

Category: **Application domains**

### Broad URL shapes

When both values parse as URLs, the tool may suggest a broad rule for the same key.
The rule matches URL-shaped values rather than one exact endpoint.

Chained schemes such as the following are also recognized:

```text
jdbc:postgresql://database.example.org:5432/app
```

Because broad rules cover more possible values, they use the higher broad-pattern
threshold described below.

Category: **Endpoints**

### Broad hostname shapes

When both values are hostname-shaped, the tool may suggest a same-key hostname rule.
An optional port is supported.

Category: **Endpoints**

### Broad IPv4 shapes

When both values are IPv4-address-shaped, with an optional port, the tool may
suggest a same-key IPv4 rule.

Category: **Endpoints**

### Exact scalar replacements

For non-list scalar keys, the tool groups an exact repeated relationship:

```yaml
region: test-east
```

```yaml
region: dev-east
```

The generated expressions preserve the key and literal value while allowing normal
indentation, optional matching quotes, and a trailing comment.

The category is inferred from the key and value shape.

### Repeated scalar fragments

The tool uses `difflib.SequenceMatcher` in two passes:

1. Token-level comparison over alphanumeric tokens
2. Character-level fallback for longer substitutions within a token

A fragment pair is rejected when:

- Either alphanumeric signal is shorter than three characters
- Either side is numeric-only
- A character-level fragment exceeds 100 characters

At most three fragment pairs are retained from a single scalar replacement. Longer,
more specific pairs are preferred.

To prevent generic fragments from dominating the Pattern Manager, at most 12
generated candidates of kind `fragment` are retained project-wide. More specific
candidate families are ranked ahead of generic fragments.

## 6. Category assignment

Exact and fragment candidates are categorized using the scalar key and value shape.
The current categories are:

- **Environment identity** — namespace, cluster, region, environment, profile, site,
  location, or `env` keys
- **Application domains** — extracted `apps.` domain substitutions
- **Endpoints** — URLs, hostnames, IP addresses, and keys resembling URL, URI, host,
  endpoint, address, IP, or port
- **Users / references** — user, account, principal, client, service account,
  reference, ConfigMap, and similar keys
- **Storage / data** — bucket, database, schema, index, storage, S3, table, topic, and
  queue keys
- **Other repeated values** — candidates that do not match a more specific category

The categories organize review; they do not change matching behavior.

## 7. Qualification thresholds

The current constants are:

```text
MIN_PATTERN_MATCHES = 2
MIN_PATTERN_FILES = 2
BROAD_PATTERN_MIN_MATCHES = 4
```

A candidate qualifies when either:

- Its total match count reaches the candidate family's minimum; or
- It appears in at least two unique files

Therefore:

- Exact, fragment, domain, and environment candidates normally need two matches,
  unless two files establish the relationship first.
- Broad URL, hostname, and IP-shape candidates normally need four matches, unless
  they already occur in at least two files.

This rule favors project-wide repetition without requiring a high count in every
repository.

## 8. Regex generation

Generated literal values and keys are passed through `re.escape()` before they are
placed into regular expressions.

Depending on the candidate kind, a generated rule may require:

- The exact YAML-like key and exact literal value
- A literal fragment anywhere on the line
- A URL, hostname, or IPv4 shape under the same key
- A literal application-domain fragment anywhere on the line

Every project pattern consists of two expressions:

- `test_regex` for TEST/current lines
- `dev_regex` for DEV/incoming lines

A deterministic 20-character rule ID is derived from a SHA-256 digest of the rule
kind and both expressions. Generated rules are project-wide; automatic file scopes
are not used.

## 9. How a rule matches a diff block

`_pattern_matches_block()` applies these requirements:

- The block must be a replacement.
- The rule must apply to the file. Current generated and saved rules are
  project-wide.
- Both sides must contain at least one nonblank changed line.
- Every nonblank TEST/current line must match `test_regex`.
- Every nonblank DEV/incoming line must match `dev_regex`.

Although automatic discovery produces candidates from single-line replacements,
manually saved expressions are evaluated through this same block-level rule.

## 10. Merging saved and suggested rules

Patterns already stored in `.config-review.yaml` are loaded first. Generated
suggestions are then added by deterministic ID, so an existing saved rule takes
precedence over an equivalent newly generated suggestion.

Invalid saved regular expressions are skipped with a diagnostic rather than
crashing the workbench.

Saved patterns remain visible in the Pattern Manager even if they currently have no
matches. This makes stale project configuration auditable and removable.

## 11. Ordering, duplicate coverage, and overlap

Candidates are sorted deterministically by:

1. Category order
2. Candidate-kind priority
3. Number of affected files, descending
4. Number of matches, descending
5. Rule name

Kind priority favors:

1. Environment fragments
2. Application domains
3. Exact scalar replacements
4. Generic fragments
5. Broad URL, hostname, and IP shapes

After sorting, a generated suggestion is removed when it covers exactly the same set
of diff blocks as an earlier candidate. Saved rules are never removed by this
coverage deduplication.

`OVERLAP` is the number of blocks matched by the candidate that are also matched by
at least one other retained candidate. It is not the number of other rules.

A block remains filtered while any enabled matching rule still applies.

## 12. Example sampling

The preview stores up to ten examples for each candidate. It first chooses examples
from different files, then fills any remaining slots from additional matches.

Each example includes:

- Relative file path
- TEST and DEV line numbers
- The changed lines
- One line of context before and after each side when available

Examples are for human validation and do not participate in matching.

## 13. Enabling and persistence

Every new suggestion starts with:

```text
STATE = VISIBLE
enabled = false
```

Nothing is hidden until the user enables a pattern in the Pattern Manager or edits
the project configuration deliberately.

Enabled and disabled rules are saved under `patterns:` in `.config-review.yaml` with
their names, regex pairs, categories, kinds, and enabled state.

Pattern filtering affects Focused Diff only. Full Diff always renders the complete
literal text comparison.

## Security considerations

The discovery process excludes common sensitive key names, but the exclusion is
heuristic. A secret stored under an innocent-looking key could still appear in an
exact or fragment candidate.

Generated pattern names and regexes may contain literal configuration values. Since
patterns are persisted in `.config-review.yaml`:

- Preview candidates before enabling them.
- Inspect `.config-review.yaml` before committing it.
- Do not approve or commit a rule containing a credential, token, private value, or
  other restricted data.
- Prefer a manually generalized expression when an exact literal should not be
  persisted.

Session files follow a different design and do not store raw changed lines, but that
session guarantee does not make project pattern configuration secret-safe by
itself.

## Known limitations

- Discovery is based on text blocks and simple scalar syntax, not complete YAML
  semantics.
- Environment recognition uses directory names plus a fixed vocabulary.
- The `.apps.` application-domain detector is intentionally OpenShift-oriented.
- The sensitive-key expression cannot identify every secret.
- A repeated relationship can still be coincidental; match count is evidence, not
  proof.
- Broad shape candidates can cover more values than an exact relationship and need
  closer review.
- Pattern matching is line-oriented and may not suit block scalars or complex
  templating syntax.
- Generated patterns are project-wide; path-specific automatic rules are not
  currently produced.

These limitations are why suggestions remain visible until explicitly enabled and
why Full Diff is always available.

## Relevant implementation points

The main implementation is currently located in `src/config_review/core.py`:

- `discover_project_pattern_candidates()` — candidate assembly, matching, ordering,
  deduplication, examples, and overlap
- `_inferred_project_pattern_rules()` — candidate generation
- `_scalar_fragment_pairs()` — token and character fragment extraction
- `_candidate_qualifies()` — match/file thresholds
- `_category_for_scalar_pattern()` — category assignment
- `_pattern_matches_block()` — final block matching semantics
- `always_review_reason()` — protected-change classification
- `classify_block()` — applies enabled rules only when a block is not protected

When changing discovery logic, add regression tests for candidate counts, affected
files, generated regexes, overlap, always-reviewed protection, and Full Diff
visibility.
