-- Strata migration: two more change-event kinds, `claim_corrected` and
-- `claim_superseded` (ADR 0017 P3, "Ruling on failed outcomes (final: option c')").
--
-- The judge's tool-level disposition for an `acted_on` contribution is one of four:
-- held / failed_corrected / failed_superseded / decline. The judgments.decision
-- column stays CHECK-constrained to its original three values (migrations 0001/0006) —
-- held and both failed_* dispositions all persist as `accept_as_context`, per the
-- ruling's own "implementation note" (no new decision values). What tells the four
-- dispositions apart in the record is THIS table: a failed_corrected or
-- failed_superseded outcome ALSO writes one change_events row —
--   contribution_id = the outcome (the source),
--   item_id         = acted_on (the target),
--   scope_id        = the outcome's own scope,
--   source_scope_id = the target's scope,
--   before           = the target's content (what it said before),
--   after            = the outcome's own content (the observation that replaces it).
--
-- Rows of these two kinds are stamped `processed_at` AT BIRTH — the same discipline
-- `directive_unspliced` uses (0012): P3 has nothing left for a drain to do with them.
-- They are NOT fanned out to readers (issue #202's condensation-style disclosure is
-- P2's read surface, not this) and they are never composed as an `input_changes`
-- notice — P4 defines the correction notice; this migration only makes the record
-- fact storable. P2's `_outcome_reading` reads `kind` off the matching row instead of
-- assuming every accepted acted_on contribution held.
--
-- SQLite cannot alter a CHECK in place, so this is 0011/0012's recreate-table rewrite
-- again: new table, copy, drop, rename, recreate the two indexes. Existing rows carry
-- across verbatim — no backfill, no reinterpretation (ADR 0013 D7, restated by ADR
-- 0014 D7).

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
                        'claim_superseded'
                    )),
    before          TEXT,
    after           TEXT,
    hop             INTEGER NOT NULL DEFAULT 0,
    processed_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- Added by 0014 as plain ALTER TABLEs (issue #197); carried across verbatim here —
    -- a recreate-table migration must include every column the live table actually
    -- has, not just the ones the LAST recreate (0012) knew about.
    self_notice     INTEGER NOT NULL DEFAULT 0,
    shown_at        TEXT
);

INSERT INTO change_events_new
    (id, change_id, contribution_id, scope_id, source_scope_id, item_id, kind,
     before, after, hop, processed_at, created_at, self_notice, shown_at)
SELECT id, change_id, contribution_id, scope_id, source_scope_id, item_id, kind,
       before, after, hop, processed_at, created_at, self_notice, shown_at
FROM change_events;

DROP TABLE change_events;

ALTER TABLE change_events_new RENAME TO change_events;

-- Both indexes go with the old table and are recreated here, unchanged: the
-- drain's "this scope's unprocessed events" read, and the once-per-change-id
-- check (ADR 0014 D4), which reads by change id ACROSS scopes.
CREATE INDEX idx_change_events_scope_pending ON change_events(scope_id, processed_at);
CREATE INDEX idx_change_events_change ON change_events(change_id);
