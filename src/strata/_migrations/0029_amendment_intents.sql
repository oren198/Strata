-- Strata migration: the write-ahead intent journal for amendments (v1.22, P2).
--
-- An amendment is a summary FILE write followed by record-side steps (stamp, retirement rows,
-- withdrawals, the reader change events). The file and the database cannot share a transaction, so
-- a process death between them lost every later step for good: a directive gone from the summary
-- with no retirement row and no notice to descendants. Before the file is written the writer now
-- appends one intent row; the after-write steps are idempotent per intent; the intent is marked
-- done at the end. Recovery (on start and at drain, under the scope lock) replays a pending
-- intent whose summary version was written, and abandons one whose version was not.
--
--   amendment_intents.state   pending | done | abandoned | suspect
--   payload_json              only what the replay needs: ids, op targets, reasons and
--                             circumstances the record already stores, the directive-id sets, and
--                             the change ids. No judge transcript, no key, no environment.
--   retirements.intent_id     the intent that wrote the row; unique per (scope, directive, intent),
--                             so a replay can never write a second retirement.
--
-- CREATE TABLE, ADD COLUMN and CREATE INDEX only: nothing existing is rewritten.

CREATE TABLE amendment_intents (
    id               TEXT PRIMARY KEY,
    scope_id         TEXT NOT NULL,
    expected_version INTEGER NOT NULL,
    state            TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'done', 'abandoned', 'suspect')),
    payload_json     TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_amendment_intents_scope_state ON amendment_intents(scope_id, state);

ALTER TABLE retirements ADD COLUMN intent_id TEXT;

CREATE UNIQUE INDEX idx_retirements_intent
    ON retirements(scope_id, directive_id, intent_id) WHERE intent_id IS NOT NULL;
