# Strata — Glossary

The canonical vocabulary for Strata. Pure glossary: definitions only, no
implementation details, no design rationale, no scratch notes. If a term needs
explaining beyond what it *is*, the explanation belongs in an ADR or design
doc, not here.

---

## Scope

A bounded region of the fleet for which a piece of memory is relevant and
authoritative. Every scope belongs to exactly one **stratum**. Both agents and
memory attach to scopes.

## Stratum

A horizontal layer of scopes. Strata define the structure along which
**directives** propagate: directives flow *down* through strata (from a parent
scope to its descendants), never upward and never sideways.

The set of strata is defined by the fleet (e.g. `executive` → `function` →
`team` → `individual`); strata are named layers, not depths.

## Inter-stratum edge

The edge from a scope to its single parent scope in the stratum immediately
above. Every scope has **exactly one** inter-stratum parent (except the root).
Carries both directives and context downward.

## Intra-stratum edge (peer reference)

A reference from one scope to another scope on the **same** stratum. A scope
may have any number of peer references; together they form a DAG within the
stratum. Carries **context only** — directives published in a peer scope do
not bind the reader. What a peer reference delivers is the referenced
scope's **publication** — its curated outward face — never its full internal
summary. To make a peer's standard binding, it must be ratified into a
common ancestor scope (i.e. published as a directive at a stratum above
both).

## Agent

A `(session, skill, scope)` triple. All three are bound at spawn time and
fixed for the agent's lifetime — the agent cannot change session, skill, or
scope. To act differently, an agent spawns a sub-agent with the bindings it
needs. A sub-agent's scope binding is bounded by its spawner's: the same
scope or a descendant of it. Reach can only narrow through delegation, never
widen.

- **Session** — execution context and short-term memory; the lifetime.
- **Skill** — what this agent does; the specialization.
- **Scope** — position in the strata; where authority comes from.

An agent's own working state lives in its **short-term memory**; only what it
writes to Strata persists. Agents come and go; the fleet does not track them
individually beyond what provenance on their writes records.

## Session

The execution-context dimension of an **agent**: a single, time-bounded run
with short-term memory of its own. Sessions are transient; they end and do
not persist.

## Skill

The specialization dimension of an **agent**: the durable definition of *what
this agent does*. Skills outlive sessions — the same skill is instantiated
across many sessions over time. Examples: `scope-manager`, `code-writer`,
`evidence-summarizer`.

## Scope-manager

The **agent** whose **skill** is to curate the memory of a single scope. All
writes to a scope pass through its scope-manager, which judges every write
(auth check, supersession, dedup, conflict detection) and updates the scope's
**scope summary** accordingly.

The scope-manager is itself a regular Strata agent — Strata uses its own
primitives (session, skill, scope) to manage itself.

## Short-term memory

Memory that lives only within a single agent (session), never published to
Strata. The agent's local working state during its execution. It ceases to
exist when the session ends.

## Long-term memory

Memory written to Strata, persisting across agents. Everything Strata's
mechanics — scope, stratum, directive/context, authority, trust, forgetting —
operate on is long-term memory.

Each scope's long-term memory has two layers:

- The **record** is the append-only, immutable log of everything ever written
  to the scope. The source of truth for accountability and forensics.
- The **scope summary** is the curated, condensed representation of the
  scope's *current* state, maintained by the scope-manager. This is the
  working view — what downstream agents actually read.

## Record

The append-only, immutable log of every write ever accepted into a scope.
Owned per-scope. Never edited; supersession and retirement are *bookkeeping
on top of* the record, not changes to it.

## Scope summary

The curated, condensed working view of a scope, maintained by the
**scope-manager**. Updated on each accepted contribution. The scope summary
is what gets composed into agents' **perspectives** when they inherit from
this scope; the record is consulted only for accountability, recovery, or
forensics.

A scope summary has two sections:

- **Directives** — listed individually, each retaining its identity so it can
  be cited, superseded, or retired distinctly.
- **Context** — a condensed digest of relevant non-binding knowledge from
  this scope.

## Contribution

An agent's submission of memory to a scope's **scope-manager**. A
contribution is never a direct write — it is a proposal the scope-manager
judges. The scope-manager exercises the scope's full authority and may:

- **Accept as directive** — the memory binds the scope and all descendants.
- **Accept as context** — the memory informs the scope without binding it.
- **Decline** — the memory does not enter the scope summary.

