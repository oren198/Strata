# Contributing to Strata

Thanks for your interest in contributing! Strata is **shared memory for agent
fleets**. Before diving in, skim [`CONTEXT.md`](../CONTEXT.md) for the canonical
vocabulary (23 terms, no synonyms) and [`docs/philosophy.md`](../docs/philosophy.md)
for the design grounding — code review leans heavily on both.

## Ways to contribute

- **Bug reports & feature requests** — open an issue using the templates.
- **Pull requests** — bug fixes, docs, and tests are always welcome. For larger
  changes or anything that touches the architecture, please open an issue first
  so we can agree on the approach before you invest time. Architectural
  decisions live in [`docs/adr/`](../docs/adr); a substantial change usually
  warrants a new ADR.

## Development setup

Requires **Python 3.11+**.

```bash
git clone https://github.com/oren198/strata.git
cd strata
python3 -m venv .venv && source .venv/bin/activate   # recommended
make install          # pip install -e ".[dev]"
```

No Anthropic API key is needed for development — the test suite mocks all
scope-manager (LLM) calls.

## Before you open a PR

Run the same checks CI runs:

```bash
make lint    # ruff check . && ruff format --check .
make test    # python -m pytest
```

- `make format` auto-applies formatting.
- Integration tests that hit the real Anthropic API are skipped by default; set
  `STRATA_RUN_INTEGRATION=1` (and a key) to run them. The `slow`/bootstrap-venv
  tests are likewise gated behind `STRATA_RUN_BOOTSTRAP_VENV=1`.

## Pull request workflow

1. Fork the repo and create a topic branch off `main`
   (`feature/...`, `fix/...`, `docs/...`).
2. Keep changes focused; add or update tests for behavior changes.
3. Ensure `make lint` and `make test` pass.
4. Open a PR against `main` and fill out the template.

A maintainer reviews and merges every PR — direct pushes to `main` are not
accepted. CI (pytest + ruff) must be green before merge.

## Code style

- Follow the existing patterns in the file you're editing — match its naming,
  comment density, and idiom.
- Use the canonical vocabulary from `CONTEXT.md`; do not introduce synonyms.
- `ruff` (config in `pyproject.toml`) is the source of truth for lint/format.

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](../LICENSE).
