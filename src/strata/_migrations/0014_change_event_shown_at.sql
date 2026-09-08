-- Strata migration: a notice written for READERS, not for a refresh
-- (ADR 0014 D1 as amended, issue #197).
--
-- Every change event until now was a refresh trigger: `processed_at` said
-- whether the scope's judge had reconciled it, and that was the whole of its
-- lifecycle. An own-scope retraction is a different animal — the scope's own
-- judge already acted, so no refresh is owed, but the scope's other agents may
-- have read the item that has just gone and owe a revision. That notice is
-- born processed and needs its own consumption rule.
--
--   shown_at  When this notice was delivered to a reader in the perspective's
--             `input_changes`. NULL means "still owed to a reader". For every
--             event that is a refresh trigger, the drain-and-read IS the
--             delivery, so the row is stamped at birth and this column never
--             decides anything; only a born-processed SELF-notice is written
--             with NULL, and `compose_perspective` composes it until the read
--             that carries it stamps it.
--
-- Backfilled to `created_at` for every existing row: history is not re-notified
-- (ADR 0013 D7 — no reinterpretation of what is already written).

ALTER TABLE change_events ADD COLUMN shown_at TEXT;

UPDATE change_events SET shown_at = created_at;
