-- Strata migration: condensation_drops (ADR 0017 P6 part 1, issue #202).
--
-- One row per accepted context contribution the engine can MECHANICALLY tell
-- was condensed away by a later amendment to the same scope: present verbatim
-- in the summary before the write, absent after (the same substring test
-- perspective._context_contributions_absent already uses for its own count,
-- here recorded per item rather than only counted). Written at the #202
-- stamping site in app.py, alongside the summary write it accompanies — same
-- `_scope_lock`, no concurrent writer to this scope, but NOT one SQL
-- transaction with the summary file write (the summary is a markdown file,
-- this is a DB row): see _write_amendment's own docstring for what a
-- mid-sequence failure does.
--
-- state_at_drop is derived from the record at drop time, never re-derived
-- later (a strict ADR 0017 P2 sibling: nothing here is stored evidence that
-- outlives the moment it was true), priority corroborated > correcting >
-- raised > unexamined:
--   'corroborated' — this item has at least one held outcome (any reporter);
--   'correcting'   — this item IS the correcting content of a failed outcome
--                    (an accepted contribution that is the SOURCE of a
--                    claim_corrected/claim_superseded change event);
--   'raised'       — an accepted contribution with `raised_from` set (P5): the
--                    philosopher's ruling is that a raised consequence at the
--                    issuer is EXAMINED — the order follows the item's ground,
--                    not its location (P6 part 2 will list it among the
--                    examined items on the judge-input side too);
--   'unexamined'   — none of the above.
-- "Unexamined", never "poorly standing" (P6 wording rule).
--
-- words_before/words_after use the same word-count measure derive_condensed
-- already uses (summary_store._word_count); budget is the scope's own
-- summary_max_words at that write.

CREATE TABLE condensation_drops (
    id              TEXT PRIMARY KEY,
    scope_id        TEXT NOT NULL,
    summary_version INTEGER NOT NULL,
    contribution_id TEXT NOT NULL REFERENCES contributions(id),
    state_at_drop   TEXT NOT NULL CHECK (state_at_drop IN (
                        'corroborated',
                        'correcting',
                        'raised',
                        'unexamined'
                    )),
    words_before    INTEGER NOT NULL,
    words_after     INTEGER NOT NULL,
    budget          INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_condensation_drops_scope ON condensation_drops(scope_id, created_at);
CREATE INDEX idx_condensation_drops_contribution ON condensation_drops(contribution_id);
