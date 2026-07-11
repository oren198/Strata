-- Strata migration: record failed judgment attempts as events (issue #57).
--
-- When the scope-manager's judge() call fails (API outage, malformed model
-- output), the contribution is already in the record (the record never
-- lies), but no judgment can be written — a verdict is an exercise of scope
-- authority and no component outside the authority chain may forge one.
-- The failure is recorded here as an EVENT against the contribution, never
-- dressed as a judgment: this table has no decision column and cannot enter
-- the judgments table, so a failed attempt can never masquerade as a verdict.
--
-- Append-only, like the rest of the record. A contribution may accumulate
-- several attempt rows (one per failed re-judge) and still, eventually, a
-- single judgments row once a re-judge succeeds. Pending contributions —
-- those with attempts but no judgment — stay out of summaries and
-- perspectives; uncurated material must not reach readers.

CREATE TABLE judgment_attempts (
    id              TEXT PRIMARY KEY,
    contribution_id TEXT NOT NULL REFERENCES contributions(id),
    error_class     TEXT NOT NULL,
    message         TEXT,
    attempted_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_judgment_attempts_contrib ON judgment_attempts(contribution_id);
