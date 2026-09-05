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
`team` → `individual`); strata are named layers, not depths. Above the
broadest stratum sits the implicit **operator** stratum (see Operator).

## Edge

A link between two scopes. Every edge is of exactly one **kind** — a **chain
edge** or a **reference edge** — and that kind determines everything the edge
carries. No other relation between scopes exists.

## Chain edge (inter-stratum edge)

The edge from a scope to its single parent scope in the stratum immediately
above. Every scope has **at most one** chain edge to a parent — a scope
without one is a root of its own chain.

Carries the parent's **directives** downward — full fidelity, every ancestor
to the root — **binding** the scope and all its descendants. It does not
carry the parent's context: raw internal memory never leaves its scope. What
a child knows of its parent beyond what binds it is what the parent chose to
**publish**, delivered one edge at a time like any publication.

A chain edge's parent is its lower-ordinal endpoint; the direction it is
written in carries no meaning and none is inferred from it. Legal only
between **adjacent** strata: authority passes through each stratum in turn,
never skipping one.

## Reference edge

A link from one scope to another scope anywhere in the fleet — the same
stratum, any distance above, or below. A scope may have any number of
reference edges. Direction means exactly one thing: the scope the edge is
written *from* references the scope it is written *to*.

Carries **context only** — directives published in the referenced scope do
not bind the reader, at any stratum distance. What a reference edge delivers
is the referenced scope's **publication** — its curated outward face — never
its full internal summary and never its operator memory. A publication
travels exactly **one edge**, here as on a chain edge: what a scope receives
it may pass on only by republishing it as its own judged act. To make a
referenced scope's standard binding, it must be ratified into a common
ancestor scope (i.e. published as a directive at a stratum above both).

Reference edges may form cycles. Two scopes referencing each other simply
means each reads the other's publication; nothing binds and nothing
propagates, so there is no cycle to resolve.

## Peer reference (intra-stratum edge)

The same-stratum case of a **reference edge**: a reference from one scope to
another on the same stratum. Named separately because it is the common case
and the one the fleet's own structure suggests; it carries exactly what any
other reference edge carries.

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
primitives (session, skill, scope) to manage itself. It exercises the
scope's authority as the **operator**'s standing delegate; the operator may
also exercise any scope's authority directly (see Operator).

## Operator

The human authority that defines the fleet — its strata, scopes, and edges —
and from which all scope authority is delegated. The operator occupies the
implicit stratum above the broadest scope: everything the operator attaches
to a scope is a **directive**, binding every scope below its attachment
point by ordinary broader-stratum precedence. The operator has no context
channel — its memory is unjudged, unbounded and composed verbatim into every
descendant, so a non-binding operator layer would behave in every observable
way like a binding one.

Operator memory is stored in Strata with external provenance, appended to a
record like all memory, and composed into perspectives verbatim as its own
labelled layer — never rewritten by any scope-manager. It is **judge-aware**:
scope-managers see the operator memory binding their scope when judging, and
decline contributions that contradict it. It is exempt from outcome-based
**trust** weighting; outcomes that contradict it are surfaced to the
operator instead. A change to the operator layer attached at a scope is a
**change event** for that scope and its descendants alike — the attachment
scope is as much a reader of the correction as anything beneath it.

The operator reads the entire store — every scope summary and record — for
verification and steering, and may directly correct any scope's memory
(supersede, retire), each correction recorded under operator provenance.

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

Publishing and **withdrawal** (removing an item from the publication) are
both judged acts by the publishing scope's authority, each appended to the
scope's **record**.

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
  corroboration for ratifying its own source, however many times it has been
  republished: an item cannot corroborate any origin of itself.
- **Change-triggering** — publishing, amending or withdrawing an item is a
  **change event** for the scopes one hop away: the source's chain children
  and the scopes that reference it, each due a **refresh** against it.

## Republication

