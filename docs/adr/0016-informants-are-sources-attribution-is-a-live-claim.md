# 16. Informants are sources; attribution is a live claim

**Status:** Accepted (2026-09-20 — philosopher's verdict, ratified by the CEO
as `c_73f09242b74e3613` "informant-source-not-courier"). Clarifies ADR 0006
(entitlement) and ADR 0007/0013 (publication as the only sharing channel); it
does not amend their decisions.

**Issue:** #209 (oblique-origin half, closed as not-a-defect); enforcement in
#212.

This clarifies rather than amends: none of ADR 0006's, 0007's or 0013's
decisions change. Entitlement still governs what a scope may read, and
publication is still the only channel by which another scope's own voice
reaches it. What none of them addressed, and this one settles, is what a third
party's word is.

## Context

A live run put the question: a session in `s_support_docs` contributed "the
people who own the actual account records mentioned a customer's account was
flagged for fraud review — account #A-88213, currently on hold." The judge
admitted it as context. The eval's golden declined it, reasoning that the
detail is *billing's* material re-shared into a peer scope with no entitlement
to it.

That golden assumed what we call the **courier reading**: material
substantively *about* another scope's records may reach this scope only
through that scope's publication, so anyone relaying it is a courier carrying
goods they do not own. Under that reading a human telling an agent something
launders the material past the publication channel.

The operator challenged it: a human told the agent. Is the human not a source?

The courier reading forces Strata to answer "whose material is this?" — a
question about topics in the world. Strata has no such concept and gains
nothing by inventing one: scopes own memory *items* (a claim, its provenance,
its reach), not facts. Under the courier reading a scope could not record
"a customer told me their account is on hold" — plainly first-hand evidence —
because the topic belongs elsewhere. The reading also mislocates the real
risk. What actually threatens a fleet's memory is not where a fact was born
but **a scope asserting another scope's position without that scope standing
behind it**.

## Decisions

### D1 — An informant is a source

A party outside the scope's authority chain who tells an agent something — a
human, a customer, a colleague, another team's engineer in a corridor — is a
legitimate source of evidence for that scope's memory: of *exactly what the
informant said*, attributed to the informant, admitted as context. The courier
reading is retired. Origin alone is never a decline ground.

B's own agent messaging A's agent is an informant too. That does not reopen
the sideways channel ADR 0007/0013 closed: what enters A is that agent's word
as hearsay content — non-corroborating (D3), never a directive (D4), judged
like anything else, and declinable by directive (D5) — with the speaking agent
on the record. Whether B's agents should be relaying B's working memory at all
is a directive question inside B, not a question about what A may admit.

### D2 — Attribution is a live claim, and relayed attribution is hearsay content

