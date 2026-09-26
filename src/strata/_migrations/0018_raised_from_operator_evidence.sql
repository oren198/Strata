-- Strata migration: engine-raised consequences of a failed directive outcome (ADR
-- 0017 P5).
--
-- P3's four-way disposition contract narrows for a DIRECTIVE target only: the
-- acting scope's judge reads exactly {held, failed, decline} — never
-- failed_corrected/failed_superseded, since a directive has no standing for THIS
-- judge to revise (D6); only the ISSUING authority may reclassify it. A `failed`
-- verdict against a directive is raised upward by the ENGINE, never the judge:
--
--   - to a scope issuer: an ordinary upward contribution, `raised_from` naming the
--     outcome that caused it, `acted_on` still naming the directive.
--   - to the operator: an `operator_evidence` row instead — never judged, shown
--     with the directive in the Console operator view until the operator
--     explicitly marks it seen (`seen_at`, set only by an operator act, never by
--     a read).
--
-- `acted_on_operator_item` is the operator-directive counterpart of `acted_on`
-- (0016): an outcome may report acting on a scope-held directive (`acted_on`,
-- REFERENCES contributions) or an operator directive (`acted_on_operator_item`,
-- REFERENCES operator_acts) — never both, and operator directives never enter a
-- scope's own contributions table (ADR 0008 D4), so they need their own FK
-- target. Mutual exclusion is enforced HERE, in the schema, not only at the app
-- boundary that already rejects `acted_on` + `supersedes` together (0016).
--
-- SQLite cannot ALTER a CHECK spanning two columns into an existing table, so
-- `contributions` is rebuilt — 0006's own precedent for rebuilding this exact
-- table: every table that FK-references contributions(id) (judgments,
-- judgment_attempts, change_events) is backed up and dropped first (foreign_keys
-- = ON would otherwise trip on the DROP), contributions is rebuilt, then each
-- dependent table is recreated and its rows restored, in rowid order so the
-- self-referential supersedes/acted_on chains stay satisfied as they replay
-- (0006's own reasoning, unchanged).

CREATE TABLE judgments_backup AS SELECT * FROM judgments;
DROP TABLE judgments;

CREATE TABLE judgment_attempts_backup AS SELECT * FROM judgment_attempts;
DROP TABLE judgment_attempts;

CREATE TABLE change_events_backup AS SELECT * FROM change_events;
DROP TABLE change_events;

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
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    acted_on                 TEXT REFERENCES contributions_new(id),
    acted_on_operator_item   TEXT REFERENCES operator_acts(id),
    raised_from              TEXT REFERENCES contributions_new(id),
    CHECK (acted_on IS NULL OR acted_on_operator_item IS NULL)
);

INSERT INTO contributions_new
    (id, scope_id, content, proposed_classification, subject, supersedes,
     contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts,
     created_at, acted_on)
SELECT id, scope_id, content, proposed_classification, subject, supersedes,
       contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts,
       created_at, acted_on
FROM contributions
ORDER BY rowid;

DROP TABLE contributions;
ALTER TABLE contributions_new RENAME TO contributions;

CREATE INDEX idx_contributions_scope ON contributions(scope_id, created_at);

-- Recreate the three dependent tables (FK to the rebuilt contributions),
-- unchanged from their live shape, and restore their rows.

CREATE TABLE judgments (
    id              TEXT PRIMARY KEY,
    contribution_id TEXT NOT NULL UNIQUE REFERENCES contributions(id),
    decision        TEXT NOT NULL CHECK (decision IN ('accept_as_directive', 'accept_as_context', 'decline')),
    judged_by       TEXT NOT NULL,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    summary_version INTEGER
);

INSERT INTO judgments
    SELECT id, contribution_id, decision, judged_by, notes, created_at, summary_version
    FROM judgments_backup
    ORDER BY rowid;

DROP TABLE judgments_backup;

CREATE INDEX idx_judgments_contrib ON judgments(contribution_id);

CREATE TABLE judgment_attempts (
    id              TEXT PRIMARY KEY,
    contribution_id TEXT NOT NULL REFERENCES contributions(id),
    error_class     TEXT NOT NULL,
    message         TEXT,
    attempted_at    TEXT NOT NULL DEFAULT (datetime('now')),
    outcome         TEXT CHECK (outcome IS NULL OR outcome = 'judge_failed')
);

INSERT INTO judgment_attempts
    SELECT id, contribution_id, error_class, message, attempted_at, outcome
    FROM judgment_attempts_backup
    ORDER BY rowid;

DROP TABLE judgment_attempts_backup;

CREATE INDEX idx_judgment_attempts_contrib ON judgment_attempts(contribution_id);

CREATE TABLE change_events (
    id              TEXT PRIMARY KEY,
    change_id       TEXT NOT NULL,
    contribution_id TEXT NOT NULL REFERENCES contributions(id),
    scope_id        TEXT NOT NULL,
    source_scope_id TEXT,
    item_id         TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN (
                        'published',
                        'amended',
                        'withdrawn',
                        'directive_appended',
                        'directive_superseded',
                        'directive_retired',
                        'directive_unspliced',
                        'operator_directive_changed',
                        'claim_corrected',
                        'claim_superseded'
                    )),
    before          TEXT,
    after           TEXT,
    hop             INTEGER NOT NULL DEFAULT 0,
    processed_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    self_notice     INTEGER NOT NULL DEFAULT 0,
    shown_at        TEXT
);

INSERT INTO change_events
    (id, change_id, contribution_id, scope_id, source_scope_id, item_id, kind,
     before, after, hop, processed_at, created_at, self_notice, shown_at)
SELECT id, change_id, contribution_id, scope_id, source_scope_id, item_id, kind,
       before, after, hop, processed_at, created_at, self_notice, shown_at
FROM change_events_backup
ORDER BY rowid;

DROP TABLE change_events_backup;

CREATE INDEX idx_change_events_scope_pending ON change_events(scope_id, processed_at);
CREATE INDEX idx_change_events_change ON change_events(change_id);

-- New: engine-raised operator evidence (never judged; shown with the
-- directive in the Console operator view until an explicit operator act
-- marks it seen).
CREATE TABLE operator_evidence (
    id                  TEXT PRIMARY KEY,
    operator_item_id    TEXT NOT NULL REFERENCES operator_acts(id),
    raised_from         TEXT NOT NULL REFERENCES contributions(id),
    reporter_scope_id   TEXT NOT NULL,
    reporter_skill      TEXT,
    reporter_session_id TEXT NOT NULL,
    reporter_ts         TEXT NOT NULL,
    content             TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    seen_at             TEXT
);

CREATE INDEX idx_operator_evidence_item ON operator_evidence(operator_item_id);
CREATE INDEX idx_operator_evidence_raised_from ON operator_evidence(raised_from);
