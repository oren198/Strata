-- Strata migration: the operator stratum's record + retirement events
-- (ADR 0008 — Operator Stratum Mechanism, issue #91).
--
-- Two tables, because the operator acts in two different capacities
-- (ADR 0008 Context — "two capacities, two records"):
--
-- operator_acts
--   The operator's OWN record (ADR 0008 D1): every act the operator takes
--   ON the operator stratum itself — publish, supersede, or retire a piece
--   of operator memory attached above some scope. This is NOT judged
--   (judgment is how *delegated* authority is exercised; the operator's own
--   stratum authority is not delegated), so this table carries no
--   contribution/judgment shape — just the act, its attachment scope, kind,
--   verbatim content, and provenance. `kind` and `content` are NULL only
--   for 'retire' (retiring removes an item; it introduces no new memory).
--   `supersedes` / `retires` reference a prior operator_acts.id (an `op_`
--   id) — never a contributions.id; operator-stratum acts never enter a
--   scope's record (ADR 0008 D4, closing line).
--
-- retirements
--   Retirement EVENTS in a SCOPE's own record (ADR 0008 D4 / CONTEXT.md
--   § Retirement): when the operator retires a native directive inside some
--   scope's summary WITHOUT a replacement (no new memory enters), no
--   contribution row is fabricated — a retirement event is appended here
--   instead, keyed to the target scope and the retired directive's
--   contribution id. `retired_by` carries operator provenance ("operator")
--   today; CONTEXT.md already places retirement events in a scope's record,
--   so a future scope-manager explicit-retire can reuse this same shape.
--
-- Both tables are append-only, like every other record table in this
-- schema — "the record never lies" applies to the operator too (ADR 0008
-- D1).

CREATE TABLE operator_acts (
    id               TEXT PRIMARY KEY,
    act              TEXT NOT NULL CHECK (act IN ('publish', 'supersede', 'retire')),
    target_scope_id  TEXT NOT NULL,
    kind             TEXT CHECK (kind IN ('directive', 'context')),
    content          TEXT,
    subject          TEXT,
    supersedes       TEXT REFERENCES operator_acts(id),
    retires          TEXT REFERENCES operator_acts(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_operator_acts_target_scope ON operator_acts(target_scope_id);

CREATE TABLE retirements (
    id           TEXT PRIMARY KEY,
    scope_id     TEXT NOT NULL,
    directive_id TEXT NOT NULL,
    retired_by   TEXT NOT NULL,
    reason       TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_retirements_scope ON retirements(scope_id);
