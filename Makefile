PYTHON ?= python3

.PHONY: test build clean

test:
	PYTHONPATH=src $(PYTHON) -m config_review --self-test

build: test
	$(PYTHON) build.py
	$(PYTHON) dist/config-review.pyz --self-test

clean:
	rm -rf build *.egg-info src/*.egg-info src/config_review/__pycache__ tests/__pycache__
	rm -f dist/config-review.pyz
