# 14. Reactive re-judgement when a scope's composed inputs change

**Status:** Proposed (2026-09-05 — drafted for grilling; no code until Accepted)

**Issue:** #186. Builds on ADR 0011 D4 (manager refresh), ADR 0013 (publication
as the only sharing channel), ADR 0007 D3/D5 (judged propagation).

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

Two facts about the engine as it stands shape everything below.

**The refresh path already exists.** ADR 0011 D4 splits a refresh in two: the
parent's directives are spliced into the scope's summary mechanically, and the
judge that follows may carry only `new_context` and lifecycle ops (`supersede`,
`retire`) — `append` and `publish` are dropped. It runs today from
`strata start`, root-first up the chain, and the refresh request is itself
appended to the record as a contribution. This ADR does not invent a
re-judgement mechanism; it changes **what triggers that path**, from "a launch
happened" to "an input changed", and extends its reach from ancestor directives
to every composed input.

**Directives already carry per-item identity; context does not.** A summary's
directives are `Directive` rows with ids and provenance, so "does this scope
still hold item X as a directive?" is answerable structurally, for free. The
context section is a single opaque string. Everything hard here is about
context.

## Decisions

### D1 — Every composed input change triggers, additions included

A refresh is triggered by any change to what `compose_perspective` would put in
front of the scope's judge:

- an ancestor's directive appended, superseded or retired (already triggers, at
  launch only — now push-triggered);
- an operator directive added, changed or removed for a chain scope;
- a one-hop publication — the immediate chain parent's, or a referenced peer's —
  published, amended, or withdrawn.

**Additions trigger exactly as removals do.** An earlier draft of this ADR
restricted triggers to removals, on the argument that removals are monotone and
therefore terminate. That argument is wrong twice over. It is unsound: a judge
cycle can create material, so a removal-triggered wave is not monotone either.
And it is incomplete: if a parent adds to its publication and the child never
re-judges, the child's memory is as false as if something had been withdrawn —
stale in the other direction. Termination is a real problem and it is solved in
D4, on its own terms, rather than by narrowing the trigger until the problem
disappears along with half the correctness.

A scope's own new contribution is not a trigger: it already has a path.

### D2 — The trigger runs the existing manager-refresh path, unchanged

The triggered cycle is ADR 0011 D4's refresh: ancestor directives are spliced in
mechanically, then the judge reconciles the context digest with the refreshed
state. Its amendment may carry only `new_context` and lifecycle ops.

This is what makes "the judge decides, never a forced edit" true by construction
rather than by prompt obligation. The engine cannot rewrite the scope's context;
only the scope's own judge can, and only by exercising the scope's own
authority.

### D3 — The affected set comes from a dependency index of judged sources

To refresh only the scopes an item actually reached, the engine records, per
judgment, **which input items the judge accepted and based its context on**.
`_AmendmentJudgment` gains:

```
context_sources: list[str] = []
```

— the ids of the items (publication items, ancestor directives, operator
directives, contributions) the amendment's `new_context` rests on. The engine
validates the declared set is a subset of what was actually rendered to that
judge and drops unknown ids into `dropped_ops`-style record notes; a judge
cannot claim a source it was never shown.

These are stored as an index (`item_id -> scope_id`) in the database, not in the
summary markdown. The affected set for a changed item is that index's rows for
the item, plus — structurally, needing no declaration — every scope whose
summary carries the item as a directive.

D4 provenance closure applies: an item republished as X is reached through X, so
"everyone the claim reached" is computed transitively through the relay chain
that already exists, with no new machinery.

**The honest risk.** A declared set is only as good as the judge's declaration.
A judge that silently under-declares leaves a scope believing a retracted claim,
and the failure is invisible — the same shape as the vacuous-pass trap. Two
mitigations, both cheap: the subset validation above catches over-declaration,
and the presented set is recorded alongside the declared one, so a divergence
audit is possible after the fact without changing the trigger. If evals show
under-declaration in practice, the fallback is to trigger on the **presented**
set instead — strictly over-approximate, mechanical, never misses, at the cost
of spurious refreshes. That fallback needs no schema change beyond what this
decision already adds.

### D4 — A wave terminates by event identity, and is damped by fixpoint

Chain edges form a tree, so a directive wave terminates on topology. Reference
edges may form cycles (CONTEXT.md § Reference edge), and a judged wave has no
monotonicity argument to lean on. Four mechanisms, in order of what each buys:

1. **Event identity with a visited set — the guarantee.** Every input change
   mints one `change_event` id. It rides the wave. A scope processes a given
   event id at most once, ever. This is what actually bounds the wave, and it is
   the only mechanism that survives an oscillating judge — the judge is not a
   monotone function and two scopes on a reference cycle could otherwise flip
   each other forever.
