# Strata

**Shared memory for agent fleets** — a system that lets many agents read from
and contribute to a common, structured memory without any one of them
corrupting it.

A single agent rediscovers everything it needs. A fleet of agents working in
isolation rediscovers everything every time, in parallel. Strata is the
layer between them that lets a fleet's performance compound.

> Read [`docs/philosophy.md`](https://github.com/oren198/Strata/blob/main/docs/philosophy.md) for the full theoretical
> grounding — the problem, why naive sharing fails, and the concepts the
> design rests on. Read [`CONTEXT.md`](https://github.com/oren198/Strata/blob/main/CONTEXT.md) for the canonical
> vocabulary all code uses (23 terms, no synonyms).

---

## How Strata works

Memory is organised into **scopes** arranged into ordered **strata**.
Agents are running sessions bound to one scope; a skill is optional — a
session's identity is its scope and session ID, and a skill only adds a
declared role on top when one is set (issue #121 made skill-less bindings
first-class). Every write is a **contribution** to the target scope's
**scope-manager** — an LLM-driven
agent that judges the contribution as a binding *directive*, non-binding
*context*, or *declines* it. Each scope has two layers of memory: an
append-only **record** (audit trail) and a **scope summary** (the curated
working view). When an agent reads, it gets a **perspective**: a composed,
provenance-labelled view of its own scope summary plus inherited scopes up
the strata. Chain edges carry directives down and bind; reference edges
carry the referenced scope's publication across, and never bind.

The V1 architecture decision is documented in
[`docs/adr/0001-v1-architecture.md`](https://github.com/oren198/Strata/blob/main/docs/adr/0001-v1-architecture.md).

---

## Status

Strata is a local-first Python service: SQLite + markdown storage,
Anthropic-hosted scope-managers, a FastAPI HTTP surface plus an embedded
MCP mode that needs no backend running, file-canonical `fleet.yaml`, a
read-only browser Console, and Claude Code / Codex CLI integration via
`strata register` and `strata launch`. Two-command onboarding
(`pipx install strata-mem` + `strata register`) wires up any existing
project — skills, MCP server, and a freshness `Stop`-hook that nudges
sessions to write back before they end — and `strata doctor` diagnoses
the whole setup offline in one pass.

What comes next is captured in [`docs/ROADMAP.md`](https://github.com/oren198/Strata/blob/main/docs/ROADMAP.md) — the
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
| Anything looks broken and you're not sure why | Run `strata doctor` first — it checks config, DB, `fleet.yaml`, Claude Code wiring, and agent binding in one pass and names the fix for each failure. |
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
pipx install strata-mem    # install strata in an isolated env; puts strata + strata-mcp on PATH
cd /path/to/your/project
strata register              # idempotent: creates .strata/, seeds fleet.yaml, wires Claude Code
```

> **PyPI distribution name vs. import/CLI names.** The Strata engine is
> published to PyPI as **`strata-mem`** (the name `strata` was already taken
> by an unrelated, dormant package — see
> [issue #49](https://github.com/oren198/Strata/issues/49); the decision is
> [ADR 0009](https://github.com/oren198/Strata/blob/main/docs/adr/0009-packaging-engine-client-split.md)). Everything you
> actually type stays `strata`: `import strata` in Python, and the
> `strata` / `strata-mcp` console scripts on your PATH. Only the
> `pipx install` / `pip install` argument differs.

### What `strata register` does

`strata register` is strictly additive — it never overwrites files you've already edited:

1. Creates `.strata/` directory and `config.toml` (relative paths, portable workspace).
2. Appends a `# Strata` block to `.gitignore` (ignores the DB and venv, never `fleet.yaml`).
3. Seeds `.strata/fleet.yaml` from a minimal template (1 scope, ready to edit).
4. Copies the `strata`, `strata-worker`, and `strata-inspect` skills to `.claude/skills/`.
5. Merges a `strata` entry into `.claude/settings.json`'s `mcpServers` block.
6. Installs the freshness `Stop`-hook: copies `.claude/hooks/strata-stop-hook`
   and merges a `hooks.Stop` entry into `.claude/settings.json` (see
   [Memory-freshness Stop-hook](#memory-freshness-stop-hook)).

Run it again at any time — it skips everything that already exists and reports what it kept.
Every step is additive: your own `mcpServers`, `hooks`, skills, and `fleet.yaml` are never overwritten.

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

Something not working? Run `strata doctor` — it checks your project config,
DB, `fleet.yaml`, Claude Code wiring (MCP entry, Stop hook, skills), and
agent binding env vars in one pass, entirely offline (no backend needs to be
running), and tells you exactly what to fix.

### `.strata/config.toml` vs `.strata-role`

Two per-project files, two independent jobs:

- **`.strata/config.toml`** — storage paths (DB, fleet YAML, summaries dir).
  Created by `strata register`. Machine-oriented; says **where memory lives**.
- **`.strata-role`** — an optional default `(scope, skill)` binding for
  `strata launch` (see below). Created by hand, committed to git; says
  **who you are by default**.

Neither implies the other: you can have storage configured with no default
role (`strata launch` prompts interactively), or a role file pointing at a
scope that resolves storage from `config.toml` as usual.

### Checking for skill updates

After `pipx upgrade strata-mem`, run:

```bash
strata register --diff       # shows what would change if you re-ran register
```

Review the diff and copy the pieces you want manually. Strata never silently
overwrites skills or settings you've already customised.

### Memory-freshness Stop-hook

Reading fleet memory and never writing back lets a scope's memory quietly go
stale. `strata register` wires a Claude Code `Stop` hook that closes that loop
at each turn end. It is engine-owned (shipped as package data, installed like
the skills) and strictly additive — your own `Stop` hooks are left untouched.

**How it works.** At every turn end the hook reads the session's mechanical
read/contribute counters (the `.strata/sessions/` state files — no judge, no
memory write). When a session has read fleet memory a few times and recorded
nothing back, the *gate* opens. What happens then depends on the mode:

- **Default (background) mode.** The hook does **not** block your prompt. It
  spawns a detached, headless *evaluator* and returns immediately. The evaluator
  reads the session transcript tail and decides whether the session produced a
  memory-worthy outcome: if so it drafts a contribution and submits it through
  the **normal judged path** — the scope-manager gates admission exactly as it
  does for a contribution you write yourself; if not, it records a mechanical
  decline. Either outcome resets the session's counters, so you are nudged at
  most once per stale stretch, never per turn. The evaluator is best-effort:
  no `.strata` project, no session state, no API key, or any error all degrade
  to a silent no-op. It never writes memory without judgment — only the decline
  is mechanical.

- **Strict (blocking) mode** — opt in with `STRATA_FRESHNESS_STRICT=1`. Instead
  of spawning an evaluator, the hook blocks the stop **once** with a
  contribute-or-decline instruction fed back to the agent, then lets it proceed
  (it respects Claude Code's `stop_hook_active` flag, so it never loops). This
  is more insistent but interrupts interactive use, so it is off by default.

At most one evaluator runs per session at a time (a lockfile beside the session
state, with a stale-lock TTL), and the gate is always checked before spawning.

**Windows: session-state counters are not cross-process locked.** The MCP server
and the detached evaluator both read-modify-write the same `.strata/sessions/`
state file. On POSIX each update takes an advisory `fcntl.flock` on a
per-session `<session_id>.json.lock` file, so concurrent updates serialize and
no increment is lost. Windows has no `fcntl`, and Strata deliberately does not
substitute `msvcrt.locking` (it locks byte ranges and cannot wait on another
process, so emulating an advisory lock means a spin-and-retry loop — a wrong
lock is worse than a documented absence of one) and pulls in no dependency for
it. On Windows the update therefore runs unlocked: writes stay atomic, so a file
is never torn or corrupted, but two simultaneous updates can lose one
increment. Nothing judged or memory-bearing rides on these counters — they are
the mechanical substrate for the read-time nudge and this hook — so the worst
case is one nudge firing a turn early or a turn late.

**Environment variables:**

| Variable | Effect |
|---|---|
| `STRATA_FRESHNESS_STRICT` | `1` switches the hook to strict (blocking) mode. Unset/anything else = default background mode. |
| `STRATA_EVALUATOR_MODEL` | Overrides the evaluator's drafting model (default `claude-haiku-4-5-20251001`). The scope-manager that *judges* the draft is unaffected. |

**Non-Claude-Code harnesses.** The hook is a documented contract, not magic —
this is the mechanism's honest limit. Any harness that can run a command at
turn end can reproduce it:

1. At each turn boundary, run `strata freshness-hook`, passing a JSON object on
   stdin with at least `transcript_path` (path to the session transcript) and
   `stop_hook_active` (whether the stop was already blocked once this turn).
2. Ensure the session's identity env vars (`STRATA_AGENT_SCOPE`,
   `STRATA_AGENT_SKILL`, `STRATA_AGENT_SESSION_ID`) are set the same way the MCP
   server sees them — the hook keys the session state by `STRATA_AGENT_SESSION_ID`.
3. In default mode the command exits `0` and (when the gate is open) spawns the
   detached evaluator itself. In strict mode it prints a
   `{"decision":"block","reason":"…"}` JSON object on stdout that your harness
   must feed back to the agent and honour as a one-time block.

Harnesses that cannot run a turn-end command get none of this automatically —
the substrate (the `#110` counters, `strata_session_stats`, the read-time
nudge) still works, but the turn-boundary evaluator does not fire without a hook
to trigger it.

### No Python 3.11+ globally? Use `--bootstrap-venv`

If `pipx` can't find Python 3.11+ (locked-down corporate environment), use:

```bash
strata register --bootstrap-venv
```

This creates `.strata/.venv/` with strata installed, and updates `.claude/settings.json`
to point at the absolute venv path. The `.strata/.venv/` directory is gitignored
automatically. Note: this downloads ~100MB of Python deps.

### Using Strata with Codex CLI

```bash
strata register --harness codex
```

This does the same per-project setup as plain `strata register` (`.strata/`,
`fleet.yaml`, `.gitignore`), but instead of wiring `.claude/settings.json` it
merges Strata's config into the **OpenAI Codex CLI**'s own config file —
`$CODEX_HOME/config.toml`, which defaults to `~/.codex/config.toml`. That is a
user-level file, not a per-project one, matching how Codex's own `codex mcp
add` manages it. Like `strata register` for Claude Code, the merge is
strictly additive and idempotent: your existing `config.toml` — comments,
other `mcp_servers` entries, everything — is left untouched, and re-running
`strata register --harness codex` is a no-op.

**What this gives you, and how confident to be in each part:**

- **MCP config — verified; the live read → contribute → judged-verdict flow
  is not yet run.** Codex CLI's support for `[mcp_servers.<name>]` in
  `config.toml` is verified hands-on against codex-cli 0.149.0: `codex mcp
  add` round-trips through `config.toml` and back out through `codex mcp
  list` / `codex mcp get` byte-for-byte, and `strata register --harness
  codex` writes exactly that shape — confirmed against a real codex-cli
  0.149.0 binary, not just the docs. What that proves is that Codex's MCP
  client will find and launch `strata-mcp` with the configured env. It does
  **not** prove the full memory flow works, because no session with real
  OpenAI credentials has driven `strata-mcp`'s tools from inside Codex — that
  is item 1 in the live-verification checklist below.

  Two things to know before you rely on this:

  **Codex does not interpolate `${VAR}`-style values inside `config.toml`** —
  env values are literal TOML strings, not shell-expanded. So register ships
  the merged block with empty placeholders:

  ```toml
  [mcp_servers.strata.env]
  STRATA_AGENT_SCOPE = ""
  STRATA_AGENT_SKILL = ""
  STRATA_AGENT_SESSION_ID = ""
  ```

  Fill these in with real values before running `codex` (or edit them per
  project/session — this file is user-level, so if you work across multiple
  Strata projects with Codex you'll want to keep them current, or maintain a
  `<repo>/.codex/config.toml` override — Codex's docs list that as a read
  location for trusted projects, though `strata register --harness codex`
  itself only writes the global file today). **`STRATA_AGENT_SESSION_ID` is
  the sharpest edge here**: session state is keyed by it, so a fixed literal
  value would merge every Codex session's freshness counters into one — there
  is currently no verified mechanism for Codex to hand a fresh, per-session
  value into a literal `config.toml` string. Leave it blank (or accept that
  merged-counter behavior) until this is resolved. It is also unverified
  whether Codex's MCP subprocess additionally inherits the *launching*
  process's environment on top of these literal `env` values, or replaces it
  — if it inherits, a literal empty string here could shadow a real value you
  exported before running `codex`. Both are live-verification checklist
  items (2 and 4 below).

- **Turn-boundary freshness hook — pending live verification.** Register also
  merges a `[[hooks.Stop]]` block that runs `strata freshness-hook` at the end
  of each turn, following the same contract documented above under
  "Non-Claude-Code harnesses" (stdin JSON with `transcript_path` and
  `stop_hook_active`; the identity env vars set the same way the MCP server
  sees them). This is schema-verified only: `codex exec --strict-config`
  accepts the block without rejecting it, confirming codex-cli 0.149.0
  understands the shape — but no session with real OpenAI credentials has
  ever actually triggered it, so whether the hook process fires at all, and
  whether it inherits `STRATA_AGENT_*` from the Codex process it's spawned
  from, is **not confirmed**. Until an operator with real OpenAI credentials
  verifies this (checklist items 3 and 4 below), treat the turn-boundary
  nudge as absent for Codex — the MCP config above is independently useful
  without it.

  `strata unregister --harness codex` reverses this wiring the same way
  `strata unregister` reverses the Claude Code wiring — only when the
  `[mcp_servers.strata]` table and the hooks.Stop block still byte-match what
  register wrote; an edited block is reported and left in place.

```toml
# what strata register --harness codex merges into config.toml
[mcp_servers.strata]
command = "strata-mcp"

[mcp_servers.strata.env]
STRATA_AGENT_SCOPE = ""
STRATA_AGENT_SKILL = ""
STRATA_AGENT_SESSION_ID = ""

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "strata freshness-hook"
timeout = 30
```

**Live-verification checklist.** Everything above the line is either
verified against a real codex-cli 0.149.0 binary or clearly labelled as
schema-only. The gaps only real OpenAI credentials can close — run these, in
order, in a scratch project, if you're the first to turn this on for real:

1. **MCP end-to-end (read → contribute → judged verdict).** Run
   `strata register` then `strata register --harness codex` in a git repo,
   fill in `STRATA_AGENT_SCOPE` / `STRATA_AGENT_SKILL` in
   `~/.codex/config.toml` (leave `STRATA_AGENT_SESSION_ID` blank for now —
   see item 4), then start `codex` in that directory and ask it to read
   Strata's fleet memory and then contribute something back. **Go:** the read
   returns real scope memory and the contribution gets an admitted/declined
   verdict from the scope-manager (check `.strata/strata.db` or the
   contribution log, not just "the tool call didn't error"). **No-go:** the
   MCP tools don't appear, or errors on connect — check `codex mcp get
   strata` first for a config problem before assuming the memory flow itself
   is broken.
2. **Env overlay vs. replace.** Before running `codex`, `export
   STRATA_AGENT_SCOPE=canary-value` in your shell, but leave the
   `config.toml` entry as register's empty string. From inside Codex, have
   it call a tool that reveals what `strata-mcp` actually received for that
   var (temporarily log the server's received env on startup). **If empty:**
   the literal `env` table replaces the inherited environment — filling in
   literal values in `config.toml` is correct and sufficient, no further
   action needed. **If `canary-value`:** Codex overlays config `env` onto an
   inherited environment, so an empty-string literal *shadows* a real
   exported value — remove the placeholder keys from `config.toml` instead
   of leaving them blank, and rely on exporting the vars before launching
   `codex`.
3. **Stop hook fires at all.** Temporarily swap `command = "strata
   freshness-hook"` for a debug script that dumps its stdin and
   `os.environ` to a file, complete one real Codex turn end-to-end (a prompt
   that gets a real response and stops), then check the file. **Go:** the
   file exists, and its JSON contains `transcript_path` (pointing at a real,
   readable `.jsonl` rollout file matching the session id in the Codex
   banner) and `stop_hook_active`. **No-go:** no file at all — the hook
   never fired; treat the turn-boundary path as non-functional and keep it
   documented as schema-verified-only.
4. **Env inheritance in the hook subprocess.** Using the same debug capture
   from item 3, check whether `STRATA_AGENT_SCOPE` / `STRATA_AGENT_SKILL` /
   `STRATA_AGENT_SESSION_ID` (exported in the shell that launched `codex`)
   show up in the hook process's environment. **Go:** they're all present —
   export a real per-session `STRATA_AGENT_SESSION_ID` before each `codex`
   session and the freshness hook keys session state correctly. **No-go:**
   they're missing — there is no way to key session state correctly for this
   path yet; leave the merged Stop-hook block installed-but-inert (or remove
   it with `strata unregister --harness codex`) until a delivery mechanism
   exists.
5. **Write down the answer.** Whatever items 1–4 find, update this section
   (and `docs/marketing/CODEX-surface-2026-08.md` in the marketing repo, if
   you have access to it) so the "pending live verification" labels reflect
   reality instead of staying permanently hedged.

### Undoing it: `strata unregister`

`strata unregister` reverses register's wiring. Like register, it is strictly
conservative — it removes each artifact **only when it still matches what
register wrote**, and reports (leaving in place) anything you have since
edited:

```bash
strata unregister               # remove the wiring; keep your .strata/ memory
strata unregister --dry-run     # preview every action, write nothing
strata unregister --purge-data  # also delete .strata/ (fleet.yaml, DB, summaries)
```

What it does, step by step:

1. Removes the managed `# Strata` block from `.gitignore`, leaving every other
   line byte-for-byte unchanged. An edited block is reported and left.
2. Removes the `mcpServers.strata` entry from `.claude/settings.json`,
   preserving all your other keys. If you customised the entry, it is left in
   place and reported.
3. Removes each of the `strata`, `strata-worker`, and `strata-inspect` skills
   **only if byte-identical to the shipped version**. A modified or
   older-version skill is left alone and reported.
4. Removes the freshness `Stop`-hook — both the `hooks.Stop` entry from
   `.claude/settings.json` (only when it byte-matches what register wrote; your
   own `Stop` hooks are preserved) and the `.claude/hooks/strata-stop-hook`
   script (only when byte-identical to the shipped version).
5. Leaves your `.strata/` workspace untouched — that is memory, not wiring.
   Pass `--purge-data` to remove it too (`--dry-run --purge-data` previews the
   purge without deleting).

With `--harness codex` (matching `strata register --harness codex`), steps 2–4
above are replaced by the reverse of the Codex merge: the `[mcp_servers.strata]`
table and the freshness `hooks.Stop` block are removed from
`$CODEX_HOME/config.toml` only when each still byte-matches what register
wrote; `.claude/settings.json` is untouched. Steps 1 and 5 are unchanged.

**Exit code:** `0` on success, including when there is nothing to do (running
it on an unregistered project is a safe no-op). It exits `1` when something you
asked to remove was left in place because it had been edited — so scripts can
detect the partial case.

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
strata doctor                                    # diagnose config/DB/fleet/wiring/binding, offline
strata migrate                                  # apply pending SQLite migrations only
strata bootstrap --config path/to/fleet.yaml    # validate a fleet YAML (no DB writes)
strata start --reload                           # uvicorn auto-reload (dev mode)
strata start --port 8001                        # serve on a different port
```

The original `make` targets (`make migrate`, `make bootstrap`, `make run`, `make test`, `make lint`, `make smoke`) still work and are useful when hacking on Strata itself.

### `strata launch` — frictionless CC session binding (ADR 0003)

`strata launch [scope_id]` validates the target scope against `fleet.yaml`
directly (embedded mode — no backend required), resolves the skill from the
scope's declaration, generates a session ID, and hands the session off to
`claude` with `STRATA_AGENT_SCOPE`, `STRATA_AGENT_SKILL`, and
`STRATA_AGENT_SESSION_ID` already set. Run `strata start` only if you also want
the Console UI.

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

#### Platform notes

`strata launch` works on POSIX and Windows. On POSIX it `execvp`s `claude`, so
the launcher process is replaced outright. Windows has no real `exec`, so the
launcher resolves `claude` on `PATH` (including `.cmd`/`.exe` shims), spawns it
as a child sharing the console, and forwards its exit code. Ctrl-C reaches the
`claude` session in both cases, and the child's exit code becomes the exit code
of `strata launch`.

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

Open <http://127.0.0.1:8000/> while the backend is running — a graph and list
view of the current fleet state, polling every 5 s, plus four new tabs and an
in-place operator-correction surface described in
[`docs/console.md`](https://github.com/oren198/Strata/blob/main/docs/console.md) and the [Console](#console) section
below. Automatic memory writes (accept/decline)
still flow only through `strata.contribute`; the Console's own write path is
limited to the two in-person operator corrections (Replace / Retire a
directive), each behind a confirm dialog. To point the UI at a non-default
backend, edit the `<meta name="strata-api-base" content="...">` tag in
`src/strata/_ui/index.html`.

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

## Console

```bash
strata start
```

...then open <http://127.0.0.1:8000/ui/index.html> in a browser. The Console
is local-only — it talks to the backend `strata start` just launched on your
own machine, nothing external. Alongside the memory graph and settings, it
has four new tabs, plus in-place Replace/Retire actions in the scope drawer;
see [`docs/console.md`](https://github.com/oren198/Strata/blob/main/docs/console.md) for the full description of each:

- **Turned down** — every contribution the scope-manager refused for a
  scope, with the reason given, plus a separate mechanical count of sessions
  that read the scope and recorded nothing.
- **Freshness** — every active scope ranked by how many sessions have read
  it since anything new was accepted, with a fleet-wide breakdown of
  sessions that contributed, closed out with nothing to record, or read
  silently.
- **Record** — one scope's full append-only contribution record, newest
  first, in plain language.
- **View as** — exactly what an agent bound to a scope receives on a read,
  broken into layers with a rough token-weight estimate for each.
- **Operator corrections** — replace or retire one of a scope's own
  directives in person, each action behind a confirm dialog.

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
| `STRATA_AGENT_SKILL` | (optional) | The skill identifier for provenance — required only when the scope declares `default_skill`/`permitted_skills` in `fleet.yaml` (issue #121) |
| `STRATA_AGENT_SESSION_ID` | (auto) | Session identifier — auto-generated when absent |
| `STRATA_MANAGER_MODEL` | `claude-haiku-4-5` | Model used by scope-managers |
| `STRATA_ANTHROPIC_API_KEY` | (unset) | Optional; `ANTHROPIC_API_KEY` (bare, unprefixed) also works — either name can be set in the shell or in `.env`; the prefixed one wins if both are set |
| `STRATA_FRESHNESS_STRICT` | (unset) | `1` switches the freshness `Stop`-hook to strict (blocking) mode ([details](#memory-freshness-stop-hook)) |
| `STRATA_EVALUATOR_MODEL` | `claude-haiku-4-5-20251001` | Model the freshness evaluator drafts with (the judge is unaffected) |

A local `.env` file is loaded automatically.

> `STRATA_BACKEND_URL` was **removed in 1.5.0** (issue #52). The CLI
> inspection commands (`scopes` / `summary` / `record`) now read the record
> and summary stores directly, like every other embedded-mode consumer
> (ADR 0004 Decision 1) — no backend needs to be running.

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
  app.py                 # FastAPI app + endpoints (serves _ui/ at /ui)
  settings.py            # pydantic-settings config
  record_store.py        # SQLite repository (append-only record + fleet config)
  summary_store.py       # Markdown on-disk scope summaries
  scope_manager.py       # LLM judgment layer (Anthropic tool use)
  bootstrap.py           # YAML fleet config loader/applier
  mcp/
    server.py            # FastMCP stdio server; operates directly on RecordStore + SummaryStore
  _skills/               # Canonical skill files vendored as package data
    strata/Skill.md      # CC skill: orientation / first-time use
    strata-worker/Skill.md  # CC skill: parametric worker — reads STRATA_AGENT_SCOPE/SKILL
    strata-inspect/Skill.md # CC skill: read-only browser
  _migrations/           # SQLite schema migrations (package data)
  _templates/            # Starter fleet.yaml templates (package data)
  _ui/                   # Strata Console (package data; no build step — Babel-standalone)
    index.html           # Entry point; served at /ui/index.html
    app.jsx              # Root app, backend polling, read-only state
    atoms.jsx            # Shared UI atoms (Icon, Field, Toast, Modal …)
    graph.jsx            # Force-directed scope graph
    scope-detail.jsx     # Scope drill-in: backend summary + scope info
    settings.jsx         # Settings screen (display prefs + fleet read-only view)
    tweaks-panel.jsx     # Floating tweaks panel
    store.js             # API client (fetch /scopes, /scopes/{id}/summary)
    atlas.css            # Atlas design system tokens + component classes
  project_config.py      # .strata/config.toml walk-up loader (ADR 0005 Decision 2)
.claude/
  skills/
    strata/              # CC skill (copy used in Strata-repo sessions)
    strata-worker/       # CC skill (copy used in Strata-repo sessions)
    strata-inspect/      # CC skill (copy used in Strata-repo sessions)
  settings.example.json  # Example MCP-server registration block (command: strata-mcp)
tests/                   # pytest suite
src/strata/_templates/   # Bundled starter fleets (dev-team.yaml is the default seed;
                          #   minimal.yaml/research-group.yaml/support-org.yaml also ship)
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

> **Entitlement-scoped reads (issue #48; ADR 0006 D3/D4):**
> `strata_read_perspective`, `strata_read_scope_summary`, and
> `strata_read_scope_record` default to your bound scope
> (`STRATA_AGENT_SCOPE`) when called with no `scope_id`. An explicit
> `scope_id` for `strata_read_scope_summary` reaches your bound scope, its
> inter-stratum ancestors, and any scope referenced by a scope on that chain
> via a reference edge — at any stratum distance, per ADR 0010 (context
> surface); `strata_read_scope_record` and `strata_read_perspective`'s target
> stay chain-only — records audit the authority that binds you, and a
> perspective composes your own chain, not a peer's. `strata_read_perspective`
> itself composes those same chain-referenced scopes in as labelled,
> non-binding `peer_reference` layers (`binding: false`) alongside the chain's
> `self`/`ancestor` layers (`binding: true`) — a referenced scope's directives
> inform the reader but never bind them. Unreferenced scopes and descendants
> stay refused everywhere.
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

**Several terminals on one machine are safe, even without the backend
running.** Every `claude` session above talks to its own `strata-mcp`
process, and two of those processes — or a process and the optional Console
backend — can end up contributing to the same scope at the same time. Each
holds its own per-scope lock file under `.strata/.locks/` for the moment it
takes to append a contribution and, separately, for the moment it takes to
judge one — the OS enforces that only one process holds a given lock file at
a time, so two contributions to the same scope can never interleave and
leave the summary out of sync with the record (ADR 0012). Nothing extra to
start or configure: the lock files are created on demand next to your
`strata.db`, so this holds whether or not `strata start` is running.
(Windows: `strata-mcp` still serializes concurrent contributions inside one
process; across processes it does not — see ADR 0012.)

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
principles plus the next horizons — is in [`docs/ROADMAP.md`](https://github.com/oren198/Strata/blob/main/docs/ROADMAP.md).

Current ADRs:

- [0001 — V1 architecture](https://github.com/oren198/Strata/blob/main/docs/adr/0001-v1-architecture.md): local Python
  backend, SQLite + markdown storage, Claude Code as the agent runtime,
  scope-manager hosted as backend-spawned Anthropic API calls.
- [0002 — Fleet config source of truth](https://github.com/oren198/Strata/blob/main/docs/adr/0002-fleet-config-source-of-truth.md):
  `fleet.yaml` is canonical; SQLite holds only contributions and judgments;
  scope lifecycle (`active`/`archived`); per-scope skill declarations.
- [0003 — `strata launch` CC binding](https://github.com/oren198/Strata/blob/main/docs/adr/0003-strata-launch-cc-binding.md):
  frictionless `(scope, skill, session_id)` binding via a single CLI command
  that validates, resolves, and `execvp`s `claude`.
- [0004 — H2 foundations](https://github.com/oren198/Strata/blob/main/docs/adr/0004-h2-foundations.md): embedded mode
  (MCP server direct-store access), manager composition, lazy refresh, bounded
  summaries.
- [0005 — Brownfield install](https://github.com/oren198/Strata/blob/main/docs/adr/0005-brownfield-install.md): `strata register`
  two-command onboarding, per-project `.strata/config.toml` discovery, `strata-mcp`
  console script, skills as package data, honest provenance enforcement.

---

## License

See [`LICENSE`](https://github.com/oren198/Strata/blob/main/LICENSE).
