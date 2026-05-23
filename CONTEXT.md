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
not bind the reader. To make a peer's standard binding, it must be ratified
into a common ancestor scope (i.e. published as a directive at a stratum
above both).

## Directive

Memory representing a **binding** decision — what the fleet (or a sub-region
of it) has resolved to do or to treat as true. Directives propagate down
through inter-stratum edges and bind every descendant scope. When two
directives conflict, the one from the broader (higher) stratum wins; a
descendant may refine within an inherited directive but may not contradict it.

## Context

Memory representing observation, working state, or non-binding knowledge.
Context propagates along both inter-stratum edges (downward) and intra-stratum
peer references (across). When two pieces of context conflict, the one from
the scope closest to the reader wins. Context never overrides a directive.
