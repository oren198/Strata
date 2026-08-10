# ADR 0011 — Judgment Economics: Amendment Ops, Mechanical Window, Coalesced Judgment

**Status:** Accepted (implementation pending)
**Date:** 2026-08-10
**Related:** ADR 0004 (summary store, version stamps), ADR 0008 (operator
stratum — verbatim-preservation obligations the judge carries), issue #63
(overflow re-ask), issue #113 (parse re-ask), issues #57/#118 (judgment
attempts machinery); CONTEXT.md § Scope summary, § Directive, § Scope-manager, § Supersession,
§ Retirement.

---

## Context

Every accepted contribution costs one scope-manager judgment, and the shape
of that judgment makes its cost **grow with the scope's maturity** — the
older and more useful a scope's memory, the more each new memory costs:

**Output: the judge regenerates the entire summary on every accept.** The
`submit_judgment` tool schema requires `new_summary` — the full
`ScopeSummary`, every existing directive re-emitted verbatim, the whole
context section rewritten — plus reasoning, capped at 4096 tokens. On a
mature scope observed in production use (five long-standing directives,
context near the 500-word budget) an accept generates ~3K output tokens, of
which only a small fraction is *new* information. Provider pricing makes
output tokens several times the price of input tokens, so this is the
dominant cost. It is also the dominant *fragility*: verbatim directive
preservation is enforced only by prompt discipline plus the one-shot
overflow re-ask (#63) — and each re-ask that fires re-sends the entire
conversation and regenerates the entire summary again, roughly doubling both
axes of the call.

**Input: the recent-contributions window carries full verbatim text.** The
judge prompt includes the last 20 record entries with their complete
contents. On the same observed scope that window alone is ~7.5K tokens
(~30KB of closeout-length contributions), out of ~13K total input. The judge
uses the window almost exclusively for recency checks: is this a duplicate,
does the named `supersedes` id exist, does the claim contradict something
just recorded. Full verbatim text is far more than those checks need.

**Throughput: same-scope judgments serialize with no coalescing.** Judgments
for one scope must serialize (single-writer semantics on the summary).
Contributions that arrive while a judgment is in flight wait their turn and
then each pay the full per-judgment cost — N queued contributions cost N
complete scope-side prompts and N full summary regenerations, back to back.

Net effect, measured: **~28K cost-weighted tokens per accept** on a mature
scope (13K input + 3K output at a 5:1 output premium). For any deployment
that meters judgment — and for any operator watching an API bill — that
bounds real fleets to a handful of judgments a day. A governed memory whose
per-write cost rises as the memory improves has its economics inverted.

The constraint that must not move: **every contribution passes judgment**.
This ADR makes judgment cheaper; it does not make it optional.

---

## Decision

### D1 — Judgment returns amendment ops; directives are never re-emitted

`submit_judgment` no longer returns `new_summary`. On accept it returns an
**amendment**:

- `directive_ops` — mechanical operations on the directives list:
  - `append` — admit the new contribution as a directive. The engine builds
    the `Directive` row itself from the contribution's verbatim content, id,
    subject, and provenance fields; the judge never restates directive text.
  - `supersede(id)` / `retire(id)` — by directive id (ids are the
    originating contribution ids and already live in every `Directive`).
    Invalid ids are a parse error, handled by the existing re-ask (#113).
- `new_context` — the rewritten **context section only** (a single condensed
  string, per ADR 0004's summary shape). This remains generated: context is
  the compression surface, condensation is the judge's actual job there.

The engine applies the amendment mechanically to the stored summary and
bumps `version` as today. Verbatim preservation of directives stops being a
prompt obligation and becomes **structural**: existing directive rows are
bytes the judge cannot touch except by id-addressed op. The word budget and
the overflow re-ask (#63) now apply to `new_context` alone — the only part
still generated — and an overflow retry regenerates a few hundred tokens,
not the whole summary. Decline verdicts are unchanged (no amendment).

The operator-stratum obligations (ADR 0008 — never copy an operator
directive into the summary, decline contradictions) are unaffected: they
constrain what the judge *decides*, not how the summary is serialized.

### D2 — The recent window is built mechanically from stored judgment rows

The judge prompt's RECENT CONTRIBUTIONS block is replaced by a mechanical
digest built from what the record already stores: for each of the last N
entries, `(contribution id, subject, timestamp, decision, judgment
reasoning)` — the reasoning being the compressed restatement of the
contribution that the judge itself wrote when the entry was judged, once,
immutably. No new LLM output maintains this digest; nothing in it can
drift. Only the most recent 2–3 entries keep full verbatim text (the
"resubmitted moments later" duplicate case, where phrasing-level comparison
earns its cost). The window is additionally **token-capped**, not just
entry-capped: N entries or a fixed token budget, whichever is smaller, so
scopes with long contributions cannot re-inflate the prompt.

This deliberately rejects the alternative of a judge-*maintained* rolling
digest emitted each turn: that adds output cost to every judgment and makes
recency context LLM-rewritten state — a drift risk class the summary
already carries and nothing else needs to.

### D3 — Multi-contribution judgment: one call, per-contribution verdicts

The scope-manager gains a batch judgment mode: one call carrying several new
contributions **in arrival order**, returning an ordered per-contribution
verdict list plus a single cumulative amendment (D1 ops). Within the call
the judge processes contributions sequentially, each against the summary as
amended by its predecessors — semantically identical to serial judgment,
minus the repeated scope-side prompt and repeated summary output. One
declined contribution does not poison the batch; each verdict lands in the
record as its own judgment row against its own contribution id, so the
record's shape and the attempts machinery (#57/#118) are unchanged.

Callers that serialize same-scope judgments use this to **coalesce**: when a
judgment completes and releases the scope's queue, drain everything that
queued behind it (up to a batch cap, e.g. 5) into one call. No timers, no
event-model change — a lone contribution is judged exactly as today; the
batch only forms under contention, which is precisely when throughput is
scarce. Batch size is capped so the prompt stays bounded and one failure
never strands more than a cap's worth of contributions (each still gets its
attempt row on failure).

---

## Consequences

**Cost.** Per-accept on the observed mature scope: input ~13K → ~4.5K
(D2), output ~3K → ~400 (D1); at the 5:1 output premium, **~28K → ~6.5K
weighted (-77%)**, and per-accept cost stops growing with directive count.
Contention adds a further division by batch size (D3). Re-asks, when they
fire, are an order of magnitude smaller.

**Correctness.** Directive preservation becomes structural (the largest
class of summary-rewrite risk is deleted outright). The remaining generated
surface — `new_context` — is smaller and easier to eval. Known quality
trade: near-duplicate detection beyond the verbatim tail of the window now
compares against subjects and judgment reasoning rather than full text;
acceptable, and recoverable by raising the verbatim tail if evals say
otherwise.

**Compatibility.** The `submit_judgment` tool schema, prompt blocks, and
overflow/parse re-ask logic change (engine-internal). The record schema, the
summary store, `ScopeSummary`, perspectives, and both re-ask disciplines'
one-retry rule are unchanged. Batch mode is additive; single-contribution
judgment remains the default path.

**Eval obligations.** Before this ships: an eval family for amendment-op
judgment (directive append/supersede/retire correctness, context-rewrite
quality vs. the full-rewrite baseline), one for windowed recency checks
(duplicate and supersedes detection at the slimmed window), and one for
batch-vs-serial equivalence (same contributions, same order → same verdicts
and same final summary).