The act by a scope of publishing onward an item it received in another
scope's **publication**. Because a publication travels only one edge,
republication is the sole path by which knowledge reaches beyond a scope's
immediate neighbours — and every hop is a fresh judged act, so each stratum
decides for itself what its own readers need.

A republished item keeps its **origin** scope and records the relay it
travelled ("according to X, via Y"), through composition and summary
rewrites alike; the judging scope is told an item is second-hand, and that
its origin is information, not permission. When the origin **withdraws** an
item, every republished copy of it is withdrawn too — mechanically, with no
judgment — so no face keeps asserting what its source has retracted.

## Change event

The permanent notice that one input a scope's memory rests on has changed
with no agent contributing: an ancestor's **directive** appended, superseded
or retired; a one-hop **publication** published, amended or withdrawn; an
operator directive attached to a scope. Recorded mechanically, with no
judgment, as a `manager-refresh` contribution in the **record** of every
scope that composes the changed item — a publication's chain children and
the scopes that reference it; a directive's chain descendants; an operator
directive's attachment scope and its descendants alike.

Every independent change mints a change id; a change derived from processing
one — a **refresh**'s admitted directive, a relayed withdrawal — inherits
it, carrying forward how many hops removed it is from the change that
started it. A scope refreshes for a given change id at most once, however
many of its notices arrive; the notice itself is never suppressed, only the
refresh it would otherwise trigger.

## Refresh

The judged cycle a **change event** triggers: the scope's pending change
events are judged together in one amendment, whose ops may admit a new
**directive** or **context** as well as retire, supersede, or withdraw a
publication — because the notice being judged is a real contribution with
honest provenance, unlike the mechanical splice that carries an ancestor's
directives into a scope's own summary on `strata launch` and `strata
refresh`, which admits nothing. Reading or binding a scope through the MCP
server runs the judged cycle alone, with no splice. A refresh runs before a
scope is bound or its perspective is read; `strata refresh` also runs it
directly, for an operator who wants it without touching either.

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
- The **directives** of every inter-stratum ancestor up to the root — never
  their context,
- The **publications** of the scopes one edge away: the agent's parent, and
  any scope its own scope references,
- The scope's own unprocessed **change events** — notices of input changes
  it has not yet been **refresh**ed against.

Each piece in the perspective is labelled with the scope it came from —
composition is **provenance-preserving**, not flattened. Directives compose
with broader-stratum winning; context composes with closest-scope winning;
context never overrides a directive.

## Directive

A kind of long-term memory representing a **binding** decision — what the
fleet (or a sub-region of it) has resolved to do or to treat as true.
Directives propagate down through chain edges and bind every
descendant scope. When two directives conflict, the one from the broader
(higher) stratum wins; a descendant may refine within an inherited directive
but may not contradict it. Appending, superseding or retiring a directive is
a **change event** for the holding scope's chain descendants, each due a
**refresh** against the new state.

## Context

A kind of long-term memory representing observation, working state, or
non-binding knowledge. Context is a scope's own internal working memory — it
never leaves the scope on its own, over a chain edge or a reference edge. It
feeds the scope's own judgments and its own choice of what to **publish**;
what reaches another scope is whatever it chose to publish, not its raw
context. When two pieces of published context conflict, the one from the
source closest to the reader wins. Context never overrides a directive.

## Authority

The right to publish memory at a scope, which thereby reaches all of that
scope's descendants. Authority is a property of the **scope** itself (its
position in the strata), not of any individual agent. An agent bound to a
scope exercises that scope's authority for the duration of its session;
authority does not outlive any single agent, but the scope continues to wield
it through whichever agents bind to it next. All scope authority is
delegated from the **operator**, in whom the chain grounds.

## Trust

A property of a **memory item** that rises or falls based on outcomes from
acting on it. Trust attaches to items, not to agents (too ephemeral) and not
to scopes (too coarse). Trust may be aggregated across items sharing a scope
or other provenance dimension for retrieval weighting and accountability, but
the canonical store of trust is per-item.