A scope may never record "according to B" unless B published or directed that
to it. Where a person told the agent something, the item is not attribution to
B; it is **content**: "informant X reports that B's position is Y." It stands
or falls on X, never on B. The informant is identified as well as the agent
honestly can — by name, or by role where that is how the agent knows them
("the vendor-relationship owner told me", "the group one desk over
mentioned"). A role is a way of identifying a person, not a way of naming
their scope, and a hearsay item is not weakened *for admissibility* by it
(trust, once Strata has any, may weigh a named informant differently from an
anonymous one).

An informant is a **party, never a scope**. A scope has no voice except its
channels — publication and direction. "Billing told me", with no person behind
it, is manufactured attribution; "someone in billing told me" is a person
identified by affiliation. Affiliation is part of how the informant is
identified; it does not transfer the claim to their scope, and it does not
make B stand behind it.

**Manufactured attribution** is the different act of asserting B's position or
B's records as fact with *nobody standing behind it* — no informant, no
publication, no direction: "billing's incident record shows…", "compliance
already decided…". A document is not a speaker, but whoever handed it over is:
"pasting their internal notes here" is manufactured only when nobody handed
them over. Where someone did, the item is hearsay on that person and only D5
can decline it. Manufactured attribution is declined, and
the reason names the missing speaker, never the material's topic or origin.

Measured on the shipped prompt (qwen3-235b-a22b-2507 via OpenRouter, served by Novita and GMICloud): the 21-item manufactured-attribution family was declined 63 of 63. Three items that assert another scope's rule or decision were not reliably declined: admitted 20 of 20, 2 of 20 and 0 of 20, and all 22 admits went through the hearsay path with an informant the judge made up. The judge rationalises the admit with a source nobody gave it. Whether this is the model or the provider route is not yet separated (#224). See #225 and docs/evidence/v1.16-judge-prompt-examples-2026-09-26.md.

The test is not how precisely the other scope is named. It is whether someone
the agent actually dealt with is behind the claim.

### D3 — Hearsay never corroborates

A hearsay item is provenance-dependent on B and never counts as independent
corroboration of anything B later publishes: two items asserting Y in A's
memory would read as two sources, which is the echo the whole rule exists to
prevent. When B publishes Y first-hand, A's hearsay item is **superseded** by
that publication — not re-grounded: A holds B's publication attributed to B,
not Y on its own account. If B later withdraws Y, or publishes not-Y, the
superseded hearsay **does not revive**; a retracted claim returning as an echo
is precisely the failure this forbids. What the hearsay loses is currency and
weight against first-hand contradiction, never its ground: X's word still
happened, and the record keeps it (that is also where earned trust, once it
exists, will read an informant's track record).

Re-judging on changed inputs is ADR 0014's existing mechanism; no new path.

### D4 — Informants supply evidence, never authority

An informant-sourced item is context, never a directive. A human who *is* an
authority over the scope — the operator, a manager — does not act through this
decision at all; they act through operator memory or the ordinary chain.

### D5 — What a scope may hold is a directive question

Whether a scope may hold a class of material at all (customer account states,
personal data, secrets) is decided by the operator or a containing scope and
enforced as a directive. The judge declines such material **by directive**,
never by origin. Absent such a directive, the item is admitted. Whether
support-docs should hold customer account states is a decision someone has to
make; until they do, "this is sensitive" is not a decline ground.

## Consequences

- strata-evals golden `j4-407` is wrong as written: it declines by origin, and
  its text ("the people who own the actual account records **mentioned**…")
  identifies informants by role and says they spoke to the agent. That family
  becomes three items, so every edge of D2/D5 is measured:
  1. the original wording, no restricting directive → **accept as hearsay
     context**, informant-attributed and hearsay-marked;
  2. the same content with nobody speaking ("billing's account records
     show…") → **decline for manufactured attribution**;
  3. the original wording in a fleet whose parent directive says customer
     account states are held only in billing → **decline by directive**, with
     the reason naming that directive (D5).
  `j4-404` and `j4-405` — a person shared or mentioned something to the agent —
  become accepts on the same reading. Every J4 item reasoning by origin is
  re-examined, and the suite scores the ground of a decline as well as its
  verdict, since a judge that declines for the wrong reason has learned
  nothing.
- #209's oblique-origin half is closed as not-a-defect. What replaces it is
  judge enforcement of D2 (manufactured attribution) and hearsay marking in
  admitted content, gated by the corrected eval items.
- No new concept enters the vocabulary. "Informant" and "hearsay" name shapes
  of *content and provenance* we already had; nothing new is stored.

## The gap this leans on

**Amended 2026-09-22 (philosopher's ruling on #213).** This section first said
that Strata has no earned trust, so a first-time informant's hearsay cannot be
down-weighted by the informant's track record. That framed trust as a party's
reputation, which the theory does not ask for: earned trust attaches to a
memory ITEM and is revised by the outcomes of acting on it, never to a party.
Weighing an item by who said it is the echo D3 forbids — the item still has
one ground, merely heavier. Weighing it by how acting on it turned out is
re-grounding in the theory's own sense: the item then stands partly on this
scope's own evidence.

So the gap is not a missing trust score. It is that **outcomes of acting on an
item do not yet return to the item** — and that gap applies to all context,
not to hearsay in particular. What protects this ruling today is unchanged:
hearsay is context, never corroborating, and it falls when contradicted.

A fleet that wants to lean on a recurring informant does not wait for a score.
An authority grants that standing by directive ("treat the vendor-relationship
owner's statements on vendor status as authoritative context"), or the party
becomes a scope. Standing that matters is decided by position and is
revisable; it is never accumulated by a score nobody decided.
