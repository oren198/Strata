# Plan: the outcome loop (ADR 0017) — v1.15 candidate

**Status:** approved by the CEO 2026-09-24 as the v1.15 spec, with three additions
(below, marked **CEO add**); the philosopher's four-test check is the last gate
before P1 starts. Written against `release/v1.14.0` @ f9fbb14. No code.

**Cadence.** v1.15 = P1–P4, one cycle, 3-revision bound. P5–P7 the cycle after.

**Contract:** [ADR 0017](../adr/0017-outcomes-return-to-the-item-acted-on.md).
Earned trust is the standing of a memory **item**, revised by the outcomes of
acting on it — never a party's reputation. An outcome is an ordinary
contribution that references the item acted on. The judge reads it as
corroboration or as correction. Standing feeds three things only: decay, the
corroboration weight ratification reads, and the weight judgment gives a
standing item against a contradiction. It never touches admissibility and never
weighs a directive down.

## What exists today (surveyed, with evidence)

| Need | Today | Gap |
|---|---|---|
| A reference from one contribution to another | Only `supersedes` — an FK to `contributions(id)`, meaning *this replaces that* (`_migrations/0001_initial.sql`; `app.py` `Contribution`). `subject` is a free-text label for matching, not a reference. | A reference that means *I acted on that*. `supersedes` must not be reused: it already means replacement, and an outcome that corroborates replaces nothing. |
| Correction vs supersession | Both emit the same retraction-kind change event (`change_events.py` `RETRACTION_KINDS`), and ADR 0014 refreshes one-hop readers identically. | ADR 0017 D3 splits them: correction owes notice to everyone the claim reached; supersession owes nobody notice. |
| Ratification input | No count or weight is stored. The judge reads corroboration qualitatively from the recency window and peer publications. CONTEXT.md: a publication is never self-corroborating. | The judge needs to see which items have been corroborated by outcomes, and by whose. |
| Decay | **None.** The only forgetting is condensation: under `summary_max_words` the scope-manager omits older context when it rewrites (CONTEXT.md, Retirement), and a mechanical signal reports what was dropped (#202). `freshness.py` is about sessions, not items. | "A poorly standing context item fades sooner" (D4) has nothing to hook into except condensation. |
| Standing | No weight, score or trust field anywhere in the schema. | See D-plan-2: standing is **derived** from the record, never stored. |

## Design

### P1 — the outcome reference

A new nullable column on `contributions`, **`acted_on`** (FK to
`contributions(id)`), and a matching optional `acted_on` parameter on
`strata_contribute`. It means: *this contribution reports what happened when I
acted on that item*. A contribution may carry `acted_on`, `supersedes`, or
neither; never both (reject at the tool boundary — an outcome that shows the
item wrong is a correction, which the judge decides, not the contributor).

This is ADR 0017 D2's "nothing new stored beyond the reference", taken
literally: one column, no type, no classification. Per the philosopher's
vocabulary test, **"outcome report" never becomes a stored type, a field value
or a judge classification** — it stays prose for a contribution that carries
`acted_on`.

**CEO add — agents must be told.** An optional reference nobody is told about
is never set. The read-time nudge and the seeded AGENTS.md norm each gain one
line: *"If you acted on something from memory, say which item and how it
went."* This is part of P1's gate: the demo eval's write-back run must show at
least one `acted_on` set by an agent that was not prompted to set it — or the
report says plainly that it does not happen, which is itself the finding.

### P2 — standing is derived, never stored

Standing is computed from the record when needed: for an item, the outcome
contributions whose `acted_on` points at it, their verdicts, and their
provenance. There is no standing column and no score. That keeps D1 honest (a
property of the item, derivable from what the record already holds), avoids a
number nobody decided, and means a correction to the record (a withdrawn
outcome) changes standing with no migration.

What the derivation counts, per the philosopher's tests:

- **Only outcomes that could have failed.** The judge applies the test (P3). An
  outcome it reads as "reviewed and confirmed" is admitted, if at all, as
  context, **and it does not count** toward standing.
- **Provenance-independence.** A scope may corroborate its own earlier claim by
  acting on it (Concept 8), so self-outcomes count toward *that scope's* item.
  But ratification upward reads **breadth through the ancestor's judgment** —
  one scope's repeated self-outcomes raise its own item's standing without
  becoming the fleet's consensus (D3).
- **Never on directives.** Outcomes against a directive are routed as
  *evidence to its issuer* (P5); they never enter a directive's standing,
  because a directive has none (D6).

### P3 — what the judge does with an outcome

Only when the contribution carries `acted_on`, the judge's input gains one
block: the item acted on (verbatim, with its provenance and current state), and
a short instruction with three outcomes it may reach:

1. **Corroboration** — the action could have failed and the item held.
   Admitted as context attributed to the reporter; counts toward the item's
   standing.
2. **Correction** — the claim was wrong. Admitted, and the item is replaced
   (the replaced claim leaves the context, as today), **plus** a correction
   notice to everyone the claim reached (P4).
3. **Supersession** — the claim was right and the world moved on. The item is
   replaced; **no notice**.

Plus the existing outcomes: decline (e.g. "reviewed and confirmed" presented as
evidence, or no ground), and the ordinary kinds rules.

**This block is added only for contributions that carry `acted_on`.** That is
the lesson of v1.14's M1 (#212): adding directive ids to every prompt fixed the
target item and degraded general judging on J1. Here, a contribution without
`acted_on` sees a prompt byte-identical to today's, and a test pins that.

### P4 — correction owes notice; supersession does not

Split the change-event kinds: a new **`claim_corrected`** kind, delivered to
every reader the claim reached — the scope's own readers, and, where the item
was published, the publication's readers (published-within-believed: the
publication follows the item, and its withdrawal reaches readers as evidence,
never as silent absence). Supersession keeps today's behaviour. ADR 0014's
refresh machinery is reused; no new delivery path.

