# 17. Outcomes return to the item acted on

**Status:** Accepted (2026-09-22 — philosopher's ruling on #213, ratified by the
CEO). Amends ADR 0016's gap section. Writing only: this records the contract;
the code is scoped from here in a later cycle.

**Issue:** #213 (retitled from "earned trust" to the outcome loop).

## Context

ADR 0016 admitted an informant's word as hearsay content and closed by naming a
gap: a first-time informant's report could not be weighed differently from a
known one, for want of "earned trust". That framing was wrong, and this ADR
records why, because the wrong shape was nearly built.

**Earned trust attaches to a memory item, not to a party.** philosophy.md
Concept 6, read as written, says that acting on a memory which leads to good
outcomes should raise *its* standing. The subject is the item; the driver is
the outcome of acting on it. One word had been covering three different things:

1. the standing of an item, revised by outcomes — the only thing that is
   *earned*;
2. the accountability of a source — "if a source proves unreliable, its
   contributions can be identified and removed wholesale": an explicit act of
   authority, informed by provenance, not a running score;
3. weight from authority — a claim from an authoritative source weighs more:
   that is **position** (Concept 3), never earned.

Party reputation is a fourth thing the theory never asks for, and it fails two
tests. It reintroduces the echo ADR 0016 D3 forbids: an item weighed by who
said it still has exactly one ground, merely heavier. And it fails
domain-generality — a registry of trust scores for parties outside the fleet
is, in a call centre, a register of customer reputations held in fleet memory,
which is a class-of-material question a directive already answers (0016 D5).

Weighing an item by how acting on it turned out does neither. It is
**re-grounding** in the theory's own sense (Concept 8): once a scope has acted
on a claim and the outcome has returned, the item stands partly on that scope's
own evidence and no longer only on the word that carried it in.

## Decisions

### D1 — Earned trust is the standing of an item, revised by outcomes

Define it that way and nowhere else: *the standing of a memory item, raised or
lowered by the outcomes of acting on it, bearing on how the item is weighed in
corroboration, supersession and decay.* It is a property of the item. It is
never a property of a party, and no identity for parties outside the fleet is
introduced — an informant stays what ADR 0016 made them: identified in content,
as well as the agent honestly can.

### D2 — An outcome is an ordinary contribution that names the item it bears on

Outcomes enter memory the only way anything does. An agent that acted on an
item reports what happened, and the report **references the item acted on**.
Nothing new is stored beyond that reference. The report is judged like any
other contribution: it can be declined, it carries provenance, it lives in the
record.

### D3 — The judge reads an outcome as corroboration or as correction, and the two are not the same act

A report that the item held reads as **corroboration**, and it is
provenance-independent: independence is a property of the evidence, not of who
first said the words, so a scope may corroborate its own earlier claim by
acting on it. **An outcome corroborates only if the action could have failed**:
"acted on it and it held" is evidence; "reviewed it and confirmed it" is echo
wearing an outcome's clothes. Provenance holds a reporter accountable that
claims otherwise, and ratification upward still reads breadth through the
ancestor's judgment, so a scope's repeated self-outcomes raise its own item's
standing without becoming the fleet's consensus.

A report that the item failed is **not automatically a supersession**. The
judge decides which happened, because the two acts differ in what they owe:

- **Correction** — the claim was wrong. Correction concerns truth, and it owes
  notice to everyone the claim reached.
- **Supersession** — the claim was right and the world moved on. Supersession
  concerns currency, and owes nobody notice.

Treating correction as a species of decay gets the removal right and the notice
wrong. Where the item had been **published**, published-within-believed means
the publication follows the item, and the withdrawal reaches its readers as
evidence, never as silent absence.

### D4 — What standing feeds

Three things, and nothing else:

1. decay — a poorly standing context item fades sooner;
2. the corroboration weight ratification reads;
3. the weight judgment gives a standing item when a contribution contradicts
   it — **not** the precedence between accepted items, which stays recency
   within context and authority for directives.

Standing bears at judgment, on whether a contradicting contribution displaces
the standing item at all. Once both are accepted, precedence is settled by the
ordinary rules.

### D5 — Standing never touches admissibility

Admission is ground, directives and judgment (ADR 0016; CONTEXT.md, *Ground*).
A well-standing item never makes a groundless one admissible; a badly-standing
one never makes a grounded contribution inadmissible. Removing an unreliable
source's material is the separate, explicit act of authority named above — it
is performed and recorded, not accumulated.

### D6 — Standing never crosses kinds: a directive is never weighed down

Context never overrides a directive (Concept 5), and erosion by accumulation is
still overriding — slowly. So outcomes never lower a directive's standing at
any level. **Outcomes that contradict a directive are evidence to the issuing
authority**, which may revise it through the ordinary channel: a scope-manager
revises its own directive on that evidence; the operator revises in person.
Nothing binding erodes quietly. Only context fades, and fading owes nobody
notice, where a correction does.

## Consequences

- ADR 0016's gap section is amended: the gap is not a missing trust score, it
  is that outcomes of acting on an item do not yet return to the item — and
  that is true of **all context**, not of hearsay in particular.
- A fleet that wants to lean on a recurring informant does not wait for a
  score. An authority grants that standing by directive ("treat the
  vendor-relationship owner's statements on vendor status as authoritative
  context"), or the party becomes a scope. Standing that matters is decided by
  position and is revisable; it is never accumulated by a score nobody decided.
- No new concept enters the vocabulary. "Outcome report" is prose — a genus
  over two things the theory already has, corroboration and correction, plus
  the reference. It passes only as a description. The moment it becomes a
  stored type, a field, or a classification the judge assigns, it has become a
  concept nobody derived; the judge's named outputs stay corroboration and
  correction.
- The code — how a report references its item, how corroboration reaches
  ratification, how standing enters decay — is scoped from this ADR in a later
  cycle, with its own eval gate.
