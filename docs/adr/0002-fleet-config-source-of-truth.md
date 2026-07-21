# ADR 0002 — Fleet Config Source of Truth

**Status:** Accepted (implemented)
**Date:** 2026-05-27
**Related:** ADR 0003 (depends on this one for the per-scope skill
declaration schema)

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
6. **`strata bootstrap` is repurposed, not removed.** The command name
   stays; its docstring becomes "Validate `fleet.yaml` and prepare the
   in-memory mirror — no DB writes." Starter templates ship in the
   repo (`dev-team`, `support-org`, `research-group`).
7. **`strata start` auto-seeds on first run.** If no `fleet.yaml` is
   present in the working directory, `strata start` copies
   `templates/dev-team.yaml` → `fleet.yaml` and prints a one-line
   notice ("seeded fleet.yaml from the default template; edit to
   suit"), then proceeds normally. This preserves the V1.1 zero-step
   first-run ergonomic; the seeded file is then owned by git as the
   ADR intends.

### Scope lifecycle

Each scope carries a `status` field with one of two values:

| value      | semantics                                                       |
|------------|-----------------------------------------------------------------|
| `active`   | Default. Visible to `/scopes`; accepts new contributions; participates in perspective composition. |
| `archived` | Invisible to `/scopes`; rejects new contributions at write time with `scope_not_active` (distinct from `scope_not_found`); remains in `fleet.yaml` so historical records resolve to a meaningful name. |

The `status` field is optional; omitted means `active`. Moving
`active` → `archived` is the controlled lifecycle event; the API
exposes no "delete" operation. A hand-edit that physically removes a
scope from `fleet.yaml` will leave historical records pointing
nowhere — that consequence is on the editor.

### Per-scope skill declaration (consumed by ADR 0003)

Each scope may declare which skills are valid bindings for sessions
opened against it. The fields:

```yaml
scopes:
  - id: g_arch
    name: Architect
    stratum_id: L1
    status: active                                       # optional; default active
    default_skill: code-writer                           # optional
    permitted_skills: [code-writer, evidence-summarizer] # optional
```

**Resolution rules** (applied by `strata launch` — see ADR 0003):

| `default_skill` | `permitted_skills` | Launch behavior (TTY) | Launch behavior (non-TTY) |
|---|---|---|---|
| set | unset | uses `default_skill` | uses `default_skill` |
| set | set, includes default | uses `default_skill`; `--skill X` allowed if X ∈ list | uses `default_skill` |
| unset | set | prompts user to pick from list | error |
| set | set, *excludes* default | **error at YAML load** (drift) | **error at YAML load** |
| unset | unset | resolves to no skill (`None`) — skill-less binding; `--skill X` still binds X | resolves to no skill (`None`) |

The drift case (default not in permitted list) is a hard load-time
error so misconfigurations surface at backend start, not at session
launch.

The last row (neither field set) resolves to a **skill-less binding**
(`resolve_skill` returns `None`) rather than an error — see issue #121
and ADR 0003 § "Amendment — skill is optional (issue #121)." An
explicit `--skill` on such an unrestricted scope still binds that skill;
the permitted-list check is unchanged for scopes that declare one.

### Validation invariants

At YAML **load** time, the backend raises and refuses to start on any of:

1. Duplicate stratum IDs.
2. Duplicate scope IDs.
3. Duplicate stratum `ordinal` values.
4. Any scope's `stratum_id` references a stratum not in the file.
5. Any edge's `from` or `to` references a scope not in the file.
6. Any edge is a self-loop.
7. Any edge violates the ±1 stratum-distance constraint (mirrors
   V1.1's `record_store.add_edge` rule).
8. Any scope sets `default_skill` not in its own `permitted_skills`
   list (per the resolution-rules drift case above).

At **contribute** time (additional, beyond load-time):

9. The target scope exists in the current `FleetConfig`.
10. The target scope has `status: active`. Rejection error is
    `scope_not_active`, distinct from `scope_not_found`.

These checks are SQL FK / CHECK constraints in V1.1; under V1.2 they
become explicit application-layer validation.

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
  the explicit validation invariants above — every contribute call
  checks scope existence and `status: active` against the in-memory
  `FleetConfig`. The API exposes no delete operation, so historical
  contributions always have a valid scope to point at; physical
  removal from `fleet.yaml` is a hand-edit consequence the operator
  takes on.
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
