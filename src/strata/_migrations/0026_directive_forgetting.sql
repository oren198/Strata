-- Strata migration: directive forgetting (v1.20 item 7, design-v19-forgetting.md).
--
-- A directive stops binding only through an act of its authority. This migration stores
-- what that act needs and nothing that decides it:
--
-- 1. `retirements` (the record event of a retire act) gains the kind, the notice, who acted,
--    why it happened, and the end condition / evidence behind it. A pre-existing row (an
--    operator correction, or a judged `retire`) keeps every new column NULL and reads as the
--    neutral label "retired".
-- 2. `contributions` gains the two inputs a creating/confirming contribution can carry:
--    `ends` (an end condition for the directive it creates, JSON) and `fulfils` (the id of a
--    judged-condition directive this contribution is evidence for).
-- 3. `change_events` is NOT changed: the ordinary directive_retired event (R3: one path) finds
--    its retirement through its directive id (RecordStore.retirement_for_directive). No new
--    change-event kind: a release and a retraction are the same event; the row says the label.
-- 4. `retirement_candidates`: SQL-only prompts (date_near, condition_evidence, age, disuse).
--    A candidate never retires anything.
--
-- All ADD COLUMNs are nullable and plain (like 0016/0022): no table rebuild, nothing backfilled.
-- The directive's own `end_condition` and `set_by` live in the scope summary file with the
-- directive (the Directive model), not here.

ALTER TABLE retirements ADD COLUMN kind TEXT CHECK (kind IN ('currency', 'correction'));
ALTER TABLE retirements ADD COLUMN notice TEXT;
ALTER TABLE retirements ADD COLUMN actor_session_id TEXT;
ALTER TABLE retirements ADD COLUMN cause TEXT
    CHECK (cause IN ('act', 'date_lapse', 'condition_judged'));
ALTER TABLE retirements ADD COLUMN end_condition TEXT;
ALTER TABLE retirements ADD COLUMN evidence_contribution_id TEXT REFERENCES contributions(id);

ALTER TABLE contributions ADD COLUMN ends TEXT;
ALTER TABLE contributions ADD COLUMN fulfils TEXT;

-- 5. `operator_act_forgetting`: what the forgetting acts add to an OPERATOR item, in a side
--    table so `operator_acts` (append-only, pinned by older code) is untouched. For a
--    publish/supersede act: the end condition (`ends`, JSON). For a retire act: the kind,
--    the notice, the cause and who made it.
CREATE TABLE operator_act_forgetting (
    act_id           TEXT PRIMARY KEY REFERENCES operator_acts(id),
    ends             TEXT,
    kind             TEXT CHECK (kind IN ('currency', 'correction')),
    notice           TEXT,
    cause            TEXT CHECK (cause IN ('act', 'date_lapse', 'condition_judged')),
    actor_session_id TEXT
);

CREATE TABLE retirement_candidates (
    id              TEXT PRIMARY KEY,
    directive_id    TEXT NOT NULL,
    scope_id        TEXT NOT NULL,
    ground          TEXT NOT NULL
                    CHECK (ground IN ('date_near', 'condition_evidence', 'age', 'disuse')),
    evidence_ids    TEXT NOT NULL DEFAULT '[]',
    opened_at       TEXT NOT NULL DEFAULT (datetime('now')),
    state           TEXT NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open', 'dismissed', 'acted')),
    dismissed_until TEXT,
    note            TEXT
);

CREATE INDEX idx_retirement_candidates_scope ON retirement_candidates(scope_id, state);
-- one open candidate per (directive, ground): a recompute never duplicates
CREATE UNIQUE INDEX idx_retirement_candidates_open
    ON retirement_candidates(directive_id, ground) WHERE state = 'open';

-- 6. Condensation lineage (v1.20 item 7(d)). `context_lineage` holds, per line of a scope's
--    stored context, the ids of the contributions that line rests on; `context_hash` ties the
--    rows to the exact context text they describe, so a write that leaves the context alone
--    keeps the chain and a write that rewrites it without lineage breaks it.
--    `condensation_order_checks` has one row per write that changed the context: measured or
--    not (with the reason), and, when measured, the dropped/kept counts and the inversions.
CREATE TABLE context_lineage (
    scope_id         TEXT NOT NULL,
    context_hash     TEXT NOT NULL,
    line_no          INTEGER NOT NULL,
    contribution_ids TEXT NOT NULL DEFAULT '[]',
    summary_version  INTEGER,
    stored_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (scope_id, context_hash, line_no)
);

CREATE TABLE condensation_order_checks (
    id              TEXT PRIMARY KEY,
    scope_id        TEXT NOT NULL,
    summary_version INTEGER,
    measured        INTEGER NOT NULL CHECK (measured IN (0, 1)),
    reason          TEXT,
    dropped_n       INTEGER NOT NULL DEFAULT 0,
    kept_n          INTEGER NOT NULL DEFAULT 0,
    unattributed_n  INTEGER NOT NULL DEFAULT 0,
    inversions_n    INTEGER NOT NULL DEFAULT 0,
    detail          TEXT NOT NULL DEFAULT '{}',
    checked_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_condensation_order_checks_scope ON condensation_order_checks(scope_id);
