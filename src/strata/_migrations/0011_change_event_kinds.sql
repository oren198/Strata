-- Strata migration: the change-event kind vocabulary, as a constraint
-- (ADR 0014 D1/D5, issue #186).
--
-- 0010 created `change_events` with `kind` deliberately unconstrained, and
-- said why: the emission vocabulary belongs to the WRITERS of an input
-- change (ADR 0014 D1), and a guessed list would have been a wrong
-- constraint on a table nothing could yet write. The writers now exist, so
-- the vocabulary is settled and becomes a CHECK. A mistyped kind is a wrong
-- notice to a scope's judge, and a wrong notice is worse than a loud
-- failure at the insert.
--
-- The seven kinds, one per thing ADR 0014 D1 names as a composed-input
-- change:
--
--   published                   A source scope published a NEW item. An
--                               addition triggers exactly as a removal does
--                               (D1) — a child that never re-judges after
--                               its parent added something is as wrong as
--                               one that never re-judges after a withdrawal.
--   amended                     A published item's content changed in place.
--   withdrawn                   A published item left its source's face,
--                               whether by the scope's own judged withdraw,
--                               mechanical anchor propagation (ADR 0007 D3)
--                               or the relay cascade (ADR 0013 D4b).
--   directive_appended          A directive entered a scope's summary, so
--                               every descendant it binds must re-judge.
--   directive_superseded        A directive was replaced by another.
--   directive_retired           A directive left with no replacement.
--   operator_directive_changed  The operator layer attached at a scope
--                               changed (ADR 0008 D1/D2). One kind for
--                               publish, supersede and retire alike: the
--                               operator's stratum is not judged, so the
--                               affected scopes need only know the binding
--                               layer above them moved — which is also why
--                               this is the one kind whose affected set
--                               includes the attachment scope ITSELF.
--
-- The same rewrite adds one column, for the same reason the CHECK lands
-- here — the writers now exist and know the answer:
--
--   source_scope_id  The scope the changed item came FROM, as opposed to
--                    `scope_id`, which is the AFFECTED scope that must
--                    refresh. Two different facts: "g_teamX must re-judge"
--                    and "because g_funcA's face changed". The perspective's
--                    `input_changes` section (ADR 0014 D5) renders both, and
--                    deriving one from the other is impossible — an item id
--                    does not name its holder. Nullable: a row written
--                    before this migration has no source scope to carry
--                    across, and nothing invents one (ADR 0013 D7 — no
--                    backfill).
--
-- SQLite cannot add a CHECK to an existing column, so this is the standard
-- recreate-table rewrite (the same shape 0002 used): new table, copy, drop,
-- rename, recreate 0010's two indexes. Existing rows are carried across
-- verbatim — no backfill, no reinterpretation (ADR 0013 D7's stance, which
-- ADR 0014 D7 restates). Any row already written outside the vocabulary
-- would fail the copy loudly rather than being silently rewritten; on the
-- live fleet there are none, because nothing has emitted yet.

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
SELECT id, change_id, contribution_id, scope_id, NULL, item_id, kind,
       before, after, hop, processed_at, created_at
FROM change_events;

DROP TABLE change_events;

ALTER TABLE change_events_new RENAME TO change_events;

-- Both of 0010's indexes go with the old table and are recreated here,
-- unchanged: the drain's "this scope's unprocessed events" read, and the
-- once-per-change-id check (ADR 0014 D4), which reads by change id ACROSS
-- scopes.
CREATE INDEX idx_change_events_scope_pending ON change_events(scope_id, processed_at);
CREATE INDEX idx_change_events_change ON change_events(change_id);
