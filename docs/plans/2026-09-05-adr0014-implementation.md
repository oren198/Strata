# ADR 0014 implementation plan (#186)

Branch: `release/v1.11.0-adr0013`. One feature branch per phase, merged back
after the architect's verification (full suite, ruff, diff review, TDD
source-revert check). Phase A is serial; B, C, D run in parallel worktrees
after A merges; E last.

## Pins every implementer follows

1. **Coalescing is batch judgment.** A pending change event is a
   `subject="manager-refresh"` contribution. N pending for one scope = one
   `ScopeManagerBatchJudgment` (ADR 0011 D3): one amendment, one summary
   write, one verdict row per event. A judged row = a processed event; drain
   is idempotent by `rejudge_contribution`'s no-op-if-judged rule. Nobody
   builds a separate coalescing mechanism.
2. **One affected-set rule, topological, for every kind of change.**
   Chain children and `references_from` readers for a publication item; all
   chain descendants for a directive (operator directives on S → S and
   descendants). No presented index, no fallback (ADR 0014 D3).
3. **Every writer of a shared input emits an event.** MCP publish/withdraw;
   HTTP `POST /contribute`; CLI `cmd_operator_publish/supersede/retire`;
   `_cascade_withdraw_relays`; and any judgment's directive ops on a scope
   with descendants, including an ordinary `run_contribution` (own
   contribution is not a trigger for the scope itself — it IS for its
   descendants). Each writer gets a test that it emits.
4. **Change events are not judge outages.** Separate `change_events` table
   `{change_id, contribution_id, scope_id, item_id, kind, before, after,
   hop, processed_at}` linked to the contribution. `doctor`, `freshness`,
   `session_stats`, Console pending view and `strata_rejudge` text report
   refresh-pending separately from outage-pending.
5. **Drain-on-read never fails the read.** `JudgeUnavailable` in drain →
   record attempt-failed as today, return the perspective with
   `input_changes` still listing the unprocessed events. Drain takes
   `scope_lock` (ADR 0012); read/bind must not deadlock against an in-flight
   `run_contribution`.
6. **Three judge modes, not a bool.** `amendment_context_only` becomes a mode:
   ordinary / splice-refresh (admitting ops dropped) / input-change refresh
   (admitting ops allowed, change events rendered). The MANAGER REFRESH
   prompt block gets a sibling for input-change refresh. `cmd_launch`'s
   `_refresh_scope` stops detecting staleness by version comparison and just
   drains the queue — one mechanism, not two (ADR 0013 D7's stance).
7. **Damping emits only on structural change**: directive-id set changed
   and descendants exist, or published-item set changed and readers exist. A
   context-only rewrite never emits, so rewording cannot restart a wave.
8. **Change id is a parameter on the judge call**, threaded into
   `withdraw_published` → cascade and directive-op fan-out. Never a lookup.
9. **Presented index covers publication items only.** Ancestor/operator
   directives are structurally in the summary after splice; do not index
   twice.
10. **Vacuous-pass guard.** Every "engine never edited context" test first
    asserts the refresh's judgment row exists.
11. No `git stash`, no self-spawned agents, no live store, and no reference
    to any sibling product in files, commits or comments.

## Findings from Phase A (binding on B/C/D)

- **The chain parent's publication is never rendered to any judge.**
  `_read_judge_inputs` builds peer publications from referenced peers only.
  ADR 0013 D2 composes the parent's publication into the perspective; the
  judge must see the same thing or a refresh triggered by a parent
  publication change has nothing to judge against. **Phase C renders the
  parent's publication to the judge**, as a `PARENT PUBLICATION` block beside
  the peer block, non-binding, with the same "according to <scope>" rule.
- **`change_id` is scalar on the single judgment; a coalesced batch carries
  several.** Decision: the batch judge and `ScopeManagerBatchJudgment` take
  `change_ids: list[str]`; a derived change event is written as **one row per
  (change id, affected scope)**, so the once-per-id check stays a row lookup
  and a scope refreshes if ANY inherited id is unseen (equivalent to D4's
  "suppressed when all seen"). Phase C owns the type change.
- `change_events.kind` has no CHECK; Phase B settles the vocabulary
  (`published`, `amended`, `withdrawn`, `directive_appended`,
  `directive_superseded`, `directive_retired`, `operator_directive_changed`)
  and adds the constraint in its migration.

## Phases

- **A — storage and plumbing (serial, first).** Migration + `change_events`
  table; `change_id` threading through
  `ScopeManagerJudgment`/batch and the judge call signature; `context_sources`
  field on `_AmendmentJudgment` with subset validation (record only). No
  behaviour change; suite stays green.
- **B — emission and affected set.** Every writer in pin 3 emits, with
  inheritance and hop; affected-set computation per pin 2;
  enqueue = append manager-refresh contribution + change_events row.
- **C — drain and judge modes.** Drain in MCP server on bind/read via batch
  rejudge in input-change mode; three-mode judge; prompt sibling block;
  `strata refresh [SCOPE | --all]`.
- **D — surfaces.** `input_changes` in `compose_perspective` (unprocessed
  only), MCP payload, Console, `doctor` counts (pin 4).
- **E — docs and collapse.** CONTEXT.md § Change event, § Refresh, amended
  § Perspective; `_refresh_scope` collapse (pin 6); ADR 0011 D4 amendment note.

## Architect's independent check

Four-scope fleet with a reference cycle, one withdrawal: exactly one refresh
per scope, the change event in the absorber's record, the absorber's context
untouched by anything but its own judgment row, and — the addition case — a
parent publication added, child refresh runs.

Then: suite → bridge → real key.