2. **Fixpoint damping — the efficiency.** A refresh that changes neither the
   scope's directive-id set, its published-item set, nor its context emits no
   onward event. Most waves die one hop from their origin, because most changes
   upstream do not move a downstream summary at all.
3. **Coalescing.** Several pending triggers for one scope collapse into one
   refresh. A root directive change fans out across a whole subtree and each
   refresh is an LLM call; without coalescing a busy root multiplies cost by its
   descendant count.
4. **Hop budget.** A hard cap, recorded when hit. A backstop for bugs, not a
   design mechanism.

**The cost, stated plainly.** Visited-set semantics buy termination with
one-step staleness on a reference cycle: A refreshes on event E before B's
republication caused by E has landed, and does not refresh again for E. A is
then correct with respect to the original change and stale with respect to B's
reaction to it. B's republication is itself a new input change with its own
event id, so the next wave carries it — convergence is eventual, not immediate.
The alternative, re-entering a scope within one event, is unbounded whenever the
judge does not converge, and an unbounded LLM wave is worse than a one-step lag.

### D5 — Notice and trigger are the same artifact

The change event is written into the affected scope's **record** as a
system-originated entry naming what changed, where it came from, and its
previous and current state. That single act does three things:

- it is the **notice** — the scope's own record now carries the fact that an
  input it relied on changed, permanently and auditably;
- it is the **judge's input** on the refresh that follows, which is exactly the
  operator's position in #186: judge according to the new input, no need to name
  a false note;
- it is **mechanical** — no LLM writes it, matching ADR 0013 D4b's stance on
  relayed withdrawal.

For agents, `compose_perspective` gains a mechanical `input_changes` section
carrying the recent change events for the scope. It is not the judge's job to
write notice into prose; prose can be summarised away, and a notice that can
vanish under a word budget is not notice.

### D6 — The cascade is deferred, never synchronous with the caller

`strata_withdraw`, `strata_publish` and an operator edit return as soon as their
own act is durable. They do not block on a wave of LLM calls fanning out across
the fleet; they report how many refreshes they enqueued.

The queue is drained at `strata start` — the point that already drains refreshes
today — and on demand. This ADR deliberately does not introduce a background
worker: a queue plus the existing drain point is the smallest thing that is
honest about when a refresh happens, and a worker can be added later without
changing anything decided here.

A pending refresh is visible: `strata doctor` reports the queue depth and the
oldest pending event, so "this scope has not caught up yet" is a fact the
operator can see rather than an invisible lag.

### D7 — No retro-fill

The dependency index starts empty. Summaries judged before this release declared
no sources, so nothing is inferred for them, and no stored state is rewritten —
the same stance as ADR 0013 D7. Those scopes join the index the first time they
are judged after the release. Until then a changed item reaches them only where
it is structurally visible as a directive.

## Consequences

- Withdrawal becomes real for readers that absorbed a claim, not only for those
  that relayed it verbatim. Philosophy.md Concept 8's second obligation stops
  being aspirational.
- Cost rises: every publication change can wake descendants and referencing
  peers, each waking an LLM call. Fixpoint damping and coalescing are what keep
  this bounded in practice; D4's hop budget keeps it bounded in the worst case.
- `_AmendmentJudgment` gains a field and the prompt gains an obligation to
  declare sources. That is a judge-output change, so the release's eval gate
  covers it — and the evals repo needs a family for it (see below).
- The perspective payload grows a section, which touches composition, the MCP
  surface and the Console.

## Evals this implies

Not part of this ADR's change, listed for the operator's decision on the evals
repo:

1. **Delivery** — an absorbed claim is withdrawn upstream; the absorbing scope's
   next perspective must carry the change, and its refresh must be triggered.
2. **Non-binding** — the same setup, asserting the engine did *not* edit the
   absorber's context. The judge may keep the claim; what must not happen is a
   mechanical deletion.
3. **Termination** — two scopes on a reference cycle, one change event, bounded
   refresh count.
4. **Source honesty** — a judge that bases context on a publication must declare
   it; the divergence between declared and presented sets is the metric.

## Rejected alternatives

- **Trigger on removals only.** Sound-looking termination argument that is
  actually false (a judge cycle creates material), and it leaves addition-driven
  staleness uncorrected. See D1.
- **TTL alone.** A hop budget without event identity still permits a scope to
  re-judge repeatedly inside the budget on a reference cycle, and the number is
  arbitrary. Kept only as a backstop.
- **Mechanical downstream deletion.** Makes publication binding. Rejected on
  ADR 0013 D2 and philosophy.md Concept 8.
- **Per-item identity for context** (splitting the context string into
  addressable items). A larger model change than #186 needs; the declared-source
  index answers the same question without restructuring stored summaries.
- **Synchronous cascade.** Makes an ordinary withdrawal take as long as the
  deepest subtree's LLM calls, with no way to observe progress.
