# 14. Reactive re-judgement when a scope's composed inputs change

**Status:** Accepted (2026-09-05 — grilled to completion with the operator)

**Issue:** #186. Builds on ADR 0011 D4 (manager refresh), ADR 0013 (publication
as the only sharing channel), ADR 0007 D2/D3/D5 (judged propagation). Amends
ADR 0011 D4 (see D2).

## Context

A scope's memory changes only when an agent contributes to it. The inputs that
memory rests on can change with no agent involved: an upstream publication is
withdrawn, amended or added to; an ancestor adds or retires a directive; a
referenced peer changes its face; the operator corrects a binding directive.
Between contributions the scope is **evidence-blind** — it goes on asserting
what its inputs no longer support, indefinitely.

The rule fixed in #186, not reopened here: **a changed input triggers a judge
cycle; the judge decides.** Never a forced edit. A mechanical downstream
deletion would make publication binding, against philosophy.md Concept 8.

Facts read out of the code, not assumed:

- **A refresh path exists** (ADR 0011 D4): parent directives are spliced in
  mechanically, then the judge reconciles context; `append`/`publish` ops are
  dropped (`scope_manager.py:2988`). At the time of writing it ran only from
  `strata launch`, root-first up the bound scope's chain, and never for an
  MCP-only user. *(Corrected in implementation: the splice now lives inside
  the drain — D6's one refresh mechanism — so it runs wherever the drain
  runs, which includes every MCP bind and read. Root-first is not preserved;
  a parent's own drain happens on the parent's own read, which is the known
  gap below.)*
- **Directives carry per-item ids; context is one string.**
- **A refresh can retract from the scope's published face**
  (`withdraw_published` is not stripped) **but can never add to it** —
  publishing is a separate agent act. Additions therefore die at one hop today.
  The operator ruled this a bug, not a property.

## Decisions

### D1 — Every composed input change triggers, additions included

Trigger on any change to what `compose_perspective` would show the scope's
judge: an ancestor directive appended/superseded/retired; an operator directive
changed; a one-hop publication (chain parent's or referenced peer's) published,
amended or withdrawn.

Additions trigger exactly as removals do. A child that never re-judges after its
parent added something is as wrong as one that never re-judges after a
withdrawal. Termination is solved in D4, not by narrowing the trigger.

A scope's own contribution is not a trigger; it already has a path.

