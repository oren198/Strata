.PHONY: install test lint format run migrate bootstrap smoke

install:
	pip install -e ".[dev]"

test:
	python -m pytest

lint:
	ruff check . && ruff format --check .

format:
	ruff format .

run:
	uvicorn strata.app:app --reload --port 8000

migrate:
	python scripts/run_migrations.py

bootstrap:
	python scripts/bootstrap_fleet.py

smoke:
	python -m pytest tests/test_e2e_smoke.py -v -s
