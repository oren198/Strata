# Changelog

This repository had no changelog file before v1.17.0. It is now the source
the release PR (`dev` → `main`) and the GitHub Release body are built from
(`docs/releasing.md`, step 3) — write entries here as work lands, and the
"Unreleased" section is cleared once a release ships.

## Unreleased (v1.17.0)

### Behaviour change

- **Interior assertions naming a non-entitled scope now get one extra judge
  call, and can be declined where 1.16 admitted them (#225).** When an
  accepted contribution names a fleet scope the current scope is not
  entitled to, the engine now fires a second, targeted judge call that
  classifies the claim's ground (ADR 0016) and verifies the answer against
  the contribution's own text, rather than trusting the judge's optional
  field:
  - A **conduct** claim must be a dealing the contributor was actually part
    of — an attestation or perception frame ("I can tell you…", "I saw…") is
    stripped before the first-person check runs.
  - An **informant** claim must quote a telling event the contributor was
    actually in, as addressee or audience ("told us", "mentioned to me") —
    not just a named teller. A scope's own name offered as its own informant,
    with no telling event, is declined.
  - Three cases are declined **by design**, not promised fixed: an observed
    act written without the contributor in it; a real telling event that
    names no addressee; and an overheard telling that names the scope
    (admissible in theory under ADR 0016 D2, declined here) — each decline
    reason tells the contributor what to say instead.
  - A rule dressed with "our" passes the check and is left to the judge.
  - Overheard tellings that name a scope: the 1.16 judge already declined
    these on its first call (0/10 before and after), so this is unchanged.
  - Not covered by this change: the batch judgment path, and alias
    references to a scope (matching stays id/name only, word-bounded).

  See the README's J4 row and ADR 0016 D2's dated note (2026-10-02, #225) for
  the measured gate and the full rulings.

- **A scope's own corrected claim is now checked for paraphrased carriers in
  its own publication, not only verbatim ones (#219 C).** When a scope's
  outcome judgment or refresh finds one of its own claims wrong
  (`failed_corrected`), the engine already withdraws published items that
  still carry the claim verbatim (#221). This adds one further owner-judge
  call per correction, deciding `carries`/`does_not_carry` for the scope's
  current published face beyond what the verbatim sweep already caught —
  recorded either way (`claim_carrier_checks`), capped at 20 candidates per
  correction with any overflow recorded, never silently dropped. A
  mechanical observed-value veto can additionally keep an item the judge
  wrongly marked `carries` (recorded `kept_by_guard`) when it states what
  was actually observed rather than the refuted claim. Measured, this
  withdraws roughly 2–7% of a scope's neighbouring valid items alongside
  genuine carriers — a known limit, addressed by the restore act below.

- **A judged `restore` act undoes a published item a correction sweep wrongly
  withdrew, under its ORIGINAL id and bytes (companion to #219 C).** Only a
  sweep withdrawal — the verbatim P4 sweep, or #219 C's own owner-judge
  `carries` decision, at any hop of either one's relay cascade — can be
  restored this way; a deliberate withdrawal is re-published, as before.
  - **Owner path, judged** (`strata_restore`/`propose_restore`): the owning
    scope's own judge re-runs the structural test only — still believed by
    its CURRENT memory, and does not re-assert the refuted claim — given the
    refuted claim, the correcting content, and the item together. A decline
    leaves the item withdrawn.
  - **Operator path, in person** (`strata operator restore <scope> <item_id>`):
    unjudged, operator provenance — the escape when the owner's judge
    declines.
  - **Relays come back mechanically** unless the relaying scope's own judge
    has processed (drained) the correction notice for the item since the
    withdrawal — then that scope gets the correction as evidence only, and
    its own judge decides whether to relay again.
  - **A new `claim_restored` notice** reaches EXACTLY the (reader scope,
    item) pairs that got the original false notice, read from the recorded
    change events by change id and item id — never recomputed from today's
    topology, and never sent to a scope that started reading only after the
    withdrawal. Evidence only: nothing is inserted into a reader's memory on
    its behalf.
  - **Detection surface:** `strata record <scope> --swept` lists every
    correction withdrawal in a scope (verbatim, judged `carries`, or relay),
    with the refuted claim and correcting content side by side, how it was
    withdrawn, and a restore command to run.
  - Not a behaviour change for anything already shipped: unused unless a
    withdrawal is actually restored.
