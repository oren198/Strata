# ADR 0011 — Judgment Economics: Amendment Ops, Mechanical Window, Coalesced Judgment

**Status:** Accepted (adversarially reviewed 2026-08-11; implementation
pending — issues #131, #132, #133)
**D4 amended by ADR 0014 (2026-09-05):** an input-change refresh's amendment
may carry `append` and `publish` ops as well as `new_context` and lifecycle
ops — see the amendment note in D4.
**Date:** 2026-08-10, revised 2026-08-12
**Related:** ADR 0004 D1 (the batched-manager work this ADR's D3 delivers,
tracked separately there), D2 (whose closing property — "the manager remains
a single LLM call per contribution" — D3 amends), D4 (version stamps and the
manager-refresh path, reshaped by D4 below), D5 (the word budget and its
words-not-tokens rationale, which D2 honors); ADR 0006 (the ENTITLEMENT
block re-costed here); ADR 0007 D1/D5 (publication is never LLM-rewritten —
the precedent D1 generalizes — and `withdraw_published`, which D1 preserves);
ADR 0008 D3/D4 (operator-echo attribution, narrowed by D1; the
explicit-retire record shape, consumed by D1); issues #63 (overflow re-ask),
#113 (parse re-ask), #57/#118 (judgment attempts machinery); CONTEXT.md
§ Scope summary, § Directive, § Scope-manager, § Ratification,
§ Supersession, § Retirement.

---

## Context

Every accepted contribution costs one scope-manager judgment, and the shape
of that judgment makes its cost **grow with the scope's maturity** — the
older and more useful a scope's memory, the more each new memory costs.
Measured on `claude-haiku-4-5` (the engine's default judge model) against a
scope whose summary sits at the combined 500-word budget (five long-standing
directives plus a condensed context section):

**Output: the judge regenerates the entire summary on every accept.** The
`submit_judgment` tool schema requires `new_summary` — the full
`ScopeSummary`, every existing directive re-emitted, the whole context
section rewritten — plus reasoning, capped at `max_tokens=4096`. On the
measured scope that is ~1.2–1.5K output tokens per accept, of which only a
small fraction is *new* information. Provider pricing puts output tokens at
several times the price of input (roughly 5:1 for the default model — check
current pricing), so re-emission dominates the marginal cost. It is also the
dominant *fragility*: verbatim directive preservation is enforced by
**nothing** — it is prompt discipline alone. The overflow re-ask (#63)
enforces the word budget, not preservation; its verbatim reminder is prompt
text inside a corrective most calls never receive. And each re-ask that does
fire re-sends the entire conversation and regenerates the entire summary
again, roughly doubling both axes of the call.

**Input: the recent-contributions window carries full verbatim text.** The
judge prompt includes the last 20 contributions with their complete
contents. On the measured scope that window alone is ~7.5K tokens of ~13K
total input (the system prompt and tool schema are cache-eligible —
`cache_control` is already applied — so ~11K of the 13K is charged at the
uncached rate on a warm cache). The judge uses the window almost exclusively
for recency checks: is this a duplicate, does the named `supersedes` id
exist, does the claim contradict something just recorded. Full verbatim text
is far more than those checks need.

**Throughput: same-scope judgments serialize with no coalescing.**
Judgments for one scope must serialize (single-writer semantics on the
summary — a per-scope lock in `locks.py`, held by `run_contribution` across
append and judgment). Contributions that arrive while a judgment is in
flight block on that lock and then each pay the full per-judgment cost — N
queued contributions cost N complete prompts and N full summary
regenerations, back to back.

Net effect: per-accept cost that rises as the memory improves. A governed
memory with that shape has its economics inverted, whatever the absolute
figures on a given model.

The constraint that must not move: **every contribution passes judgment**.
This ADR makes judgment cheaper; it does not make it optional.

---

## Decision

### D1. Judgment returns amendment ops; existing directives are never re-emitted

`submit_judgment` no longer returns `new_summary`. On accept it returns an
**amendment**:

- **`directive_ops`** — operations on the directives list:
  - **`append`** — admit the new contribution as a directive **in its own
    words**. The engine builds the `Directive` row itself from the
    contribution's verbatim content, id, subject, and provenance fields; the
    judge never restates the text. This is the default op, and the prompt
    biases hard toward it: byte-exact admission is cheaper, cannot drift,
    and keeps the summary's directive identical to what the record shows was
    submitted.
  - **`publish`** — admit a directive **in the judge's words**:
    `publish(content, subject?, supersedes?)`, with the id minted from the
    triggering contribution so provenance still anchors in the record. This
    op exists because three behaviors the engine already owns cannot be
    expressed as byte-copies, and this ADR keeps all three:
    *ratification* (CONTEXT.md § Ratification — consolidating a pattern
    across several prior contributions into one directive published with the
    scope's authority; no single contribution's bytes are that directive),
    *local wording* (the scope-manager's existing latitude to word
    locally-originating directives as it sees fit), and *attribution*
    (ADR 0008 D3's "per operator directive `<id>`" and ADR 0007 D5's
    "according to `<scope>`", which must be written **into** the echoed
    text). The decision rule the prompt encodes: *append unless the binding
    text must differ from the contribution's text; if it must, publish, and
    say why in your reasoning* — so every `publish` is auditable in the
    judgment row.
  - **`supersede(id)`** — valid **only** in an amendment that also carries an
    `append` or `publish`: supersession replaces (CONTEXT.md
    § Supersession); an unpaired `supersede` is a retirement wearing the
    wrong name and is rejected at parse. The record's explanation is the
    incoming directive's own `supersedes` reference.
  - **`retire(id)`** — removes a directive and appends a `Retirement` row
    via the record store's existing `append_retirement` with
    `retired_by="scope-manager"` — this **is** the scope-manager
    explicit-retire that ADR 0008 D4 reserved its record shape for. No
    tombstone remains in the summary (CONTEXT.md § Retirement).
- **`new_context`** — the rewritten **context section only** (a single
  condensed string, per ADR 0004's summary shape). This remains generated:
  context is the compression surface, and condensation is the judge's actual
  job there.
- **`withdraw_published`** — unchanged in the schema. Its prompt rule is
  rephrased against *the amendment being submitted* rather than "your
  rewritten summary"; the mechanical propagation path reads removed
  directive ids **directly from `directive_ops`** instead of diffing two
  summary generations — strictly simpler than today.

The engine applies the amendment mechanically to the stored summary and
bumps `version` as today. Verbatim preservation of untouched directives
stops being a prompt obligation and becomes **structural**: existing
directive rows are bytes the judge cannot reach except by id-addressed op.
This generalizes a pattern the engine has already committed to twice — the
publication artifact is never LLM-rewritten (ADR 0007 D1) and the operator
layer is spliced mechanically (ADR 0008 D4).

**Invalid ids.** An amendment naming an unknown or already-retired directive
id gets **exactly one** corrective re-ask listing the valid ids (a new
corrective text — the #113 re-ask's wording targets a different failure
mode, but the one-retry discipline is the same). If the second attempt still
names an invalid id, the bad op is **dropped**, the rest of the amendment
applies, and the dropped op is noted in the judgment record. A bad op must
never cost the contribution its verdict — the alternative (routing to the
parse-failure path) would convert a hallucinated id into a stranded,
unjudged contribution, a strictly worse failure than today's merely-wrong
summary.

**The word budget keeps its combined definition** (ADR 0004 D5:
`_summary_word_count` sums context words and directive content words), so
the budget continues to exert retirement pressure on the whole summary. What
changes is the corrective's vocabulary: the overflow re-ask (#63) now asks
for `retire` ops and/or a shorter `new_context` — the two levers that
actually exist under this ADR — and regenerates a few hundred tokens, not
the whole summary. If the judge declines to retire and the summary stays
over budget, the existing keep-first discipline holds: an over-budget
summary is strictly better than a destroyed or reversed judgment. Decline
verdicts are unchanged (no amendment).

**ADR 0008 interactions, stated precisely.** The decline-on-contradiction
rule is unaffected. The attribution obligation (D3) is **narrowed**: because
an `append`ed directive's text is the contribution's verbatim bytes, "per
operator directive `<id>`" attribution can be written only into `publish`ed
directive text or into `new_context`. An operator-echo that arrives as a
contribution whose bytes lack the attribution is admitted via `publish`
(with the attribution written in) or attributed in context — the judge may
not `append` it unattributed. ADR 0008's O3 acceptance gate is re-specified
accordingly (see Acceptance below).

### D2. The recency window is built mechanically from what the record stores

The judge prompt's RECENT CONTRIBUTIONS block is replaced by a mechanical
digest of the last N contributions: per row —
`(contribution id, subject, timestamp, state, decision, judgment reasoning,
content prefix)`, where:

- **state** comes from the record's contribution-state machinery (#57/#118):
  `judged` rows carry their decision and reasoning; `pending` and
  `judge_failed` rows render their state with the reasoning column empty.
  The window will routinely contain such rows — including the contribution
  currently under judgment, which is appended to the record before the
  window is read.
- **judgment reasoning** is the verdict explanation the judge wrote when the
  row was judged — written once, stored immutably, so nothing in the digest
  can drift.
- **content prefix** is a fixed-length mechanical excerpt of the
  contribution's content (~200 characters). Reasoning alone is a verdict
  *justification*, not a restatement — "declined: contradicts operator
  directive `op_x`" says nothing about what the contribution claimed — and
  `subject` is optional, so without a prefix a subject-less declined row
  would be nearly empty exactly where duplicate detection needs content.

Only the most recent few contributions keep full verbatim text (the
"resubmitted moments later" case, where phrasing-level comparison earns its
cost) — a named, configurable setting (`window_verbatim_tail`, default 3),
per ADR 0004 D5's precedent of named constants. The window is additionally
capped **by characters, not tokens** (N rows or a fixed character budget,
whichever bites first) — ADR 0004 D5's reasoning stands: tokens require a
tokenizer round-trip the manager loop doesn't have. On the measured scope
the window drops from ~7.5K tokens to roughly 1K.

Assembling the digest needs one new windowed record-store read that returns
(contribution, state, judgment-notes) triples for the last N — no existing
read carries the notes and the state together.

### D3. Multi-contribution judgment: one call, per-contribution verdicts

The scope-manager gains a batch judgment mode: one call carrying several new
contributions **in arrival order**, returning an ordered per-contribution
verdict list plus a single cumulative amendment (D1 ops). Within the call
the judge processes contributions sequentially, each against the summary as
amended by its predecessors — the same verdicts serial judgment would
produce, minus N−1 repeated prompts and summary outputs. One declined
contribution does not poison the batch; each verdict lands in the record as
its own judgment row against its own contribution id, so the record's shape
and the attempts machinery (#57/#118) are untouched at the storage layer.

This amends ADR 0004 D2's closing property: the manager is now a single LLM
call per **batch**; a batch of one — the default and the common case — is
exactly today's single call.

**Serialization changes shape.** Today's per-scope `threading.Lock` in
`locks.py` cannot enumerate or drain waiters, and `run_contribution` appends
and judges under a single hold. The lock is replaced by a per-scope work
queue with a single drain: `run_contribution` appends its contribution to
the record (under the lock), enqueues it, and either becomes the drain
worker — judging everything queued, up to the batch cap — or waits for its
own verdict from the batch that includes it. `ContributionOutcome` is
unchanged per caller; the judge-failure error gains a per-contribution
shape (today it carries exactly one contribution id). This is a real, if
modest, change to the engine's concurrency model, and it is confined to
`run_contribution` and `locks.py`.

**Bounds and stamps.** Batch size is capped (default 5) so the prompt stays
bounded and a failed call never strands more than a cap's worth — each
still gets its attempt row on failure. `max_tokens` scales with the cap (N
reasonings plus one amendment must fit). A batch produces exactly **one**
summary write, hence one `version` increment for N accepts, with
`parent_version` stamped from the parent summary read at batch start —
staleness detection (ADR 0004 D4) needs monotonicity, not one-tick-per-
contribution, and this ADR states that rather than leaving it inferred.

### D4. The manager-refresh path is rebuilt on the same ops

> **Amendment (2026-09-05, ADR 0014 D2):** this decision's drop of admitting
> ops was correct for the launch-time parent-splice refresh below, and stays
> for it. It does not hold for the reactive refresh ADR 0014 adds on an input
> change other than a parent splice: that refresh's synthetic contribution is
> a real record row reporting a real event (an input changed), so a directive
> minted from it carries honest provenance — the thing this D4 said a refresh
> never had. On that path the amendment may carry `append` and `publish` ops
> as well as `new_context` and lifecycle ops. The splice-only refresh below is
> unchanged: the parent's directives are already spliced in mechanically, so
> admitting anything more from that judgment would still have nothing to mint
> it from.

`strata launch`'s refresh (ADR 0004 D4) currently judges a synthetic
contribution whose entire purpose is to make the judge re-emit the summary
incorporating refreshed parent state — inexpressible once `new_summary` is
gone, and built on the same fragile quoting rule as everything else (the
prompt's "quote parent directives VERBATIM" instruction). Under this ADR:

- **Parent-directive incorporation becomes a mechanical splice.** The engine
  copies new or changed parent `Directive` rows into the child summary
  byte-exactly, ids and provenance preserved — verbatim by construction, no
  LLM involved. The prompt's parent-quoting rule is deleted; the class of
  paraphrase bugs it guarded against is deleted with it.
- **The refresh judge call becomes context-only**: the synthetic refresh
  contribution is judged as today (the record trail is unchanged — the
  summary never moves without one), but its amendment may carry only
  `new_context` and lifecycle ops — reconciling the context digest with the
  refreshed parent state is the only part of a refresh that is genuinely
  judgment.

---

## Alternatives considered

- **Keep the full rewrite; lean harder on prompt caching.** Caching is
  already applied to the system prompt and tool schema and helps only the
  input axis. Output — the expensive and fragile axis — is untouched, and
  preservation stays a prompt obligation. Rejected.
- **A judge-maintained rolling digest** (the judge emits, each turn, a
  compressed window for the next turn). Adds output cost to every judgment
  and makes recency context LLM-rewritten state — a drift risk class the
  summary already carries and nothing else needs to. Rejected in favor of
  D2's mechanical digest, which is built from text written once and stored
  immutably.
- **A second, cheaper summarizer call for window entries.** Adds a call and
  a second LLM-authored state to every judgment to save what the content
  prefix saves mechanically. Rejected.
- **Drop to a smaller judge model.** Orthogonal: it scales the price of the
  shape without fixing the shape — cost still grows with maturity, and
  preservation is still prompted, not structural. Available to operators
  regardless of this ADR.
- **Hard-cap the directives list.** Punishes legitimate maturity; the
  combined word budget plus the overflow re-ask's new `retire` vocabulary
  already exerts the pressure a cap would, without a cliff. Rejected.
- **Skip judgment for small context contributions.** Breaks "every
  contribution passes judgment" for a saving smaller than D1 delivers.
  Rejected outright.

---

## Consequences

**Cost, per axis, on the measured scope** (`claude-haiku-4-5`, summary at
the combined 500-word budget): input ~13K → ~4.5K tokens (the window cut,
D2); output ~1.2–1.5K → ~300–400 tokens (no re-emission, D1). With output
priced at several times input, that is roughly a two-thirds to
three-quarters reduction in what an accept costs, and — the structural
point — per-accept **output** stops growing with directive count. Input
still carries the summary once per call (`_render_summary` renders every
directive into the CURRENT SUMMARY block); the combined budget bounds that.
Re-asks, when they fire, regenerate a few hundred tokens instead of the
whole summary. Contention adds a further division by batch size (D3).

**Correctness.** Preservation of untouched directives becomes structural —
the largest class of silent summary corruption is deleted outright, the same
move ADR 0007 D1 and ADR 0008 D4 made for their surfaces. The remaining
generated text — `new_context`, `publish`ed directives, reasoning — is a
smaller and more auditable surface. Known quality trades, stated with their
recovery levers: near-duplicate detection beyond the verbatim tail now
compares subjects, reasoning, and content prefixes rather than full text
(raise `window_verbatim_tail` if evals say so); `publish` reintroduces
judge-authored directive text where byte-copies cannot serve (its usage
rate is eval-tracked — a judge that publishes where it should append is
re-inflating the surface this ADR shrinks).

**Compatibility.** Changes: the `submit_judgment` tool schema and prompt
blocks; both corrective re-asks' texts (plus the new invalid-id
corrective); `run_contribution` and `locks.py` (per-scope queue, D3); the
manager-refresh path (D4); the `withdraw_published` propagation source
(ops, not summary diffs); one new windowed record-store read (D2).
Unchanged: the record schema (D1's `retire` *consumes* ADR 0008 D4's
reserved `Retirement` shape rather than adding one); `ScopeSummary` and the
summary store; perspectives and composition; the one-retry discipline of
every corrective; `ContributionOutcome` per caller; the combined word
budget's definition (ADR 0004 D5).

---

## Acceptance (release gate)

Ship gates, named so the eval suite can reference them. J1 and J5 are
mechanical asserts (target 1.0); the rest are judged eval families with
baselines.

- **J1 — structural preservation:** across N accepted judgments, every
  directive row not named by an id-addressed op is byte-identical before and
  after. The headline correctness claim, and the cheapest eval in the
  family.
- **J2 — amendment-op correctness:** append/publish/supersede/retire chosen
  and formed correctly on labeled cases; `publish` used only where the
  binding text must differ from the contribution's bytes, with the reason in
  the judgment's reasoning; unpaired `supersede` rejected.
- **J3 — context-rewrite quality:** `new_context` versus the full-rewrite
  baseline on the same cases.
- **J4 — recency checks at the digest window:** duplicate and `supersedes`
  detection versus the verbatim-window baseline; digest rows for `pending`
  and `judge_failed` contributions render correctly.
- **J5 — invalid-id fallback:** an amendment naming an unknown or retired id
  never strands the contribution — one corrective, then drop-and-note.
  Mechanical assert.
- **J6 — budget overflow:** the overflow corrective requests retire ops
  and/or shorter context; the directives-alone-over-budget case ends in
  keep-first, never a destroyed judgment.
- **J7 — ADR 0008 re-run:** O2 (operator layer byte-identical across N
  generations) and the re-specified O3 (operator-echo attribution present in
  `publish`ed text or context; unattributed `append` of an echo declined).
- **J8 — ADR 0007 propagation:** `withdraw_published` verdicts and the
  ops-sourced mechanical propagation of removed directive ids.
- **J9 — refresh:** a stale child picks up a new parent directive on
  `strata launch` byte-exactly via the mechanical splice; the refresh
  judgment amends context only.
- **J10 — batch equivalence:** same contributions, same order → same
  verdicts and same final summary as serial judgment; one `version`
  increment per batch; a failed batch call leaves an attempt row per
  member.
