# ADR 0007 — Publication Mechanism: The Publication as a Judged Outward Artifact

**Status:** Proposed (architect grill 2026-07-12; owner ratification pending).
Amends ADR 0006 D3 — see § Decision D4 and the erratum note in ADR 0006.
**Date:** 2026-07-12
**Related:** ADR 0006 (entitlement; D3 amended here), ADR 0004 (read-time
composition), ADR 0002 (state lives where humans can read it); issues #71
(adoption decision + owner rulings), #89 (this ADR's tracker), #90
(implementation), #83 (library primitives this lands inside), strata-evals#5
(P-family eval gate); `philosophy.md` Concept 8; `CONTEXT.md` § Publication,
§ Intra-stratum edge, § Perspective.

---

## Context

The model-level decision is made and owner-ratified (#71, 2026-07-10):
**publication** is Strata's third boundary-crossing channel — a scope exports
a curated, judged, non-binding subset of its memory for scopes that do not
contain it. Publication is a *channel*, not a third memory kind; directive
and context remain the only kinds; Concept 5 precedence is untouched.
`philosophy.md` Concept 8 records the boundary-crossing principle and the six
obligations; `CONTEXT.md` § Publication, § Intra-stratum edge, and
§ Perspective already speak the model language ("what a peer reference
delivers is the referenced scope's **publication** — never its full internal
summary").

What was deliberately deferred to this ADR is the **mechanism**:
published-flag on memory items vs. a dedicated publication scope (or a
hybrid), judged against the six obligations:

1. **Non-binding, always.**
2. **Published ⊆ believed** — supersession/retirement of the internal source
   propagates to its publication.
3. **Trust flows home** to the source item.
4. **Attribution survives condensation** — "according to X" survives every
   summary rewrite.
5. **Accountable** — provenance on every published item.
6. **Forgetting with extra force** — withdrawal must actually stop readers.

Two facts about the existing substrate decide more of this than any abstract
comparison:

- **Context has no item identity.** A scope summary is a list of directives
  (each keyed to its contribution id) plus **one condensed prose digest** for
  context (`SummaryStore`, `ScopeSummary.context: str`). Most of what
  publication exists to carry — interfaces, conventions, findings, status —
  is context-kind. A "flag on memory items" is undefined for exactly the
  content that matters most, unless the context section is first
  restructured into itemized entries, which is a far larger change than the
  word "flag" suggests.
- **Summaries are LLM-rewritten on every accepted contribution.** Anything
  stored *inside* the summary is re-worded by the scope-manager as a side
  effect of unrelated judgments. A published face living inside the summary
  is therefore a moving target: every rewrite silently re-publishes wording
  nobody judged for export. This is the same failure the operator ruling
  (#80) already rejected for its layer — "a paraphrased ruling would make
  the human's authority probabilistic." Judged-for-export content that then
  drifts under paraphrase is not judged-for-export content.

The mechanism must also land **inside** the #83 library primitives
(`compose_perspective`), so no consumer ever re-implements publication
composition, and its invariants must be stated mechanism-independently
enough for the strata-evals P-family (evals#5) to test the contract.

---

## Decision

**The publication is a per-scope outward artifact — a hybrid of the two
candidate mechanisms: copy-shaped like a publication scope's face, but owned
by the publishing scope like a flag, with a provenance anchor on every item
supplying the link the copy model owes.**

### D1. The publication artifact

Each scope owns at most one **publication**: a small on-disk markdown
artifact, sibling to its scope summary (exact path is #90's choice; it lives
with the summaries so a human reads a scope's inward and outward faces side
by side). It contains **published items**, each carrying:

- `id` — the publish act that created it (record key).
- `content` — the outward wording, **verbatim as judged**. The publication
  is never LLM-rewritten; it changes only through publish and withdraw acts.
- `kind` — `directive` or `context` *as it stands in the publisher's own
  memory*. Purely informative to readers: every published item is
  non-binding to them (obligation 1). Readers see what the publisher holds
  binding internally without inheriting any of its force.
- `subject` — optional label, as on contributions.
- `anchor` — the provenance link (obligations 2, 3, 7): the internal
  directive id(s) this item restates, or, for context-derived items, the
  `subject` it condenses. At least one anchor is required.
- `published_at` — timestamp.

The artifact is the scope's current outward face — canonical working state,
mirroring the record-vs-working-view split every scope already has: acts in
the record, current state in a human-readable file (ROADMAP principle 3).

### D2. Publish and withdraw are judged acts, recorded

Publishing is a judged act by the publishing scope's authority — distinct
from internal acceptance ("true and useful for us" ≠ "ready for others to
act on"). Concretely:

- **Agents of the scope propose.** A new agent surface (`strata_publish`,
  `strata_withdraw`) mirrors `strata_contribute`: a proposal, never a direct
  write. The publish surface is the **bound scope only** — there is no
  publishing upward or sideways; upward influence remains contribution +
  ratification, and publishing *for* another scope would exercise authority
  the agent does not hold.
- **The scope-manager judges.** One call, same shape as contribution
  judgment. Its inputs include the scope's current internal summary and its
  current publication. The judge enforces **published ⊆ believed** the same
  way the #79 admission rule verifies authority claims: content absent from
  or contradicted by the rendered internal summary is declined — including
  the hard case, a plausible-sounding *extension* of internal memory (the
  publisher must not "round up"). It also judges audience fitness: internal
  scratch, dead ends, and low-trust observations are what this channel
  exists to hold back.
- **The operator may publish or withdraw in person** — an exercise of the
  scope's authority under operator provenance (ADR 0008 D4 defines the
  correction shape; publication acts reuse it).
- **Every proposal and its judgment are appended to the scope's record**,
  accepted or declined — the record never lies. Publish/withdraw record
  entries are distinguishable from contribution entries (they are acts on
  the outward face, not proposals into the summary); exact schema is #90's,
  within this contract, and any new migration first picks up the #76
  migrator hardening.

### D3. Staleness propagation (obligations 2 and 6)

Withdrawal is effective immediately: composition is read-time (ADR 0004), so
an item removed from the publication is gone from every subsequent reader's
perspective. No reader-side cache exists to invalidate.

Propagation from internal forgetting to the publication has two paths, by
anchor type:

- **Directive-anchored items: mechanical.** When a directive leaves the
  summary — superseded or retired by a scope-manager rewrite, or corrected
  by the operator — the contribute choke point (`strata.app`, under the
  per-scope lock from #38) diffs the surviving directive ids and withdraws
  any published item whose anchors all vanished, appending the withdrawal to
  the record with the triggering event referenced. No LLM in the loop.
- **Subject-anchored (context-derived) items: judged.** Context forgetting
  is a silent omission from the next rewrite, so no mechanical signal
  exists. Instead, the publishing scope-manager's judgment inputs include
  the current publication (D2 already requires this), and the judge's
  contract extends: when a summary rewrite drops or contradicts the belief
  behind a published item, the judgment names that item for withdrawal.
  This is probabilistic where the mechanical path is not — accepted,
  eval-gated (P2 covers both paths), and bounded: publishers that want
  hard-edged propagation anchor to directives.

### D4. Composition delivers publications — the ADR 0006 D3 amendment

**Whole-face peer reads are retired** (owner leaning recorded in the #71
session: one channel, one rule — restrict-to-opt-in was considered and
rejected, see Alternatives).

- `compose_perspective` (#83A) composes, for each peer scope referenced by
  the reader's chain, that peer's **publication** as the layer payload —
  labelled with origin scope, `relation: "peer_reference"`,
  `binding: false`, provenance-preserving, verbatim. Never the peer's
  internal summary.
- `strata_read_scope_summary` on a chain-referenced peer returns the peer's
  publication, not its internal summary. (The tool name stays; the entitled
  *content* for a peer was always "its outward face" — the face just became
  real.) A scope may always read its own publication. Records stay
  chain-only (ADR 0006 D4 unchanged).
- A scope that has published nothing presents an honestly empty face: the
  layer appears with zero items, so the structure — "you reference this
  scope; it publishes nothing" — stays visible. That visibility is the
  "unmet demand" signal the #71 ruling asked for; publication remains
  demand-driven, never a duty.
- The `test_perspective_peer_edges_not_traversed`-class pins and the ADR
  0006 D3 acceptance assertions update to the new contract (tracked in
  #90's acceptance criteria; G1's peer-presence assertion flips to
  publication-presence).

**Migration story for fleets relying on D3 whole-face layers today:** peer
layers go empty on upgrade until referenced scopes publish. The crossing is
deliberate, not automatic: a one-shot, operator-initiated bootstrap
(`strata publish --bootstrap <scope>` or equivalent) has the scope-manager
propose an initial publication distilled from the scope's current summary,
judged through the normal D2 path — one judged act per scope, run by a
human, never by the upgrade itself. Release notes name the change and the
command. (Mass auto-bootstrap was rejected — see Alternatives.)

### D5. Attribution, echo, and trust routing (obligations 3, 4, 7)

- **Attribution through condensation.** The judge's system prompt gains the
  rule: material incorporated into a summary from another scope's
  publication is written with its source named — "according to X" — and
  every subsequent rewrite preserves the citation, exactly as inherited
  parent directives are already preserved verbatim. P3 measures retention
  across N rewrite generations.
- **No echo.** Wherever a judgment weighs corroboration toward ratification,
  provenance-dependent corroboration does not count: a publication never
  corroborates its own source, however many scopes republish it.
  Attribution (above) is what makes same-source detection possible; the
  admission rule from #79 already treats "peer X published this" claims as
  unestablished unless the rendered publication confirms them.
- **Trust flows home.** The anchor on every published item is the routing
  pointer: outcome feedback on a published item lands on its internal source
  item. Trust mechanics themselves remain Horizon 3; this ADR's obligation
  is structural — nothing in the mechanism severs the pointer, and the
  anchor is append-only record data from day one, so H3 has history to work
  with when it arrives.

### D6. Where this lands

Inside the #83 library primitives, not beside them: `compose_perspective`
carries the peer-publication layers (and ADR 0008's operator layers); the
publish/withdraw/propagation operations are engine (library) functions the
MCP server, the CLI, and any hosting consumer (strata-web) call. No consumer
re-implements publication semantics; strata-web adopts purely through its
adapter (strata-web#40/#41).

---

## Alternatives Considered

- **Published-flag on memory items** (the "flag model"). Rejected on two
  structural grounds. (1) Context has no item identity — the flag is
  undefined for the channel's main cargo unless the summary's context digest
  is first itemized, a deep change to `ScopeSummary`, the judge's rewrite
  contract, and every consumer. (2) The summary is LLM-rewritten; flagged
  content is re-worded by unrelated judgments, so readers receive wording
  nobody judged for export — the same paraphrase-makes-authority-
  probabilistic failure that #80 rejected for operator memory. The flag
  model's known weak spots from the #71 ruling (no separate outward record;
  multiple audiences) follow from these and were confirmed, not resolved.
- **Dedicated publication scope** (a companion scope per publisher holding
  the outward face). Rejected: a scope is "a bounded region of the fleet"
  (CONTEXT.md) with a stratum position, agents, and its own scope-manager —
  a publication face is none of these. It would double the visual and
  structural weight of `fleet.yaml`, demand an unanswerable stratum
  assignment, and place export judgment under an authority *other than* the
  scope that believes the content, contradicting "a judged act by the
  publishing scope's authority" (Concept 8). Everything attractive about it
  (a distinct outward artifact with its own record trail) survives in D1
  without the category error.
- **Restrict whole-face peer reads to explicit opt-in** (per-edge
  `full_face: true`) instead of retiring. Rejected with the owner's session
  leaning: it keeps two sideways channels alive indefinitely, each with its
  own rule, and the opt-in edge silently re-creates the contamination D3
  was identified for — internal memory exported without an export judgment.
  One channel, one rule.
- **Auto-bootstrap every referenced scope's publication at upgrade.**
  Rejected: mass-publishing summaries nobody judged for export is the D3
  anomaly re-created at migration time. The bootstrap exists (D4) but is
  per-scope, operator-initiated, and judged.
- **Publish-on-accept** (the contribution judgment also marks material
  exportable, no separate act). Rejected: it collapses exactly the two
  judgments Concept 8 insists are different — "true and useful for us" vs
  "ready for others to act on" — and reintroduces the flag model's drift
  problem, since the "exportable" material still lives in the rewritten
  summary.
- **Per-audience publications** (distinct faces for distinct readers).
  Deferred, not rejected: one face per scope in V1. The mechanism leaves
  room (a publication is already per-scope data; audiences would partition
  its items), and no current fleet demands it. Revisit on real demand;
  noted in Consequences.

---

## Consequences

**Positive:**

- One sideways channel with one rule; the boundary-crossing principle holds
  everywhere: nothing crosses a scope boundary raw.
- The outward face is deliberate, verbatim, human-readable on disk, and
  auditable end-to-end in the record (publish, withdraw, propagation).
- Withdrawal is instant for readers (read-time composition) — obligation 6
  has teeth.
- Anchors give obligations 2, 3, and 7 their structural hooks; P-family
  evals test the contract without touching the mechanism.
- `fleet.yaml` is untouched: no new node kinds, no new edge kinds. Reference
  edges mean what they always meant; only their payload sharpens.

**Negative / accepted costs:**

- A second artifact per publishing scope, and a judge call per publish act.
  Acceptable: publication is deliberate and low-frequency by design.
- Subject-anchored staleness propagation is judged, therefore
  probabilistic. Eval-gated (P2); bounded by preferring directive anchors.
- Fleets relying on D3 whole-face layers lose peer content at upgrade until
  scopes publish (deliberate bootstrap provided). This is the honest cost
  of retiring an unjudged channel.
- Readers get less by default than D3 gave them. That is the point — but it
  shifts real curation work onto publishing scopes, and unmet demand
  (visible empty faces) is the signal to do it.
- One face per scope: audience-specific curation is not expressible in V1.

---

## Acceptance (release gate, pre-wired in strata-evals#5)

- **P1 fidelity** — publish acts absent from / contradicted by /
  plausibly-extending the internal summary are declined (live-judged).
- **P2 staleness propagation** — directive-anchored: mechanical withdrawal
  on supersession/retirement, asserted 1.0; subject-anchored: judged
  withdrawal, live-eval'd with residuals documented.
- **P3 attribution through condensation** — "according to X" survives N
  rewrite generations.
- **P4 echo resistance** — provenance-dependent corroboration does not
  ratify; depends on P3's attribution.
- New J4 item: "peer X published this" origin-spoofing variant goes active.
- G1 peer-presence assertions flip to publication-presence, both
  directions; `test_perspective_peer_edges_not_traversed`-class pins
  updated.
- Live run against the #90 dev branch; residuals documented ADR-0006-style;
  0 attack successes is the gate and any residual is an owner-named
  decision.

---

## Execution order

1. This ADR accepted (with ADR 0008, which shares the composition
   primitive).
2. **S2.1** — `compose_perspective` extracted as the #83A library
   primitive, byte-identical output (golden test), before either feature
   lands in it.
3. **S2.3 / #90** — the publication channel per this ADR, inside the
   primitive; any migration picks up #76 first; P-family lands in the same
   release.
4. strata-web adopts via its adapter (strata-web#40, #41) after the Strata
   release pins.