A contribution carries the contributor's **proposed classification**
(`directive` | `context`), but this is a hint, not a constraint: the
scope-manager has the broader information (the full record, the inherited
perspective, accumulated trust) and is free to re-classify a contribution in
either direction — including upgrading peer-submitted context into a
directive.

Every contribution — accepted, classified, or declined — is appended to the
scope's **record** for accountability.

## Ratification

The act by a scope-manager of publishing a new **directive** based on
**context** accumulated within its scope. Ratification is how evidence
flows upward into binding authority: contributions that a scope-manager
accepted as context (e.g. peer-submitted observations) can, once a pattern
or consensus warrants, be consolidated into a directive published with the
scope's authority.

Ratification is not a separate primitive — it is a directive write by the
scope-manager, using its scope authority. The term names the *pattern* of
context-to-directive consolidation.

## Publication

The act by a scope of exporting a curated subset of its memory for scopes
that do not contain it — the sideways channel, counterpart to
**ratification** (which widens *binding* reach upward, where publication
widens *read* reach sideways, conveying no authority). Publishing is a
judged act by the publishing scope's authority, distinct from internal
acceptance: being in the scope's memory does not make an item published.

Properties of published memory:

- **Non-binding** to every reader; the only path to binding beyond a scope's
  subtree remains ratification.
- **Published within believed** — a scope publishes from its own memory
  only; when the source memory is superseded or retired, the publication
  follows.
- **Attributed** — publication-derived memory stays attributed to its source
  scope ("according to X") through composition and through condensation
  (summary rewrites) alike, and outcome-based **trust** feedback on it flows
  back to the source memory.
- **Never self-corroborating** — a publication does not count as independent
  corroboration for ratifying its own source.

## Supersession

The pattern by which one **directive** replaces another on the same subject.
A new directive's contribution carries a `supersedes` reference to the
prior item; the scope-manager publishes the new directive into the summary
and removes the old one. The supersession event lives in the **record**; no
tombstone remains in the summary.

## Retirement

The deliberate removal of a directive from a scope summary by its
scope-manager. Retirement may be implicit (the directive was superseded by
a new one — see **Supersession**) or explicit (the scope-manager retires it
without a replacement). Either way, the directive ceases to appear in the
scope summary; the retirement event lives in the **record** as audit trail;
no tombstone is left in the summary.

Retirement exists only for **directives**. Context "forgetting" requires no
ceremony — the scope-manager simply omits stale context from the next
summary it rewrites.

## Fleet

The total set of scopes and the agents that contribute to and read from
them. The scope hierarchy (strata + edges) is the fleet's structural
definition; agents are transient members instantiated against it.

## Provenance

The metadata that travels with a memory item identifying its origin — the
contributing `(scope, skill, session, timestamp)`. Provenance is preserved
through composition into **perspectives** so readers know where each piece
came from; it is the basis for accountability and for aggregating **trust**
along any of its dimensions.

## Perspective

An agent's composed view of long-term memory at read time. A perspective
assembles:

- The agent's own **scope summary**,
- The summaries of every inter-stratum ancestor up to the root,
- The **publications** of any peer scopes referenced by scopes on that chain.

Each piece in the perspective is labelled with the scope it came from —
composition is **provenance-preserving**, not flattened. Directives compose
with broader-stratum winning; context composes with closest-scope winning;
context never overrides a directive.

## Directive

A kind of long-term memory representing a **binding** decision — what the
fleet (or a sub-region of it) has resolved to do or to treat as true.
Directives propagate down through inter-stratum edges and bind every
descendant scope. When two directives conflict, the one from the broader
(higher) stratum wins; a descendant may refine within an inherited directive
but may not contradict it.

## Context

A kind of long-term memory representing observation, working state, or
non-binding knowledge. Context propagates along both inter-stratum edges
(downward) and intra-stratum peer references (across). When two pieces of
context conflict, the one from the scope closest to the reader wins. Context
never overrides a directive.

## Authority

The right to publish memory at a scope, which thereby reaches all of that
scope's descendants. Authority is a property of the **scope** itself (its
position in the strata), not of any individual agent. An agent bound to a
scope exercises that scope's authority for the duration of its session;
authority does not outlive any single agent, but the scope continues to wield
it through whichever agents bind to it next.

## Trust

A property of a **memory item** that rises or falls based on outcomes from
acting on it. Trust attaches to items, not to agents (too ephemeral) and not
to scopes (too coarse). Trust may be aggregated across items sharing a scope
or other provenance dimension for retrieval weighting and accountability, but
the canonical store of trust is per-item.
