-- Strata migration: human review levels (v1.21, ADR 0018, #126).
--
-- At level 1 a delegated party's directive change is held for the operator instead of applied.
-- The judge's verdict is recorded verbatim and never overwritten; the disposition sits beside it.
--
--   judgments.disposition   NULL (no review: today's behaviour) | held | approved | rejected |
--                           lapsed
--   held_judgments          the judge's whole answer (ops, new context, notes) for a held
--                           judgment, so an approval can apply exactly those ops later. The ops
--                           are not stored anywhere else.
--   retirement_requests     a bound session's own strata_retire at level 1: actor session, kind,
--                           notice and time, with the same dispositions. It changes nothing until
--                           the operator approves.
--   review_acts             the operator's review act: actor, reason, time, and the resulting
--                           directive id. Nothing is deleted; a rejected item stays in the record.
--
-- ADD COLUMN and CREATE TABLE only: nothing existing is rewritten.

ALTER TABLE judgments ADD COLUMN disposition TEXT
    CHECK (disposition IS NULL OR disposition IN ('held', 'approved', 'rejected', 'lapsed'));

CREATE TABLE held_judgments (
    contribution_id TEXT PRIMARY KEY REFERENCES contributions(id),
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE retirement_requests (
    id               TEXT PRIMARY KEY,
    scope_id         TEXT NOT NULL,
    directive_id     TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('currency', 'correction')),
    notice           TEXT NOT NULL,
    actor_session_id TEXT NOT NULL,
    requested_at     TEXT NOT NULL DEFAULT (datetime('now')),
    disposition      TEXT NOT NULL DEFAULT 'held'
        CHECK (disposition IN ('held', 'approved', 'rejected', 'lapsed')),
    reason           TEXT
);

CREATE INDEX idx_retirement_requests_scope ON retirement_requests(scope_id, disposition);

CREATE TABLE review_acts (
    id           TEXT PRIMARY KEY,
    item_id      TEXT NOT NULL,
    scope_id     TEXT NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('approve', 'reject', 'lapse')),
    actor        TEXT NOT NULL,
    reason       TEXT,
    directive_id TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_review_acts_item ON review_acts(item_id);
