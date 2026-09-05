# 14. Reactive re-judgement when a scope's composed inputs change

**Status:** Proposed (2026-09-05 — drafted for grilling; no code until Accepted)

**Issue:** #186. Builds on ADR 0011 D4 (manager refresh), ADR 0013 (publication
as the only sharing channel), ADR 0007 D2/D3/D5 (judged propagation).

## Context

A scope's memory changes only when an agent contributes to it. The inputs that
memory rests on can change with no agent involved: an upstream publication is
withdrawn or amended, an ancestor adds or retires a directive, a referenced peer
changes its face, the operator corrects a binding directive. Between
contributions the scope is **evidence-blind** — it goes on asserting what its
inputs no longer support, indefinitely.

The rule the operator fixed in #186, and which this ADR does not reopen: **a
changed input triggers a judge cycle; the judge decides.** Never a forced edit.
A mechanical downstream deletion would make publication binding and would
contradict philosophy.md Concept 8 ("a publication informs; it never binds a
reader"). Reactive judging, yes. Reactive editing, never.

Three facts about the engine as it stands shape everything below; all three were
read out of the code rather than assumed.

**The refresh path already exists.** ADR 0011 D4 splits a refresh in two: the
parent's directives are spliced into the scope's summary mechanically, and the
judge that follows may carry only `new_context` and lifecycle ops (`supersede`,
`retire`) — `append` and `publish` are dropped (`scope_manager.py:2988`). It
runs today from `strata start`, root-first up the chain, and the refresh request
is itself appended to the record as a contribution with `subject="manager-refresh"`
(`__main__.py:2075`). This ADR does not invent a re-judgement mechanism; it
changes **what triggers that path** and extends its reach.

**Directives already carry per-item identity; context does not.** A summary's
directives are `Directive` rows with ids and provenance, so "does this scope
still hold item X?" is answerable structurally, for free. The context section is
a single opaque string. Everything hard here is about context.

**A refresh can shrink a scope's face but cannot grow it.** `withdraw_published`
is *not* stripped on the refresh path — only `append`/`publish` ops are. So a
refresh may retract a published item whose belief it drops, but may never
publish anything new. See D2b, which is the load-bearing consequence.

## Decisions

### D1 — Every composed input change triggers, additions included

A refresh is triggered by any change to what `compose_perspective` would put in
front of the scope's judge:

- an ancestor's directive appended, superseded or retired (already triggers, at
  launch only — now push-triggered);
- an operator directive added, changed or removed for a chain scope;
- a one-hop publication — the immediate chain parent's, or a referenced peer's —
  published, amended, or withdrawn.

**Additions trigger exactly as removals do.** An earlier draft restricted
triggers to removals, on the argument that removals are monotone and therefore
terminate. That argument is wrong twice over. It is unsound: a judge cycle can
create material, so a removal-triggered wave is not monotone either. And it is
incomplete: if a parent adds to its publication and the child never re-judges,
the child's memory is as false as if something had been withdrawn — stale in the
other direction. Termination is a real problem and is solved on its own terms in
D4, not by narrowing the trigger until the problem disappears along with half
the correctness.

A scope's own new contribution is not a trigger: it already has a path.

### D2 — The trigger runs the existing manager-refresh path

The triggered cycle is ADR 0011 D4's refresh: ancestor directives are spliced in
mechanically, then the judge reconciles the context digest with the refreshed
state, its amendment carrying `new_context`, lifecycle ops, and
`withdraw_published`.

This is what makes "the judge decides, never a forced edit" true by construction
rather than by prompt obligation. The engine cannot rewrite the scope's context;
only the scope's own judge can, exercising the scope's own authority.

### D2b — The refresh path's publish asymmetry is kept, deliberately

A refresh may **retract** from the scope's published face and may never **add**
to it. Three consequences, all intended:

1. **Addition-driven waves die at one hop.** A parent adds to its publication;
   the child refreshes and may absorb it into context, but cannot re-export it.
   Passing it on takes an agent's contribution — a deliberate act, which is what
   ADR 0013 D3's one-edge rule already asked for. The child is corrected without
   the fleet being flooded.
2. **Removal-driven waves do propagate**, including across reference edges, via
   `withdraw_published`. This is exactly the case where propagation is *owed*:
   the reader that paraphrased a now-dead claim into its own face is the one
   D4b's mechanical relay cascade cannot reach, because a paraphrase is not a
   relay. So the storm risk in D4 is real, and it is real specifically for
   retraction — the direction where stopping early would leave a false claim
   standing.
3. Fixpoint damping on the published-item set (D4.2) is therefore meaningful on
   this path, but only ever in the shrinking direction.

