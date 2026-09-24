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
| Decay | **None.** The only forgetting is condensation: under `summary_max_words` the scope-manager omits older context when it rewrites (CONTEXT.md, Retirement), and a mechanical signal reports what was dropped (#202). `freshness.py` is about sessions, not items. | Unexamined context fading first (D4) has nothing to hook into except condensation. |
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

- **Only outcomes that could have failed — and nothing is stored to say so.**
  The judge applies the test (P3), and the closure below makes the record
  carry the reading by construction: an **accepted** `acted_on` contribution
  that did not replace the item **held**. No judge-assigned reading is ever
  stored.
- **A scope's own outcomes are full corroboration.** Independence is a property
  of the evidence, not of who first said the words (Concept 8), and each action
  could have failed — so outcomes a scope reports on its own claim count in
  full toward that item's standing. What they cannot establish is
  **generality**: standing is earned where the outcomes occurred. Ratification
  reads breadth because reach is a claim about generality, and P7's reporter
  scopes and their independence already carry that.
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

**The closure (philosopher, confirmed by the CEO, 2026-09-24).** An `acted_on`
contribution is **accepted only when it reports that the item HELD**
(corroboration; nothing replaced) **or that it FAILED** (correction or
supersession; the item is replaced). **Any other `acted_on` contribution is
declined** — an echo ("reviewed it and confirmed it"), an ambiguous report
("partially worked"), a pending one ("result unclear"). The reason must first
**name the missing ground** — *"no outcome: the action could not have failed"*,
or *"no outcome reported"* — and only then, as guidance, say it may be
resubmitted without `acted_on` if it is worth keeping as ordinary context. The
same content without `acted_on` is judged as ordinary context, as today. With
that closure, *accepted and not replaced* means *held*, so P2's derivation
cannot over-count, and no judge-assigned reading is ever stored.

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

**What `claim_corrected` carries.** The identity of the corrected claim and the
correcting content, arriving at each reader **as evidence its own judge acts
on**. It never deletes anything from the reader: the reader's judge decides
what the correction means for what that scope holds, exactly as with any other
input change.

**After the split, supersession must not signal wrongness.** "This changed" and
"this was wrong" owe different things. The supersession refresh keeps its
existing kind and wording and must never read as a correction; a test pins that
a supersession's notice carries no correction language and no correcting
content.

**CEO add — the fan-out is bounded, and the bound is stated.** `claim_corrected`
is an input change under ADR 0014 and inherits its termination rule (D4): the
correction mints one change id; every change derived from processing it
inherits that id; a scope refreshes for a given change id **at most once**;
coalescing collapses several pending changes into one refresh; fixpoint damping
means a refresh that changes nothing emits nothing; and the hop budget remains
as the recorded backstop. So a correction reaches every reader once, and a
reference cycle cannot turn it into a wave. The eval gate carries an item for it
(item 8).

### P5 — outcomes that bear on a directive

The theory draws a line here, and the judge holds it.

- **A contribution that asserts a contrary rule is declined.** "Use 9443" set
  against a directive that says 8443 is a rule competing with a rule; context
  never overrides a directive (Concept 5), so it is declined, as today.
- **An outcome that reports a consequence of following the directive is
  admitted as context, and raised to the issuing scope.** "Used 8443 as
  directed; the service refused connections" is an observation of the world,
  and it stands. The directive is never weighed down (D6).

**Raised to whom, and how.** To the scope that **issued** the directive, not to
the scope where the outcome occurred: the issuer of an inherited directive is an
ancestor. The channel is the ordinary one — an **upward contribution** to the
issuing scope, judged there like any other, with the reporting scope's
provenance. For an operator directive, the issuer is the operator, and the
evidence appears in the operator's view. The issuer revises through the
ordinary channel, or doesn't. Nothing binding erodes quietly.

### P6 — decay, the smallest honest version

There is no decay today, so this plan does not invent a clock. It adds standing
where forgetting already happens: **condensation**. When the scope-manager
rewrites a summary under the word budget, the rewrite prompt lists, for each
context item it carries, whether outcomes have corroborated it. The
instruction: under budget pressure, drop **unexamined** context before
**corroborated** context. (A corrected item is not in the order at all: it has
already been replaced.) The #202 condensation signal already reports what was
dropped, so the effect is measurable.

**CEO add — the order is recorded, not only tested.** The #202 condensation
signal records, for each context item dropped, whether it was corroborated,
corrected or unexamined at the time, so a stranger reading the record can see
that unexamined context went first — the claim is visible in the product,
not only asserted by a test.

This realises D4.1 without a time model, and the plan's wording is
**unexamined**, never "poorly standing": an item nobody has acted on has not
been judged poor, only untested.

`acted_on` is also a **usage** signal: an item somebody acted on is an item in
use. So the one reference serves both of Concept 7's decay drivers — how an
item has fared, and whether it is used — without a second mechanism. A
time-based decay model stays out of scope; it is a separate decision.

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
   The golden requires the contribution to state **what happened** — an
   observable result ("the pipeline picked up the rel- tag") — and the judge
   reaches *held* only on one.
   **1b. No observable** — carrying `acted_on`: "I acted on it and it worked" →
   **declined** as ambiguous, "no outcome reported": nothing observable was
   stated, so there is nothing that could have failed.
2. **Corrects** — "I used the documented port 8443 and the service refused
   connections; it listens on 9443" → correction: the item is replaced, and a
   `claim_corrected` notice is raised.
3. **Echo, must not count** — carrying `acted_on`: "I reviewed the runbook
   entry and confirm it is right" → **declined**, the reason naming the missing
   ground ("no outcome: the action could not have failed") before the
   resubmit-without-`acted_on` guidance. Hard stop for the release (CEO
   ruling 5).
   **3b. Ambiguous or pending** — carrying `acted_on`: "it partially worked" /
   "result unclear so far" → **declined**, "no outcome reported".
4. **Supersession, not correction** — "the port was 8443 until today's
   migration; it is now 9443" → replaced, **no** correction notice.
5. **Self-corroboration** — the scope's own earlier claim, acted on, held →
   counts in full toward its own item's standing.
   **5b. Generality** — three such outcomes, all from the one scope → when the
   ancestor considers ratification, the evidence shows one reporting scope, and
   the judge's reason treats it as local standing, not as evidence of reach.
6. **Consequence of following a directive** — "used 8443 as directed; the
   service refused connections", against an inherited directive → accepted as
   context, the directive unchanged, and the evidence raised as an upward
   contribution to the ISSUING scope (an ancestor), not the reporting one.
   **6b. Contrary rule** — "use 9443", against the same 8443 directive →
   declined (Concept 5).
7. **Decay** — a scope over budget with one corroborated and one unexamined
   context item of similar length → the unexamined one is dropped first
   (read from the #202 condensation signal, which records the drop order).
8. **Correction fan-out, bounded** — a corrected claim that had been published
   to two readers → both receive the `claim_corrected` notice exactly once, and
   nothing loops (a reference cycle between the two readers is included).
**Hazard pin for P7 (next cycle).** A summary holding a restatement of an item
and no `acted_on` outcome for it → when ratification is considered, the reason
cites **no corroboration**. A restatement is never read as evidence.

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

## CEO rulings (2026-09-24)

Six product questions, raised when the operator had another agent review this
plan, and ruled on by the CEO. They bind v1.15 alongside the plan above.

1. **Adoption is measured, not gated.** The nudge and the AGENTS.md line are the
   forcing function. If P1's live run shows zero unprompted `acted_on`, P2–P4
   still ship; the README says *"plumbing shipped; adoption measured: N of M
   sessions"*; and a structured affordance becomes a design decision taken with
   a number in hand. (This replaces P1's "at least one unprompted `acted_on`
   passes" with "the number is measured and reported".)
2. **Correction blast radius.** A correction may reach any published item,
   bounded by ADR 0014 D4 and pinned by P4's tests (eval item 8). The dogfood is
   our own fleet: the first multi-scope correction runs in Strata's own scopes,
   with the architect watching the record.
3. **Self-corroboration has no cap.** A cap is a number nobody decided. Local
   standing can be high; fleet reach stays the ancestor's judgment at
   ratification. This is stated as an explicit acceptance, not an oversight.
4. **P4 is must-ship.** If `claim_corrected` slips, v1.15 does not tag. P4's
   value stands independent of adoption: it fixes the collapse of correction
   into supersession for **every** path, not only for outcome reports.
5. **Eval item 3 is a hard stop.** If the judge counts "reviewed and confirmed"
   as corroboration, the release is blocked. No override with a known miss.
6. **Copy.** *"Outcome loop: plumbing shipped, adoption measured"* — never
   "standing works" — until the adoption number exists.

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
