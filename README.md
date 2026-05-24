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

**V1 backend, Strata Console UI, and Claude Code plugin all in place.**
Local Python service with SQLite + markdown storage, Anthropic-hosted
scope-managers, FastAPI HTTP surface, YAML fleet bootstrap, a read-only
browser-based Console, and a Claude Code MCP plugin + skills.

---

## Quick start

### Prerequisites

- Python 3.11+
- An Anthropic API key in `ANTHROPIC_API_KEY` (needed for live
  scope-manager calls; the test suite mocks them)

### Install

```bash
make install      # pip install -e ".[dev,cc-plugin]"
```

### One command to run the system

```bash
strata start
```

That single command applies any pending SQLite migrations, auto-bootstraps
the fleet from `fleet.yaml` (or `fleet.example.yaml` if no local
`fleet.yaml` exists) when the DB is empty, and starts the FastAPI
backend on port 8000. The Strata Console UI is served from the same
process at <http://127.0.0.1:8000/>.

### Look at memory from the terminal

In a separate shell (while the backend is up):

```bash
strata scopes              # list the fleet's strata, scopes, edges
strata summary g_arch      # print a scope's curated summary (directives + context)
strata record g_arch       # print every contribution + judgment in the scope's record
```

### Make a contribution by hand

```bash
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
```

The contribution is judged by the scope-manager (an Anthropic API call)
and, on accept, the scope summary at `summaries/g_arch.md` updates.

### Advanced subcommands

`strata` also exposes the individual steps:

```bash
strata migrate                # apply pending SQLite migrations only
strata bootstrap --config fleet.example.yaml   # apply a YAML fleet config only
strata start --no-bootstrap   # skip auto-bootstrap on first run
strata start --reload         # uvicorn auto-reload (dev mode)
```

The original `make` targets (`make migrate`, `make bootstrap`, `make run`,
`make test`, `make lint`, `make smoke`) all still work and are useful when
hacking on Strata itself.

### Strata Console UI

After `make run`, open [http://localhost:8000/](http://localhost:8000/) in your
browser. The root URL redirects to the Strata Console, a read-only graph and
list view of the current fleet state.

The UI polls the backend every 5 seconds; there is no WebSocket in V1. All
memory mutations flow through `strata.contribute` — the UI has no write path
in V1.

To point the UI at a non-default backend, edit the
`<meta name="strata-api-base" content="...">` tag in `ui/index.html`.

### Run the tests

```bash
make test         # full suite (scope-manager mocked)
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
  app.py                 # FastAPI app + endpoints (serves ui/ at /ui)
  settings.py            # pydantic-settings config
  record_store.py        # SQLite repository (append-only record + fleet config)
  summary_store.py       # Markdown on-disk scope summaries
  scope_manager.py       # LLM judgment layer (Anthropic tool use)
  bootstrap.py           # YAML fleet config loader/applier
ui/                      # Strata Console (no build step — Babel-standalone in browser)
  index.html             # Entry point; served at /ui/index.html
  app.jsx                # Root app, backend polling, read-only state
  atoms.jsx              # Shared UI atoms (Icon, Field, Toast, Modal …)
  graph.jsx              # Force-directed scope graph
  scope-detail.jsx       # Scope drill-in: backend summary + scope info
  settings.jsx           # Settings screen (display prefs + fleet read-only view)
  tweaks-panel.jsx       # Floating tweaks panel
  store.js               # API client (fetch /scopes, /scopes/{id}/summary)
  atlas.css              # Atlas design system tokens + component classes
mcp_server/              # Claude Code plugin — MCP server proxying to the backend
  strata_mcp.py          # FastMCP stdio server exposing tools to CC sessions
.claude/
  skills/
    strata/              # CC skill: orientation / first-time use
    strata-worker/       # CC skill: parametric worker — reads STRATA_AGENT_SCOPE/SKILL
    strata-inspect/      # CC skill: read-only browser
  settings.example.json  # Example MCP-server registration block
tests/                   # pytest suite
migrations/              # SQLite schema migrations
scripts/                 # CLI runners (run_migrations.py, bootstrap_fleet.py)
fleet.example.yaml       # Example fleet definition consumed by `make bootstrap`
Makefile                 # Common tasks (install / test / lint / run / migrate / bootstrap / smoke)
pyproject.toml           # Project metadata + deps + ruff/pytest config
```

---

## Running Strata in Claude Code

### 1. Start the backend (once, in its own terminal)

```bash
strata start
```

### 2. Register the MCP server in Claude Code

Copy `.claude/settings.example.json` to `.claude/settings.json` (or merge
the `mcpServers` block into your existing settings). The env vars in
that block identify the **scope this CC session acts at** and the
**role identifier** for provenance — change them per session.

```json
{
  "mcpServers": {
    "strata": {
      "command": "python",
      "args": ["-m", "mcp_server.strata_mcp"],
      "env": {
        "STRATA_BACKEND_URL": "http://127.0.0.1:8000",
        "STRATA_AGENT_SCOPE":       "g_arch",
        "STRATA_AGENT_SKILL":       "architect",
        "STRATA_AGENT_SESSION_ID":  "sess_local"
      }
    }
  }
}
```

`STRATA_AGENT_SKILL` is just a human-readable role tag (`architect`,
`developer`, `security_reviewer`, etc.) — it shows up in provenance, but
**the same generic CC skill (`strata-worker`) works for any role at any
scope**. You don't need a separate Claude Code skill file per role.

### 3. Invoke a skill

The repo ships three CC skills under `.claude/skills/`:

| Skill | What it does |
|---|---|
| `/strata` | First-time orientation: shows the fleet, helps you pick a role, points you to the next skill. Use once. |
| `/strata-worker` | Binds the current CC session as a worker at `STRATA_AGENT_SCOPE`. Reads the perspective, contributes observations as `context`, contributes decisions as `directive`, cites memory back to you. **The main skill you'll use.** |
| `/strata-inspect` | Read-only browser. Use when you want to look around without acting. |

### 4. Worked example (multi-session)

Three terminals, three different roles, one shared Strata:

```bash
# Terminal 1 — backend
strata start

# Terminal 2 — architect (set env vars before launching `claude`)
STRATA_AGENT_SCOPE=g_arch     STRATA_AGENT_SKILL=architect     \
STRATA_AGENT_SESSION_ID=sess_arch  claude
# Then in the CC session:  /strata-worker

# Terminal 3 — backend developer
STRATA_AGENT_SCOPE=g_backend  STRATA_AGENT_SKILL=backend_dev   \
STRATA_AGENT_SESSION_ID=sess_dev   claude
# Then in the CC session:  /strata-worker
```

Each session contributes to the same backend. The developer captures
implementation patterns as `context`; the architect ratifies recurring
patterns into `directive`s that bind everyone below. Watch the state
evolve in <http://127.0.0.1:8000/> (the Console UI) or run `strata
summary g_arch` from a fourth terminal.

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
