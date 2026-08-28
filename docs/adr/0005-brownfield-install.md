# ADR 0005 — Brownfield Install: Strata as a Foreign Project's Memory Layer

**Status:** Accepted (implemented)
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

> **Supersedes ADR 0004 D1's path** of `mcp_server/strata_mcp.py`.
> The module moves to `src/strata/mcp/server.py` so it ships as proper
> package data; the embedded-mode contract (no HTTP, in-process stores)
> is unchanged.

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
- **`.strata/` directory sanity check.** Before any action, check
  `.strata/`: if the directory exists but lacks a `config.toml`,
  fail with `"existing .strata/ directory at <path> does not look
  like a Strata workspace (no config.toml). Please remove or rename
  it before running strata register."` This prevents silently
  writing into a foreign tool's directory and prevents `register`
  from running against a half-initialised state from an interrupted
  prior register.
- Create `.strata/` if absent. Create `.strata/config.toml` with
  default relative paths (Decision 2).
- Update `.gitignore` (append, with a `# Strata` marker block,
  idempotent): ignore `.strata/.venv/`, `.strata/strata.db*`,
  `.strata/summaries/`. Never `.strata/fleet.yaml` — that file is the
  org chart, must be committed.
- Seed `.strata/fleet.yaml` from a **minimum-viable single-scope
  template** if absent (one stratum `L0`, one scope `g_root`, no
  edges; user edits in whatever they actually need). Brownfield
  projects don't want the dev-team example fleet — they need
  something neutral that fits their context. Skip if present.
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

**All four checks run independently; all failures are reported in a
single error message before exit.** A user with multiple missing
pieces sees the complete remediation list in one pass rather than
fix-one-rerun-fix-next. Checks are ordered 1 → 4 so the message
flows from "outermost setup gap" (no project config) to "innermost
binding mismatch" (skill not permitted).

Any failure → `sys.exit(1)` with the actionable message → CC surfaces
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
- **Stale-shape detection (V1.2 → V1.3 upgrade path).** If the
  existing `mcpServers.strata` entry matches a known-stale V1.2
  shape (`command: python` + `args: ["-m", "mcp_server.strata_mcp"]`
  or any `env.STRATA_BACKEND_URL`), register emits a clear warning:
  `"WARNING: your existing strata mcpServer entry is V1.2-shape and
  will silently fail on V1.3 (no mcp_server module; no
  STRATA_BACKEND_URL). The canonical V1.3 entry is: <one-liner>.
  Strata never overwrites your settings — run `strata register
  --diff` to see the canonical, then update by hand."` The
  strict-additive rule still holds: register does not overwrite.
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
corporate systems, etc.), `strata register --bootstrap-venv
[--python PATH]`:

- Creates `.strata/.venv/` with strata installed inside.
- Writes `.claude/settings.json` to point at the absolute path
  `<project>/.strata/.venv/bin/strata-mcp`.

**Python discovery.** If Strata itself was installed against a
Python ≥ 3.11, `--bootstrap-venv` uses `sys.executable` to seed the
new venv. Otherwise the user must pass `--python /path/to/python3.11+`
explicitly. Strata cannot create a 3.11 venv from a 3.10 interpreter,
so this requirement is surfaced before invoking `venv` with a clear
remediation message rather than failing inside the venv stdlib. The
typical user invoking `--bootstrap-venv` already has Strata installed
somehow, and that install used a Python ≥ 3.11, so `sys.executable`
is the right default; `--python` is the escape hatch for the
edge case.

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

---

## Amendment — soft-start: the refusal travels in tool results, not just stderr (2026-08-29)

**The incident.** Decision 5's refuse-to-start validation worked exactly as
designed and still failed the user: a fleet with 2+ scopes and an unbound
session (or an unknown scope, an impermissible skill, or a missing/invalid
fleet) printed the aggregated, actionable error to stderr and called
`sys.exit(1)`. Both Codex and Claude Code — the two harnesses this server
actually runs inside — swallow a failed MCP server's stderr entirely. The
human never sees "valid scopes are …"; they see only an opaque "MCP
handshake failed." Decision 5 assumed a human reads stderr. Running inside
an agent harness, nobody does.

**The fix.** The MCP server always completes the handshake now. Every
startup validation failure that used to call `sys.exit(1)` — unbound
multi-scope, unknown scope, impermissible skill, missing/invalid fleet
config, missing `.strata/config.toml` — is instead aggregated into a
startup-failure list, exactly as before, and stored on the running server.
`main()` no longer exits on a binding failure; `_validate_binding` returns
the list instead of calling `sys.exit`.

Every memory tool except `strata_bind` checks this state first
(`_require_bound_or_elicit`, `strata/mcp/server.py`): while unresolved, it
returns that same aggregated list as its error result, appended with the
recovery instructions that used to only reach stderr — which scopes exist,
and that `strata_bind(scope_id=...)` (or fixing the underlying env/config
and calling it again) resolves the session with no restart. An agent
working inside a harness that hides stderr now reads the refusal exactly
where it already reads every other tool result.

`strata_bind` — Decision 5's own runtime-rebind companion, added after this
ADR's initial acceptance — is untouched by this gate and is *the* recovery
path: it re-reads `fleet.yaml` through the same reloader every other call
uses, so a fleet fixed (or created) after startup is bindable immediately,
matching the "no restart required" property `strata_bind` already had.

**What still exits.** Storage initialisation failures (an unwritable
directory, a corrupt DB) still call `sys.exit(1)`. Decision 5's binding
checks are all recoverable via `strata_bind` once the server is running;
a storage layer that cannot be opened is not — there is no tool call that
fixes a corrupt SQLite file, so refusing to start is still the right
answer there. Single-scope auto-bind (Decision 5, checked before the
unset-scope failure) is unchanged: it never produced a failure to begin
with.

**Companion change (same session): elicitation.** When a tool is called
unbound and the fleet itself loads fine, the server now attempts one
server-initiated MCP elicitation first — offering the caller a pick of the
fleet's scopes — before falling back to the aggregated error above. See
`_attempt_elicit_bind` in `strata/mcp/server.py`. Tolerant by construction:
a client that lacks the elicitation capability, declines, cancels, or hits
a transport error falls back silently to the same error result a harness
without elicitation support gets. This is genuinely conditional on client
support — as of this addendum, the two harnesses this server ships for
(Codex, Claude Code) are not known to implement server-initiated MCP
elicitation, so in practice this path is dormant there today and the
aggregated error result (the fix above) is what actually reaches an agent
in either harness; the elicitation attempt is there for any client that
does support it, present or future, and costs nothing when it isn't.
