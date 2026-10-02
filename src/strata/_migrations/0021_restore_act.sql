-- Strata migration: the RESTORE act (issue #219 restore, companion to #219 C).
--
-- Undoes a published item wrongly withdrawn by a correction sweep (the
-- verbatim P4 sweep, a #219 C `carries` decision, or either one's relay
-- cascade) — re-inserting the item under its ORIGINAL id and bytes, and
-- telling exactly the readers who got the false notice that it was false.
--
-- Three changes, one migration since they are one feature:
--
-- 1. `publication_acts` gains a third `act` value, `'restore'`, plus:
--      restores      The `pub_` id of the item being brought back — the
--                     withdraw act's own `withdraws` target. NULL unless
--                     act = 'restore'.
--      restored_by   On a WITHDRAW act only: the id of the `restore` act
--                     that reversed it, once one has. NULL until restored.
--                     Lives on the withdraw act (not `claim_carrier_checks`)
--                     because a verbatim-sweep withdrawal never gets a
--                     claim_carrier_checks row at all (that table is #219 C's
--                     own judged-classification audit only, per its own
--                     migration's header) — every restorable withdrawal IS a
--                     withdraw act, so this is the one place both methods
--                     can record it.
--      acknowledged  On a WITHDRAW act only: 1 once an operator has reviewed
--                     it and chosen to keep it withdrawn (the Console's
--                     "keep withdrawn" action) — hides it from the "to
--                     review" filter without restoring it. 0 by default.
--    SQLite cannot widen a CHECK constraint in place, so this is the
--    standard recreate-table rewrite (0006, 0011, 0012, 0017's shape):
--    new table, copy, drop, rename, recreate the index. Every connection
--    runs PRAGMA foreign_keys = ON (the migrator sets it before opening
--    this file's transaction), so DROP TABLE publication_acts performs an
--    implicit DELETE of every row first, which the two tables that
--    reference it (publication_judgments.act_id, migration 0005/0006;
--    publication_judgment_attempts.act_id, migration 0008) would then
--    violate on a real database with rows in them (0006's own header warns
--    of exactly this). Both are backed up to a plain, FK-free temp table
--    and dropped BEFORE publication_acts is rebuilt, then recreated and
--    restored afterwards — the same shape 0006 already established for
--    this exact table, extended here to publication_judgment_attempts,
--    which did not exist yet when 0006 was written.
--
-- 2. `change_events` gains:
--      claim_id    The corrected claim's own id (ADR 0017 P4/#219 C),
--                  structured rather than only rendered into the notice's
--                  prose (which is all `emit()`'s existing `claim_id`
--                  parameter reached before this migration). Restore needs
--                  it queryable: a claim-corrected self-notice row is how an
--                  item is identified as restorable at all (see
--                  strata.publication's restorability check), and the claim
--                  id is the join key into `claim_corrections` below.
--                  NULL for every kind that isn't `claim_corrected` or
--                  `claim_restored`, and for every row written before this
--                  migration (no backfill, ADR 0013 D7 / 0014 D7's stance).
--    and one more `kind`:
--      claim_restored   A claim-corrected withdrawal was reversed. Sent to
--                       EXACTLY the (scope, item) pairs that received the
--                       reversed `claim_corrected` notice — read from the
--                       ORIGINAL event rows by change id and item id, never
--                       recomputed from today's topology (see
--                       strata.change_events.emit_restore_notice). Evidence
--                       only: nothing is inserted into a reader's memory.
--
-- 3. A new table, `claim_corrections` — the claim's own refuted wording and
--    the correcting observation, written ONCE per correction wave (by
--    strata.app, inside the same scope_lock the sweep itself runs under),
--    not once per withdrawn item: one correction has one claim and one
--    correcting text, however many published items it withdraws. This is
--    what the owner-path restore judge call reads to show the judge "the
--    refuted claim, the correcting content, and the item" together
--    (contract line 1), and what the Console's "Correction withdrawals" view
--    reads for its side-by-side row. Keyed by (change_id, corrected_claim_id)
--    rather than reusing `claim_carrier_checks`, which only exists for #219
--    C's OWN judged rows — a verbatim-only correction has no row there.

CREATE TABLE publication_judgments_backup AS SELECT * FROM publication_judgments;
DROP TABLE publication_judgments;

CREATE TABLE publication_judgment_attempts_backup AS SELECT * FROM publication_judgment_attempts;
DROP TABLE publication_judgment_attempts;

CREATE TABLE publication_acts_new (
    id                       TEXT PRIMARY KEY,
    scope_id                 TEXT NOT NULL,
    act                      TEXT NOT NULL CHECK (act IN ('publish', 'withdraw', 'restore')),
    kind                     TEXT CHECK (kind IN ('directive', 'context')),
    content                  TEXT,
    subject                  TEXT,
    anchors                  TEXT,
    withdraws                TEXT REFERENCES publication_acts_new(id),
    restores                 TEXT REFERENCES publication_acts_new(id),
    restored_by              TEXT REFERENCES publication_acts_new(id),
    acknowledged             INTEGER NOT NULL DEFAULT 0,
    "trigger"                TEXT,
    proposer_scope_id        TEXT NOT NULL,
    proposer_skill           TEXT,
    proposer_session_id      TEXT NOT NULL,
    proposer_ts              TEXT NOT NULL,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    origin_scope_id          TEXT,
    relay_scope_id           TEXT,
    relay_item_id            TEXT
);

INSERT INTO publication_acts_new (
    id, scope_id, act, kind, content, subject, anchors, withdraws, restores,
    restored_by, acknowledged, "trigger", proposer_scope_id, proposer_skill,
    proposer_session_id, proposer_ts, created_at, origin_scope_id,
    relay_scope_id, relay_item_id
)
SELECT
    id, scope_id, act, kind, content, subject, anchors, withdraws, NULL,
    NULL, 0, "trigger", proposer_scope_id, proposer_skill,
    proposer_session_id, proposer_ts, created_at, origin_scope_id,
    relay_scope_id, relay_item_id
FROM publication_acts
ORDER BY rowid;

DROP TABLE publication_acts;

ALTER TABLE publication_acts_new RENAME TO publication_acts;

CREATE INDEX idx_publication_acts_scope ON publication_acts(scope_id);

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

CREATE TABLE publication_judgment_attempts (
    id           TEXT PRIMARY KEY,
    act_id       TEXT NOT NULL REFERENCES publication_acts(id),
    error_class  TEXT NOT NULL,
    message      TEXT,
    attempted_at TEXT NOT NULL DEFAULT (datetime('now')),
    outcome      TEXT CHECK (outcome IS NULL OR outcome = 'judge_failed')
);

INSERT INTO publication_judgment_attempts
    SELECT id, act_id, error_class, message, attempted_at, outcome
    FROM publication_judgment_attempts_backup;

DROP TABLE publication_judgment_attempts_backup;

CREATE INDEX idx_publication_judgment_attempts_act ON publication_judgment_attempts(act_id);

CREATE TABLE change_events_new (
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
                        'claim_superseded',
                        'claim_restored'
                    )),
    before          TEXT,
    after           TEXT,
    hop             INTEGER NOT NULL DEFAULT 0,
    processed_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    self_notice     INTEGER NOT NULL DEFAULT 0,
    shown_at        TEXT,
    claim_id        TEXT
);

INSERT INTO change_events_new
    (id, change_id, contribution_id, scope_id, source_scope_id, item_id, kind,
     before, after, hop, processed_at, created_at, self_notice, shown_at, claim_id)
SELECT id, change_id, contribution_id, scope_id, source_scope_id, item_id, kind,
       before, after, hop, processed_at, created_at, self_notice, shown_at, NULL
FROM change_events;

DROP TABLE change_events;

ALTER TABLE change_events_new RENAME TO change_events;

CREATE INDEX idx_change_events_scope_pending ON change_events(scope_id, processed_at);
CREATE INDEX idx_change_events_change ON change_events(change_id);

CREATE TABLE claim_corrections (
    id                      TEXT PRIMARY KEY,
    change_id               TEXT NOT NULL,
    claim_id                TEXT NOT NULL,
    scope_id                TEXT NOT NULL,
    corrected_claim_content TEXT NOT NULL,
    correcting_content      TEXT NOT NULL,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_claim_corrections_claim ON claim_corrections(claim_id, change_id);
