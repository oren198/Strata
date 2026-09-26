# Spike: a paraphrased published claim stays up after its owner rules it wrong (#219)

**Status:** paper spike for v1.16. **No build.** It is written for the v1.17 planning decision.

## The gap

When a scope's judge rules one of its claims wrong (`failed_corrected`), the engine withdraws that
scope's own published items that carry the claim **verbatim**, and sends `claim_corrected` to their
readers (P4, decision A). Since v1.16 (#221), relays that carry it verbatim are withdrawn as well.

A published item that **paraphrases** the claim is not caught. The judge is asked to withdraw
non-verbatim carriers itself, but it measured 0/3 in v1.15 (ol-017). This judge does not fill
optional fields (`withdraw_published`), the same pattern seen in P3 and P5.

v1.16 measurements make this more likely, not less: when this judge condenses, it paraphrases
routinely (P6, #227). A published face written from paraphrased context will rarely match verbatim.

## Options

**A. Record provenance at publish time (engine).** When a publish op is judged and accepted, the
engine records which context contributions the published text came from. A correction then finds
carriers by id, not by content.
- The source of the link is the hard part. `context_sources` exists on the judge tool (ADR 0014 D3),
  but it is an optional field, which this judge omits.
- A mechanical fallback is possible: link every accepted context contribution whose content
  overlaps the published text above a similarity threshold (the same heuristic #227 proposes for
  display). This is admissibility-adjacent, because it decides what gets withdrawn, so a false link
  would withdraw a correct publication.
- **Cost:** a migration (`publication_sources`), the link computed at publish time, and the
  correction path reads it.
- **Risk:** false withdrawals at any threshold low enough to catch paraphrase.

**B. Notify without withdrawing (engine).** On `failed_corrected`, the engine sends a
`claim_corrected` notice to the readers of every published item of the holding scope whose text
is similar to the corrected claim above threshold *t*. It does not withdraw anything, so a false
match costs the reader's judge one look, not a wrong withdrawal. The owner's own publication stays
up until its judge or operator acts.
- **Cost:** no migration, only a similarity function at the correction site. There is an honest
  asymmetry: a false positive costs attention, a false negative leaves readers acting on a
  falsehood, which is the same trade the c′ ruling made for "default to corrected".
- **Risk:** the published face still carries the wrong claim, and only the readers are warned.

**C. Ask the judge in its decision, not in an optional field.** On `failed_corrected`, a second
judge call is made to the holding scope's judge: its whole decision is "which of these published
items carry the corrected claim?", enumerated as the decision itself. That is lesson 1: the verdict
goes in `decision`.
- **Cost:** one extra judge call per correction on a scope that publishes. The input change is
  gated on `failed_corrected`, so identity holds elsewhere.
- **Risk:** measurement is needed, and this is a judge change on an unpinned key.

## Recommendation

**B first, C as the measured follow-up.**
- B is small, fails toward over-notifying (the direction c′ already chose), withdraws nothing wrongly,
  and needs no new judge input.
- C closes the owner's side properly but needs a live gate, which should wait for a pinned key
  (#224).
- A's automatic withdrawal on similarity is the riskiest option, and it is not recommended.

**Gate for B:** an eval class with paraphrased published carriers (from ol-017) and near-miss
non-carriers that are similar in topic but a different claim. Pass = carriers are notified and
non-carriers are not, with *t* chosen on a fixture set and pinned.