**CEO add — the fan-out is bounded, and the bound is stated.** `claim_corrected`
is an input change under ADR 0014 and inherits its termination rule (D4): the
correction mints one change id; every change derived from processing it
inherits that id; a scope refreshes for a given change id **at most once**;
coalescing collapses several pending changes into one refresh; fixpoint damping
means a refresh that changes nothing emits nothing; and the hop budget remains
as the recorded backstop. So a correction reaches every reader once, and a
reference cycle cannot turn it into a wave. The eval gate carries an item for it
(item 8).

### P5 — outcomes that contradict a directive

Never weigh the directive down. The judge admits such an outcome as context
(the observation stands), and the engine raises it to the directive's issuer as
evidence — for a scope-manager, a change event on its own scope; for an
operator directive, a note in the operator's view. The issuer revises through
the ordinary channel or doesn't. Nothing binding erodes quietly (D6).

### P6 — decay, the smallest honest version

There is no decay today, so this plan does not invent a clock. It adds standing
where forgetting already happens: **condensation**. When the scope-manager
rewrites a summary under the word budget, the rewrite prompt lists, for each
context item it carries, whether outcomes have corroborated or corrected it.
The instruction: under budget pressure, drop uncorroborated context before
corroborated context. The #202 condensation signal already reports what was
dropped, so the effect is measurable.

**CEO add — the order is recorded, not only tested.** The #202 condensation
signal records, for each context item dropped, whether it was corroborated,
corrected or unexamined at the time, so a stranger reading the record can see
that uncorroborated context went first — the claim is visible in the product,
not only asserted by a test.

This realises D4.1 ("a poorly standing context item fades sooner") without a
time model. A real time-based decay is a separate decision, and this plan does
not take it.

### P7 — ratification

The judge already reads corroboration qualitatively. When it considers
ratifying context into a directive, the prompt shows each candidate item's
**derived** standing: the corroborating outcomes, their reporters' scopes, and
whether they are independent of the item's source. Nothing is counted
numerically in storage. The judge still decides; standing is evidence it reads,
per D4.2.

## Code paths that change

- `_migrations/` — one migration adding `contributions.acted_on`.
- `app.py` `Contribution`, `run_contribution` / `rejudge_contribution` — carry
  `acted_on`; reject `acted_on` together with `supersedes`.
