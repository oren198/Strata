-- Strata migration: the publication channel (ADR 0007 — Publication
-- Mechanism, issue #90 / #83).
--
-- Two tables, mirroring the contributions/judgments split (append-only, like
-- every other record table in this schema):
--
-- publication_acts
--   Every publish or withdraw act on a scope's OUTWARD FACE (ADR 0007 D1/D2)
--   — never its internal summary. `act` = 'publish' introduces a new
--   published item (kind/content/subject/anchors all populated, `withdraws`
--   NULL); `act` = 'withdraw' removes one (kind/content/subject/anchors all
--   NULL, `withdraws` names the publish act's id being removed). `anchors`
--   is a JSON array of anchor strings (`directive:<id>` or `subject:<text>`,
--   ADR 0007 D1) — NULL for withdraw. `"trigger"` is NULL for an
--   agent-proposed or operator-in-person act; for a MECHANICALLY propagated
--   withdrawal (ADR 0007 D3 — a directive-anchored item whose anchors all
--   vanished from the internal summary) it carries the record id of the
--   triggering event (a contribution id or an operator retirement id) so the
--   withdrawal is traceable to the internal change that caused it. Proposer
--   provenance columns mirror `contributions`' `contributor_*` columns
--   (CONTEXT.md § Provenance) — populated for agent/operator acts, and for
--   mechanical acts carry the triggering scope's own provenance (see
--   `strata.publication`).
--
-- publication_judgments
--   The scope-manager's verdict on a publish/withdraw act — same shape as
--   `judgments`, one row per judged act, UNIQUE on `act_id`. Mechanical
--   propagation withdrawals (`"trigger"` IS NOT NULL on the act) get NO row
--   here: they are a structural consequence of an internal change that was
--   ALREADY judged (the contribution or operator correction that removed the
--   directive), not a fresh judgment on the publication itself. Every
--   agent-proposed act and every judged-propagation withdrawal (ADR 0007
--   D3's subject-anchored path, carried on the ORIGINATING contribution's
--   judgment, recorded here against the derived withdraw act) DOES get one.
--
-- `"trigger"` is double-quoted throughout (a SQLite keyword) so the column
-- name matches ADR 0007's vocabulary exactly rather than a renamed synonym.

CREATE TABLE publication_acts (
    id                       TEXT PRIMARY KEY,
    scope_id                 TEXT NOT NULL,
    act                      TEXT NOT NULL CHECK (act IN ('publish', 'withdraw')),
    kind                     TEXT CHECK (kind IN ('directive', 'context')),
    content                  TEXT,
    subject                  TEXT,
    anchors                  TEXT,
    withdraws                TEXT REFERENCES publication_acts(id),
    "trigger"                TEXT,
    proposer_scope_id        TEXT NOT NULL,
    proposer_skill           TEXT NOT NULL,
    proposer_session_id      TEXT NOT NULL,
    proposer_ts              TEXT NOT NULL,
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_publication_acts_scope ON publication_acts(scope_id);

CREATE TABLE publication_judgments (
    id          TEXT PRIMARY KEY,
    act_id      TEXT NOT NULL UNIQUE REFERENCES publication_acts(id),
    decision    TEXT NOT NULL CHECK (decision IN ('accept', 'decline')),
    judged_by   TEXT NOT NULL,
    reasoning   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
