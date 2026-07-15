# Security and Code Quality Checks

The project uses the same core checks locally, before a commit, and in GitHub Actions.
The goal is to catch simple mistakes quickly while keeping the release package verified
against the source that produced it.

## What runs

| Check | Purpose | GitHub behavior |
|---|---|---|
| Python compilation | Catches syntax errors before execution | Blocks |
| Ruff lint | Catches undefined names, unused imports, and common correctness errors | Blocks |
| Ruff format check | Prevents unformatted Python from being merged | Blocks |
| pytest | Catches behavioral regressions in the source modules | Blocks on Python 3.10, 3.11, and 3.12 |
| Application `--self-test` | Exercises integrated source behavior | Blocks package creation |
| Packaged `.pyz` self-test | Verifies the distributed artifact starts and behaves correctly | Blocks |
| Bandit | Finds common Python security mistakes | Blocks on medium/high findings |
| pip-audit | Checks declared build/runtime dependencies for known vulnerabilities | Blocks |
| CodeQL | Performs GitHub-native static security analysis | Reported under **Security and quality** |
| GitHub secret scanning | Detects committed credentials and private keys | Configured in repository settings |
| Dependabot | Opens dependency and GitHub Actions update pull requests | Runs weekly |

All CI checks are blocking because the current project passes them. If a check fails,
fix or review the finding rather than making the job optional by default.

## Local setup

From the project root. The project currently has no `requirements.txt`; development and
CI tools are installed from `requirements-dev.txt`:

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

## Why the Makefile commands are separate

The Makefile is a convenience layer over the same tools used by the project and CI. It
does not introduce another testing system.

| Command | Why it exists |
|---|---|
| `make test` | Runs the fast pytest suite while developing behavior. |
| `make self-test` | Runs the application's built-in regression flow from source, exercising startup and integrated behavior. |
| `make lint` | Performs syntax compilation and Ruff correctness checks without changing files. |
| `make format` | Applies safe Ruff fixes and formatting. It is separate because it intentionally edits files. |
| `make security` | Scans repository-owned source with Bandit and audits declared build/runtime dependencies. |
| `make check` | Runs every read-only local gate before a commit or push. |
| `make build` | Runs tests, builds the `.pyz`, and runs the packaged self-test. This verifies the distributed artifact, not only the source tree. |
| `make clean` | Removes generated artifacts and project caches while leaving `.venv` and source files untouched. |

Keeping these responsibilities separate makes failures easier to understand. A test
failure means behavior changed, a lint failure means a code-quality problem exists, and
a packaged self-test failure means the build artifact differs from the working source.

The preferred local flow is:

```bash
make format
make check
make build
```

Review `git diff` after `make format`, because formatting and automatic fixes modify the
working tree.

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

## GitHub Actions behavior

The old `.gitlab-ci.yml` was removed because GitHub does not read GitLab pipeline files.
GitHub discovers workflows under `.github/workflows/`.

### CI workflow

`.github/workflows/ci.yml` runs on:

- every push, including tags;
- every pull request;
- manual runs from the **Actions** tab.

It creates separate jobs for quality, supported-Python tests, security, and package
validation. The package job only runs after the other jobs pass. Repeated runs for the
same branch cancel older in-progress CI runs so stale work does not waste runner time.

Open the repository's **Actions** tab to see each run and its logs.

### CodeQL workflow

`.github/workflows/codeql.yml` replaces the GitLab SAST template. It scans Python on:

- pushes to `main`;
- pull requests targeting `main`;
- a weekly schedule;
- manual runs.

Results appear under **Security and quality > Code scanning**. Do not also enable
CodeQL default setup while this advanced workflow exists; use one setup method or the
other.

### Secret scanning

Secret scanning is a GitHub repository security feature rather than a normal CI command.
Check it under:

1. **Settings**
2. **Security and analysis** or **Advanced Security**
3. Enable **Secret scanning** and **Push protection** when those options are available

Secret scanning checks committed content and push protection can stop supported secrets
before they are added to the repository.

### Dependabot

`.github/dependabot.yml` checks Python dependencies and GitHub Actions weekly. It opens
pull requests instead of modifying `main` directly. Dependabot alerts should also be
enabled under the repository's security settings so known vulnerable dependencies are
reported under **Security and quality**.

## Handling findings

Do not blindly suppress a finding. First determine whether it is:

1. a real defect that should be fixed;
2. safe code that needs a narrow inline explanation or tool-specific exclusion;
3. generated/vendor code that should be excluded at the directory level.

Keep exceptions as narrow as possible and include a comment explaining why the code is
safe.
