# ADR 0005 — Brownfield Install: Strata as a Foreign Project's Memory Layer

**Status:** Proposed
**Date:** 2026-05-31
**Related:** ADR 0001 (V1 architecture), ADR 0002 (fleet config source of
truth), ADR 0003 (`strata launch`), ADR 0004 (H2 foundations: embedded
mode, manager composition, lazy refresh)

---

## Context

Strata's purpose, per `docs/philosophy.md` and `CONTEXT.md`, is to be a
memory layer for an agent fleet. The intended consumer is a *foreign
project* — one that has its own codebase, runtime, venv, and
`.claude/settings.json`. Strata lives alongside it, not inside it.

V1.2, and the H2 foundations work that followed (ADR 0004), treated
this consumer as theoretical. To bind a Claude Code session in a
foreign project to a Strata scope today, the user must hand-edit
`.claude/settings.json` to:

1. Hardcode the absolute path to Strata's venv Python
   (e.g. `command: /.../Strata/.venv/bin/python`).
2. Set `PYTHONPATH=<Strata repo root>` because `mcp_server/` is not
   packaged — `pyproject.toml` ships `packages = ["src/strata"]` only.
3. Manually merge the MCP entry into any pre-existing `settings.json`.
4. Copy `.claude/skills/strata*` from the Strata repo into the foreign
   project so the slash commands appear.

All four bake host-filesystem assumptions into the consumer. None of
them survive Strata being moved, reinstalled, or upgraded.

**Embedded mode (ADR 0004 Decision 1) actually tightened this gap.**
Pre-embedded, the consumer could in theory point an HTTP client at any
`STRATA_BACKEND_URL`. Post-embedded, the MCP server `import`s the
`strata` package directly in the CC session's Python runtime. The
brownfield install surface is now harder, not easier.

## The bar

A new user with an existing project should run **two universal
commands** and end up with a working CC session bound to a scope,
honest provenance, slash commands available — without editing JSON or
knowing where Strata is installed:

```
pipx install strata          # one-time, system-wide
strata register              # from the project root, idempotent
```

---

## Decisions

### 1. Packaging: fold `mcp_server/` into `src/strata/`

Move `mcp_server/strata_mcp.py` to `src/strata/mcp/server.py`. Update
`pyproject.toml` to include the `strata.mcp` sub-package. Add a
console-script entry:

```toml
[project.scripts]
strata = "strata.__main__:main"
strata-mcp = "strata.mcp.server:main"
```

After this, `.claude/settings.json` needs only:

```json
"command": "strata-mcp"
```

— no Python path, no `PYTHONPATH`, no absolute paths. The shell
resolves `strata-mcp` via the user's PATH (where pipx installs it).

### 2. Per-project discovery via `.strata/config.toml`

Each foreign project gets `.strata/config.toml` at its root, written
by `strata register`. Contains paths to the project's local data
(relative, so the workspace is portable):

```toml
db = ".strata/strata.db"
fleet_yaml = ".strata/fleet.yaml"
summaries_dir = ".strata/summaries"
```

The bridge walks up from CWD looking for `.strata/config.toml`. The
file **does not reference Strata's install location**. Strata can be
moved, reinstalled, or upgraded without breaking the foreign project's
integration.

### 3. `STRATA_HOME` reserved for future global-server mode

In a future version, `STRATA_HOME` may point to a centralised Strata
workspace shared across multiple foreign projects (one fleet, many
consuming repos). V1.3 does **not** use this env var — discovery is
purely per-project via CWD walk-up. The name stays free.

### 4. `strata register [path]` — the registration subcommand

Defaults to cwd. Idempotent. Strictly additive (see Decision 6).

Concrete actions, in order:

- Detect the project root (`path` or cwd; fail if no clear marker
  like `.git/` or `pyproject.toml`).
- Create `.strata/` if absent. Create `.strata/config.toml` with
  default relative paths (Decision 2).
- Update `.gitignore` (append, with a `# Strata` marker block,
  idempotent): ignore `.strata/.venv/`, `.strata/strata.db*`,
  `.strata/summaries/`. Never `.strata/fleet.yaml` — that file is the
  org chart, must be committed.
