# ADR 0002 — Fleet Config Source of Truth

**Status:** Proposed
**Date:** 2026-05-27

---

## Context

In V1, fleet configuration (strata, scopes, edges) is authored as
`fleet.yaml` and read **once** by `bootstrap.py` to seed SQLite tables
(`strata`, `scopes`, `edges`). After bootstrap, SQLite is the operational
source of truth — UI reads, the `/scopes` endpoint, scope-manager
lookups, and the perspective walker all consult the DB. The YAML is then
effectively abandoned.

This works in V1 because nothing else writes fleet config. V1.2 changes
that:

- **UI write endpoints** (open work item) introduce a second writer that
  mutates fleet state at runtime.
- **Per-scope skill declarations** are needed by `strata launch`
  (ADR 0003) to validate and bind sessions. They must live somewhere
  canonical.
- **Reproducibility expectations** rise as Strata is run against real
  fleets — "what was the fleet shape last Tuesday?" should be answerable
  from git history.

With two writers and no canonical source, reconciliation between
`fleet.yaml` and SQLite becomes ambiguous: hand-edits during runtime go
stale; UI writes don't surface in version control; `strata bootstrap`
re-runs become non-idempotent depending on schema drift.

## Decision

**Fleet configuration is file-canonical.** `fleet.yaml` is the single
source of truth; SQLite no longer stores fleet config at all.

Specifically:

1. **`fleet.yaml` holds all fleet config** — strata, scopes, edges, and
   per-scope declarations consumed by other ADRs (e.g. default skill —
   see ADR 0003).
2. **The backend materializes `fleet.yaml` to an in-memory `FleetConfig`
   object on startup.** All reads (the `/scopes` endpoint, scope-manager
   lookups, perspective walks) serve from this in-memory mirror.
3. **SQLite holds only the record** — `contributions` and `judgments`.
   The `strata`, `scopes`, and `edges` tables are removed in a new
   migration.
4. **`scope_id` integrity is enforced at write time** against the
   in-memory `FleetConfig`, not via SQL foreign keys. A contribution
   targeting an unknown scope is rejected with a clear error.
5. **All mutations are serialized through the backend.** UI writes go
   via HTTP; the backend mutates the parsed YAML, writes atomically
   (`*.tmp` + `os.replace`), and refreshes the in-memory mirror. An
   in-process Python lock is sufficient — FastAPI runs single-process in
   V1, so no OS-level file lock is needed.
6. **`strata bootstrap` is removed.** Starting a new fleet means
   authoring `fleet.yaml` directly. The repo ships starter templates
   (`dev-team`, `support-org`, `research-group`) the user can copy.

### Open choices (deliberately deferred)

- **YAML library.** PyYAML is sufficient for V1.2. Programmatic
  mutations will not preserve comments through round-trips. If users
  start heavily annotating `fleet.yaml`, swap to `ruamel.yaml` in a
  follow-up — additive change, no schema impact.
- **Hot reload on disk change.** Not in V1.2. The contract is "edit
  through the UI, or stop the backend, edit, restart." A mtime watcher
  for live reload is an obvious follow-up if the friction is real.

## Alternatives Considered

- **DB-canonical, YAML as seed + export (Path B).** Simpler today —
  single writer (FastAPI), no comment preservation. But reproducibility
  requires a discipline (`strata export` after every change) that will
  be forgotten in practice. Drift between environments is invisible
  until something breaks. Rejected because the divergence problem only
  grows.
- **YAML canonical with DB as derived index (Path A original form).**
  Keep `scopes`/`strata`/`edges` tables in SQLite, rebuild from YAML on
  every change. Two representations of the same data to keep in sync —
  more complex than the in-memory mirror for the same reproducibility
  benefit. Rejected because no read pattern benefits from a SQL index
  over an in-memory dict at our scale.
- **Hybrid auto-export (DB writes trigger YAML re-emit; file changes
  trigger DB re-import).** Race-prone; two writers, both authoritative,
  no clean ordering. Rejected.
- **Make fleet config immutable post-bootstrap, force restart for
  changes.** Eliminates the writer problem by removing writers. But
  contradicts the UI write endpoint requirement. Rejected.

## Consequences

### Architectural

- **The single-source-of-truth invariant is restored.** Fleet config has
  exactly one representation on disk; the in-memory mirror is a cache.
- **The record store shrinks.** `RecordStore` no longer owns
  `strata`/`scopes`/`edges`. A new `FleetConfig` module owns
  `fleet.yaml` ↔ in-memory state.
- **The markdown-summaries philosophy extends to fleet config.** Both
  are human-readable, diff-friendly files on disk; the DB is reserved
  for append-only LLM-emitted records and judgments.

### What is given up

- **FK enforcement on `contributions.scope_id` is lost.** Mitigated by
  application-layer validation against the in-memory `FleetConfig` on
  every contribute call. Scopes are never deleted (only marked retired
  in the YAML), so historical contributions always have a valid scope
  to point at.
- **Edge walking moves from SQL JOINs to Python iteration.** Acceptable
  at our scale (dozens of scopes, single-digit stratum depth).
- **Programmatic YAML mutations don't preserve comments** under PyYAML.
  Acceptable in V1.2; revisitable.

### What is gained

- **Git tracks fleet evolution by default.** PRs review fleet changes;
  `git blame` answers "who added this scope?"; revert is one command.
- **Simpler schema, fewer migrations.** SQLite tables drop by half;
  remaining schema (contributions, judgments) is genuinely append-only.
- **One fewer reconciliation problem.** No "DB and YAML disagree" mode.

### Compatibility / migration

- Existing V1 instances have data in `strata`/`scopes`/`edges` tables.
  A one-shot migration exports current DB state to `fleet.yaml` and
  drops the tables. Documented in the migration script.
- Existing `bootstrap.py` becomes a thin "validate `fleet.yaml`"
  command — no DB writes.

### Out of scope (for follow-up ADRs)

- Multi-host backends and shared fleet config across machines (V2).
- Optimistic-concurrency revision tokens for concurrent UI clients.
- Structured migration of `fleet.yaml` schema if it evolves
  non-additively.