Enabling `publish` on the refresh path would reopen ADR 0011 D4 and turn every
upstream addition into a fleet-wide fan-out. Not in this ADR.

### D3 — The affected set comes from a dependency index of judged sources

The engine records, per judgment, **which input items the judge accepted and
based its context on**. `_AmendmentJudgment` gains:

```
context_sources: list[str] = []
```

— the ids of the items (publication items, ancestor directives, operator
directives, contributions) the amendment's `new_context` rests on. The engine
validates the declared set is a subset of what was rendered to that judge and
notes unknown ids in the record; a judge cannot claim a source it was never
shown.

Stored as an index (`item_id -> scope_id`) in the database, never in the summary
markdown. The affected set for a changed item is that index's rows, plus —
structurally, needing no declaration — every scope whose summary carries the
item as a directive.

**Index update semantics**, because an implementer will otherwise guess:

- an amendment with `new_context` set **replaces** the scope's rows with the
  declared set — the new context is the only context, so its sources are the
  only sources;
- an amendment with `new_context is None`, and every decline, **carries the
  existing rows unchanged** — an untouched context still rests on what it rested
  on.

Append-only would leave scopes permanently "affected" by items they dropped;
replace-without-carry would silently lose every dependency on any amendment that
left the context alone.

D4 provenance closure applies: an item republished as X is reached through X. The
mechanism is D6's — `_cascade_withdraw_relays` emits one change event per relayed
withdrawal — not a separate computation.

**The honest risk.** A declared set is only as good as the declaration. A judge
that silently under-declares leaves a scope believing a retracted claim, and the
failure is invisible — the shape of the vacuous-pass trap. Mitigations: the
subset validation catches over-declaration, and the **presented** set is recorded
alongside the declared one, so divergence is auditable after the fact without
changing the trigger. D7 makes the presented set the live fallback rather than a
hypothetical one.

### D4 — A wave terminates by causal wave identity, damped by fixpoint

Chain edges form a tree, so a directive wave terminates on topology. Reference
edges may form cycles (CONTEXT.md § Reference edge), and a judged wave has no
monotonicity argument to lean on. Four mechanisms, in order of what each buys:

1. **Causal wave id with a visited set — the guarantee.** An *independent* input
   change (an agent publishes, an operator edits, a withdrawal is requested)
   mints one wave id. Every change **derived** from processing that wave —
   including a refresh's own `withdraw_published`, and every relayed withdrawal
   it cascades — **inherits** that wave id rather than minting a fresh one. A
   scope processes a given wave id at most once, ever. Inheritance is the whole
   guarantee: if derived changes minted new ids, the visited set would bound
   nothing, and A→B→A→B on a reference cycle would run forever. Under coalescing
   (D4.3), an onward change carries the **union** of the wave ids that caused it,
   and is suppressed at a scope that has seen all of them.
2. **Fixpoint damping — the efficiency.** A refresh that changes neither the
   scope's directive-id set, its published-item set, nor its context emits no
   onward change. Most waves die one hop from their origin, because most upstream
   changes do not move a downstream summary at all.
3. **Coalescing.** Several pending triggers for one scope collapse into one
   refresh. A root directive change fans out across a subtree and each refresh is
   an LLM call; without coalescing a busy root multiplies cost by its descendant
   count.
4. **Hop budget.** A hard cap, recorded when hit. A backstop for bugs, not a
   design mechanism.

**The cost, stated plainly and not softened.** Visited-set semantics buy
termination with **residual staleness on a reference cycle**: A processes wave
E, B reacts to A and republishes carrying E, and A does *not* re-process E. A is
correct with respect to the original change and stale with respect to B's
reaction to it — and it stays that way until some **independent** change touches
A again. This is not "eventual convergence" in the sense of converging on its
own; nothing in the system re-enters that wave. It is a bounded, permanent
one-step lag per cycle, visible in the record because both change events are
there.

The alternative — re-entering a scope within one wave — is unbounded whenever the
judge does not converge, and the judge is not a monotone function. An unbounded
LLM wave is a worse failure than a one-step lag that the record makes visible.

### D5 — Notice is immediate; only absorption is deferred

The change event is written into the affected scope's **record** at trigger time,
mechanically, as a contribution with `subject="manager-refresh"` carrying the
change payload as content — the vehicle the refresh path already uses. No new
record row type. That single act does three things:

- it is the **notice** — the scope's record permanently and auditably carries
  the fact that an input it relied on changed, what it was, and its previous and
  current state;
- it is the **judge's input** on the refresh that follows, which is the
  operator's position in #186: judge according to the new input, no need to name
  a false note;
- it is **mechanical** — no LLM writes it, matching ADR 0013 D4b.

