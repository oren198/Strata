# 18. Human review levels; level 1, a person disposes of every directive change

**Status:** Accepted (2026-11-21: the operator's ruling on #126, checked twice by the philosopher, ratified by the CEO). Level 1 is built in v1.21.

**Issue:** #126.

**Basis.** Concept 3 says the scope-manager exercises the operator's authority as the operator's standing delegate, "and the operator may always exercise it in person". A review scope is the operator narrowing that delegation for one kind of act: changes to binding memory. This adds nothing new to the theory.

**When to use it.** The mode suits scopes where directives are rare and weighty. It does not suit busy scopes, because an operator who steers day to day has replaced the self-correcting system (Concept 3). A hold nobody gets to fails closed, but it also stalls the scope. So the operator always sees how many items are held and the age of the oldest (D4).

## Decisions

### D1. A review LEVEL, per scope
- fleet.yaml sets `human_review: <int>` on a scope. The default is `0`: off, and byte-identical to today, so the test suite and the regression checks show no change.
- `1` is level 1 (D5): every directive change goes through the human gate. It is the only level built.
- The field is an integer level, not a boolean or an enum of names, so later levels can be added with no migration of the setting. Any value other than 0 or 1 is refused when fleet.yaml is loaded ("level N is not defined"). Unknown levels never fall back to 0 or 1.
- Later levels are NOT designed here. The operator's principle: a human chooses what reaches them. What a later level gates is decided when one is asked for.
- **Levels stack.** A level is how much delegated authority the operator takes back to exercise in person (Concept 3); it is not a new concept. Each level holds everything the level below holds, plus more. A future need that does not stack that way gets its own name, not a number. At every level, the operator's own acts and lapses decided at creation are never held.
- Level 1 is a legitimate narrowing of the philosopher's 2026-10-03 position ruling (the operator taking delegated authority back), not an exception to it.
- "Review scope" below means a scope at level 1.

### D2. "Held" is a proposal state, not a verdict
In a review scope the judge still judges every contribution, and its decision is recorded **verbatim**, ops included.
- If the judgment would change the scope's directives, the item's *disposition* is `held, awaiting the operator`, and nothing is applied. That covers accept_as_directive, append, supersede, retire, and a judged end-condition confirmation.
- `held` is never written over the judge's decision. The verdict is the evidence the person decides on.
- A held item is not memory: it was never admitted. It enters only when the operator approves it.
- accept_as_context lands as it does today, and so does decline.

### D3. Held is neither accepted nor pending
- `is_accepted` is decided by the verdict, so a held item needs a rule of its own. `ContributionState.accepted` is False while the item is held. The state is `held`, alongside judged, judge_failed, pending and note.
- A held item is not pending judgment, because the judge has already spoken. It is not accepted either: it is not memory, because it was never admitted.

### D4. The operator disposes, and that is an act
The commands are `strata operator review list | approve <id> | reject <id> --reason`.
- `list` shows the count of held items and the age of the oldest. The Console shows the same, read-only, in the first version.
- **Approve is an operator act, recorded as one.**
  - The resulting directive is the **scope's own directive** at the scope's position. It is not operator memory and gets no operator exemptions.
  - Its provenance names the operator as the one who enacted it, with the judge's verdict as evidence.
  - The scope can later revise it like any of its directives, and in a review scope that revision is held too.
- **Re-check before apply.** If the scope's summary or its inherited directives changed after the judgment, the approval re-runs the engine's mechanical checks on the ops against the current state before applying them. Those are the checks the scope-manager already runs after a judgment: the inherited-conflict check, the existence of supersede/retire targets, and the position gate.
  - If a check fails, the approval is refused with the reason and nothing changes. The operator can reject the item, or the contributor can re-propose.
  - A scope's own directive may never contradict what it inherits, even when it is enacted in person (Concept 5).
- **Reject** records the reason. Nothing enters.
- There is no amend in v1. An amendment is a new contribution or an operator publish.

### D5. Level 1: what is held and what passes
- **Held: every directive change by a delegated party.**
  - The judge's directive admits, supersedes and retires.
  - The judged end-condition confirmation.
  - A bound session's own `strata_retire`. A supersede by a session goes through `strata_contribute`, so it is a judge op and is already covered above.
  - Context is not gated.
- **A held session act is the same held state.** It is a proposed change to binding memory by a delegated party, awaiting the authority's act. A judge's verdict is only one kind of evidence attached to such a proposal, so no separate definition is needed.
  - A bound session's `strata_retire` in a level-1 scope is recorded as a held retirement request: actor session, kind, notice and time. It changes nothing until the operator approves.
  - **The directive still binds while the request is held, including for the requester.** The reply says so: "retirement requested; held for the operator; <id> still binds".
  - **Notice goes out when the retirement is enacted, not when it is requested.** Readers get the ordinary retirement notice on approval, and the recorded kind picks release (*retired*) or retraction (*retracted*).
  - **The re-check confirms it is the SAME directive** (same id, still in the summary, not superseded). If the directive was superseded or retired while the request was held, the request lapses and is recorded as lapsed. Nothing is applied.
  - On approval, the record names the session as requester and the operator as enactor.
- **Passes directly:**
  - the operator's own acts: publish, supersede, retire, retire-item, and review approve/reject;
  - a date lapse. It is the authority's own act, decided at creation and only carried out by a mechanism.
- A held retirement leaves the directive binding, and a held addition leaves it out. The scope's memory never shows a change that nobody with authority made.

### D6. Visibility
The contributing scope sees the held item: its id, its content, and the label "held for the operator: proposes …; not binding". It later sees "approved → directive <id>" or "rejected: <reason>". The rejection reason reaches the scope as evidence.
- Descendants never see a held item, because they inherit directives only.
- Publications never see it either, because a held item is not memory: it was never admitted.
- A held item never appears as a directive. It must also not leak into the scope's condensed context prose as settled: it appears only as a labelled held proposal.

### D7. Bypass guards
- **Reclassification is one-way.** Anything the judge reads as binding is held, whatever it was proposed as. In a review scope the judge never admits a binding reading as context to avoid the hold.
- **Rule-shaped context is still declined, as today.** In a review scope that guard carries the load, because it is the only thing between a held directive and its paraphrase. The regression checks must show it holds at level 1.

### D8. Record
One migration adds:
- the disposition, `held` / `approved` / `rejected` / `lapsed`, next to the unchanged verdict;
- a held retirement request from a bound session (actor session, kind, notice, time), with the same dispositions;
- the operator's review act: actor, reason, time, and the resulting directive id.

Nothing is deleted. A rejected item stays in the record.

## Rulings
1. **Level 1 holds every directive change by a delegated party** (the operator, 2026-11-21). In a level-1 scope this narrows v1.20's rule that the owning scope decides a judged end condition: there, a person decides every directive change.
2. **Re-check before apply is mechanical only** (inherited conflict, target exists, position gate), with no judge call. If a check fails, the approval is refused with the reason and nothing enters. Approvals work while the judge is down.
3. **A level, not a boolean.** The setting is `human_review: 0|1`.
4. **The operator's own acts and date lapses pass directly, and context is not gated** (the operator, 2026-11-21).

## Known limits
- **Approving again after a crash.** The amendment writer records an intent (migration 0029) before the summary file is written, makes each after-write step idempotent per intent, and marks the intent done at the end. An approval first lets recovery finish any pending intent of the scope under the scope lock, then treats an item whose intent is done as applied: a second approve is recorded as approved, for an append, a publish, a supersede-only item and a retire-only item alike, and nothing is written twice. A death before the file write abandons the intent, so nothing was applied and the approve proceeds normally. Two paths are not covered by the journal: a held session retirement request is enacted through the retire act, not the amendment writer, so its crash window is the one the retirement-row check handles; and the diagnostic condensation-lineage and drop rows and the claim-correction paraphrase check are not replayed, because only the live writer has their inputs.