- Seed `.strata/fleet.yaml` from a minimal template if absent. Skip
  if present.
- Copy canonical skills to `.claude/skills/strata*` **only if absent**
  (Decision 6).
- Merge the `strata` entry into `.claude/settings.json`'s `mcpServers`
  block **only if absent** (Decision 6).
- Print a clear next-steps message (set `STRATA_AGENT_SCOPE`, edit
  `fleet.yaml`, open `claude`).

`strata register --diff` is a read-only mode that shows the delta
between the user's skills/settings and the canonical, so the user can
decide what to copy manually.

### 5. Refuse-to-start provenance

The current MCP server defaults `STRATA_AGENT_SCOPE` (and SKILL,
SESSION_ID) to `"unknown"` at `mcp_server/strata_mcp.py:58`. This
silently writes contributions under a wrong identity — provenance
pollution that is irreversible once it's in the record.

Under the brownfield bar (honest provenance), this is a real bug. The
new bridge validates at startup:

1. `.strata/config.toml` is resolvable — else fail with the discovery
   path that was searched.
2. `STRATA_AGENT_SCOPE` env var is set — else fail with the canonical
   binding instructions.
3. The scope exists in `fleet.yaml` — else fail listing available
   scopes.
4. `STRATA_AGENT_SKILL` is in the scope's `permitted_skills` — else
   fail listing permitted skills.

Any failure → `sys.exit(1)` with an actionable message → CC surfaces
the unusable MCP server → user fixes config. The `"unknown"` defaults
are dropped entirely.

### 6. Strictly additive — never overwrite user state

Per architect directive: **we never delete or override users' skills
or settings.**

- Skills are copied at register time. If `.claude/skills/strata-worker/`
  already exists, register skips it and reports
  `"kept user's strata-worker"`.
- Settings entries are merged. If `mcpServers.strata` already exists,
  register skips it and reports `"kept user's strata mcpServer entry"`.
- `.strata/fleet.yaml` is seeded once; if it exists, register leaves
  it alone.
