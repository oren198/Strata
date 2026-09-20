-- Strata migration: a retirement records its GROUND (#209).
--
-- A scope-manager `retire` removes a directive with nothing replacing it, so the
-- record must say WHAT CHANGED: `ground` is the changed circumstance, in the
-- contribution's own words, that the judge was required to state and the engine
-- checked mechanically before the retirement was applied. NULL for rows that predate
-- the column and for operator retirements, which are the operator's own decision and
-- need no ground.
ALTER TABLE retirements ADD COLUMN ground TEXT;
