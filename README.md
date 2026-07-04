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

## How Strata works

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

**V1.2 shipped.** Local Python service with SQLite + markdown storage,
Anthropic-hosted scope-managers, FastAPI HTTP surface, file-canonical
`fleet.yaml` with in-memory mirror (ADR 0002), `strata launch` for
frictionless Claude Code session binding (ADR 0003), a read-only
browser-based Console, and a Claude Code MCP plugin + skills.

**V1.2.1 shipped** — H2 foundations per
[ADR 0004](docs/adr/0004-h2-foundations.md): embedded mode (the MCP
server operates directly on the record store; the FastAPI backend is the
UI layer only), real perspective composition (the agent's read walks
the inter-stratum ancestor chain), parent-aware scope-managers, and
lazy refresh + bounded summaries via a pre-session hook.

**V1.3 shipped** — brownfield install per
[ADR 0005](docs/adr/0005-brownfield-install.md): `strata register` for
two-command onboarding of any foreign project, per-project
`.strata/config.toml` discovery, `strata-mcp` console script (no more
Python-path gymnastics), skills vendored as package data, preflight
checks on `strata start` / `strata launch`, and honest provenance — the
MCP server refuses to start without a valid scope binding.

What comes next is captured in [`docs/ROADMAP.md`](docs/ROADMAP.md) — the
enduring design principles and the sequenced direction the project is
heading. See also the [Architecture decisions](#architecture-decisions)
section below for the ADRs already landed.

---

## Quick start

A first-time, copy-paste-able run. Five steps, ~5 minutes.

### 1. Prerequisites

- **Python 3.11 or newer.** Check: `python3 --version`. If your system Python is older, install 3.11+ via `pyenv`, your package manager, or [python.org](https://www.python.org/downloads/).
- **`make`** (usually preinstalled on macOS/Linux; `xcode-select --install` on macOS if missing).
- **An Anthropic API key.** Get one at <https://console.anthropic.com/>. It's only needed to make real scope-manager calls — the test suite mocks them, so you can run tests without it.

### 2. Clone and install

```bash
git clone https://github.com/oren198/Strata.git
cd Strata
make install        # editable install + dev extras
```

`make install` runs `pip install -e ".[dev]"`. If you prefer an isolated virtual environment first:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
make install
```

### 3. Set your API key

Either export it in your shell:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

…or create a `.env` file at the repo root (auto-loaded by the backend):

```
ANTHROPIC_API_KEY=sk-ant-...
```

Without a key you can still run `make test` and `make lint`, but live contributions will fail when the backend tries to call the model.

### 4. Start everything with one command

```bash
strata start
```

This (a) applies SQLite migrations to `./strata.db`, (b) **auto-seeds `fleet.yaml`** from the bundled dev-team starter template because no `fleet.yaml` exists yet, and (c) launches the FastAPI server. Per ADR 0002, the backend then reads `fleet.yaml` directly into an in-memory `FleetConfig` mirror — there is no separate "bootstrap into DB" step.

**Success looks like this:**

```
seeded fleet.yaml from the default template; edit to suit

Strata backend → http://127.0.0.1:8000
Strata Console → http://127.0.0.1:8000/

INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Now open <http://127.0.0.1:8000/> in your browser — you should see the Strata Console with three lanes (Executive / Function / Team) and four scope bubbles (CEO, Engineering, Architect, Backend Dev). Leave `strata start` running.

### 5. Make a contribution and watch memory update

In a **second terminal** (the first is busy serving):

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

Expected response (decision text may vary — the LLM judges):

```json
{
  "contribution_id": "c_xxxxxx",
  "judgment": {
    "decision": "accept_as_directive",
    "reasoning": "...",
    "summary_updated": true
  }
}
```

Then inspect the result:

```bash
strata summary g_arch        # see the new directive in the curated summary
cat summaries/g_arch.md      # same content as a markdown file
strata record g_arch         # full contribution + judgment log
```

The UI tab will reflect the change within ~5 seconds (it polls).

### Stopping

`Ctrl+C` in the terminal running `strata start`. State persists across restarts in `./strata.db` and `./summaries/`.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `strata: command not found` | You didn't run `make install`, or your venv isn't activated. Re-run `make install`. |
| `Address already in use` on port 8000 | Another process owns the port. Either stop it or run `strata start --port 8001`. |
| `strata scopes` says `Connection refused` | The backend isn't running. Start it with `strata start` in another terminal. |
| Contribution returns 500 with `scope_manager_failure` | Your `ANTHROPIC_API_KEY` is missing or invalid. Check step 3. |
| Want to start over with a fresh DB | `rm -f strata.db && rm -rf summaries/`, then `strata start` re-bootstraps. |

---

## Quick Start for an existing project

This section is for users who have **an existing project** and want to add Strata as its memory layer — without cloning this repo or touching their project's Python runtime.

Two universal commands, then you're ready:

```bash
pipx install strata          # install strata in an isolated env; puts strata-mcp on PATH
cd /path/to/your/project
strata register              # idempotent: creates .strata/, seeds fleet.yaml, wires Claude Code
```

> **Until Strata is published on PyPI**, install from the repo instead:
> `pipx install git+https://github.com/oren198/Strata.git` (or
> `pipx install /path/to/Strata` from a local clone). Everything below is
> identical either way.

### What `strata register` does

`strata register` is strictly additive — it never overwrites files you've already edited:

1. Creates `.strata/` directory and `config.toml` (relative paths, portable workspace).
2. Appends a `# Strata` block to `.gitignore` (ignores the DB and venv, never `fleet.yaml`).
3. Seeds `.strata/fleet.yaml` from a minimal template (1 scope, ready to edit).
4. Copies the `strata`, `strata-worker`, and `strata-inspect` skills to `.claude/skills/`.
5. Merges a `strata` entry into `.claude/settings.json`'s `mcpServers` block.

Run it again at any time — it skips everything that already exists and reports what it kept.

### After registration

```bash
# Edit your fleet to match your team
$EDITOR .strata/fleet.yaml

# Set your scope binding in the shell that opens Claude Code
export STRATA_AGENT_SCOPE=g_root       # scope ID from your fleet.yaml
export STRATA_AGENT_SKILL=strata-worker  # your role name

# Open Claude Code — the MCP server validates the binding at startup
claude
```

The MCP server starts with `strata-mcp` (on your PATH from pipx). It reads
`.strata/config.toml` automatically — no `STRATA_DB_PATH` or `STRATA_FLEET_CONFIG`
env vars needed. If binding is wrong (scope unknown, skill not permitted), the
server exits immediately with an actionable message.

### Checking for skill updates

After `pipx upgrade strata`, run:

```bash
strata register --diff       # shows what would change if you re-ran register
```

Review the diff and copy the pieces you want manually. Strata never silently
overwrites skills or settings you've already customised.

### No Python 3.11+ globally? Use `--bootstrap-venv`

If `pipx` can't find Python 3.11+ (locked-down corporate environment), use:

```bash
strata register --bootstrap-venv
```

This creates `.strata/.venv/` with strata installed, and updates `.claude/settings.json`
to point at the absolute venv path. The `.strata/.venv/` directory is gitignored
automatically. Note: this downloads ~100MB of Python deps.

---

## More commands

### Inspect memory from the terminal

```bash
strata scopes              # list the fleet's strata, scopes, edges
strata summary <scope_id>  # curated summary (directives + context)
strata record  <scope_id>  # every contribution + judgment in the scope's record
```

### Advanced subcommands

```bash
strata migrate                                  # apply pending SQLite migrations only
strata bootstrap --config fleet.example.yaml    # validate a fleet YAML (no DB writes)
strata start --reload                           # uvicorn auto-reload (dev mode)
strata start --port 8001                        # serve on a different port
```

The original `make` targets (`make migrate`, `make bootstrap`, `make run`, `make test`, `make lint`, `make smoke`) still work and are useful when hacking on Strata itself.

### `strata launch` — frictionless CC session binding (ADR 0003)

`strata launch [scope_id]` validates the target scope against `fleet.yaml`
directly (embedded mode — no backend required), resolves the skill from the
scope's declaration, generates a session ID, and `execvp`s `claude` with
`STRATA_AGENT_SCOPE`, `STRATA_AGENT_SKILL`, and `STRATA_AGENT_SESSION_ID`
already set. Run `strata start` only if you also want the Console UI.

```bash
strata launch g_arch                            # use default_skill from fleet.yaml
strata launch g_arch --skill evidence-summarizer  # override skill
strata launch g_arch --session my-sess          # override auto-generated session ID
strata launch                                   # pick from interactive list, or use .strata-role
```

#### `.strata-role` — per-project default binding

Place a `.strata-role` file at the root of a project repo so that
`strata launch` (with no positional argument) binds automatically:

```toml
scope = "g_arch"
skill = "code-writer"   # optional; resolved from fleet.yaml if omitted
```

The file is committed to git alongside the project. When you open the repo and
run `strata launch`, Strata finds the file, validates the scope, and launches
`claude` already bound — no manual `export` step needed.

### Upgrading from V1.1 to V1.2

V1.2 moves fleet configuration (strata, scopes, edges) out of SQLite and into a
file-canonical `fleet.yaml` (ADR 0002). Before upgrading, export your existing
fleet shape so it isn't lost when migration 0002 drops the SQL fleet tables:

1. **Upgrade code** — pull V1.2 (`git pull`, `make install`). The migration has not run yet.
2. **Export your fleet** — reads the still-present V1 tables and writes `fleet.yaml`:
   ```bash
   strata export-fleet          # writes ./fleet.yaml from ./strata.db
   # or specify paths explicitly:
   strata export-fleet --db /path/to/strata.db --out /path/to/fleet.yaml
   ```
3. **Start V1.2** — applies migration 0002 (drops the SQL fleet tables) and loads the exported config:
   ```bash
   strata start
   ```

`strata start` will refuse to proceed if you forget step 2: it detects a V1 fleet config in the DB with no `fleet.yaml` and exits with an actionable error pointing you back to `strata export-fleet`.

After step 3, edit `fleet.yaml` by hand to add per-scope skill declarations
(`default_skill`, `permitted_skills`) as needed for `strata launch` (ADR 0003).

### Strata Console UI

Open <http://127.0.0.1:8000/> while the backend is running — a read-only graph and list view of the current fleet state, polling every 5 s. All memory mutations flow through `strata.contribute`; the UI has no write path in V1. To point the UI at a non-default backend, edit the `<meta name="strata-api-base" content="...">` tag in `ui/index.html`.

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

### Per-project: `.strata/config.toml`

When `strata register` has been run, the project root contains
`.strata/config.toml` with relative storage paths:

```toml
db = ".strata/strata.db"
fleet_yaml = ".strata/fleet.yaml"
summaries_dir = ".strata/summaries"
```

The MCP server walks up from its current directory to find this file. When
present, it takes precedence over the env vars below — no shell exports needed
for storage paths.

### Environment variables

All settings are env-var driven, prefixed `STRATA_`. When `.strata/config.toml`
is present, the first three are ignored for the MCP server (project config wins):

| Variable | Default | Purpose |
|---|---|---|
| `STRATA_DB_PATH` | `./strata.db` | SQLite path for the record store (overridden by `config.toml`) |
| `STRATA_SUMMARIES_DIR` | `./summaries` | Directory for per-scope summary files (overridden by `config.toml`) |
| `STRATA_FLEET_CONFIG` | `./fleet.yaml` | Fleet YAML (overridden by `config.toml`) |
| `STRATA_AGENT_SCOPE` | (required) | The scope this session acts at — MCP server refuses to start if unset |
| `STRATA_AGENT_SKILL` | (required) | The skill identifier for provenance — MCP server refuses to start if unset |
| `STRATA_AGENT_SESSION_ID` | (auto) | Session identifier — auto-generated when absent |
| `STRATA_MANAGER_MODEL` | `claude-haiku-4-5` | Model used by scope-managers |
| `STRATA_ANTHROPIC_API_KEY` | (unset) | Optional; falls back to `ANTHROPIC_API_KEY` |
| `STRATA_BACKEND_URL` | `http://127.0.0.1:8000` | Read only by the CLI inspection commands (`scopes`/`summary`/`record`), which query the Console backend — the MCP server and `strata launch` never read it (ADR 0004 Decision 1; deprecation tracked in #52) |

A local `.env` file is loaded automatically.

---

## Project layout

```
README.md                # This file
CONTEXT.md               # Canonical glossary (23 terms — single source of vocabulary)
docs/
  philosophy.md          # Theoretical foundations — why Strata exists
  ROADMAP.md             # Enduring principles + sequenced direction (post-V1.2)
  adr/
    0001-v1-architecture.md
    0002-fleet-config-source-of-truth.md
    0003-strata-launch-cc-binding.md
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
src/strata/
  mcp/
    server.py            # FastMCP stdio server; operates directly on RecordStore + SummaryStore
  _skills/               # Canonical skill files vendored as package data
    strata/Skill.md      # CC skill: orientation / first-time use
    strata-worker/Skill.md  # CC skill: parametric worker — reads STRATA_AGENT_SCOPE/SKILL
    strata-inspect/Skill.md # CC skill: read-only browser
  _migrations/           # SQLite schema migrations (package data)
  _templates/            # Starter fleet.yaml templates (package data)
  project_config.py      # .strata/config.toml walk-up loader (ADR 0005 Decision 2)
.claude/
  skills/
    strata/              # CC skill (copy used in Strata-repo sessions)
    strata-worker/       # CC skill (copy used in Strata-repo sessions)
    strata-inspect/      # CC skill (copy used in Strata-repo sessions)
  settings.example.json  # Example MCP-server registration block (command: strata-mcp)
tests/                   # pytest suite
scripts/                 # CLI runners (run_migrations.py, bootstrap_fleet.py)
fleet.example.yaml       # Example fleet definition consumed by `make bootstrap`
Makefile                 # Common tasks (install / test / lint / run / migrate / bootstrap / smoke)
pyproject.toml           # Project metadata + deps + ruff/pytest config
```

---

## Running Strata in Claude Code

The MCP server operates directly on the SQLite record store and summary files
(ADR 0004 Decision 1, "embedded mode"). The FastAPI backend is the Console UI
layer; running `strata start` is required only to view the UI. The agent loop
— contributions, scope-manager judgments, perspective reads — works whether
the backend is up or down.

> **Entitlement-scoped reads (issue #48):** `strata_read_perspective`,
> `strata_read_scope_summary`, and `strata_read_scope_record` default to your
> bound scope (`STRATA_AGENT_SCOPE`) when called with no `scope_id`. An
> explicit `scope_id` is limited to your bound scope plus its inter-stratum
> ancestors — peer scopes are not directly readable; they reach you only
> through ratified content composed into your perspective (see issue #41).
> This supersedes the old HTTP-parity note for `strata_read_scope_record`:
> it now loads the fleet on every call to run this check, so reading your
> own scope's record while it has no rows still returns the empty record
> shape (`{"contributions": [], "judgments": []}`), but a scope outside your
> entitled surface raises instead of silently returning an empty record.

**For a foreign project**: use `strata register` (see
[Quick Start for an existing project](#quick-start-for-an-existing-project)
above). The steps below are for developing on Strata itself.

### 1. Start the backend (optional — Console UI only)

```bash
strata start
```

The backend is only required if you want the browser Console UI at
<http://127.0.0.1:8000/>. MCP tool calls work with or without it.

### 2. Register the MCP server in Claude Code

After running `strata register`, `.claude/settings.json` already contains the
correct `mcpServers.strata` entry. **This applies to the Strata repo itself
too**: the MCP server refuses to start without a discoverable
`.strata/config.toml` (ADR 0005 D5), so for developing on Strata run
`strata register` once from the repo root — it is strictly additive, and the
created `.strata/` workspace is gitignored. The settings entry it merges is:

```json
{
  "mcpServers": {
    "strata": {
      "command": "strata-mcp",
      "env": {}
    }
  }
}
```

Set `STRATA_AGENT_SCOPE` and `STRATA_AGENT_SKILL` in the shell before launching
`claude`. Storage paths are read from `.strata/config.toml`.

`STRATA_AGENT_SKILL` is a skill identifier recorded in provenance and
**validated against the scope's `permitted_skills`** in `fleet.yaml` (when
that list is set, the MCP server refuses to start on a mismatch). It does
not select a Claude Code skill file — **the same generic CC skill
(`strata-worker`) works for any role at any scope**.

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

# Terminal 2 — architect (skills must be permitted for the scope in fleet.yaml;
# the dev-team template permits code-writer + evidence-summarizer here)
STRATA_AGENT_SCOPE=g_arch     STRATA_AGENT_SKILL=code-writer   \
STRATA_AGENT_SESSION_ID=sess_arch  claude
# Then in the CC session:  /strata-worker

# Terminal 3 — backend developer
STRATA_AGENT_SCOPE=g_backend  STRATA_AGENT_SKILL=code-writer   \
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
context, alternatives, and consequences. The future direction —
principles plus the next horizons — is in [`docs/ROADMAP.md`](docs/ROADMAP.md).

Current ADRs:

- [0001 — V1 architecture](docs/adr/0001-v1-architecture.md): local Python
  backend, SQLite + markdown storage, Claude Code as the agent runtime,
  scope-manager hosted as backend-spawned Anthropic API calls.
- [0002 — Fleet config source of truth](docs/adr/0002-fleet-config-source-of-truth.md):
  `fleet.yaml` is canonical; SQLite holds only contributions and judgments;
  scope lifecycle (`active`/`archived`); per-scope skill declarations.
- [0003 — `strata launch` CC binding](docs/adr/0003-strata-launch-cc-binding.md):
  frictionless `(scope, skill, session_id)` binding via a single CLI command
  that validates, resolves, and `execvp`s `claude`.
- [0004 — H2 foundations](docs/adr/0004-h2-foundations.md): embedded mode
  (MCP server direct-store access), manager composition, lazy refresh, bounded
  summaries.
- [0005 — Brownfield install](docs/adr/0005-brownfield-install.md): `strata register`
  two-command onboarding, per-project `.strata/config.toml` discovery, `strata-mcp`
  console script, skills as package data, honest provenance enforcement.

---

## License

See [`LICENSE`](LICENSE).
