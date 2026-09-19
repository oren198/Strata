-- Strata migration: record failed publication judgments as events, with a
-- terminal marker (issue #90 follow-up).
--
-- 0005 gave publication acts their own record (publication_acts +
-- publication_judgments), mirroring contributions/judgments. What it did NOT
-- mirror is 0003 + 0007's reliability treatment: when
-- ScopeManager.judge_publication() raises, strata.publication.propose_publish
-- / propose_withdraw let the exception propagate with no attempt row and no
-- marker (see publication.py's former "deliberately simple" note). The
-- publish/withdraw act already exists in the record at that point (the
-- record never lies), so on every read surface it sits indistinguishable
-- from an act nobody has gotten around to judging yet — a judge crash is
-- silent and permanent.
--
-- This table is the exact publication-side counterpart of
-- judgment_attempts, folding in 0007's outcome column from the start (a
-- fresh table needs no two-step ALTER):
--
--   outcome IS NULL          the attempt was recorded, but nothing asserts
--                            the judge run ended there.
--   outcome = 'judge_failed' MECHANICAL marker: judge_publication() failed
--                            and the judge run is over. No judge or LLM is
--                            involved in writing this — same rule as 0007.
--
-- An event, never a verdict — this table has no decision column and cannot
-- enter publication_judgments, so a failure can never masquerade as an
-- 'accept'/'decline'.
--
-- OUT OF SCOPE FOR THIS MARKER: mechanically propagated withdrawals
-- (publication_acts."trigger" IS NOT NULL — ADR 0007 D3's directive-removal
-- path, see 0005's header comment). Those acts get no judgment row BY
-- DESIGN — they are a structural consequence of an already-judged internal
-- change, not a fresh judgment attempt — so they never write a row here
-- either; a read surface must tell that apart from "awaiting judgment" by
-- checking "trigger" itself, not by looking for an attempt.
--
-- EXISTING STRANDED PUBLISH/WITHDRAW ACTS ARE LEFT AS THEY ARE. Same append-
-- only rule as 0007: the record is never rewritten, and back-filling
-- 'judge_failed' onto an act whose terminality nothing observed would be
-- inventing history. They keep zero attempt rows and read as pending. New
-- terminal failures carry the marker from here on.

CREATE TABLE publication_judgment_attempts (
    id           TEXT PRIMARY KEY,
    act_id       TEXT NOT NULL REFERENCES publication_acts(id),
    error_class  TEXT NOT NULL,
    message      TEXT,
    attempted_at TEXT NOT NULL DEFAULT (datetime('now')),
    outcome      TEXT CHECK (outcome IS NULL OR outcome = 'judge_failed')
);

CREATE INDEX idx_publication_judgment_attempts_act ON publication_judgment_attempts(act_id);
