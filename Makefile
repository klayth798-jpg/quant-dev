.PHONY: install dev test lint bootstrap

install:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e ".[dev]"

dev:
	.venv/bin/uvicorn quantdev.api:app --app-dir backend --reload --host 127.0.0.1 --port 8000

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check backend tests

bootstrap:
	.venv/bin/python -m quantdev.cli bootstrap

