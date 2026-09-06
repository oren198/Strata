-- Strata migration: a judgment row names the summary version it produced.
--
-- ADR 0011 D3 made a coalesced batch ONE amendment — one summary write, one
-- `version` bump — however many verdict rows it recorded, and ADR 0014's
-- drain batches every refresh. "Each summary rewrite ties to exactly one
-- judgment" (strata-evals MEASURES.md Decision 4) was being checked by
-- counting rows against `version`; a batch breaks that by construction. The
-- tie is now recorded rather than inferred.
--
--   summary_version  The `ScopeSummary.version` this judgment's amendment
--                    wrote. A batch's accepted rows share one value. NULL for
--                    a decline (nothing written) and for every row judged
--                    before this migration — no backfill, no reinterpretation
--                    (ADR 0013 D7).

ALTER TABLE judgments ADD COLUMN summary_version INTEGER;
