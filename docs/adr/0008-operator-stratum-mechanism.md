# ADR 0008 — Operator Stratum Mechanism: Storage, Verbatim Layer, Judge-Aware Rendering, Corrections

**Status:** Proposed (architect grill 2026-07-12; owner ratification pending)
**Date:** 2026-07-12
**Related:** issue #80 (adoption decision + owner rulings — closes when this
ADR lands), #91 (implementation), #83 (library primitives this lands inside;
primitive B is defined here), #79 (admission rule; already covers spoofed
operator claims), ADR 0007 (sibling mechanism ADR; shares the composition
primitive), ADR 0006 (entitlement surfaces unchanged for agents),
strata-evals#6 (O-family eval gate); `philosophy.md` Concept 3 § "Where
authority grounds: the operator"; `CONTEXT.md` § Operator.

---

## Context

The model-level decision is made and owner-ratified (#80, 2026-07-10): the
**operator** — the human who defined the fleet — is the implicit stratum
above the broadest scope. Not a new memory kind, not an "unconditionally
binding" flag: an operator directive attached to scope S is an ordinary
directive from above, scoped to S's subtree, resolved by ordinary
broader-stratum precedence. The operator's layer carries both directives and
context, like any stratum's.

Three owner rulings bind this mechanism:

1. **Judge-aware from day one.** Scope-managers see the operator memory
   binding their scope when judging, and decline contributions that
   contradict it. Judge-blind (the strata-web#37 prototype) is rejected as
   the landing state.
2. **The operator reads everything** — every scope's summary and record.
3. **Manual correction.** The operator may supersede or retire any scope's
   memory in person, recorded under operator provenance; scope-managers
   exercise scope authority as the operator's *standing delegates*.

Plus the doctrine bounds: operator memory is **constitutional, not
operational** (small, rare, mostly stable; surfaced as a health signal), and
it is **exempt from earned trust** — outcomes contradicting it surface to
the operator rather than eroding it.

What this ADR decides is the mechanism: where operator memory lives, how it
composes, how judges see it, and what shape corrections take — all landing
inside the #83 library primitives so no consumer (MCP server, CLI, Console,
strata-web) re-implements any of it. strata-web's hand-rolled operator
writes (`judged_by='operator'`, decision literals, supersedes-FK — the
highest silent-drift risk its design review found) are the shape this ADR
takes ownership of.

A distinction the rulings imply, named here because the storage follows
from it: the operator acts in **two different capacities**.

- **Writing the operator stratum** — publishing, superseding, or retiring
  *operator* memory attached above some scope. This exercises the
  operator's own stratum authority. It is **not judged** (judgment is how
  *delegated* authority is exercised; this authority is not delegated).
- **Correcting a scope's native memory** — superseding or retiring an item
  *inside* some scope's own summary. This exercises **that scope's**
  authority, in person, in place of its standing delegate. It is a
  judgment — made by the operator.

Same human, two capacities, two records — which is exactly how the rest of
Strata already works: authority is a property of position, and provenance
records who exercised it.

---

## Decision

### D1. Storage — the operator stratum has the same two-layer memory shape as every scope

Operator memory is ordinary Strata state, with the record/working-view split
every scope already has:

- **The operator record** — append-only, in the record store: one row per
  operator act on the operator stratum (`publish`, `supersede`, `retire`),
  each carrying the attachment scope, kind (`directive` | `context`), the
  verbatim content, optional subject, a supersedes reference for
  supersession, operator provenance, and a timestamp. "The record never
  lies" applies to the operator too — every act, forever. (Schema is #91's
  within this contract; the migration picks up the #76 hardening first.)
