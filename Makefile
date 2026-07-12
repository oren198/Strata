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
	strata start --reload

migrate:
	python -m strata migrate

bootstrap:
	python -m strata bootstrap

smoke:
	python -m pytest tests/test_e2e_smoke.py -v -s