- `.gitignore` block is added once, marked with `# Strata` for
  idempotence (re-running doesn't duplicate).

To pick up newer canonical versions, the user runs `strata register
--diff` and manually copies the changes they want. The package never
silently rewrites user state. Trade-off: skill upgrades become a
manual step; honesty wins over convenience.

### 7. Install pattern: pipx canonical, `--bootstrap-venv` alternative

Bare `pip install strata` works but installs strata into whichever
venv is active — risk of polluting the foreign project's venv with
strata's transitive deps. Canonical for V1.3:

```
pipx install strata          # isolated 3.11+ env, strata-mcp on PATH
strata register              # from project root
```

pipx finds a Python ≥ 3.11 on the system, builds a dedicated venv
for strata, installs `strata-mcp` into `~/.local/bin/` (already on
PATH). The **foreign project's Python version is irrelevant** —
strata runs in its own pipx-managed environment.

For users with no global Python ≥ 3.11 available (locked-down
corporate systems, etc.), `strata register --bootstrap-venv`:

- Creates `.strata/.venv/` with strata installed inside.
- Writes `.claude/settings.json` to point at the absolute path
  `<project>/.strata/.venv/bin/strata-mcp`.

Less universal but works where pipx can't reach.

### 8. UI lifecycle: project-root only

`strata start` from inside the project root reads `.strata/config.toml`
(same discovery as the bridge) and serves the Console UI at
`http://127.0.0.1:8000/`. The UI is purely optional — a brownfield
user may never run it; the agent loop is complete without it.

`strata register` does **not** auto-start the UI. The user starts it
manually if/when they want to inspect.

---

## Alternatives Considered

- **Standalone slim `strata-mcp` PyPI package** (the bridge as its own
  release artifact). Rejected for V1.3: maintenance burden (two
  packages to version, ship, document) for marginal benefit (slim CI
  containers, polyglot environments). Revisit when a real user
  complains.
- **Symlinked skills** (always reflect the installed strata version).
  Rejected: `pipx upgrade strata` silently changing user-visible CC
  behaviour is a surprise-failure waiting to happen. The "we never
  override user state" directive forbids it.
- **`STRATA_HOME` for per-project discovery.** Rejected: the env var
  is reserved for future global-server mode; per-project CWD walk-up
  matches the per-project data layout.
- **Service discovery (mDNS, UDS sockets) for backend coordination.**
  Rejected: embedded mode has no backend to discover.
- **Curl-pipe-bash one-step installer.** Rejected for V1.3: pipx is
  the right answer for Python tools; non-Python install patterns
  (binary distribution via pyoxidizer/nuitka) are V2+ work.
- **Lower Strata's Python floor to 3.9** to broaden venv-sharing
  compatibility. Rejected: pipx isolation makes the floor irrelevant
  to consumers; lowering it would add backport burden (`tomli` for
  `tomllib`, etc.) for no real gain.

---

## Consequences

**Positive:**

- A foreign project with its own runtime can adopt Strata in two
  universal commands — no hand-editing of JSON, no PATH gymnastics,
  no knowing where Strata is installed.
- Strata's install location is never referenced in the foreign
  project's configuration. Strata can move, upgrade, or be
  reinstalled without breaking integration.
- Honest provenance enforced by default: misbound sessions can't
  pollute the record under `"unknown"`.
- The foreign project's Python version is independent of Strata's
  (pipx isolates strata).
- User state — existing skills, existing settings, existing
  `fleet.yaml` — is never silently rewritten.

**Negative:**

- Foreign projects must install pipx (or have Python ≥ 3.11 available)
  — small additional cognitive load vs. "just pip install."
- `--bootstrap-venv` mode adds ~100MB to a project (a full Python
  venv with strata's deps). Acceptable edge case.
- Multiple foreign projects = multiple independent Strata workspaces.
  Shared-fleet across-project use cases are deferred (`STRATA_HOME` /
  global server future work).
- Skill upgrades are a manual step (driven by `--diff`). Trade-off
  the user explicitly chose: never silently overwrite user state.

---

## Out of scope (deferred)

- **Global Strata server / `STRATA_HOME`** — multi-project shared
  workspaces. Future version; env var reserved here so we don't
  repurpose it.
- **`strata unregister`** — cleaning up the JSON merge and removing
  skills. Probably needed but deferred to a follow-up. Users can
  manually remove `mcpServers.strata` and `rm -rf .claude/skills/strata*`
  in the interim.
- **Auto-skill-upgrade UX** beyond `--diff`. The strict-additive
  principle precludes silent overwrites; if users demand it, we can
  add `--upgrade-skills` later with explicit per-skill opt-in.
- **Non-Python foreign-project install patterns.** pipx covers this
  implicitly today (strata-mcp on PATH regardless of project
  language). A slim standalone `strata-mcp` PyPI package may be
  warranted if usage demands.
- **`strata register --bootstrap-venv` cross-platform polish.** The
  initial implementation targets Linux/macOS; Windows nuances are a
  follow-up.

---

## Execution order

Three feature branches off `dev`, stacked sequentially:

1. **`feature/manager-refresh`** — closes ADR 0004 (Decisions 4 + 5
   — pre-session refresh hook, YAML-frontmatter version stamps,
   `STRATA_SUMMARY_MAX_WORDS` prompt parameter). Also folds in the
   non-blocker fixes from PR #29 review: multi-inter-stratum-edge
   load-time invariant, parent_summary wiring assertion test.
2. **`feature/preflight`** — cross-cutting prerequisite checks
   (Python ≥ 3.11, git available, `claude` CLI on PATH, write
   perms, port availability) for `strata start` and `strata
   register`.
3. **`feature/brownfield-install`** — this ADR's full
   implementation.

**V1.2.1** = branches 1 + 2 (closes ADR 0004, adds preflight
hygiene).
**V1.3** = branch 3 (ADR 0005 in full — the brownfield install).
