# ADR 0013 — Publication Is the Only Sharing Channel; Directives Are the Only Inheritance

**Status:** Accepted (2026-08-31 — grilled to completion with the operator;
all decision branches resolved, no open questions)
**Date:** 2026-08-31
**Related:** ADR 0006 (entitlement — **amends D3**, peer-reference
composition), ADR 0007 (publication mechanism — **amends D4**, what a
reference delivers), ADR 0008 (operator stratum — **amends D1/D2**, the
operator layer's kinds), ADR 0010 (typed edges — the chain/reference
distinction this ADR gives a single composition rule); issues #168 (this
ADR), #173 (enforcement, decided here), #143, #128, #135, #169 (mechanism
work that follows); CONTEXT.md § Chain edge, § Reference edge, §
Perspective, § Publication, § Operator.

---

## Context

A scope's perspective today includes the **full summary** — directives *and*
context — of every inter-stratum ancestor up to the root. The operator raised
two objections (2026-08-29, issue #168):

1. **Identity collapse.** A child carrying its parent's full context
   effectively *is* parent-plus-child. Scope identity dissolves upward. What
   a child knows of its parent should be what the parent chose to say.
2. **Inconsistency with our own principle.** ADR 0007 exists because
   composing raw internal memory into a reader who never judged it for export
   is wrong. We enforce that for peers and violate it on every chain edge.

The two channels had drifted into different rules for no recorded reason:
publication travels **one hop** along a reference edge (`entitlement_view`
does not traverse references-of-references), while ancestor summaries compose
the **entire chain** with no hop limit and no export judgment.

---

## Decisions

### D1. Chain edges carry directives; they no longer carry context

A chain edge's payload is the ancestor's **directives** — full fidelity,
every ancestor, root-first. Nothing else about ancestry implies a right to
the ancestor's working memory.

Directives keep the full walk without exception: a directive binds every
descendant, and an agent bound by a directive it cannot see cannot comply.
Hiding binding memory is a correctness hazard, not a privacy feature (the
same rule ADR 0006's grant-narrowing note already states).

A scope's own **context** section survives and keeps its job — it is the
scope's internal working memory, feeding its own judgments, its ratifications,
and what it chooses to publish. It simply stops leaving the scope on its own.

### D2. Publication is the single sharing channel for non-binding knowledge

Everything a scope shares that does not bind travels as its **publication** —
the same judged, attributed, curated face for peers and descendants alike.

**One face, not two.** A scope publishes exactly one outward face; peers and
descendants receive identical items. Rejected: a chain-facing face distinct
from a peer-facing one, and a single face with per-reader-class visibility
tags. Both buy the ability to tell your subtree something you withhold from
your peers, and both pay for it by doubling the publish surface, the budget
(#135), the browser (#169) and the judge prompt — after which every new
reader class asks for another face. A scope that needs to say something to a
subset says it to that subset's scope.

### D3. Publication travels exactly one edge — chain or reference

One hop, uniformly. A grandchild does not receive the root's publication; it
receives its parent's.

This is the decision that repays the most. Each stratum becomes a **curation
checkpoint** rather than a pass-through: a parent that receives the root's
face decides what its own subtree needs and republishes that. Relevance
selection across scopes — the half of roadmap principle #5 the read side
still owes — arrives as a consequence of the model instead of as a ranking
algorithm bolted onto composition. It also removes chain depth from the
perspective size equation entirely, which is most of what made #135 urgent.

The cost is that the root's material reaches a grandchild only if the parent
relays it. That is not a leak in the design; it is the design. Judgment at
each hop is the point.

### D4. Republication preserves provenance transitively

A republished item keeps its **origin scope** and records the relay:
*"according to root, via parent."* The publishing scope's judge sees that it
is republishing foreign material and judges it as such.

"Never self-corroborating" (ADR 0007) extends transitively with it: an item
cannot corroborate any ancestor of itself, however many relays it has passed
through. Trust feedback flows to the origin, not the relay.

Without this, one-hop publication launders provenance at the second hop — a
relayed root decision becomes indistinguishable from the parent's own — which
is precisely the consolidation failure issue #134 cites. Attribution must
survive summary rewrites, so #134's hard requirement lands here first, for
publication items, ahead of any general epistemic-status work.

### D4b. A withdrawn item takes its relayed copies with it

When a scope withdraws a published item, every downstream copy that was
relayed from it is withdrawn too, mechanically, with no LLM in the loop.

This is a fourth choke point of the class ADR 0007 D3 established, one edge
further out: a relay is anchored to its origin exactly as a published item is
anchored to the directive it came from, and `propagate_directive_removals`
already does the same job one level in. "Published within believed" is the
existing rule and a relay believes an item only because its origin does — a
face still showing what its source has retracted is asserting something
nobody stands behind, under a label ("according to root") that has become
false.

Rejected: notifying the relay and letting its judge decide. It puts an LLM
call on a path that is mechanical everywhere else, and the only coherent way
to keep a retracted item is to re-attribute it to the relay — which is the
laundering D4 exists to prevent.

Consequence to build for: a withdrawal can change several scopes' faces in
one act, and relays learn of it after the fact. Every relayed item therefore
has to record its origin well enough to be found — the origin field D4
already requires, now load-bearing for removal as well as for attribution.

### D4c. The judge sees that an item is second-hand

When a scope judges a `publish` act that relays material received from
another scope, the judge is told the item is a relay and shown its origin.

Relaying someone else's claim is a different decision from publishing your
own: the question is "do my readers need to hear this", not "is this true and
mine to say", and a judge cannot ask the right one blind. It also lets a
judge decline to relay something that contradicts what its own scope already
publishes — a check that is impossible if every item arrives anonymous.

The prompt must be explicit that origin is **information, not permission**:
that an ancestor said it is not by itself a reason to pass it on. Otherwise
D3's curation checkpoint degrades into an automatic pass-through with an
LLM call attached.

### D5. Operator memory collapses to directives only

The `context` kind is removed from operator memory. Everything the operator
attaches to a scope **binds**.

This makes the model honest about behaviour that already exists. Operator
context is unjudged, unbounded, exempt from trust weighting, never rewritten
by any scope-manager, and composed verbatim into every descendant — it
behaves in every observable way like the binding channel while being labelled
as the one that merely informs. Once D1 removes every *scope's* ability to
push raw context down a chain, an operator `context` kind would be the single
remaining exception to the rule, and an exception belonging to the one actor
no one can judge.

The operator stays exempt from D2/D3, and ADR 0008's sovereignty contract is
otherwise untouched: operator memory is verbatim, unbounded, its own labelled
layer. There is no scope-manager above the operator to judge a publish act,
and an operator judging itself is not judgment. The operator's layer is now
simply directives.

### D6. Enforcement: register seeds harness deny-rules for `.strata/`

D1 widens the gap between what an agent is *entitled* to read and what it
*can* read. Before this ADR a child was entitled to its parent's full
summary, so shell-reading `summaries/parent.md` got roughly what the layer
would serve. After it, that file holds context the child is explicitly denied
— and the live incident in #173 was an agent doing exactly that.

`strata register` seeds file-access denials for `.strata/` on harnesses that
support them (Claude Code permission deny rules, Codex sandbox config), and
the README and CLI state plainly that outside such a harness, scoping is
**discipline, not security**.

**Rejected: obfuscating the store** — hashed filenames with a name map in the
database, or XOR'd file contents decoded by the server on load. Both are
obfuscation, not enforcement: the map lives in a database the same agent can
read, the encoding is trivially reversed, and an agent needs neither — it can
ask the MCP server, which decodes by design. They also break roadmap
principle #3 (*state lives where humans can read it; scope summaries =
markdown*) and CONTEXT.md § Operator (*the operator reads the entire store*),
costing reviewable `git diff`s on memory, greppability, an intelligible
backup story, and the operator's own direct view — to raise the bar by one
`xxd`. Genuine at-rest protection is real encryption with a key outside the
project tree; that is a separate, larger decision, and it still does not stop
the agent that simply calls the server.

**Rejected: relocating storage outside the project tree** — stronger against
casual discovery, but it trades away the portable in-project workspace and
committed-fleet ergonomics that were chosen deliberately, and storage-path
resolution is exactly where issue #178's data-loss incident lives.

### D7. No migration; new rules govern acts from the release forward

Stored state is never rewritten. There is no migration pass, no auto-publish
of existing ancestor context, no rewriting of operator layers.

All *behaviour* follows the new rules immediately — composition at read time
and judgment alike. The consequences, said out loud because reading is itself
a future act:

- A parent's existing raw context stays exactly as it is in its summary and
  simply stops being served downward. To reach descendants it must be
  published, judged like anything else.
- An existing operator `context` item stays on disk and in the record but
  stops composing into perspectives. Future operator writes are directives.

Rejected: grandfathering items that exist at upgrade time so they keep
composing under the old rules. It leaves two composition paths running
indefinitely, separated by nothing a reader can see except a timestamp.
There is one composition rule, and it takes effect at once; what changes is
what gets *served*, never what is stored.

Rejected: migrating ancestor context into publications automatically. It
would auto-export material never authored as an outward face and never judged
for export — the exact thing this ADR exists to end.

---

## Consequences

- **ADR 0006 D3 and ADR 0007 D4 are amended**, and the two now state one
  rule instead of two: *publication travels one edge; directives travel the
  chain.* `relation` on a perspective layer keeps its labels, but `binding`
  becomes the honest discriminator — true for directives from self and
  ancestors, false for every publication layer regardless of where it came
  from.
- **CONTEXT.md changes**: § Chain edge no longer "carries both directives and
  context downward"; § Perspective no longer composes ancestor *summaries*; §
  Publication gains republication and transitive non-corroboration; § Operator
  loses the context/directive split.
- **#135 shrinks.** With depth out of the size equation, the publication
  budget bounds one face against its readers, not a chain product.
- **#169 must be designed after this**, not before: "what a given reader
  receives" is exactly what D3 and D4 redefine.
- **#143 gets more urgent, not less.** If publication is the only sharing
  channel, a `judge_publication` blind to operator memory means the operator's
  binding directives constrain nothing about what a scope tells anyone.
- **#136's per-layer metrics** should land after this, when the layers are
  final.
- Republication needs an origin field and a relay path on publication items;
  both must survive summary rewrites, and D4b makes them load-bearing for
  withdrawal, not just for display.
- The judge prompt gains a relay-origin input (D4c), and #143's operator-memory
  input lands in the same prompt — worth doing as one change to
  `judge_publication` rather than two.

## Open

None. All decision branches were resolved in the 2026-08-31 grill; what
remains before acceptance is review, not research.
