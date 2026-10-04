PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: help venv install test lint run

help:
	@echo "make install  - create a virtualenv (.venv) and editable-install with dev extras"
	@echo "make test     - run pytest in .venv"
	@echo "make lint     - ruff check in .venv"
	@echo "make run      - reakto --help"

venv:
	@test -x $(PY) || $(PYTHON) -m venv $(VENV)

install: venv
	$(PIP) install -q -e ".[dev]"

test: install
	$(PY) -m pytest -q

lint: install
	$(VENV)/bin/ruff check src tests

run: install
	$(PY) -m reakto --help
