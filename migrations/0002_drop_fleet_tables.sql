-- Strata V1.2 migration: drop fleet config tables and remove FK on contributions.scope_id.
--
-- Under ADR 0002, fleet configuration (strata, scopes, edges) is now
-- file-canonical: fleet.yaml is the single source of truth and the backend
-- holds an in-memory mirror.  The strata, scopes, and edges tables are no
-- longer used.
--
-- scope_id in contributions becomes an unvalidated string column; scope-
-- existence and active-status checks are enforced at the application layer
-- via the in-memory FleetConfig.
--
-- SQLite does not support DROP COLUMN or dropping a FK in place, so we
-- recreate contributions without the FK reference to scopes(id).  Because
-- judgments has a FK to contributions, we back up judgments to a temp
-- table before the rebuild and restore afterwards — this preserves all
-- judgment history across the migration.
--
-- Data loss note: any rows in strata, scopes, and edges are intentionally
-- discarded (their data moves to fleet.yaml via the separate one-shot
-- migration/v1-to-v1.2 exporter).  Contributions and judgments are
-- preserved end-to-end.

-- Back up judgments so we can restore after the contributions rebuild.
CREATE TABLE judgments_backup AS SELECT * FROM judgments;
DROP TABLE judgments;

-- Recreate contributions without the FK on scope_id.
CREATE TABLE contributions_new (
    id                       TEXT PRIMARY KEY,
    scope_id                 TEXT NOT NULL,
    content                  TEXT NOT NULL,
    proposed_classification  TEXT NOT NULL CHECK (proposed_classification IN ('directive', 'context')),
    subject                  TEXT,
    supersedes               TEXT REFERENCES contributions_new(id),
    contributor_scope_id     TEXT NOT NULL,
    contributor_skill        TEXT NOT NULL,
    contributor_session_id   TEXT NOT NULL,
    contributor_ts           TEXT NOT NULL,
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO contributions_new
    SELECT id, scope_id, content, proposed_classification,
           subject, supersedes,
           contributor_scope_id, contributor_skill,
           contributor_session_id, contributor_ts,
           created_at
    FROM contributions;

DROP TABLE contributions;
ALTER TABLE contributions_new RENAME TO contributions;

-- Recreate judgments with the same schema as V1.1 (FK to the rebuilt
-- contributions table) and restore data from the backup.
CREATE TABLE judgments (
    id              TEXT PRIMARY KEY,
    contribution_id TEXT NOT NULL UNIQUE REFERENCES contributions(id),
    decision        TEXT NOT NULL CHECK (decision IN ('accept_as_directive', 'accept_as_context', 'decline')),
    judged_by       TEXT NOT NULL,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO judgments
    SELECT id, contribution_id, decision, judged_by, notes, created_at
    FROM judgments_backup;

DROP TABLE judgments_backup;

CREATE INDEX idx_contributions_scope ON contributions(scope_id, created_at);
CREATE INDEX idx_judgments_contrib ON judgments(contribution_id);

DROP TABLE IF EXISTS edges;
DROP TABLE IF EXISTS scopes;
DROP TABLE IF EXISTS strata;

