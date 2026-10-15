-- Strata migration: judge usage (#246).
--
-- One row per judge call. Tokens are the response's own usage figures.
-- This table references nothing, so applying it does not rebuild a table
-- that already holds rows. A failure to insert a row is the engine's
-- problem to log; it is never a reason to fail the judgment.

CREATE TABLE judge_usage (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    scope_id        TEXT,
    call_kind       TEXT NOT NULL,
    contribution_id TEXT,
    model           TEXT,
    provider        TEXT,
    latency_ms      INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER
);

CREATE INDEX idx_judge_usage_created ON judge_usage(created_at);
CREATE INDEX idx_judge_usage_scope ON judge_usage(scope_id, created_at);
