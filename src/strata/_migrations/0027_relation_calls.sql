-- Strata migration: the relation answer behind a judgment (v1.20, #242 (b)).
--
-- The engine's relation re-asks (a context note against an inherited directive, a directive
-- against an inherited directive) were stored as one protocol-note string; the answer itself
-- (its kind, the spans it quoted, which directive it named, the marker the engine fired on)
-- was dropped, so a decline could be explained only by replaying it. One row per call, written
-- in the same transaction as the judgment. CREATE TABLE only: nothing existing is touched.
--
-- `requirement_span` is reserved for the broad-directive requirement quote; always NULL until
-- that part ships.

CREATE TABLE relation_calls (
    contribution_id  TEXT NOT NULL REFERENCES contributions(id),
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
    PRIMARY KEY (contribution_id, call)
);
