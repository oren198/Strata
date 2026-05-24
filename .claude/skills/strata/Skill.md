---
name: strata
description: Onboarding entry skill for Strata — the shared-memory layer for agent fleets. Use this skill once at the start of a session to orient yourself; switch to strata-worker (to act as an agent at a scope) or strata-inspect (to browse memory) once you know where you want to go.
---

# Strata — first time?

Strata is a **shared memory system for fleets of agents**. The full theory
lives in `docs/philosophy.md`; the canonical vocabulary (23 terms) lives in
`CONTEXT.md`. Both are short and worth one read.

## What you do in this skill

1. **Read `CONTEXT.md`** if you haven't yet — the rest of Strata only makes
   sense in that vocabulary.
2. **Verify the backend is running**: call `strata_list_scopes`. If it
   errors with a connection refused, the user needs to start the backend
   (`strata start` in another terminal). Don't try to start it yourself.
3. **Show the user the fleet**: report the strata, scopes, and edges from
   `strata_list_scopes`. Explain which scopes the user can act *as*.
4. **Help the user pick a role**:
   - If they want to *act as an agent* and contribute memory at a specific
     scope, point them to the `strata-worker` skill. They start a new CC
     session with these env vars set, then invoke `/strata-worker`:
     ```
     STRATA_AGENT_SCOPE=<scope_id>
     STRATA_AGENT_SKILL=<a human-readable skill name, e.g. "architect">
     STRATA_AGENT_SESSION_ID=<any unique-per-session string>
     ```
   - If they want to *browse memory* without contributing, point them to
     `strata-inspect`. No env vars required.
   - If they want a visual view, the Strata Console is at
     `http://localhost:8000/` once the backend is running.
5. **Stop here.** This skill is the airport map, not the destination. Do
   not call `strata_contribute` from this skill — that's the worker's job.

## Available tools (read-only from this skill)

| Tool | Use |
|---|---|
| `strata_list_scopes()` | Show the fleet |
| `strata_read_scope_summary(scope_id)` | Peek at a scope's current state |
| `strata_read_perspective(scope_id)` | Same as summary in V1 |
| `strata_read_scope_record(scope_id)` | Forensic — every contribution + judgment |

## What you do NOT do here

- Do not contribute. Use `strata-worker` for that.
- Do not modify config. Bootstrap happens via `strata bootstrap` on the CLI.
- Do not assume which scope the user belongs at — ask, or list and let them
  pick.
