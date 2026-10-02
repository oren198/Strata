-- Strata migration: claim_carrier_checks (ADR 0017 P4 follow-up, issue #219 C).
--
-- One row per published item the owner-judge was asked to classify against a
-- claim its own outcome report (same-scope `failed_corrected`) or its own
-- refresh for a drained `claim_corrected` notice (cross-scope, #221's
-- centralised sweep) just found WRONG. The mechanical verbatim sweep
-- (propagate_claim_correction) already withdraws exact-byte carriers before
-- this ever runs; this table records what happened to everything else in
-- the scope's own current face — a judged decision, an item left out of the
-- judge call because the face exceeded the 20-item cap, or an item the judge
-- named but whose decision could not be read.
--
-- outcome:
--   'carries'              — the judge said this item still asserts the
--                            corrected claim; it was withdrawn and its own
--                            relays cascaded, exactly like a verbatim hit.
--   'does_not_carry'       — the judge said this item does not assert it; a
--                            judgment was made, so the row exists even
--                            though nothing was withdrawn (Philis: "carries /
--                            does_not_carry are acts, not labels").
--   'unresolved_overflow'  — the face had more than 20 surviving candidates;
--                            this one ranked outside the top 20 sent to the
--                            judge and was never classified.
--   'unresolved_unreadable'— the judge named this item but its decision
--                            value did not parse, or named no decision for
--                            it at all.
--
-- Relays are never rows here: a relay is a copy of a face item, carried down
-- by the same cascade the verbatim path uses when its origin is judged
-- `carries` — the judge sees the owner's own face only.

CREATE TABLE claim_carrier_checks (
    id                  TEXT PRIMARY KEY,
    change_id           TEXT NOT NULL,
    scope_id            TEXT NOT NULL,
    corrected_claim_id  TEXT NOT NULL,
    item_id             TEXT NOT NULL,
    outcome             TEXT NOT NULL CHECK (outcome IN (
                            'carries',
                            'does_not_carry',
                            'unresolved_overflow',
                            'unresolved_unreadable'
                        )),
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_claim_carrier_checks_scope ON claim_carrier_checks(scope_id, created_at);
CREATE INDEX idx_claim_carrier_checks_claim ON claim_carrier_checks(corrected_claim_id);
