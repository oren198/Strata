-- Strata migration: change events — the reactive re-judgement trigger row
-- (ADR 0014 D5, issue #186).
--
-- A scope's memory changes only when an agent contributes to it; the inputs
-- that memory rests on can change with no agent involved (an upstream
-- publication withdrawn or amended, an ancestor directive retired, an
-- operator correction). ADR 0014 D5 makes the notice of such a change a row
-- in the scope's own record: the engine appends a `subject="manager-refresh"`
-- CONTRIBUTION carrying the change payload, and this table is that
-- contribution's structured half — the same event, machine-readable, so
-- `compose_perspective`'s `input_changes` section and the drain can find
-- unprocessed events without parsing prose.
--
-- Deliberately NOT a judgment_attempts sibling (pin 4): a pending refresh is
-- not a judge outage, and every surface that counts one — `doctor`,
-- `freshness`, `session_stats`, the Console — reports the two separately.
--
--   change_id        The independent input change this event belongs to.
--                    Every change DERIVED from processing it inherits the
--                    same id (ADR 0014 D4), which is what bounds a wave: a
--                    scope refreshes for a given change id at most once. Not
--                    unique here — one change fans out to every affected
--                    scope, a row each.
--   contribution_id  The `manager-refresh` contribution carrying this event's
--                    payload in the scope's record. The link is what makes
--                    the notice permanent and auditable: judging that
--                    contribution IS processing this event.
--   scope_id         The affected scope — the one that must refresh.
--   item_id          The input item that changed: a published item id, a
--                    directive id, or an operator act id.
--   kind             What happened to it. No CHECK constraint: the emission
--                    vocabulary is settled by the writers in ADR 0014 D1 (a
--                    later phase), and a guessed list here would be a wrong
--                    constraint on a table nothing can yet write.
--   before / after   The item's previous and current state, rendered for the
--                    judge. Either may be NULL — an addition has no before, a
--                    withdrawal no after.
--   hop              How many derived hops from the originating change this
--                    event is. ADR 0014 D4's backstop budget is checked
--                    against it, and hitting the budget is recorded.
--   processed_at     NULL until a refresh has processed this event, whatever
--                    the verdict (ADR 0014 D5). The row itself is never
--                    deleted — the record keeps it forever.

CREATE TABLE change_events (
    id              TEXT PRIMARY KEY,
    change_id       TEXT NOT NULL,
    contribution_id TEXT NOT NULL REFERENCES contributions(id),
    scope_id        TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    before          TEXT,
    after           TEXT,
    hop             INTEGER NOT NULL DEFAULT 0,
    processed_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The drain's read: this scope's unprocessed events, oldest first (ADR 0014
-- D6). Also what `strata doctor` reports queue depth and oldest-pending from.
CREATE INDEX idx_change_events_scope_pending ON change_events(scope_id, processed_at);

-- The once-per-change-id check (ADR 0014 D4) reads by change id across scopes.
CREATE INDEX idx_change_events_change ON change_events(change_id);
