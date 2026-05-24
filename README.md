# Strata

**Shared memory for agent fleets** — a system that lets many agents read from
and contribute to a common, structured memory without any one of them
corrupting it.

A single agent rediscovers everything it needs. A fleet of agents working in
isolation rediscovers everything every time, in parallel. Strata is the
layer between them that lets a fleet's performance compound.

> Read [`docs/philosophy.md`](docs/philosophy.md) for the full theoretical
> grounding — the problem, why naive sharing fails, and the concepts the
> design rests on. Read [`CONTEXT.md`](CONTEXT.md) for the canonical
> vocabulary all code uses (23 terms, no synonyms).

---

## How Strata works (one paragraph)

Memory is organised into **scopes** arranged into ordered **strata**.
Agents are sessions running a **skill**, bound to one scope. Every write is
a **contribution** to the target scope's **scope-manager** — an LLM-driven
agent that judges the contribution as a binding *directive*, non-binding
*context*, or *declines* it. Each scope has two layers of memory: an
append-only **record** (audit trail) and a **scope summary** (the curated
working view). When an agent reads, it gets a **perspective**: a composed,
provenance-labelled view of its own scope summary plus inherited scopes up
the strata. Directives flow down; peer references carry context only.

The V1 architecture decision is documented in
[`docs/adr/0001-v1-architecture.md`](docs/adr/0001-v1-architecture.md).

---

## Status

**V1 backend is feature-complete.** Local Python service, SQLite + markdown
storage, Anthropic-hosted scope-managers, FastAPI HTTP surface, YAML fleet
bootstrap. CC plugin and Strata Console UI integration are next.

---

## Quick start

### Prerequisites

- Python 3.11+
- An Anthropic API key in `ANTHROPIC_API_KEY` (only needed to make real
  scope-manager calls; the test suite mocks them)

### Install

```bash
make install      # pip install -e ".[dev]"
```

### Bootstrap a fleet and run

```bash
make migrate                    # apply SQLite schema to ./strata.db
make bootstrap                  # apply fleet.example.yaml — 3 strata, 4 scopes, 4 edges
make run                        # uvicorn strata.app:app --reload --port 8000
```

In another shell:

```bash
# List the fleet
curl -s http://localhost:8000/scopes | jq

# Contribute to the architect scope
curl -s -X POST http://localhost:8000/contribute \
  -H "Content-Type: application/json" \
  -d '{
    "scope_id": "g_arch",
    "content": "all services use gRPC, not REST",
    "proposed_classification": "directive",
    "subject": "rpc-protocol",
    "supersedes": null,
    "contributor": {
      "scope_id": "g_arch",
      "skill": "architect",
      "session_id": "sess_demo",
      "ts": "2026-05-23T20:00:00Z"
    }
  }' | jq

# Read the scope summary
curl -s http://localhost:8000/scopes/g_arch/summary | jq
cat ./summaries/g_arch.md
```

### Run the tests

```bash
make test         # full suite (55 tests, scope-manager mocked)
make smoke        # end-to-end smoke (bootstrap → contribute → summary)
make lint         # ruff check + ruff format --check
```

To run the (skipped-by-default) integration test that hits the real
Anthropic API:

```bash
STRATA_RUN_INTEGRATION=1 ANTHROPIC_API_KEY=... pytest tests/test_scope_manager.py -v
```

---

## Configuration

All settings are env-var driven, prefixed `STRATA_`:

| Variable | Default | Purpose |
|---|---|---|
| `STRATA_DB_PATH` | `./strata.db` | SQLite path for the record store |
| `STRATA_SUMMARIES_DIR` | `./summaries` | Directory for per-scope markdown summary files |
| `STRATA_MANAGER_MODEL` | `claude-haiku-4-5` | Model used by scope-managers |
| `STRATA_ANTHROPIC_API_KEY` | (unset) | Optional; falls back to `ANTHROPIC_API_KEY` |
| `STRATA_FLEET_CONFIG` | `./fleet.yaml` | YAML config consumed by `make bootstrap` |

A local `.env` file is loaded automatically.

---

## Project layout

```
README.md                # This file
CONTEXT.md               # Canonical glossary (23 terms — single source of vocabulary)
docs/
  philosophy.md          # Theoretical foundations — why Strata exists
  adr/
    0001-v1-architecture.md
src/strata/              # Python backend package
  app.py                 # FastAPI app + endpoints
  settings.py            # pydantic-settings config
  record_store.py        # SQLite repository (append-only record + fleet config)
  summary_store.py       # Markdown on-disk scope summaries
  scope_manager.py       # LLM judgment layer (Anthropic tool use)
  bootstrap.py           # YAML fleet config loader/applier
tests/                   # pytest suite
migrations/              # SQLite schema migrations
scripts/                 # CLI runners (run_migrations.py, bootstrap_fleet.py)
fleet.example.yaml       # Example fleet definition consumed by `make bootstrap`
Makefile                 # Common tasks (install / test / lint / run / migrate / bootstrap / smoke)
pyproject.toml           # Project metadata + deps + ruff/pytest config
```

---

## Git workflow

- `main` — the last verified version of Strata.
- `dev` — the integration branch. All feature work merges here first.
- `feature/*` — branched from `dev`, merged back into `dev` via PR.
- Releases are PRs from `dev` → `main`.

---

## Architecture decisions

ADRs live under `docs/adr/`. Each captures a hard-to-reverse decision with
context, alternatives, and consequences.

Current ADRs:

- [0001 — V1 architecture](docs/adr/0001-v1-architecture.md): local Python
  backend, SQLite + markdown storage, Claude Code as the agent runtime,
  scope-manager hosted as backend-spawned Anthropic API calls.

---

## License

See [`LICENSE`](LICENSE).
