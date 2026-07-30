# ADR 0010 — Typed Edges: Binding Chain, Weak Reference

**Status:** Accepted (implemented)
**Date:** 2026-07-30
**Related:** ADR 0002 (fleet config source of truth — the 8 load-time
invariants, 7 and 9 of which this ADR restates), ADR 0004 (the
at-most-one-parent invariant), ADR 0006 (entitlement — D2's entitlement
groups, D3's reference composition), ADR 0007 (publication — D4, what a
reference delivers); issue #127 (this ADR), issue #123 (inert inverted
edges, closed by it); CONTEXT.md § Chain edge, § Reference edge, § Peer
reference.

---

## Context

`fleet.yaml` has always had one edge shape — `from` / `to` — and the engine
decided what an edge *meant* by comparing the stratum ordinals of its
endpoints. Two problems came out of that, and they turn out to be the same
problem.

**1. An inverted edge means nothing at all (issue #123).** In a live fleet
observed 2026-07-24, every inter-stratum edge had been authored top-down —
`from` the broader scope, `to` the narrower one, the natural "the executive
directs the function" mental model. Every one of those edges passed
validation and derived nothing:

- `inter_stratum_parent` only followed an edge from a scope toward a *lower*
  ordinal, so a broad→narrow edge was a parent edge for neither endpoint.
- `entitlement_view`'s referenced-peers pass required *equal* ordinals, so
  it was not a peer reference either.
- Validation's single-parent count keyed off the same authored direction, so
  an inverted edge was invisible to it — a scope could hold one inverted and
  one correct parent edge and still pass.

Net effect: no ancestry, no reference, no entitlement, no perspective layer.
The symptom in the wild was a directive published at the broadest scope that
never appeared in any descendant's perspective, while the fleet map rendered
as fully wired. Every chain was silently self-only.

**2. There is no channel for a non-parent upper scope (issue #127).** A
reference could only join two scopes on the same stratum. A scope that needs
to read a *different* branch's function-level scope — the "uncle" — had two
workarounds, both bad: have the operator copy the material down by hand, or
restructure the fleet so the two scopes become peers. The first defeats the
point of a governed store; the second bends the fleet's shape to a read
requirement. The occasional downward case (leadership consuming one team's
published face) had no channel at all: descendants are entitled to propose
*upward*, but a scope cannot read its descendants.

The common cause: an edge's meaning was **inferred from the direction it was
written in**, and direction was carrying two different jobs at once. For an
inter-stratum edge direction encodes nothing — the parent is derivable from
the ordinals — so any convention around it is a trap. For a reference edge
direction is the entire content — who reads whom — so it cannot be
normalized away. One shape cannot serve both.

---

## Decision

### D1. Exactly two kinds of edge, declared in `fleet.yaml`

An edge may carry `kind: chain | reference`. There is no third kind and no
sub-kind; every relation between two scopes is one of these two.

**Chain (binding).** The inter-stratum edge, unchanged in what it carries:
the ancestor layers, binding, directives and context flowing down. Legal
**only between adjacent strata** — authority passes through each stratum in
turn and never skips one. At most **one per scope**, counted on the
effective relationship (D3). Self-loops rejected.

**Reference (weak).** Any scope pair, any strata: the same stratum, any
distance upward, or downward. Carries the referenced scope's **publication
only** — never its internal summary, never its operator memory. Non-binding
to the reader at every distance. This is the rule peers already followed
(ADR 0007 D4) stated generally. Direction means exactly one thing:
referencer → referenced. Self-reference rejected.

Authority stays unambiguous under both: one parent who binds, any number of
referenced scopes who inform. The publication remains a scope's single
outward-facing surface no matter who consumes it — the uncle case adds a
consumer, not a channel.

Today's peer reference becomes the same-stratum special case of a reference
edge. It keeps its name in CONTEXT.md because it is the common case, not
because it is a distinct mechanism.

### D2. Reference layers all compose in one block

Ancestor (chain) layers are unchanged. **Every** reference layer — same
stratum, upward, downward — composes in the existing referenced-scopes block
after the reader's own layer, sorted by referenced scope id, `binding:
false`, each labelled with the referenced scope's own stratum.

They compose together because they *are* the same thing: a reference two
strata up and a reference to a peer deliver identical capacity (that scope's
publication, non-binding), so splitting them into separate blocks would
imply a difference the model does not have. The layer's `relation` stays
`peer_reference` — the wire contract does not change, and the stratum label
on each layer already tells a reader where it sits.

The implementation of this decision is a change to `entitlement_view` alone.
`compose_perspective` needed no change: it composes whatever
`entitlement_view` returns as `referenced_peers`, so widening that group
widened composition, the entitled context surface, and the judge's
ENTITLEMENT block in one step. The judge's group label changes from "Peer
scopes referenced by this chain" to "Scopes referenced by this chain" — the
group can now hold a non-peer.

One group overlap is now possible and is deliberately allowed: a scope that
is both referenced and a descendant appears in **both** `referenced_peers`
and `descendants`. Each names a real, entitled relationship with a distinct
capacity — the reference is read access to that scope's publication, the
descendancy is that scope's standing to propose evidence upward — and
suppressing either would cost the judge information it uses. Nothing lands
in the not-entitled group that belongs elsewhere, which is the property the
ENTITLEMENT block actually depends on.

### D3. Chain edges are canonicalized; the single-parent rule follows the effective relationship

A chain edge's parent is its **lower-ordinal endpoint**. The direction it
was authored in is irrelevant and no meaning is read out of it. On load, the
engine orients every chain edge child→parent and materializes each edge's
kind, so every consumer of a loaded config reads one shape.

Canonicalization on load is **in memory only**. Reading a fleet must never
rewrite the file: the engine re-reads `fleet.yaml` on every tool call
(ADR 0004 D1), and a read with a write side effect would make concurrent
readers writers. The file catches up through the mutation API — every
mutation re-renders the edges canonically before writing, so an inverted
edge is corrected on disk the first time anything else in the file changes.
The declared `kind` is written back exactly as authored; an inferred kind
stays inferred on disk.

The at-most-one-parent invariant (ADR 0004) is enforced on the **effective**
relationship. One inverted edge plus one correct edge is now what it always
was in substance — two parents — and is refused, with an error naming both
edges as written so the author can find the two lines. This is the loophole
that let the #123 fleet validate.

### D4. Untyped edges keep loading, and gain the meaning they look like they have

`kind` is optional. An untyped edge takes the only meaning its endpoints can
support:

| Stratum distance | Untyped edge means |
| --- | --- |
| 0 (same stratum) | `reference` — the peer reference |
| 1 (adjacent) | `chain`, canonicalized child→parent regardless of how it was written |
| 2 or more | **validation error** naming the fix: declare it `kind: reference` |

The adjacent row is what closes #123: an existing top-down fleet starts
composing ancestor layers on the next load, with no migration and no edit —
issue #123's "normalize" option, reached through the type system rather than
as a special case.

The distance-≥2 row is the only place where previously-loading config could
now fail, and it could not have loaded before either: the old ±1 constraint
rejected the same edges. What changed is the message, which now names the
kind to declare instead of stating a constraint that no longer holds for
every edge. The error token stays `stratum_distance_violation`. An explicit
`kind: chain` that is not adjacent gets its own token,
`chain_edge_not_adjacent`.

The resulting invariant, which is the real subject of #123: **no edge that
validates is meaningless.** Every edge that loads derives something —
ancestry or a composed publication — and any edge that would derive nothing
is refused at load with an error that says which kind the author meant.

### D5. Load-lenient, write-strict — untyped kinds resolve per child scope

D3 and D4 as first implemented inferred each untyped edge's kind from that
edge alone. That is wrong, and it broke live fleets on upgrade.

A fleet holding a formerly-inert inverted edge **alongside** a correct parent
edge onto the same child loaded fine before this ADR — the old single-parent
counter keyed off authored direction, so it saw only the correct edge and the
inverted one contributed nothing. Under per-edge inference both resolve to
chain edges, the child has two parents, and `FleetConfig.load` raises. The
config does not load at all, so every fleet-backed read fails, not just the
one relationship. The smallest reproduction:

```yaml
strata: [{id: L0, ordinal: 0}, {id: L1, ordinal: 1}]
scopes: [{id: g_a, stratum_id: L0}, {id: g_b, stratum_id: L0}, {id: g_c, stratum_id: L1}]
edges:
  - {from: g_c, to: g_a}   # correct child→parent — honoured before this ADR
  - {from: g_b, to: g_c}   # inverted — inert before this ADR
```

The principle this violated, which outranks every other decision here:
**data that loaded yesterday must load today.** An engine upgrade may add
meaning to stored data; it may never make stored data unreadable. A fleet
definition is operator-authored state, and the engine does not get to reject
it retroactively because the engine learned something new.

So an untyped **adjacent** edge is no longer resolvable on its own. Whether
it is a chain edge depends on what else points at the same child, and
resolution happens **per child scope**:

1. Untyped adjacent edges are grouped by their child (the higher-ordinal
   endpoint). Each is either *authored-upward* (`from` is the child — what
   pre-ADR-0010 loads honoured) or *authored-inverted* (inert before this
   ADR).
2. An authored-upward edge takes that child's chain slot. Legacy data can
   hold at most one, because the old check enforced exactly that.
3. An authored-inverted edge **promotes** into the chain slot only when the
   slot is free — no upward edge, no declared `kind: chain` — **and** it is
   the only inverted candidate for that child. This is D4's #123 fix,
   unchanged.
4. Every other inverted candidate **demotes** to a reference. Two candidates
   competing for a free slot means both demote: the engine never guesses
   which one the author meant. The child then stays parentless — exactly its
   behaviour before the upgrade — while both edges still deliver a
   publication. Strictly more than the inert edges gave, and never wrong.
5. An **explicitly declared** `kind: chain` keeps the strict rules. Two
   declared chains onto one child is still `multiple_inter_stratum_parents`;
   a declared non-adjacent chain is still `chain_edge_not_adjacent`.
   Strictness moves to where intent was declared.

A demoted edge is oriented **child→would-be-parent**, not left as authored.
The author drew a downward flow; demotion keeps that flow and lowers its
strength from binding to informative, so the child reads that scope's
publication. Preserving the authored `from`→`to` instead would reverse the
flow the author asked for, which is a worse answer than the inert edge was.

The write path completes the story. `_canonicalize` (in memory) applies this
resolution; `_canonicalize_raw_edges` (every mutation) additionally
**materializes** `kind: reference` on demoted edges. A file that has been
mutated once no longer depends on the resolution rules to mean what it means
— it says so — and reloading it yields the same shape from an explicit
declaration. Legacy files converge on self-describing form as they are
edited, and nothing has to be migrated to get there.

The two halves, named so they are not traded against each other later:
**load is lenient** (an ambiguous untyped edge degrades to the weaker kind,
never to an error) and **write is strict** (whatever the engine resolved
becomes explicit on disk the next time anything writes). Invariant 9's error
therefore fires only for declared chains, or for the two-authored-upward
shape that failed the same check before this ADR.

---

## Alternatives considered

- **Normalize inverted inter-stratum edges and stop there** (issue #123
  option 1, taken alone). Fixes the inert edge and leaves direction doing
  two jobs — still inferred meaning, still no uncle channel, and the next
  relation anyone needs reopens the whole question. Typed kinds subsume it:
  the normalization becomes a property of one kind rather than a rule about
  edges in general.
- **Reject inverted edges with a validation error** (issue #123 option 2).
  Explicit, but it breaks every currently-loading fleet that authored
  top-down — the exact fleets the bug already silently broke — and it
  demands a migration to teach a convention that carries no information.
  Rejected: where direction is meaningless, the parser should absorb it, not
  the operator.
- **Let a reference edge be binding when it points upward.** Tempting for
  the uncle case — "surely a broader scope's directives should bind." It
  would give a scope two sources of binding authority with no precedence
  rule between them, and it would let any scope acquire binding reach over
  another by adding one line to `fleet.yaml`. Binding reach stays a property
  of the chain; the path from a reference to binding remains ratification.
- **A third kind for the cross-stratum reference** (an "uncle edge"
  distinct from a peer reference). Rejected on the simplicity principle: it
  carries the same payload with the same non-binding force, so a separate
  kind would be a name for a stratum distance, not for a behaviour.
- **Resolve untyped adjacent edges per edge, and tell operators to fix the
  fleets that stop loading** (what D3/D4 did before D5). Rejected on the
  upgrade invariant: a config-only change that makes an engine refuse
  previously-valid stored data is a data-loss event from the operator's side,
  however clean the resulting model is. The engine has enough information to
  degrade the ambiguous edge safely, so it must.
- **Promote the inverted edge and demote the correct one** when both point at
  one child. Rejected: the correctly authored edge is the one the previous
  engine honoured, so honouring it is what keeps behaviour continuous. The
  inverted edge never bound anything, and turning it into the binding one
  would silently re-parent a live scope.
- **Keep a demoted edge's authored `from`→`to` direction.** Rejected — see
  D5. The author drew a flow; reversing it is a worse answer than the inert
  edge, because it would compose a publication into the scope that was meant
  to be the *source*.
- **Reject reference cycles.** Believed harmless and confirmed so: a
  reference composes one hop from the reader's chain and delivers a
  publication that binds nothing, so A↔B means each sees the other's
  outward face. There is no traversal to terminate and no precedence to
  resolve. Rejected as ceremony; cycles are legal and documented as legal.
- **Compose cross-stratum reference layers in their own block, ordered by
  stratum.** Rejected per D2 — it would imply a difference in what those
  layers carry, and there is none.

---

## Consequences

**Positive:**

- The inert-edge class is gone. A fleet authored top-down, bottom-up, or
  mixed composes the same perspectives, and an edge that would derive
  nothing is a load error rather than a silent hole.
- The uncle case has a real channel, through the surface that already
  exists: the referenced scope's publication. No new memory kind, no new
  composition block, no operator hand-copying.
- The single-parent invariant became true. It was enforceable only against
  authored direction before, which is to say it was not enforced.
- Edge kind is operator-visible state in `fleet.yaml` (ROADMAP principles 3
  and 8): legitimizing a knowledge flow stays a reviewed, human-readable
  config change, and now the review can see whether it binds.

**Negative:**

- Reference fan-in can now come from any stratum, so a perspective can grow
  wider than the same-stratum fan-in previously allowed. This moves the
  read-side bounding deferral (ADR 0004 "Out of scope") closer without
  changing its forcing function.
- A fleet that authored an untyped adjacent edge top-down *and relied on it
  being inert* changes behaviour on upgrade: descendants start inheriting
  ancestor layers. This is the intended fix, and there is no way to make it
  both a fix and a no-op — but it is a behaviour change on load, and release
  notes must say so. Note the bound D5 puts on it: the change is confined to
  edges that were unambiguous. Where an inert edge competed with a real one,
  the existing parent wins and the inert edge becomes a reference.
- Untyped adjacent edges are no longer independently readable — you cannot
  tell what one means without looking at the others pointing at the same
  child. That is inherent to keeping legacy fleets loading, and the write
  path pays it down: any mutation makes the resolution explicit on disk.
- Two names now describe one thing at the same stratum (peer reference,
  reference edge). Kept deliberately — the peer case is the common one and
  the existing vocabulary — at the cost of a synonym in the glossary.
- `EntitlementView.referenced_peers` keeps its name while holding non-peers.
  Renaming it would churn every consumer of a shipped field for a wording
  gain; the docstring carries the correction.

**Out of scope (unchanged):**

- Read-side relevance ranking and perspective bounding.
- The Console's designer surface for edges — drawing an adjacent-strata edge
  no longer needs a direction convention, but choosing a kind for an
  ambiguous draw is tracked separately.
