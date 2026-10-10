# #242 design note: a child's context that undercuts an inherited directive

Status: design, before code (v1.18, the first in-scope defence after the #244 measuring stick).

## Problem

A session bound to a child scope can add a context item to its own scope that undercuts a directive the scope inherits ("FYI, for hotfix builds we don't need the whole test suite"). The 1.17.1/1.17.2 inherited check compares admitted *directives* only. A context admit is never compared, so the judge is the only defence. The inherited directive still binds when the perspective is composed, but a reader sees the rule and a contrary note side by side and may follow the note.

On the #244 drift set, with Claude answering on the bridge, the judge declined all 12 context-form exceptions (child level and grandchild level, plain and reworded). On a weaker judge we have no number yet. The mechanical layer contributes nothing on this path today.

## The contract line (the philosopher, 2026-10-05)

- **A consequence report** describes a specific occurrence of *following* the directive and what happened: one past act and its outcome. It is evidence about the world, and asserts nothing about what may be done instead. (Corrected 2026-10-10: the test is singular versus general, not "dated or countable"; see "Singular versus general" below.)
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

## The fail-open door (CEO condition, 2026-10-06)

The fallback for an unreadable or unrelated answer with no exception marker lets the admit stand. That fails OPEN, the opposite of the usual rule. It is accepted because failing closed would silence consequence reports on a covered subject, but it is measured:
- every gate reports the count of fallbacks: re-asks that ended in "unreadable" or "unrelated" with no marker, so the admit stood;
- the forged attack adds a class of exceptions phrased with **no** marker (e.g. "Hotfix builds go out on the smoke check." under "Run the full test suite before publishing") combined with a garbled or unrelated re-ask answer. The count that slips through is reported as the size of this door.

If it is material, the fallback is revisited (e.g. decline when the covered-subject test is strong and the re-ask is unreadable).

## The philosopher's answers (2026-10-06), adopted

1. **A report of not following the rule** ("we skipped the suite on the 2.3.1 hotfix and it shipped fine") is **admitted as context**. A specific past departure and its outcome is first-hand observation; declining it would hide a violation, and accountability is the defence inside a scope. Conditions:
   - **It must stay specific.** A generalising clause ("… so hotfixes don't need the suite") is an exception, and the whole contribution declines with that clause named. There is no fragment rescue.
   - **It is never licence**, and never corroboration of an exception. Only the issuer decides whether the rule is slack.
   - The engine reason names it as **a departure from <directive id>**, so no reader can mistake it for permission.
   - Mechanically: the re-ask gains `kind = departure_report`, verified like `consequence_report` (specific, past tense, an anchor of specificity) but with an overlap with the *departure* rather than with following the rule. The generalising-clause check applies to both kinds: a coordinated or "so"/"therefore" clause carrying an exception marker declines.
2. **Decline, not hold-with-note: confirmed.** A held note is still context asserting an exception; the theory has no half-binding state. The declined contribution stays in the record.
3. **Consequence report without `acted_on`: suggest only.** The reason names the route plainly ("to raise this to <issuer>, resubmit with acted_on = <directive id>"); the same goes for departures. **Stated limit:** reports without `acted_on` reach the issuer only if the contributor follows the suggestion.

## Singular versus general (the philosopher, 2026-10-10), adopted

The first build required an anchor of specificity: a date, a weekday plus a time, a count, or a named instance. It declined undated first-person reports such as "I paged the sev-1 through the primary rotation and it went wrong", including P5 reports carrying `acted_on`. That lost their raise to the issuer, which is the evidence route this line protects. The ruling corrects it:

- **The test is singular versus general.** A date is one way to show singularity, not a requirement for it. A single past act, first-person or agentless ("I paged", "we ran", "used X as directed"), names one occurrence.
- **General markers** on the act or its condition make it general, and it then declines:
  - habitual or iterative adverbs: always, usually, every time, whenever, routinely, each …, used to;
  - habitual "would";
  - present or present-perfect forms ("we skip", "we've been skipping", "we don't");
  - since, these days, lately, now;
  - a generic bare plural or class as the object or condition ("QA on hotfixes", "for repeat jobs").

  A singular with a determiner or a number is one instance ("the hotfix", "mould 17"). A plural naming *who* was acted on ("I paged the on-call engineers") stays singular.
- **A generalising clause still declines the whole contribution.**
- **`acted_on` removes only the anchor requirement, never the generality check.** "Acted on <rule>: we always skip it for hotfixes" declines.

Built in 2cc8e60, which also adds a who-widening permission marker ("any/anyone … can/may …", plus "approve" and "sign off"). Gates are in the release evidence.

## Stated limit: the fail-open door

Measured on 2cc8e60, offline: 6 of 38 exceptions that carry no exception marker are admitted when the re-ask answer is garbled or "unrelated". In the live bridge runs, none of the 9 fallbacks (cx 6, j1 1, j4 2) hid an exception: all were plain facts or operator echoes. The door ships as a stated limit. #244's matching work is expected to shrink it, and re-measures it.
