-- Strata V1 initial schema.
--
-- Vocabulary follows CONTEXT.md exactly:
--   scope, stratum, contribution, directive, context, record, provenance.
--
-- Every connection must run: PRAGMA foreign_keys = ON;
-- (enforced by the record store on every new connection)

CREATE TABLE strata (
    id          TEXT PRIMARY KEY,           -- e.g. 'L1'
    name        TEXT NOT NULL,
    ordinal     INTEGER NOT NULL UNIQUE,    -- 0 = broadest
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE scopes (
    id          TEXT PRIMARY KEY,           -- e.g. 'g_arch'
    name        TEXT NOT NULL,
    stratum_id  TEXT NOT NULL REFERENCES strata(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE edges (
    id              TEXT PRIMARY KEY,
    from_scope_id   TEXT NOT NULL REFERENCES scopes(id),
    to_scope_id     TEXT NOT NULL REFERENCES scopes(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (from_scope_id, to_scope_id)
);

CREATE TABLE contributions (
    id                       TEXT PRIMARY KEY,
    scope_id                 TEXT NOT NULL REFERENCES scopes(id),
    content                  TEXT NOT NULL,
    proposed_classification  TEXT NOT NULL CHECK (proposed_classification IN ('directive', 'context')),
    subject                  TEXT,
    supersedes               TEXT REFERENCES contributions(id),
    contributor_scope_id     TEXT NOT NULL,
    contributor_skill        TEXT NOT NULL,
    contributor_session_id   TEXT NOT NULL,
    contributor_ts           TEXT NOT NULL,
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE judgments (
    id              TEXT PRIMARY KEY,
    contribution_id TEXT NOT NULL UNIQUE REFERENCES contributions(id),
    decision        TEXT NOT NULL CHECK (decision IN ('accept_as_directive', 'accept_as_context', 'decline')),
    judged_by       TEXT NOT NULL,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_contributions_scope ON contributions(scope_id, created_at);
CREATE INDEX idx_judgments_contrib ON judgments(contribution_id);
CREATE INDEX idx_edges_from ON edges(from_scope_id);
CREATE INDEX idx_edges_to ON edges(to_scope_id);
