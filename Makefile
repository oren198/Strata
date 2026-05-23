.PHONY: install test lint format run migrate

install:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check . && ruff format --check .

format:
	ruff format .

run:
	uvicorn strata.app:app --reload --port 8000

migrate:
	python scripts/run_migrations.py