`compose_perspective` gains a mechanical `input_changes` section, read from the
**pending queue and record**, not from the refreshed summary. So an agent binding
to a scope whose refresh has not yet run still sees that an input changed. This
matters more than the refresh itself: it means the #186 complaint — the absorber
goes on asserting a dead claim — is answered at the moment of withdrawal, not at
the moment of the next drain. It is not the judge's job to write notice into
prose; prose can be summarised away, and notice that vanishes under a word budget
is not notice.

### D6 — The cascade is deferred, never synchronous with the caller

`strata_withdraw`, `strata_publish` and an operator edit return as soon as their
own act, and its change events, are durable. They do not block on a wave of LLM
calls fanning across the fleet; they report how many refreshes they enqueued.

The queue is drained at `strata start` — the point that already drains refreshes
— and on demand. **Drain scope: the bound scope's own chain**, matching today's
`_refresh_scope` walk. A peer's pending refresh runs when that peer's session
starts. This is defensible because D5 makes notice immediate: a scope that has
not drained still shows its agent that an input changed. What is deferred is the
judge's absorption, not the fleet's honesty.

This ADR deliberately does not introduce a background worker. A queue plus the
existing drain point is the smallest thing honest about when a refresh happens,
and a worker can be added later without changing anything decided here.

A pending refresh is visible: `strata doctor` reports queue depth and the oldest
pending event, so "this scope has not caught up" is a fact the operator can see
rather than an invisible lag.

### D7 — No retro-fill; unindexed scopes fall back to the presented set

The dependency index starts empty, and no stored state is rewritten — the stance
of ADR 0013 D7. But an empty index would make #186 inert for exactly the fleet
that motivated it: every currently absorbed claim has no rows, so nothing would
trigger until a coincidental contribution.

So: **a scope with no index rows is treated as depending on its entire presented
set** — all its current one-hop inputs — until its first post-release judgment
writes real rows. Over-approximate, mechanical, never misses. This also gives the
D3 fallback a defined activation condition instead of leaving it as "if evals
show a problem".

## Consequences

- Withdrawal becomes real for readers that absorbed a claim, not only for those
  that relayed it verbatim. Philosophy.md Concept 8's second obligation stops
  being aspirational.
- Cost rises: a publication change can wake descendants and referencing peers,
  each waking an LLM call. Fixpoint damping and coalescing bound this in
  practice, D2b's publish asymmetry bounds addition-driven waves structurally,
  and D4's hop budget bounds the worst case.
- `_AmendmentJudgment` gains a field and the prompt gains an obligation to
  declare sources. That is a judge-output change, so the release's eval gate
  covers it — and it re-dates any bridge run performed before this lands.
- The perspective payload grows a section, touching composition, the MCP surface
  and the Console.
- On acceptance, CONTEXT.md needs § Change event, § Refresh, and an amended
  § Perspective, as ADR 0013 did.

## Evals this implies

Not part of this ADR's change; listed for the operator's decision on the evals
repo.

1. **Delivery** — an absorbed claim is withdrawn upstream; the absorbing scope's
   next perspective carries the change, and its refresh is triggered.
2. **Non-binding** — the same setup, asserting the engine did *not* edit the
   absorber's context. The judge may keep the claim; a mechanical deletion must
   not happen.
3. **Termination** — two scopes on a reference cycle, one independent change,
   bounded refresh count.
4. **Source honesty** — a judge that bases context on a publication must declare
   it; the declared-vs-presented divergence is the metric.

Note for whoever writes these: hand-built judgments (the bench's
`_MechanicalJudge`, the evals' `ScriptedJudge`) default to `context_sources=[]`,
so those scopes never enter the index and fall to D7's presented-set path.
Expected behaviour, not a bug.

## Rejected alternatives

- **Trigger on removals only.** A sound-looking termination argument that is
  false (a judge cycle creates material), leaving addition-driven staleness
  uncorrected. See D1.
- **Fresh event ids for derived changes.** Makes the visited set bound nothing.
  See D4.1.
- **TTL alone.** A hop budget without wave identity still permits repeated
  re-judging inside the budget on a reference cycle, and the number is
  arbitrary. Kept only as a backstop.
- **Mechanical downstream deletion.** Makes publication binding. Rejected on ADR
  0013 D2 and philosophy.md Concept 8.
- **Per-item identity for context** (splitting the context string into
  addressable items). A larger model change than #186 needs; the declared-source
  index answers the same question without restructuring stored summaries.
- **Enabling `publish` on the refresh path.** Reopens ADR 0011 D4 and turns every
  upstream addition into a fleet-wide fan-out. See D2b.
- **Synchronous cascade.** Makes an ordinary withdrawal take as long as the
  deepest subtree's LLM calls, with no way to observe progress.
