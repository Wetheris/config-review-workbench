# Security and Code Quality Checks

The project now has the same basic checks locally, before a commit, and in GitLab CI.

## What runs

| Check | Purpose | Initial pipeline behavior |
|---|---|---|
| Python compilation | Catches syntax errors before execution | Blocks |
| Ruff lint | Catches undefined names, unused imports, and common correctness errors | Blocks |
| Ruff format check | Reports inconsistent formatting | Warning |
| pytest / embedded `--self-test` | Catches behavioral regressions | Blocks |
| Bandit | Finds common Python security mistakes | Warning |
| pip-audit | Checks runtime and build dependencies for known vulnerabilities | Warning |
| GitLab SAST | Performs GitLab's source security analysis | Reported by GitLab |
| GitLab Secret Detection | Detects committed credentials and private keys | Reported by GitLab |

The format and Python security jobs intentionally begin with `allow_failure: true`.
That prevents old formatting or existing findings from blocking every commit on day
one. After the initial findings are fixed or deliberately documented, remove
`allow_failure: true` from `ruff-format` and `python-security`.

## Local setup

From the project root. The project currently has no `requirements.txt`; the checks only require `requirements-dev.txt`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

Run everything:

```bash
python scripts/check_project.py all
```

Run one category:

```bash
python scripts/check_project.py quality
python scripts/check_project.py format
python scripts/check_project.py test
python scripts/check_project.py security
```

## Commit-time checks

Install the local hooks once:

```bash
pre-commit install
```

After that, Ruff and Python compilation run against changed Python files before each
commit. To run the hooks manually:

```bash
pre-commit run --all-files
```

The pre-commit configuration uses the tools installed in the active environment. This
avoids downloading separate hook environments, which is helpful on restricted networks.

## GitLab pipeline behavior

The pipeline runs for:

- normal branch commits;
- merge requests;
- tags.

Once a branch has an open merge request, the workflow prefers the merge-request
pipeline and suppresses the duplicate branch pipeline.

Generated `build/` and `dist/` content is excluded from source analysis. The source
files that create the `.pyz` remain scanned.

## One-time historic secret scan

Normal secret detection focuses on the commits relevant to the pipeline. Run one full
history scan after first enabling the pipeline:

1. Open **Build > Pipelines > New pipeline** in GitLab.
2. Add the variable `SECRET_DETECTION_HISTORIC_SCAN` with value `true`.
3. Run the pipeline.
4. Review any findings before deciding whether a credential must be revoked or a
   false positive needs an exception.

Do not enable the historic scan permanently; scanning the entire repository history on
every commit is unnecessarily expensive.

## Handling findings

Do not blindly suppress a finding. First determine whether it is:

1. a real defect that should be fixed;
2. safe code that needs a narrow inline explanation or tool-specific exclusion;
3. generated/vendor code that should be excluded at the directory level.

Keep exceptions as narrow as possible and include a comment explaining why the code is
safe.
