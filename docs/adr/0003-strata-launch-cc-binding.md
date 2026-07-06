# ADR 0003 — Frictionless CC Session Binding (`strata launch`)

**Status:** Accepted (implemented)
**Date:** 2026-05-27
**Related:** ADR 0002 (owns the per-scope skill declaration schema and
the scope lifecycle)

---

## Context

V1's CC plugin binds a Claude Code session to a Strata `(scope, skill)`
via three environment variables — `STRATA_AGENT_SCOPE`,
`STRATA_AGENT_SKILL`, `STRATA_AGENT_SESSION_ID` — exported by the user
before each `claude` launch.

Two failure modes follow:

1. **Typos go silent.** A misspelled `STRATA_AGENT_SCOPE` (e.g.
   `g_arc` vs `g_arch`) does not fail at launch — only when the first
   contribution lands at the backend and the scope-manager either
   rejects it or, worse, accepts it under the wrong scope. Provenance
   is permanently wrong; the record is polluted.
2. **It doesn't scale.** Two terminals are already awkward. A real
   fleet (4–8 concurrent roles) makes the manual env-var dance
   infeasible. Operators wearing different roles across the day will
   give up.

The CC plugin and skills work correctly once bindings are set; the
problem is purely at the launch surface.

## Decision

Add a new CLI subcommand: **`strata launch [scope_id]`**.

### Behaviour

1. **Validate scope against the live fleet** via the backend's
   `/scopes` endpoint. On miss, exit non-zero with the list of valid
   scope IDs. Backend unreachable → exit non-zero with the
   start-the-backend hint; do not proceed.
2. **Resolve skill from the scope's declaration in `fleet.yaml`.** The
   schema (`default_skill`, `permitted_skills`) and the full resolution
   table are owned by ADR 0002 § "Per-scope skill declaration." This
   ADR consumes the table verbatim; do not duplicate it here.

   **The skill is never inferred from the scope name.** Skills and
   scopes are orthogonal in CONTEXT.md — a skill is *what this agent
   does*, a scope is *where it sits*.
3. **Auto-generate session ID** as
   `sess_<scope>_<skill>_<YYYYMMDD-HHMMSS>` — the timestamp format is
   pinned (compact, unambiguous, lexicographically sortable). Example:

   ```
   sess_g_arch_code-writer_20260527-134215
   ```

   This makes the record self-documenting — reading provenance without
   joining tables still tells you which session was bound where, and
   sessions sort chronologically by ID alone. Override via `--session`.
4. **Set `STRATA_AGENT_*` env vars in the child environment** and
   `execvp` the `claude` binary. The CC process replaces the
   `strata launch` process so Ctrl-C, exit codes, and tty semantics are
   preserved.
5. **No positional arg → behaviour depends on context:**
   - If a `.strata-role` file exists in the current directory or any
     ancestor up to the repo root, use the declared `(scope, skill)`
     from it.
   - Otherwise, present an interactive picker listing scopes with
     `id | stratum | name | description` columns. Bare scope-ID lists
     are not useful when scope IDs are short hashes.
   - **Non-TTY behavior.** If `sys.stdin.isatty()` is false **and** the
     binding cannot be resolved unambiguously from the positional
     argument or a discovered `.strata-role` file, exit non-zero
     immediately with the same "valid scopes are …" error as the
     wrong-scope case. **Never block on stdin in non-interactive
     contexts** — CI, scripted invocations, and piped callers must
     fail fast, not hang on `input()`.

### `.strata-role` file

Per-project default binding. Lives in the project repo, committed to
git. TOML, one row:

```toml
scope = "g_arch"
skill = "code-writer"   # optional; resolves from fleet.yaml if omitted
```

Pairs with `strata launch` so that "I open this repo, I'm the
architect" is automatic. Does **not** conflict with `fleet.yaml` —
`.strata-role` declares one row of binding per project; `fleet.yaml`
declares fleet shape and per-scope skill declarations.

### Commitment to the binding model

`strata launch` is **the** canonical binding mechanism going forward.
Env-var-at-spawn is canon, not transitional.

The MCP-args alternative — where a session would rebind itself via a
tool call such as `/strata-worker g_arch architect` — is rejected
because it permits **mid-session rebinding**, which violates
CONTEXT.md § Agent:

> All three [session, skill, scope] are bound at spawn time and fixed
> for the agent's lifetime — the agent cannot change session, skill,
> or scope. To act differently, an agent spawns a sub-agent.

Spawn-time binding is the correct implementation of this rule, not a
workaround for missing MCP plumbing.

## Alternatives Considered

- **Per-role wrapper scripts (`bin/cc-arch.sh`, etc.).** One file per
  role; drifts from `fleet.yaml` as scopes change; doesn't validate.
  Rejected.
- **Per-role `.claude/settings.*.json` files** selected at launch.
  Depends on CC supporting alternate-settings selection; multiplies
  config files; same drift problem. Rejected.
- **MCP-args binding from within a CC session.** Cleanest end-state
  in principle, but enables mid-session rebinding, which violates the
  conceptual model. Rejected on correctness grounds, not blast radius.
- **Status quo (manual `export` before `claude`).** Doesn't validate;
  doesn't scale. Rejected.

## Consequences

- **Provenance integrity rises sharply.** Typos at the launch surface
  are caught before any contribution is written.
- **Onboarding compresses to one command.** `strata launch` in a
  `.strata-role`-bearing project repo Just Works.
- **`fleet.yaml` gains a per-scope skill declaration field** (new and
  additive; consumed by `strata launch`, owned by ADR 0002).
- **A `.strata-role` file convention is established** for per-project
  binding. Lives in the project repo, not the Strata install.
- **`STRATA_AGENT_*` env vars remain the on-the-wire contract**
  between CC and the strata-worker skill. `strata launch` is the
  ergonomic frontend; the skill is unchanged.
- **`strata launch` requires the backend to be reachable.** This is a
  feature (fail-fast validation), not a bug. Documented prominently.

### Out of scope (for follow-up)

- Rebinding within a running CC session (forbidden by the model).
- Multi-fleet support (one backend per launch is enough for V1.2).
- Discovery of multiple backends on a network.
