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
  dropped (`scope_manager.py:2988`). It runs from `strata launch`, root-first
  up the bound scope's chain. It never runs for an MCP-only user.
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

### D2 — The trigger runs the manager-refresh path, with admitting ops allowed

The triggered cycle is ADR 0011 D4's refresh, **amended**: on an input-change
refresh the judge's amendment may carry `append` and `publish` ops as well as
`new_context`, lifecycle ops and `withdraw_published`. The drop of admitting ops
stays only for the launch-time parent-splice refresh, where the splice already
did the work.

Why the amendment is now safe: ADR 0011 dropped admitting ops because a refresh
had no real contribution to mint a directive from. It now does — the change
event is a record row (D5), so a directive minted from it carries honest
provenance: this entered because input X changed.

The engine never edits the scope's memory. Only the scope's judge does,
exercising the scope's authority.

### D3 — The affected set is mechanical: the presented index

Per judgment, the engine records **which input items it showed the judge** —
publication items, ancestor and operator directives — as an index
`item_id -> scope_id` in the database, never in the summary markdown. The
affected set for a changed item is that index's rows plus, structurally, every
scope holding the item as a directive.

Presented, not declared, is the trigger because it is mechanical and cannot
miss: a spurious refresh costs one LLM call; a missed one costs correctness.

Update semantics: a judgment that sets `new_context` **replaces** the scope's
rows with what was presented to that judgment; a judgment that leaves context
untouched, and every decline, **carries** existing rows.

`_AmendmentJudgment` additionally gains `context_sources: list[str]` — the ids
the judge declares its `new_context` rests on. It is **record, not trigger**: it
tells an operator what the judge actually used, shows an agent what is new, and
lets declared-vs-presented divergence be measured. The engine validates it is a
subset of the presented set and notes anything else in the record.

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

### D6 — Refresh runs inside the MCP server, on read; no daemon, no CLI needed

`strata_withdraw`, `strata_publish`, operator edits and directive changes write
their change events and enqueue refreshes, then return. They never block on LLM
calls fanning across the fleet.

The queue for a scope is drained **by the MCP server when that scope is bound or
its perspective is read**, before composition, under the scope's lock. Nobody
can read a scope without first bringing it up to date. The system is correct for
a user who never runs `strata launch`, `strata start` or any CLI. `strata
refresh [SCOPE | --all]` exists for the operator; `strata doctor` reports queue
depth and the oldest pending event.

No background worker in this version; one can be added without changing
anything decided here.

### D7 — No retro-fill; an unindexed scope depends on its whole presented set

The index starts empty and no stored state is rewritten (ADR 0013 D7). Until a
scope's first post-release judgment writes rows, it is treated as depending on
all its current one-hop inputs — so the live fleet's existing absorbed claims
are covered from day one.

## Known gap — transitive staleness under read-time drain

Documented and left open. With D6, C reads B, B reads A, A changes: C's drain
refreshes C against B's *current* face, but B's face is stale until B itself is
bound or read. C sees A's change only after B does an operation.

Candidate fix, not decided: drain a scope's one-hop sources before the scope
itself, recursively with a visited set — the way the launch-time refresh already
goes root-first up the chain. It makes reading C run B's judge, which is B's
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
- ADR 0011 D4 is amended as in D2. CONTEXT.md needs § Change event, § Refresh,
  and an amended § Perspective.

## Evals this implies (for the operator's decision, not changed here)

1. **Delivery** — absorbed claim withdrawn upstream; absorber's next perspective
   carries the event and its refresh runs.
2. **Addition** — parent adds a publication; child's refresh runs and may admit.
3. **Non-binding** — engine never edited the absorber's context.
4. **Termination** — reference cycle, one change, bounded refresh count.
5. **Source honesty** — declared `context_sources` vs presented set.

Hand-built judgments (bench `_MechanicalJudge`, evals `ScriptedJudge`) default
`context_sources` empty; that is expected, not a bug — the trigger is D3's
presented index, which needs no judge cooperation.

## Rejected

- **Removals-only trigger.** Leaves addition-driven staleness; and its
  monotonicity argument was false anyway once refresh can admit.
- **Fresh ids for derived changes.** The visited set bounds nothing. See D4.
- **TTL alone.** Arbitrary, and permits repeated re-judging inside the budget.
- **Declared sources as the trigger.** A judge under-declaring is a silent miss.
  Kept as record (D3), never as trigger.
- **Mechanical downstream deletion.** Makes publication binding.
- **Drain at `strata launch`/`strata start`.** Never runs for an MCP-only user.
- **Synchronous cascade.** A withdrawal would take as long as the deepest
  subtree's LLM calls.
