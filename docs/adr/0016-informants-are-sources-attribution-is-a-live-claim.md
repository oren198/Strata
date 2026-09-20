# 16. Informants are sources; attribution is a live claim

**Status:** Accepted (2026-09-20 — philosopher's verdict, ratified by the CEO
as `c_73f09242b74e3613` "informant-source-not-courier"). Clarifies ADR 0006
(entitlement) and ADR 0007/0013 (publication as the only sharing channel); it
does not amend their decisions.

**Issue:** #209 (oblique-origin half, closed as not-a-defect).

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

### D2 — Attribution is a live claim, and relayed attribution is hearsay content

A scope may never record "according to B" unless B published or directed that
to it. Where a person told the agent something, the item is not attribution to
B; it is **content**: "informant X reports that B's position is Y." It stands
or falls on X, never on B. The informant is identified as well as the agent
honestly can — by name, or by role where that is how the agent knows them
("the vendor-relationship owner told me", "the group one desk over
mentioned"). A role is a way of identifying a person, not a way of naming
their scope, and a hearsay item is not weakened by it.

**Manufactured attribution** is the different act of asserting B's position or
B's records as fact with *nobody standing behind it* — no informant, no
publication, no direction: "billing's incident record shows…", "compliance
already decided…", "pasting their internal notes here". That is declined, and
the reason names the missing speaker, never the material's topic or origin.

The test is not how precisely the other scope is named. It is whether someone
the agent actually dealt with is behind the claim.

### D3 — Hearsay never corroborates

A hearsay item is provenance-dependent on B and never counts as independent
corroboration of anything B later publishes. If B publishes Y first-hand, the
scope re-grounds on that item and the hearsay is superseded by it. If B
publishes not-Y, or withdraws, the hearsay loses its ground when it is
re-judged (ADR 0014's reactive re-judgement is the mechanism; no new path).

### D4 — Informants supply evidence, never authority

An informant-sourced item is context, never a directive. A human who *is* an
authority over the scope — the operator, a manager — does not act through this
decision at all; they act through operator memory or the ordinary chain.

### D5 — What a scope may hold is a directive question

Whether a scope may hold a class of material at all (customer account states,
personal data, secrets) is decided by the operator or a containing scope and
enforced as a directive. The judge declines such material **by directive**,
never by origin. The `#A-88213` case keeps its answer — support-docs probably
should not hold customer account states — but for the right reason, and only
where someone has actually decided it.

## Consequences

- strata-evals golden `j4-407` is wrong as written: it declines by origin. Its
  one correct expectation is **decline for manufactured attribution** — it
  asserts the content of another scope's account records as fact while naming
  no one who told the agent. Items where a person did tell the agent
  (`j4-404`, `j4-405`) become **accept as hearsay context**, absent a
  directive restricting the material. Every J4 item reasoning by origin is
  re-examined, and the suite scores the ground of a decline, not only its
  verdict.
- #209's oblique-origin half is closed as not-a-defect. What replaces it is
  judge enforcement of D2 (manufactured attribution) and hearsay marking in
  admitted content, gated by the corrected eval items.
- No new concept enters the vocabulary. "Informant" and "hearsay" name shapes
  of *content and provenance* we already had; nothing new is stored.

## The gap this leans on

D1 admits a first-time informant's report on the same footing as a trusted
one: Strata has no earned trust, so hearsay cannot be down-weighted by the
informant's track record. The protection today is that hearsay is context,
never corroborating, and falls when contradicted. Earned trust is a roadmap
item, not a prerequisite for this ruling.