> **Amended at the 1.11.0 gate (#197).** A scope's own contribution is never a
> *refresh* trigger for itself — its judge authored it, so there is nothing
> left to reconcile — but a **retraction** IS notice to the scope's own
> readers. When an amendment retires or supersedes one of the scope's own
> directives, or withdraws one of its own published items, a change event is
> written to the scope itself (`source_scope_id` = the scope), *born
> processed*: no refresh is queued and `drain_is_noop` stays true. The reason
> is the fleet premise itself — a scope is a mix of agents, not one mind, and
> another agent in it may have read the item and acted on it. Silent removal
> is how decay behaves; a retraction is a correction, and a correction owes
> notice. Additions are outside this: nothing a reader already holds stops
> being true when a directive is appended, and the reader's own next read
> composes it.

### D2 — The trigger runs the manager-refresh path; `publish` allowed, `append` not

> **Amendment (2026-09-06, ADR 0015 D1/D6):** the parent-splice refresh this
> decision distinguishes itself from no longer exists. There are two judge
> modes, `ordinary` and `input_change_refresh`, and a drain is always the
> latter — so the paragraph below about "the drop of both admitting ops stays
> for the parent-splice refresh" now describes nothing. Everything this
> decision says about the input-change refresh itself is unchanged.

The triggered cycle is ADR 0011 D4's refresh, **amended**: on an input-change
refresh the judge's amendment may carry `publish` ops as well as
`new_context`, lifecycle ops and `withdraw_published`. `append` stays dropped
on this path too: in a refresh the only contribution in the batch is the
change notice, and `append` copies the triggering contribution's content and
subject verbatim (ADR 0011 D1) — it would mint a directive whose text is the
mechanical change payload under the subject `manager-refresh`. `publish`
carries the judge's own words on the notice's id and provenance, which is all
the rationale below needs. The drop of both admitting ops stays for the
parent-splice refresh, where the splice already did the work —
that is a distinction between the two judge MODES, not between two entry
points: the splice runs inside every drain, wherever the drain runs, and a
drain that both splices and has change events pending judges once, in
input-change mode.

Why the amendment is now safe: ADR 0011 dropped admitting ops because a refresh
had no real contribution to mint a directive from. It now does — the change
event is a record row (D5), so a directive published from it carries honest
provenance: this entered because input X changed.

The engine never edits the scope's memory. Only the scope's judge does,
exercising the scope's authority.

> **Amended at the 1.11.0 gate (#198).** `publish` is dropped on an
> input-change refresh as well as `append`. The memory-governance-bench run
> showed the judge re-admitting every notice as the hearer's own material — a
> peer's *note* republished as the hearer's binding *rule*, an ancestor's or
> operator's directive republished "per inherited directive" — with the hearer
> as origin, so a withdrawal at the source never reached the copy (D4b cascades
> relays only). The changed input is already composed for every reader
> (ADR 0013/0015); the refresh has nothing of its own to admit from it. The
> amendment on this path is `new_context` (never restating the changed input),
> `supersede`/`retire` of the scope's own directives, and `withdraw_published`
> of its own face — ADR 0011 D4's shape with `withdraw_published` kept. The
> rationale above ("a directive published from the notice carries honest
> provenance") was true and beside the point: provenance was honest, authority
> was manufactured.

> **Amended again (2026-09-08, #198 third form).** The refresh's `new_context`
> is now mechanically dropped when every pending event on the refresh is an
> **addition** — `published`, `amended`, `directive_appended`. The amendment
> may then carry lifecycle ops (`supersede`, `retire`) and `withdraw_published`
> only; the drop is noted in the judgment record. When at least one pending
> event is a **removal** — `withdrawn`, `directive_retired`,
> `directive_superseded`, `operator_directive_changed` — `new_context` stays,
> because the scope may be asserting something its inputs no longer support and
> dropping that belief is the refresh's whole purpose. `directive_unspliced`
> (ADR 0015 D5) is neither, and an unrecognised kind is neither: the rule locks
> on a positive classification, never on the absence of a removal, so anything
> unclassified keeps the old behaviour. `append`/`publish` stay dropped on
> every refresh; an ordinary contribution is untouched.
>
> Why mechanical, when the prompt already said it: the paragraph above forbids
> restating the changed input, and a 235B judge restated a peer's publication
> into the listener's own context *with attribution* — "…— according to
> billing" — and called that acknowledging. The reader was then shown the same
> claim twice, once in the publication layer with billing as origin and a
> receipt, once in its own context with no receipt. A prompt obligation is a
> request; the reader's guarantee has to be a property of the engine. On an
> addition there is nothing of the scope's own to reconcile, so the only thing
> a rewrite can carry IS the restatement — which is what makes the drop safe
> rather than lossy.

### D3 — The affected set is topological, one rule for every kind of change

The scopes affected by a change to item X are the scopes that compose X: for a
publication item, the source's chain children and every scope whose reference
edge points at the source (`FleetConfig.references_from`); for a directive, the
holding scope's chain descendants (an operator directive on S: S and its
descendants). The same rule for an addition, an amendment and a withdrawal.

Rejected: a "presented index" of which items each judge was actually shown,
used for removals only. It is a strict subset of the topological set — anyone
shown X is one hop from X's source by construction — so it buys only precision:
skipping scopes that never read since X appeared. That precision costs a second
table, a second rule (additions have no index rows and must use topology
anyway), a retro-fill fallback, and a place for the two to disagree. A spurious
refresh costs one judge call, which fixpoint damping (D4) then stops. One
mechanism.

`_AmendmentJudgment` gains `context_sources: list[str]` — the ids the judge
declares its `new_context` rests on. It is **record, not trigger**: it tells an
operator what the judge actually used, shows an agent what is new, and lets the
judge's declaration be audited against what was rendered. The engine validates
it is a subset of the rendered item ids and notes anything else in the record.

### D4 — One refresh per scope per change id

Every independent input change mints a **change id**. Every change derived from
processing it — a refresh's admitted directive, its `withdraw_published`, a
relayed withdrawal from `_cascade_withdraw_relays` — **inherits** that id. A
scope refreshes for a given change id **at most once**. Coalescing: several
pending changes for one scope collapse into one refresh, whose derived changes
carry the union of their ids.

Inheritance is the whole guarantee. With fresh ids per derived change the
visited set would bound nothing and a reference cycle would run forever. Chain
edges form a tree and need none of this; reference edges may form cycles and
need all of it.

Also: **fixpoint damping** — a refresh that changes nothing emits no derived
change; and a **hop budget** as a backstop for bugs, recorded when hit.

**Cost, stated plainly.** On a reference cycle A↔B: A refreshes for change E,
B reacts and republishes carrying E, A does not refresh again for E. A is
correct about the original change and one step behind B's reaction to it, until
an independent change touches A. The operator chose this knowingly over an
unbounded wave.

### D5 — Notice is immediate and mechanical; it is the same row as the trigger

At trigger time the engine appends to each affected scope's **record** a
contribution with `subject="manager-refresh"` — the vehicle the refresh path
already uses, no new row type — whose content is the change payload: change id,
item, source scope, kind, previous and current state. That row is at once the
permanent auditable notice, the judge's input on the refresh, and mechanical
(no LLM writes it, matching ADR 0013 D4b).

`compose_perspective` gains an `input_changes` section carrying the scope's
**unprocessed** change events. An event is consumed once a refresh has processed
it, whatever the verdict; the record keeps it forever. Notice is never left to
the judge's prose — prose condenses away under a word budget, and notice that
can vanish is not notice.

> **Amended at the 1.11.0 gate (#203, #197).** "Notice is immediate; only
> absorption is deferred" needs two things this decision left implicit,
> because a read DRAINS before it composes (D6) and composition filtered to
> unprocessed events — so a successful drain handed the agent a reconciled
> summary and no notice at all, the reader who paid for the refresh being the
> one reader never told.
>
> 1. **The read that drains shows what it drained.** `drain_scope` returns the
>    events it processed; the read surface composes them into `input_changes`
>    on that read, and they are gone on the next. A bind drains too, and hands
>    them back on its own result in the same verbatim shape.
> 2. **A notice with no refresh behind it is consumed by being shown, not by
>    being drained.** The own-retraction notice of D1's amendment is born
>    processed, so the drain will never consume it; it carries `shown_at`
>    instead, and is composed until one read of the scope delivers it. One new
>    column, no second queue.

### D6 — Refresh runs inside the MCP server, on read; no daemon, no CLI needed

> **Amendment (2026-09-06, ADR 0015 D3/D5/D6):** a drain is three things now,
> in this order: the one-off unsplice of legacy copied rows (ADR 0015 D5), the
> mechanical sweep of published items an ancestor's retirement un-anchored
> (D3), then the judged cycle this decision describes. The first two need no
> judge, which is what a keyless server still does at read time; the parent
> splice that used to run alongside them is gone (D1). One consequence for
> this decision's read-path economics: with nothing unconditional left to do,
> "nothing pending" is now the whole of `drain_is_noop`, so a quiet read costs
> what reading a current scope costs.

`strata_withdraw`, `strata_publish`, operator edits and directive changes write
their change events and enqueue refreshes, then return. They never block on LLM
calls fanning across the fleet.

The queue for a scope is drained **by the MCP server when that scope is bound or
its perspective is read**, before composition, under the scope's lock. Nobody
can read a scope without the engine first attempting to bring it up to date;
if the judge is unavailable or no key is configured, the read still returns,
with `input_changes` listing what is owed (the refresh is deferred, never the
notice). The system is correct for
a user who never runs `strata launch`, `strata start` or any CLI. `strata
refresh [SCOPE | --all]` exists for the operator; `strata doctor` reports queue
depth and the oldest pending event.

No background worker in this version; one can be added without changing
anything decided here.

### D7 — No retro-fill

No stored state is rewritten (ADR 0013 D7). Because D3 is topological, the
live fleet's existing absorbed claims are covered from the first change event
with nothing to backfill.

> **Amendment (2026-09-06, ADR 0015 D5):** one deliberate exception, and the
> only one. Directive rows that the pre-1.11 splice copied into a descendant's
> summary are removed on that scope's first drain after the 1.11 release. They
> are not the state this decision protects: this decision protects **judged**
> state — memory a scope's own judge admitted — and a spliced row was never
> judged into the scope that holds it. The removal is mechanical, exact (a
> directive id is a contribution id, and a contribution belongs to exactly one
> scope's record), idempotent, and named in the record.

### D8 — A fleet structure change is an input change: `channel_removed` and `chain_changed`

> **Added 2026-10-10 (v1.18, #247 core half). Draft for the philosopher's check.**

`fleet.yaml` is now changed through Strata (#247): a bound scope proposes, the
owner (the lowest common ancestor of what the change touches) or the operator
applies, and each applied change is recorded once in `fleet_structure_acts`,
never judged. Until this decision the apply emitted nothing and printed a
stated-limit line instead, because the settled vocabulary (D1) had no faithful
kind for "a channel this scope read from is gone" or "the chain above this
scope moved". Reusing `withdrawn` or `directive_retired` would be false
notice: nothing was withdrawn and no directive was retired; the reader simply
stopped composing it.

**What a structure change owes** (the philosopher, 2026-10-07): notice exactly
where the change alters what binds a scope, or cuts a channel a live
attribution depends on; everywhere else the next read is the notice.

| Change | Event | Affected | Refresh |
|---|---|---|---|
| Reference edge added | none | — | — (the next read composes it; D1's addition rule) |
| Reference edge removed | `channel_removed` | the reader (`from`) | yes: removal-class |
| Scope removed | none of its own | — | — (see below) |
| Chain edge changed (re-parent) | `chain_changed` | the moved scope and every chain descendant | yes: removal-class, own-directive ops held |
| Description changed | none | — | — (relevance is judged against it from then on) |

**Scope removed.** #247 refuses to remove a scope that still has chain
children or reference edges. Every reader has therefore already received
`channel_removed` from the edge removal that had to come first, and no
descendant exists to lose a binding. A removed leaf's chain parent never
composed the leaf. The act owes nothing further, and the scope's memory is
kept. If the refusal is ever relaxed, the removal emits `channel_removed` to
each reader and `chain_changed` to each descendant, exactly as the edge
removals and re-parents it would replace.

**`channel_removed`.** The payload carries the act id, the reader, the source,
and the source's publication item ids as they stood when the edge was removed
(read from the source's current publication at apply time). The reader's
refresh is removal-class (D2's #198 third form), so `new_context` stays. The
judge is told the source is no longer composed: any context resting on it
(its `context_sources`, or attribution "according to <source>") can no longer
be kept live from that channel, and the judge re-grounds it on what is still
rendered or lets it fade. Nothing is corrected: the source's claims were not
found wrong, and `claim_corrected` stays reserved for that. `supersede`/
`retire` of the reader's own directives are allowed on this refresh as on any
removal refresh (D2).

**`chain_changed`.** The payload carries the act id, the moved scope, the
chain before and after (root-first ids), and, for the receiving scope, the
inherited directives it gained and lost (ids and text, computed from the
before/after topology the act records). Then:

1. **Every affected scope refreshes** against its new composed inputs. The
   refresh is removal-class, since some inherited directives and a parent
   publication stopped binding or being composed. The authority's act of
   moving the scope is a legitimate refresh input.
2. **The scope's own directives are never retired or superseded by this
   refresh.** The refresh runs on the scope manager's authority, but this
   change was made by an authority above the scope, and the position gate
   (1.17) says only a session bound to the scope changes its directives.
   `supersede`/`retire` ops on a refresh whose pending events include
   `chain_changed` are dropped mechanically, and the drop is noted in the
   judgment record.
3. **Conflicts are surfaced, mechanically.** At emit time the engine runs the
   1.17 inherited check (`inherited_conflict`) on each own directive of each
   affected scope against each newly inherited directive. Every conflict is
   written into that scope's notice:

   > Own directive <id> conflicts with newly inherited <id> (<scope>): <reason>. It still stands; a session bound to <scope> decides.

   `input_changes` carries it to every reader of the scope. The operator sees
   the same list in the Console with the act.
   - The check's stated limits apply. A conflict phrased with no value,
     polarity or marker is not surfaced, and the refresh's judge may still
     notice it in prose.
   - The newly inherited directive binds from the moment of the move,
     whatever the own directive says. That is composition, unchanged.

**Emission is the engine's operation, not the peripheral's.** `strata.change_events`
gains one whole operation, `emit_structure_change(act_id)`. It reads the act's
recorded before/after topology, never today's fleet, so a later structure
change can't redirect the notice. It computes the affected set by the table
above and emits through the existing `emit` machinery (change ids, D4 once per
scope, D5 notice row, the hop budget). The apply path in `fleet_changes` calls
it once, after the act row is committed and under the same cross-process
fleet lock. Its source-scan test changes from "never calls emit" to "calls
only `emit_structure_change`".

An emission failure is recorded the way `_record_emission_failure` records one
today. The applied change stands: the file and the act are the truth.
- `emit_structure_change` is idempotent per act. Change events carry the act
  id, and an act that already has events emits nothing.
- `strata doctor` lists acts with no events.
- The operator re-runs the emission for one act from the CLI.

**Vocabulary and schema.**
- `channel_removed` and `chain_changed` join the settled kinds in
  `change_events.py` and the `change_events.kind` CHECK. The CHECK is rebuilt in
  one migration, the next free number after the v1.18 peripheral migrations,
  the same recreate-table pattern as 0011, 0012 and 0021.
- Both join `_REFRESH_REMOVAL_KINDS`, so `new_context` is never dropped on
  their refresh.
- Neither is in `RETRACTION_KINDS`. Nothing the source owned was taken back;
  the reader's composition changed, and the reader is the one told.

**Rejected:**
- Reusing `withdrawn` for a cut channel: it states a withdrawal that never
  happened, and a reader would doubt a claim nobody retracted.
- Reusing `directive_retired` for a re-parent: same falsehood, and it would
  invite the refresh to treat the old parent's directives as wrong.
- Auto-retiring an own directive that conflicts after a move: an authority
  above the scope would change the scope's own rules through the back door,
  which is exactly what the position gate forbids.
- Emitting from today's topology at drain time: a second move before the
  drain would rewrite whom the first move notified.

## Known gap — transitive staleness under read-time drain

Documented and left open. With D6, C reads B, B reads A, A changes: C's drain
refreshes C against B's *current* face, but B's face is stale until B itself is
bound or read. C sees A's change only after B does an operation.

Candidate fix, not decided: drain a scope's one-hop sources before the scope
itself, recursively with a visited set — the way `strata launch` already walks
the fleet root-first before draining each scope. It makes reading C run B's judge, which is B's
authority exercised on B's memory, but it also makes one read fan LLM calls
across the fleet. Revisit with data on how often the gap bites.

## Consequences

- Withdrawal and addition both reach readers that absorbed a claim, not only
  those that relayed it verbatim.
- Cost rises: an input change can wake descendants and referencing peers, one
  LLM call each. Coalescing and fixpoint damping bound it in practice, D4's
  once-per-id rule bounds it absolutely.
- Judge schema and prompt change (`context_sources`, admitting ops on refresh).
  The release's eval gate covers it; any bridge run before this lands is stale.
- Perspective gains `input_changes`; touches composition, MCP surface, Console.
- 2026-09-08 (#202): the self layer also gains `condensation` —
  `{condensed, context_contributions_absent}`. A refresh that reflows context
  is exactly the shortening D5's notices set in motion, and until now nothing
  told the reader it had happened: a reader cannot distinguish "condensed
  away" from "never admitted", so both owe the same disclosure. Both halves
  are derived from the summary and the record with no judge in the loop, and
  both over-approximate (word count cannot see a same-length rewrite;
  a substring test counts a paraphrase as absent) — deliberately, since
  over-disclosure is the safe direction for a signal about what is missing.
- ADR 0011 D4 is amended as in D2. CONTEXT.md needs § Change event, § Refresh,
  and an amended § Perspective — done, this release (§ Publication, §
  Directive and § Operator amended too, each noting it is a source of change
  events).

## Evals this implies (for the operator's decision, not changed here)

1. **Delivery** — absorbed claim withdrawn upstream; absorber's next perspective
   carries the event and its refresh runs.
2. **Addition** — parent adds a publication; child's refresh runs and may admit.
3. **Non-binding** — engine never edited the absorber's context.
4. **Termination** — reference cycle, one change, bounded refresh count.
5. **Source honesty** — declared `context_sources` vs the ids rendered to the judge.

Hand-built judgments (bench `_MechanicalJudge`, evals `ScriptedJudge`) default
`context_sources` empty; that is expected, not a bug — the trigger is D3's
topology, which needs no judge cooperation.

## Rejected

- **Removals-only trigger.** Leaves addition-driven staleness; and its
  monotonicity argument was false anyway once refresh can admit.
- **Fresh ids for derived changes.** The visited set bounds nothing. See D4.
- **TTL alone.** Arbitrary, and permits repeated re-judging inside the budget.
- **Declared sources as the trigger.** A judge under-declaring is a silent miss.
  Kept as record (D3), never as trigger.
- **A presented index for removals.** A second mechanism beside topology,
  buying precision only. See D3.
- **Mechanical downstream deletion.** Makes publication binding.
- **Drain at `strata launch`/`strata start`.** Never runs for an MCP-only user.
- **Synchronous cascade.** A withdrawal would take as long as the deepest
  subtree's LLM calls.
