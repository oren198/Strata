-- Strata migration: make contribution/publication skill provenance optional
-- (issue #121 — a skill carries a body or it is omitted).
--
-- Owner ruling: memory must not float, but provenance already carries the
-- agent's scope and session, so a bare skill NAME adds nothing. Either a
-- skill carries a body or it is omitted. Agent identity (scope + session)
-- remains mandatory; only the skill column loosens.
--
-- Two NOT NULL columns loosen to nullable:
--   contributions.contributor_skill
--   publication_acts.proposer_skill
--
-- SQLite cannot drop a NOT NULL constraint in place, so each owning table is
-- rebuilt — the same drop-and-rebuild-with-temp-backup pattern as
-- 0002_drop_fleet_tables.sql. Every connection runs PRAGMA foreign_keys = ON
-- (the migrator sets it before opening this file's transaction), so a table
-- referenced by others is drained by an implicit DELETE on DROP and would
-- violate those children's foreign keys mid-rebuild. Each referencing table
-- is therefore backed up to a temp table and dropped BEFORE its parent is
-- rebuilt, then recreated and restored afterwards — no record is lost, and
-- existing rows keep their skill values verbatim (NULL only ever appears on
-- rows written after this migration).
--
-- The self-referential foreign keys (contributions.supersedes,
-- publication_acts.withdraws) are preserved: a superseding/withdrawing row is
-- always written after the row it references, so replaying INSERT ... SELECT
-- in rowid order keeps every self-reference satisfied under foreign_keys = ON.
-- SQLite rewrites the "*_new" self-reference to the final table name on
-- RENAME, so the rebuilt tables reference themselves, not the scratch name.
--
-- The whole file runs in one transaction supplied by the migration runner
-- (run_migrations opens BEGIN and commits the script + its tracking row
-- together), so a crash mid-rebuild rolls everything back — no BEGIN/COMMIT
-- is written here.

-- === contributions: contributor_skill NOT NULL -> nullable ===============

-- Back up the two tables that reference contributions(id) so contributions
-- can be dropped without tripping their foreign keys.
CREATE TABLE judgments_backup AS SELECT * FROM judgments;
DROP TABLE judgments;

CREATE TABLE judgment_attempts_backup AS SELECT * FROM judgment_attempts;
DROP TABLE judgment_attempts;

-- Rebuild contributions with contributor_skill nullable; every other column,
-- constraint, and the supersedes self-reference are unchanged.
CREATE TABLE contributions_new (
    id                       TEXT PRIMARY KEY,
    scope_id                 TEXT NOT NULL,
    content                  TEXT NOT NULL,
    proposed_classification  TEXT NOT NULL CHECK (proposed_classification IN ('directive', 'context')),
    subject                  TEXT,
    supersedes               TEXT REFERENCES contributions_new(id),
    contributor_scope_id     TEXT NOT NULL,
    contributor_skill        TEXT,
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
    FROM contributions
    ORDER BY rowid;

DROP TABLE contributions;
ALTER TABLE contributions_new RENAME TO contributions;

-- Recreate judgments and judgment_attempts (FK to the rebuilt contributions)
-- and restore their rows from the backups.
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

CREATE TABLE judgment_attempts (
    id              TEXT PRIMARY KEY,
    contribution_id TEXT NOT NULL REFERENCES contributions(id),
    error_class     TEXT NOT NULL,
    message         TEXT,
    attempted_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO judgment_attempts
    SELECT id, contribution_id, error_class, message, attempted_at
    FROM judgment_attempts_backup;

DROP TABLE judgment_attempts_backup;

CREATE INDEX idx_contributions_scope ON contributions(scope_id, created_at);
CREATE INDEX idx_judgments_contrib ON judgments(contribution_id);
CREATE INDEX idx_judgment_attempts_contrib ON judgment_attempts(contribution_id);

-- === publication_acts: proposer_skill NOT NULL -> nullable ===============

-- Back up publication_judgments (FK to publication_acts(id)) so the acts
-- table can be dropped without tripping it.
CREATE TABLE publication_judgments_backup AS SELECT * FROM publication_judgments;
DROP TABLE publication_judgments;

-- Rebuild publication_acts with proposer_skill nullable; every other column,
-- constraint, and the withdraws self-reference are unchanged. "trigger" stays
-- double-quoted (a SQLite keyword), matching 0005.
CREATE TABLE publication_acts_new (
    id                       TEXT PRIMARY KEY,
    scope_id                 TEXT NOT NULL,
    act                      TEXT NOT NULL CHECK (act IN ('publish', 'withdraw')),
    kind                     TEXT CHECK (kind IN ('directive', 'context')),
    content                  TEXT,
    subject                  TEXT,
    anchors                  TEXT,
    withdraws                TEXT REFERENCES publication_acts_new(id),
    "trigger"                TEXT,
    proposer_scope_id        TEXT NOT NULL,
    proposer_skill           TEXT,
    proposer_session_id      TEXT NOT NULL,
    proposer_ts              TEXT NOT NULL,
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO publication_acts_new
    SELECT id, scope_id, act, kind, content, subject, anchors, withdraws,
           "trigger", proposer_scope_id, proposer_skill, proposer_session_id,
           proposer_ts, created_at
    FROM publication_acts
    ORDER BY rowid;

DROP TABLE publication_acts;
ALTER TABLE publication_acts_new RENAME TO publication_acts;

CREATE TABLE publication_judgments (
    id          TEXT PRIMARY KEY,
    act_id      TEXT NOT NULL UNIQUE REFERENCES publication_acts(id),
    decision    TEXT NOT NULL CHECK (decision IN ('accept', 'decline')),
    judged_by   TEXT NOT NULL,
    reasoning   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO publication_judgments
    SELECT id, act_id, decision, judged_by, reasoning, created_at
    FROM publication_judgments_backup;

DROP TABLE publication_judgments_backup;

CREATE INDEX idx_publication_acts_scope ON publication_acts(scope_id);
