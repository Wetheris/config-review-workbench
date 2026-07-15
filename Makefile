PYTHON ?= python3
CHECK_PATHS := src scripts tests

.DEFAULT_GOAL := help

.PHONY: help test self-test lint format security check build clean

help:
	@printf '%s\n' \
		'Config Review Workbench development commands:' \
		'  make test       Run the pytest test suite' \
		'  make self-test  Run the application regression suite from source' \
		'  make lint       Compile Python and run Ruff correctness checks' \
		'  make format     Apply safe Ruff fixes and format Python files' \
		'  make security   Run Bandit and dependency vulnerability checks' \
		'  make check      Run all read-only quality, test, and security checks' \
		'  make build      Test, build the .pyz, and test the packaged app' \
		'  make clean      Remove generated build and cache files'

test:
	$(PYTHON) -m pytest -q

self-test:
	PYTHONPATH=src $(PYTHON) -m config_review --self-test

lint:
	$(PYTHON) scripts/check_project.py quality

format:
	ruff check $(CHECK_PATHS) --fix
	ruff format $(CHECK_PATHS)

security:
	$(PYTHON) scripts/check_project.py security

check:
	$(PYTHON) scripts/check_project.py all

build: test self-test
	$(PYTHON) build.py
	$(PYTHON) dist/config-review.pyz --self-test

clean:
	rm -rf build .pytest_cache .ruff_cache
	find src scripts tests -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -maxdepth 2 -type d -name '*.egg-info' -prune -exec rm -rf {} +
	rm -f dist/config-review.pyz
