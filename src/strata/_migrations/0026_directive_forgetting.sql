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
-- 3. `change_events` gains `retirement_id`, so the ordinary directive_retired change event
--    (R3: one path) can carry the label and the notice to the reader's perspective.
--    No new change-event kind: a release and a retraction are the same event; the retirement
--    row it points at says which label.
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

ALTER TABLE change_events ADD COLUMN retirement_id TEXT REFERENCES retirements(id);

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
