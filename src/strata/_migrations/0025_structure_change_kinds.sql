-- Strata migration: the structure-change kinds (ADR 0014 D8, #247 core half).
--
-- `change_events.kind` gains two values:
--   channel_removed   A reference edge this scope read through was removed.
--   chain_changed     The chain above this scope moved (a re-parent).
-- Neither is a publication or directive change: nothing was withdrawn and no
-- directive was retired, so no older kind tells the truth.
--
-- SQLite cannot widen a CHECK in place, so this is the recreate-table rewrite
-- (0011, 0012, 0021's shape): new table, copy, drop, rename, recreate indexes.
-- No other table references change_events, so no FK backup is needed. The
-- columns are exactly 0021's.

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
                        'claim_restored',
                        'channel_removed',
                        'chain_changed'
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
       before, after, hop, processed_at, created_at, self_notice, shown_at, claim_id
FROM change_events;

DROP TABLE change_events;

ALTER TABLE change_events_new RENAME TO change_events;

CREATE INDEX idx_change_events_scope_pending ON change_events(scope_id, processed_at);
CREATE INDEX idx_change_events_change ON change_events(change_id);
