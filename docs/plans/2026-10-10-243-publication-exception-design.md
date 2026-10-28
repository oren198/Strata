# #243 design note: a publication that carries an exception down

Status: design, before code (v1.18, the third in-scope defence, after #242).

## Problem

v1.17.1 keeps a child's own directive from changing an inherited rule. #242 keeps a child's own context from doing it. A scope can still carry an exception *outward*: it publishes an item, and the item reaches its chain children (as the parent publication) and every scope that references it. The drift eval moves an exception down exactly this way, and the grandchild's adoption is now caught (1.17.2 for directives, #242 for context). The publication itself still stands. It is composed beside the inherited rule, non-binding but readable, in every reader's perspective. A reader agent can follow it, and a reader's judge has to keep declining it.

Facts from the code (release/v1.18.0):
- **Ancestor directives never reach the publication judge.** `judge_publication` is given the scope's own summary, its current publication, and the operator memory binding the scope (ADR 0008 D3, #90). The ancestor directives binding the publisher are not rendered to it, so it cannot see that an item contradicts one.
- **Anchors may name an inherited directive.** A publish may anchor to any directive binding the scope, ancestors included (ADR 0015 D3). An item can therefore cite the parent's rule as its anchor while wording an exception to it.
- **Legacy memory can still hold an exception.** Exceptions admitted before 1.17.1 and #242 are still in some scopes' memory, and published ⊆ believed lets them be published.
- **Restore re-publishes an item byte-identical** under its original id. Inherited directives may have changed since the item was first published.

## The line

The same line as 1.17.1 and #242, applied to the outward face. **A scope may not publish what it may not hold.**
- A published **directive** that loosens, exempts from, softens or calls outdated an inherited directive is refused. This is 1.17's inherited check.
- A published **context** item that states what may or does happen instead of an inherited rule is refused. This is #242's exception line: singular versus general, generalising clauses, and who-widening.
- A consequence report may be published. Evidence travels, and readers may need it.
- A departure report may be published **only with the engine's label** (the philosopher, 2026-10-10, option 2). Its text is prefixed "Departure from <directive id> (<issuing scope>): …", and the publisher can't drop the label.
  - Unlabelled publication (option 1) fails "published within believed". The scope holds the item *as* a departure, so publishing it bare would publish a precedent the scope never admitted. That is the laundering "attribution survives condensation" forbids.
  - Refusing it (option 3) fails "evidence travels". An outcome that contradicts a directive is evidence to its issuer.
  - The #242 singular-versus-general test runs before publishing. Only a singular past act publishes; a general or habitual clause refuses the whole item.

## Mechanism

The layering is the same as #242. Mechanical first, the judge only where needed, the engine verifies.

1. **Trigger.** An `accept` from `judge_publication` on `publish` or `restore`, by a scope that inherits at least one directive (ancestor or operator). Withdrawals are never checked: removing an item can't carry an exception down.
2. **Directive items.** `inherited_conflict(item_text, ancestor_text)` against every inherited directive, with the 1.17.2 comparator and covered-subject rules. A conflict refuses the publish. No judge call.
3. **Context items.** The #242 re-ask (`classify_inherited_relation`) and `verify_inherited_relation`, unchanged, on the item's text against the inherited directives it covers. Verdicts map onto the publish decision:
   - `decline` or `decline_unspecific` → refuse;
   - `consequence_report` → accept;
   - `departure_report` → accept, with the engine's departure label prefixed to the published text;
   - the fallback → accept, counted.
4. **The refusal** is recorded as the publication judgment's outcome, with an engine note:
   > [Refused: contrary to inherited directive <id> (<scope>). A scope can't publish an exception to a rule it inherits. Publish a specific report instead, or propose the exception to <scope>.]

   The artifact is untouched, exactly as for a decline today. The publish act and the judge's own verdict stay in the record, so the record shows the judge accepted and the engine refused.
5. **Relays** (the philosopher, 2026-10-10). A relayed item (`relay_origin_scope_id`) keeps the origin's departure label verbatim, naming the origin scope.
   - It is also checked against the RELAYER's inherited directives, but with the exception test only. A singular relayed report passes; a generalised one is refused.
   - It is never re-labelled "Departure from <relayer's directive>": the relayer departed from nothing, so that label would be false.
   - When the reported act conflicts with a directive that binds the relayer, the engine adds a separate note: "The reported act conflicts with <id>, which binds <relayer scope>."
   - When the origin withdraws, the relay's label and claim go with it, because attribution is a live claim.
6. **Bootstrap publication** (ADR 0007 D4, the one-shot initial distill) runs the same check per proposed item. A refused item is dropped from the initial set, with the note.

**Why refuse rather than publish with a note.** It's the same reason as #242's decline: a published exception with a note is still an exception on the scope's face. A refusal leaves the face clean, and the act stays in the record.

**What this does not do.** It never edits or withdraws an already-published item. Existing exceptions on a scope's face are left for D3's sweep and the scope's own agents. A one-off audit command that lists published items failing the check, with no change made, is cheap and could be added. That is the operator's call.

## Gates (offline where no judge is needed; otherwise targeted live runs on the pinned judge)

1. **#244's publication chains.**
   - The drift set's "child publishes the exception" items must be refused at the publish step. This needs a publish-path harness: the drift items are contribution-shaped today, so add a `publish` act form with the publisher's ancestors.
   - Legitimate tightenings and reports must publish.
2. **A forged-answer attack on the context path**, both directions, reusing the #242 harness on publish-shaped inputs.
3. **Regression.** These suites must be unchanged:
   - P1 publication fidelity;
   - P4 echo;
   - the J-suite publication items;
   - the first call's input identity for every publish that doesn't trigger.
4. **Fallback count and the door, reported.** The door is the same 6/38 class as #242, and #244's matching work shrinks both.
5. **Departure label and relays, offline.**
   - A departure report publishes with the label, and the label survives every edit path.
   - A generalised departure is refused.
   - A relayed departure keeps the origin's label and is never re-labelled.
   - The relayer's conflict note appears only when a relayer directive conflicts.
   - When the origin withdraws, the relay goes too.

## Cost

One extra judge call per accepted **context** publish in a scope with inherited directives. Publishing is rarer than contributing by orders of magnitude, so the cost is negligible beside #242's +12%. Directive publishes cost no call.
