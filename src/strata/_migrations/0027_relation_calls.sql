-- Strata migration: the relation answer behind a verdict (v1.20, #242 (b)).
--
-- The engine's relation re-asks (a context note against an inherited directive, a directive
-- against an inherited directive, a published context item against an inherited directive) were
-- stored as one protocol-note string; the answer itself (its kind, the spans it quoted, which
-- directive it named, the marker the engine fired on) was dropped, so a decline could be
-- explained only by replaying it. One row per call, written in the same transaction as the
-- verdict it fed. CREATE TABLE / CREATE INDEX only: nothing existing is touched.
--
-- A row belongs to exactly one of
--   contribution_id  a contribution's judgment (the ordinary path), or
--   act_id           a publication act (publish or restore), kept whether the act was accepted
--                    or declined: the act row exists before the judge is asked, so a declined
--                    publication has a stable id (the `pub_...` act id).
--
-- `requirement_span` is reserved for the broad-directive requirement quote; always NULL until
-- that part ships.

CREATE TABLE relation_calls (
    id               TEXT PRIMARY KEY,
    contribution_id  TEXT REFERENCES contributions(id),
    act_id           TEXT REFERENCES publication_acts(id),
    call             TEXT NOT NULL CHECK (call IN ('context', 'directive')),
    answer_kind      TEXT,
    inherited_id     TEXT,
    origin           TEXT,
    occurrence_span  TEXT,
    instead_span     TEXT,
    requirement_span TEXT,
    verdict          TEXT NOT NULL,
    fallback         INTEGER NOT NULL DEFAULT 0 CHECK (fallback IN (0, 1)),
    reason           TEXT,
    marker           TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((contribution_id IS NULL) <> (act_id IS NULL))
);

CREATE UNIQUE INDEX idx_relation_calls_contribution
    ON relation_calls(contribution_id, call) WHERE contribution_id IS NOT NULL;
CREATE UNIQUE INDEX idx_relation_calls_act
    ON relation_calls(act_id, call) WHERE act_id IS NOT NULL;