- `mcp/server.py` `strata_contribute` — the optional `acted_on` parameter and
  its docstring, stating what an outcome is and the "could have failed" test in
  plain words for the agent.
- `record_store.py` — persist `acted_on`; a query for the outcomes referencing
  an item (P2's derivation).
- `scope_manager.py` — the `acted_on` block in the user message (P3), only when
  present; the three outcomes in the judge tool; standing in the condensation
  rewrite prompt (P6) and the ratification context (P7).
- `change_events.py` — the `claim_corrected` kind and its fan-out (P4).
- `operator.py` / the Console operator view — the evidence-to-issuer note for
  outcomes against an operator directive (P5).
- `perspective.py` — nothing new beyond surfacing a correction notice through
  the existing change-event layer.

## Eval gate (strata-evals, a new `outcome_loop` family)

Each item sits in a scope holding the item acted on. Goldens state decision
**and** ground, scored with J4-style reason scoring.

1. **Corroborates** — "I deployed with the rel- tag convention and the release
   pipeline picked it up" (the action could have failed and didn't) → accept
   as context, counted toward the item's standing.
2. **Corrects** — "I used the documented port 8443 and the service refused
   connections; it listens on 9443" → correction: the item is replaced, and a
   `claim_corrected` notice is raised.
3. **Echo, must not count** — "I reviewed the runbook entry and confirm it is
   right" → admitted as context at most, **not** counted toward standing;
   decline if presented as evidence.
4. **Supersession, not correction** — "the port was 8443 until today's
   migration; it is now 9443" → replaced, **no** correction notice.
5. **Self-corroboration** — the scope's own earlier claim, acted on, held →
   counts toward its own item; a second item checks that three self-outcomes
   from one scope do not appear as fleet consensus in ratification.
6. **Against a directive** — an outcome contradicting an operator directive →
   admitted as context, the directive unchanged, evidence raised to the issuer.
7. **Decay** — a scope over budget with one corroborated and one uncorroborated
   context item of similar length → the uncorroborated one is dropped first
   (read from the #202 condensation signal, which records the drop order).
8. **Correction fan-out, bounded** — a corrected claim that had been published
   to two readers → both receive the `claim_corrected` notice exactly once, and
   nothing loops (a reference cycle between the two readers is included).
9. **Unprompted `acted_on`** — in the demo eval's write-back run, an agent that
   was not told to set the reference sets it at least once; or the report says
   it does not happen.

**No-regression gate — run first, not last** (the v1.14 lesson): J1 at reps=3
against the release head, and J4, with every item that does not carry
`acted_on` required to show no verdict change. Twins-style repeats (10 reps) on
items 1 and 3, the corroborate/echo pair, since that is the boundary most likely
to be noisy.

## Cost

- **Engineering:** P1–P4 are one item (reference, judge block, notice split);
  P5–P7 a second. Two one-cycle items, each with its own gate and the usual
  3-revision bound.
- **Judging:** zero extra calls for contributions without `acted_on` (the prompt
  is unchanged). One judgment per outcome report, same as any contribution. The
  condensation and ratification prompts grow by the listed standing lines —
  small, and bounded by the items in the summary.
- **Measurement:** the new family (~10 items), J1 reps=3 and J4 on two trees —
  well under $0.20 on the current OpenRouter judge.

## Risks and open questions

1. **Will the judge apply "could have failed"?** It is a judgment, not a
   mechanism, and v1.14 showed this judge does not fill structured fields.
   Nothing here depends on a structured field being filled: the verdict and its
   reason carry the outcome, and the derivation reads verdicts. If the judge
   cannot tell corroboration from echo, item 3 fails and the plan stops there.
2. **Agents must set `acted_on`.** An agent that reports an outcome without the
   reference gets ordinary judging — no harm, but no standing either. The
   AGENTS.md norm and the tool docstring carry the instruction. Write-back rate
   tells us whether they use it.
3. **Correction notice fan-out** — for a widely published item, notice reaches
   many scopes. Reuses ADR 0014's bounded refresh; to be checked against its
   termination rule, not assumed.
4. **Time-based decay is out of scope** by choice (P6). If condensation-based
   fading proves too weak, that is a finding and a separate decision.
