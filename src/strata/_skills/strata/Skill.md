---
name: strata
description: Onboarding entry skill for Strata — the shared-memory layer for agent fleets. Use this skill once at the start of a session to orient yourself; switch to strata-worker (to act as an agent at a scope) or strata-inspect (to browse memory) once you know where you want to go.
---

# Strata — first time?

Strata is a **shared memory system for fleets of agents**.

## Vocabulary (canonical — use these terms verbatim)

- **scope** — a bounded region of the fleet where memory attaches.
- **stratum** — a horizontal layer of scopes; lower ordinal = broader.
- **contribution** — any proposed write to a scope's memory; always
  appended to the scope's **record** (append-only, never edited).
- **scope-manager** — the agent that judges every contribution for its scope.
- **directive** — binding memory; flows down to descendant scopes.
- **context** — non-binding memory; informs but never binds.
- **scope summary** — the curated working view of one scope.
- **perspective** — composed view: own summary + inter-stratum ancestors',
  ordered root-first.
- **supersession** — a new directive replacing an old one by ID.

(In the Strata repo itself, `CONTEXT.md` has the full 23-term glossary and
`docs/philosophy.md` the theory — read them when present.)

## What you do in this skill

1. **Check the MCP tools work**: call `strata_list_scopes`. The tools are
   embedded — no backend process is involved. If the tool is unavailable,
   the Strata MCP server refused to start (its startup message names the
   fix — usually `STRATA_AGENT_SCOPE`/`STRATA_AGENT_SKILL` or a missing
   `.strata/config.toml`); relay that to the user.
2. **Show the user the fleet**: report the strata, scopes, and edges from
   `strata_list_scopes`. Explain which scopes the user can act *as*.
3. **Help the user pick a role**:
   - If they want to *act as an agent* and contribute memory at a specific
     scope, point them to the `strata-worker` skill. They start a new CC
     session with these env vars set, then invoke `/strata-worker`:
     ```
     STRATA_AGENT_SCOPE=<scope_id>
     STRATA_AGENT_SKILL=<a skill name permitted for that scope in fleet.yaml>
     STRATA_AGENT_SESSION_ID=<any unique-per-session string>
     ```
   - If they want to *browse memory* without contributing, point them to
     `strata-inspect` (same env vars — the MCP server validates the binding
     for every session, read-only or not).
   - If they want a visual view, the Strata Console is at
     `http://localhost:8000/` — the one thing that DOES need the backend
     (`strata start`).
4. **Stop here.** This skill is the airport map, not the destination. Do
   not call `strata_contribute` from this skill — that's the worker's job.

## Available tools (read-only from this skill)

Read tools default to your bound scope when called with no argument. An
explicit `scope_id` for `strata_read_scope_summary` reaches your bound scope,
its inter-stratum ancestors, and any peer scope referenced by a scope on that
chain (context only); `strata_read_scope_record` and `strata_read_perspective`'s
target stay chain-only (issue #48; ADR 0006 D3/D4).

| Tool | Use |
|---|---|
| `strata_list_scopes()` | Show the fleet |
| `strata_read_scope_summary(scope_id=None)` | Peek at a scope's current state |
| `strata_read_perspective(scope_id=None)` | Composed view: this scope's summary + every inter-stratum ancestor's summary, ordered root-first |
| `strata_read_scope_record(scope_id=None)` | Forensic — every contribution + judgment |

## What you do NOT do here

- Do not contribute. Use `strata-worker` for that.
- Do not modify config. `fleet.yaml` is the source of truth; the user edits
  it and can validate with `strata bootstrap`.
- Do not assume which scope the user belongs at — ask, or list and let them
  pick.
