.PHONY: install test lint format run migrate bootstrap smoke

install:
	pip install -e ".[dev,cc-plugin]"

test:
	python -m pytest

lint:
	ruff check . && ruff format --check .

format:
	ruff format .

run:
	strata start --reload

migrate:
	python scripts/run_migrations.py

bootstrap:
	python scripts/bootstrap_fleet.py

smoke:
	python -m pytest tests/test_e2e_smoke.py -v -s