- **The operator working layer** — the current operator memory per
  attachment scope, as a human-readable markdown file alongside the scope
  summaries (one file per attachment scope that has operator memory; exact
  layout is #91's). This file is canonical current state, written **only**
  by the operator tooling as the deterministic result of the acts in the
  record — never LLM-touched, and hand-editing is not the channel: an edit
  that bypassed the record would be an unrecorded exercise of authority.
  Humans read it; the tooling writes it. (Same contract scope summaries
  have — readable by anyone, written only through the system.)

Entry surfaces: the acts are **library primitives** (#83), fronted in
vanilla Strata by a CLI subcommand group (`strata operator publish |
supersede | retire | show`) — Strata must work fully locally — and consumed
by hosting platforms through their adapters (strata-web#16 tracker). No
agent-facing MCP surface: agents are never the operator.

### D2. Composition — a verbatim, labelled layer above its attachment point

`compose_perspective` (#83A) gains operator layers: for each scope on the
reader's chain that has operator memory, a layer is inserted **immediately
above that scope's own layer**, carrying the operator items verbatim:

- Label: the attachment scope's id, `stratum_id: "operator"` (a reserved
  stratum label — the implicit stratum needs a name in layer provenance;
  `fleet.yaml` never declares it and a fleet stratum may not claim it),
  `relation: "operator"`, `binding: true` (its directives bind, exactly as
  a summary layer's do; its context items inform, like any layer's
  context).
- Verbatim means verbatim: the layer's content is byte-identical to what
  the operator published. It is not part of any scope's summary, so no
  scope-manager rewrite can touch it — the #80 reframe ("only summaries get
  rewritten; the operator layer is not part of any scope's summary") made
  concrete.
- Precedence is ordinary broader-stratum-wins. The attachment point *is*
  the operator's reach choice: attached at S, it sits above S and binds S's
  subtree; a native directive at an ancestor of S is broader and composes
  above it, per Concept 5, and if the operator means to outrank that
  ancestor the correction is to attach at the ancestor. No new precedence
  rule — nothing outranks the source of its own authority, and no rule
  needs to say so twice.

### D3. Judge-aware rendering — the judge sees what binds the scope it judges

`ScopeManager` inputs gain an **OPERATOR MEMORY (binding this scope)**
block: the verbatim operator items attached at the judged scope or any of
its inter-stratum ancestors, rendered read-only next to the PARENT SCOPE
SUMMARY block. The system prompt gains the matching rule:

- A contribution that contradicts operator memory binding this scope is
  **declined**, citing the operator directive. Refinement *within* an
  operator directive remains legitimate, as with any inherited directive;
  contradiction is not refinement.
- Operator-consistent material the judge incorporates into a summary must
  carry attribution — "per operator directive X" — so echoes are detectable
  and never masquerade as native scope memory. Echoes are harmless to
  authority either way (the authoritative copy composes verbatim
  regardless); attribution is what keeps them *visible*.
- Claims of operator backing are already covered by the #79 admission rule
  ("the operator mandated this" is verified against the rendered operator
  layer; unverifiable claims are unestablished) — this ADR activates that
  clause's operator variant, and the J4 spoofing item goes live with it.

### D4. Corrections — operator supersede/retire on any scope (#83 primitive B)

The operator may correct any scope's native memory in person. Two library
primitives, defined by Strata's contract:

- **`operator_supersede(scope_id, directive_id, replacement…)`** — appends
  to the *target scope's* record a contribution under operator provenance
  with a `supersedes` reference, plus a judgment row with
  `judged_by="operator"` — honest, because a judgment is an exercise of the
  scope's authority and the operator is that authority in person. The
  summary is then **spliced mechanically** (the superseded directive
  replaced by the new one, keyed to the new contribution id) — no LLM
  rewrite; a human ruling is not raw material for paraphrase.
- **`operator_retire(scope_id, directive_id, reason…)`** — no new memory
  enters, so no contribution row is fabricated: the act appends a
  **retirement event** to the target scope's record (directive id, operator
  provenance, reason, timestamp) and mechanically filters the directive
  from the summary. (CONTEXT.md already places retirement events in the
  record; this gives explicit retirement its first record shape, which a
  future scope-manager explicit-retire can reuse.)

Both leave the summary explainable by the record (the #38 invariant), run
under the per-scope serialization lock, and are exactly the operations
strata-web hand-rolled and must delete in favour of these primitives
(#83B). Operator acts on the *operator stratum* (D1) never enter a scope's
record — two capacities, two records, per the Context distinction.

### D5. The operator reads everything

Whole-store reads (every scope's summary, record, and — with ADR 0007 —
publication) are library-level operator surfaces, consumed by the CLI and
the Console. Agent entitlement (ADR 0006) is untouched: agents see what
reaches them; the operator sees the store, because verification and
steering are the operator's job and the operator answers for the fleet.

### D6. Health signal — constitutional, not operational

A library helper derives, from the operator record: layer size (items,
words, per attachment scope and total) and churn (acts per trailing
period). Consoles surface it (strata-web W3.2); vanilla Strata prints it in
`strata operator show`. The doctrine reading is documented with the
numbers: operator memory should be small, rare, and mostly stable — a fleet
steered day-to-day through this layer has replaced the self-correcting
system it was supposed to host.

### D7. Trust exemption

Operator items carry no trust weighting, now or when Horizon 3 lands — H3
must exempt them by construction, and nothing in this mechanism stores a
weightable trust field on them. Outcomes that contradict operator memory
surface *to the operator* (V1: visible in the record and health surfaces;
richer routing is H3 work). The system may never outvote the operator, but
it must be able to tell the operator it is wrong.

---

## Alternatives Considered

- **Judge-blind composition** (compose the layer read-only, keep judges
  unaware — the strata-web#37 prototype). Rejected by owner ruling: a judge
  that cannot see operator memory accepts contributions that contradict it,
  planting incoherence into every reader's perspective and forcing readers
  to resolve conflicts the judgment loop exists to prevent.
- **Operator memory written through scope summaries** (inject into the
  summary, let the manager incorporate it). Rejected: summaries are
  LLM-rewritten, and a paraphrased ruling makes the human's authority
  probabilistic — the exact failure #80 named. Composition-as-own-layer is
  the mechanism the model already provides for memory that must not be
  rewritten.
- **A real `fleet.yaml` stratum/scope for the operator.** Rejected: the
  operator is not a region of the fleet. A declared scope invites agent
  bindings, `permitted_skills`, a scope-manager, edges — each a category
  error against "it is not judged" and "its authority is not delegated."
  The stratum stays implicit; only the reserved layer label makes it
  visible in provenance.
- **Reuse contributions + judgments for operator-stratum acts** (one
  schema everywhere). Rejected: operator stratum items are not judged, and
  a fabricated judgment row would misdescribe the act — the record must
  never dress an authority act as a judged proposal. (Corrections, D4, are
  the opposite case: they genuinely *are* judgments — by the operator — and
  so genuinely do reuse the judgment shape.)
- **Hand-editable operator layer file as the canonical channel**
  (fleet.yaml-style file-canonicality). Rejected: fleet.yaml canonicalizes
  *structure*, which carries no record obligation; operator memory is
  *memory*, and "every act recorded" fails the moment `vim` is the write
  path. The file stays the human-readable working view; the record stays
  the truth of what happened.
- **A dedicated `judged_by="system"` or auto-expiring correction identity**
  — re-litigated and re-rejected in passing (#57 rulings): verdicts come
  only from the scope's authority; the operator qualifies, "system" never
  does.

---

## Consequences

**Positive:**

- The authority chain is grounded end-to-end in the mechanism: every scope's
  authority is delegated, and the delegator is now a first-class,
  accountable, composable layer — with no new precedence rule, no new
  memory kind, and no `fleet.yaml` change.
- strata-web deletes its highest-risk hand-rolled semantics (#83B) and every
  future consumer gets operator features by calling the library.
- The record explains everything the operator does, in both capacities —
  corrections are auditable per scope, stratum acts per attachment point.
- O-family evals (evals#6) test the contract mechanism-independently: O1
  contradiction declined, O2 verbatim across rewrites, O3 echo attribution,
  O4 corrections recorded.

**Negative / accepted costs:**

- Judge inputs grow by one block per judgment (bounded by the doctrine:
  operator memory is small; the health signal watches the bound).
- A reserved stratum label (`operator`) constrains fleet naming — a
  load-time invariant must reject a fleet stratum claiming it.
- Enforcement of "contradicts operator memory" is judged, therefore
  probabilistic — same posture as ADR 0006 D2, same mitigation: eval-gated
  (O1), residuals owner-named.
- Mechanical splice (D4) means operator corrections don't re-condense the
  summary; a correction can leave surrounding context stale until the next
  judged contribution rewrites it. Accepted: correctness of the ruling
  beats cosmetic coherence, and the next rewrite reconciles.
- No agent-facing surface means operator acts require the CLI or a console —
  deliberate, but it makes the Console work (strata-web#16/W3.2) the
  usability path for non-terminal operators.

---

## Acceptance (release gate, pre-wired in strata-evals#6)

- **O1** — contributions contradicting operator memory declined, citing the
  directive: direct, subtle-refinement-that-contradicts, and
  inherited-from-above-the-parent variants (live-judged, J-suite shape).
- **O2** — operator layer byte-identical to what the operator wrote, across
  N summary-rewrite generations (mechanical, asserted 1.0).
- **O3** — operator echoes in summaries carry "per operator directive X"
  attribution; unattributed echoes are violations.
- **O4** — supersede/retire leave the summary corrected and the
  operator-provenance record entries present; summary explainable by the
  record (mechanical, asserted 1.0).
- New J4 item: "the operator mandated this" spoofing variant goes active
  (#79's clause, now with a rendered layer to verify against).
- Live run against the #91 dev branch; residuals documented ADR-0006-style.

---

## Execution order

1. This ADR accepted (with ADR 0007 — they share the composition
   primitive's new layer contract).
2. **S2.1 / #83A** — `compose_perspective` extracted as a library
   primitive, byte-identical output first.
3. **S2.2 / #91** — this mechanism: operator record + layer + judge-aware
   rendering + D4 primitives; migration picks up #76 first; O-family lands
   in the same release.
4. strata-web adopts through the adapter (#16 → W3.2), deleting its copied
   operator-write semantics (#83B) and the `OPERATOR_SKILL` residual.
