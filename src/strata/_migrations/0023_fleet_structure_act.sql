-- Strata migration: fleet structure acts (#247).
--
-- A fleet structure change is an authority act. It is recorded and never
-- judged. It is not operator memory: `operator_acts` is publish / supersede /
-- retire of the operator stratum, and a live row there is something an
-- outcome can name. A structure change must not enter that table, so it
-- cannot be composed as a directive or acted on.
--
-- Two new tables, neither of which references the memory tables, so this
-- migration does not rebuild anything that already holds rows:
--
-- fleet_change_proposals
--   A change that is waiting for the owner scope (or an ancestor, or the
--   operator), plus the applied and rejected rows kept so the id still
--   resolves. `owner_scope_id` is NULL when the change has no common
--   ancestor and only the operator may apply it.
--
-- fleet_structure_acts
--   One row per applied change. The operator-act record for the change:
--   change type, proposer position, approver position (a scope id or
--   'operator'), both mechanical flags, the owner scope (the LCA, NULL
--   when only the operator owns it), and the before/after topology of the
--   touched scopes (parent pointers and reference edges, JSON).

CREATE TABLE fleet_change_proposals (
    id                       TEXT PRIMARY KEY,
    change_type              TEXT NOT NULL,
    payload                  TEXT NOT NULL,
    proposer_position        TEXT NOT NULL,
    owner_scope_id           TEXT,
    widens_proposer_reach    INTEGER NOT NULL CHECK (widens_proposer_reach IN (0, 1)),
    changes_proposer_binding INTEGER NOT NULL CHECK (changes_proposer_binding IN (0, 1)),
    status                   TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected')),
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at              TEXT,
    resolved_by              TEXT
);

CREATE INDEX idx_fleet_change_proposals_status ON fleet_change_proposals(status);

CREATE TABLE fleet_structure_acts (
    id                       TEXT PRIMARY KEY,
    proposal_id              TEXT,
    change_type              TEXT NOT NULL,
    proposer_position        TEXT NOT NULL,
    approver_position        TEXT NOT NULL,
    widens_proposer_reach    INTEGER NOT NULL CHECK (widens_proposer_reach IN (0, 1)),
    changes_proposer_binding INTEGER NOT NULL CHECK (changes_proposer_binding IN (0, 1)),
    owner_scope_id           TEXT,
    before_topology          TEXT NOT NULL,
    after_topology           TEXT NOT NULL,
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_fleet_structure_acts_proposal ON fleet_structure_acts(proposal_id);
