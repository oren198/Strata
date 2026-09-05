-- Strata migration: one more change-event kind, `directive_unspliced`
-- (ADR 0015 D5, issue #189).
--
-- 0011 settled the vocabulary at seven kinds, one per thing ADR 0014 D1 names
-- as a composed-input change. ADR 0015 D5 adds work of a different shape and
-- it needs a name of its own:
--
--   directive_unspliced  A directive row that a pre-1.11 splice (ADR 0011 D4)
--                        had copied into this scope's summary was removed
--                        from it. Deliberately NOT one of the three
--                        `directive_*` kinds above: those say a scope's own
--                        directive set moved, and every descendant is due a
--                        refresh against it. This one says the opposite — a
--                        row that was never this scope's has stopped
--                        pretending to be, while the directive it copied is
--                        unchanged in its owner's summary and still binds
--                        this scope through the ancestor walk (ADR 0015 D2).
--                        Nothing downstream changed, so nothing downstream is
--                        told.
--
-- Rows of this kind are stamped processed at birth — recorded, never drained
-- (ADR 0014 D4's shape for a notice that reports work already done). The
-- summary rewrite is mechanical and complete before the row is written; there
-- is nothing left for a judge to do about it.
--
-- SQLite cannot alter a CHECK in place, so this is 0011's recreate-table
-- rewrite again: new table, copy, drop, rename, recreate the two indexes.
-- Existing rows carry across verbatim — no backfill, no reinterpretation
-- (ADR 0013 D7, restated by ADR 0014 D7).

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
                        'operator_directive_changed'
                    )),
    before          TEXT,
    after           TEXT,
    hop             INTEGER NOT NULL DEFAULT 0,
    processed_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO change_events_new
    (id, change_id, contribution_id, scope_id, source_scope_id, item_id, kind,
     before, after, hop, processed_at, created_at)
SELECT id, change_id, contribution_id, scope_id, source_scope_id, item_id, kind,
       before, after, hop, processed_at, created_at
FROM change_events;

DROP TABLE change_events;

ALTER TABLE change_events_new RENAME TO change_events;

-- Both indexes go with the old table and are recreated here, unchanged: the
-- drain's "this scope's unprocessed events" read, and the once-per-change-id
-- check (ADR 0014 D4), which reads by change id ACROSS scopes.
CREATE INDEX idx_change_events_scope_pending ON change_events(scope_id, processed_at);
CREATE INDEX idx_change_events_change ON change_events(change_id);
