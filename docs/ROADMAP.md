# Strata Roadmap

This is the architect's compass — what the project is doing next, and the
principles that should govern those choices. It does **not** specify
implementations; it sets direction. Re-derive details from
[`philosophy.md`](philosophy.md), [`../CONTEXT.md`](../CONTEXT.md), and the
existing ADRs in [`adr/`](adr/). When a direction is picked up: grill the
foundations, write an ADR if the bar is met (hard-to-reverse, surprising
without context, real trade-off), split into testable feature branches,
build, review, merge.

---

## Enduring principles

These govern every future decision. They are the answer when an ADR is silent.

1. **The central tension is the whole job.** Widen who can contribute to
   shared memory; prevent any contributor from corrupting it. Every change
   must serve both halves; serving only one is wrong by construction.
   (See `philosophy.md`.)
2. **Domain-general, not a dev tool.** The dev-team fleet is one instance.
   Call center, support, SRE, research are equal citizens. Don't bake the
   dev cycle into the core model.
3. **State lives where humans can read it.** Fleet config = canonical YAML;
   scope summaries = markdown; SQLite is reserved for append-only,
   machine-emitted records (contributions, judgments). (ADR 0002.)
4. **The record is sacred; the working view is curated.** The record is
   append-only and never lies. The working view (summary → perspective) is
   finite, selective, and *forgets on purpose*.
5. **The working view is bounded and relevance-ranked** — within a scope
   (the summary budget) and across scopes (perspective selection). The
   record is unbounded; the working view is not.
6. **Authority gates the dangerous writes.** Directives bind and require
   authority; context flows freely and binds no one. Ratification of context
   into a directive is the scope-manager's **judgment**, observable in logs,
   never a mechanical counter that trips at N.
7. **LLM-native, no spaghetti, no premature abstraction.** Reject on
   conceptual-model grounds (cite `CONTEXT.md` / `philosophy.md`), not just
   blast radius.

---

## Horizon 1 — V1.2 (delivered)

- ADR 0002 — fleet config file-canonical.
- ADR 0003 — `strata launch` frictionless CC binding.
- `feature/fleet-config-rewrite`, `feature/strata-launch`, and the V1 → V1.2
  data-exporter all merged to `dev`.

Nothing else here. Look forward.

---

## Horizon 2 — Realize the core: perspective composition + bounded working view

The single most important unrealized concept. Today `read_perspective` is a
stub that returns the agent's own-scope summary. Until perspectives genuinely
compose across the strata, the system isn't yet Strata.

This horizon is one principle (#5 above) at two layers; the architect decides
whether it's a single ADR or a sibling pair:

- **Perspective composition** — walk the inter-stratum ancestor chain plus
  any peer references; assemble a **provenance-preserving** view (each item
  labelled with origin scope); directives broadest-first, never dropped;
  context **relevance-ranked**, not concatenated wholesale.
- **Bounded working view** — per-scope summary size budget; the
  scope-manager condenses / supersedes / retires on overflow. Forgetting
  gets a concrete trigger instead of a vague "the manager curates."

*Research anchors (inspiration, not mandate):* Generative Agents'
recency × importance × relevance for context selection; MemGPT-style paging
pressure for the summary budget. See ADR 0001 alternatives for prior
thinking.

*Dependency:* none beyond V1.2.

---

## Horizon 3 — Trust mechanics

Trust is defined conceptually in `CONTEXT.md` with **no mechanism**. This
horizon needs a grilling pass before any code.

Open questions to pin in an ADR:

- What counts as an **outcome**, and who reports it?
- Where does trust attach — to the item, to a provenance dimension
  (scope/skill), to both?
- How does the scope-manager use trust at **acceptance** (gate) and at
  **retrieval ranking** (weight)?

Bundled sub-considerations (only earn their place inside this work, not
before):

- **Importance / salience score** at acceptance time, feeding retrieval
  ranking.
- **Justification-based revision** (TMS-style): record which context items
  justified a ratified directive, so retracting the evidence flags the
  directive for re-review. Powerful for the "prevent corruption" half but
  adds dependency tracking — likely a sub-direction, not the V1 of trust.

*Dependency:* Horizon 2. Trust has nothing to rank until perspectives
compose.

---

## Horizon 4 — Operation & reach (interleaved as need shows up)

Independent arcs; sequence by which domain or operator need arrives first.

- **UI write endpoints → Command & Control.** Fleet-config writes through
  the API; the Console grows from read-only viewer into the operator console
  (the stated long-term UI goal).
- **Batched / async scope-manager.** Same prompt, judges N contributions per
  pass; adds a `pending` decision state and a scheduler. Unlocks
  high-throughput domains (call center, support) and proves domain
  generality at scale.
- **Human-in-the-loop scope-manager (hybrid).** A CC session can take over a
  scope's curation. The V2 hybrid sketched in ADR 0001 alternatives.

---

## Standing backlog

- **Multi-worker uvicorn / shared fleet-config state** — Issue #19 (V2).
- **Windows `strata launch`** (replace POSIX `execvp`) — Issue #20.
- **Archived-scope `strata launch` error message** — non-blocking nit on
  ADR 0003.

---

## Recommended order

**H1 → H2 → H3**, with H4 arcs interleaved opportunistically. H2 before H3
is firm: trust has nothing to rank until perspectives compose.
