.PHONY: install install-tinyshare dev test lint bootstrap reset-real worker paper-runner verify-broker

install:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e ".[dev]"

install-tinyshare:
	.venv/bin/python -m pip install tinyshare -i https://minidoc.pages.dev/simple/ --upgrade

dev:
	.venv/bin/uvicorn quantdev.api:app --app-dir backend --reload --host 127.0.0.1 --port 8000

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check backend tests

bootstrap:
	.venv/bin/python -m quantdev.cli bootstrap

reset-real:
	.venv/bin/python -m quantdev.cli reset-real

worker:
	.venv/bin/python -m quantdev.cli worker

paper-runner:
	.venv/bin/python -m quantdev.cli paper-runner

verify-broker:
	.venv/bin/python -m quantdev.cli verify-broker
