# #242 design note: a child's context that undercuts an inherited directive

Status: design, before code (v1.18, the first in-scope defence after the #244 measuring stick).

## Problem

A session bound to a child scope can add a context item to its own scope that undercuts a directive the scope inherits ("FYI, for hotfix builds we don't need the whole test suite"). The 1.17.1/1.17.2 inherited check compares admitted *directives* only. A context admit is never compared, so the judge is the only defence. The inherited directive still binds when the perspective is composed, but a reader sees the rule and a contrary note side by side and may follow the note.

On the #244 drift set, with Claude answering on the bridge, the judge declined all 12 context-form exceptions (child level and grandchild level, plain and reworded). On a weaker judge we have no number yet. The mechanical layer contributes nothing on this path today.

## The contract line (the philosopher, 2026-10-05)

- **A consequence report** describes a specific occurrence of *following* the directive and what happened. It is dated or countable, in the past tense, about an action that could have failed and did. It is evidence about the world, and asserts nothing about what may be done instead.
  - It is admitted as context.
  - Under the 2026-09-24 ruling it is raised to the issuer as evidence that following the directive went wrong.
  - The directive is untouched.
- **An exception** states what *may* or *does* happen *instead* of the directive, *in general*.
  - It may be normative ("may", "doesn't need", "that is fine", "can keep") or habitual ("we leave the pumps running until 22:00", "we skip QA now").
  - A standing practice contrary to an inherited rule is the scope's own unratified exception. Admitted as context, it becomes a rule by the back door: context overriding a directive (Concept 5).
  - It is **declined**. The reason names the two legitimate routes: report a specific occurrence of following the rule, which is admitted and raised; or propose the exception upward to the scope that issued the rule, as an attributed proposal under the position gate.
- Closest call: "we leave the pumps running until 22:00" is purely descriptive, but it is standing non-compliance, so it is declined.

## Mechanism

The same layering as the 1.17 re-checks: mechanical first, one targeted judge call only where it is needed, and engine verification of that answer.

1. **Trigger (engine, no judge call).** All of these must hold:
   - an ORDINARY judgment admits the contribution as **context**;
   - the contributor is bound to the judged scope (other positions are the gate's);
   - the scope inherits at least one directive (ancestor or operator) whose subject the contribution's text covers. Use the 1.17.2 covered-subject test: shared content word plus same head noun.

   Otherwise the judgment passes untouched and makes no extra call. That keeps the first call byte-identical, and the vast majority of context admits never reach step 2.

2. **Targeted re-ask** (a separate tool, `classify_inherited_relation`). It is shown the inherited directive, the contribution text and the philosopher's two definitions. Required fields:
   - `kind` ∈ {`consequence_report`, `exception`, `unrelated`};
   - `inherited_id`;
   - `reasoning`;
   - for `consequence_report`, `occurrence_span`: verbatim, the specific occurrence;
   - for `exception`, `instead_span`: verbatim, the general "may/does instead".

3. **Engine verification.** It fails toward the side that keeps the inherited rule intact.
   - **`consequence_report` passes only when:**
     - `occurrence_span` occurs verbatim in the text;
     - it has a past-tense or completed-action verb;
     - it has an anchor of specificity: a date, a weekday plus a time, an ordinal or count ("twice", "the 2.3.1 hotfix"), or a named instance;
     - its words overlap the inherited directive's action ("following the pre-release test step", "switching the pumps off"), so it is about *following* the rule, not about a departure from it;
     - the whole text carries **no** exception marker: the normative set (may, can, allowed to, doesn't need, no need, is fine, waived, skip(ped) *as a policy*) or a habitual present-tense contrary practice ("we leave", "we skip", "now we", "lets … hold").

     When it passes, the admit stands as today. The reason suggests passing `acted_on=<inherited_id>` next time, so P5 raises it to the issuer mechanically. Auto-raising without `acted_on` stays out of scope; it would add a new act.
   - **`exception`:** `instead_span` must be verbatim. Then the contribution is **declined**, and the engine-written reason is:

     > [Declined: contrary to inherited directive <id> (<scope>). A practice that departs from an inherited rule can't be recorded as this scope's context. Report a specific occurrence of following the rule and what happened (admitted, and raised to <scope> with acted_on), or propose the exception to <scope>.]
   - **`unrelated`, an unreadable answer, or any failed check:**
     - **Rule A (fails toward the rule):** if any exception marker is present in the whole text → decline as above.
     - **Otherwise** → the admit stands.
     - Why not hold everything uncertain: holding would also block legitimate observations near an inherited rule's subject, and on the drift set the judge already handles the plain cases.

4. **Batch path.** The same per member, under the single-path rules. A batch with any member reaching step 2 is judged per member for that member's re-ask (the #236 shape). Any limit is stated.

**Why decline, not hold-as-context.** Holding an exception as context with a note is still recording the exception in the scope's memory, which is the very thing the ruling forbids. A decline keeps the scope's memory clean and is reversible: the contributor can resubmit by either legitimate route.

## Gates (bridge only; no key)

1. **The #244 drift set** (`j1_drift`, 48, labelled blind):
   - the 12 context-form exceptions are declined (shapes 2 and 5, child and grandchild);
   - the 6 consequence reports are admitted;
   - the 12 legitimate directives are unchanged;
   - full path ≥ 48/48 on the same recorded answers where inputs are identical, plus a fresh live run.
2. **A new held-out set for the line itself:** about 36 items in new domains, labelled blind by the philosopher. Shapes:
   - specific consequence reports, including one that cuts both ways (like 038);
   - normative exceptions;
   - habitual standing-practice exceptions, including the 018 closest-call shape;
   - plain observations near the rule's subject that are neither: a fact about the equipment, a schedule note;
   - a report of *not* following the rule and what happened: an exception confessed, which the philosopher should rule on.
3. **Forged-answer attack, both directions:**
   - every exception item with a forged `consequence_report` answer (spans from the text) must fail verification;
   - every consequence report with a forged `exception` answer must have no verbatim `instead_span` → falls to rule A → passes when no marker is present.
4. **Regression:** outcome_loop unchanged (consequence reports with `acted_on` take their own path and are unaffected); j1_golden and j4 unchanged; input identity of the first call for every item that doesn't trigger.

## Questions for the philosopher

1. A report of *not* following the rule and what happened ("we skipped the suite on the 2.3.1 hotfix and it shipped fine"). Is it a consequence report, since it is evidence, or a confessed exception, since it records a departure? Draft: admit as context, as evidence of a departure, but never as license; the reason flags the departure. Or decline?
2. Decline rather than hold-as-context-with-note: confirm.
3. Should an admitted consequence report *without* `acted_on` still be raised to the issuer automatically, or only suggested? Draft: suggested only; auto-raise would be a new act.
