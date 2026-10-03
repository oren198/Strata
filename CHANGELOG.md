# Changelog

This repository had no changelog file before v1.17.0. It is now the source
the release PR (`dev` → `main`) and the GitHub Release body are built from
(`docs/releasing.md`, step 3) — write entries here as work lands, and the
"Unreleased" section is cleared once a release ships.

## Unreleased (v1.17.0)

### Behaviour change

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
  was actually observed rather than the refuted claim. #219 C wrongly
  withdraws about 2–7% of a scope's neighbouring items (measured). Each one
  is listed in the Console's Correction withdrawals view and can be
  restored there by the operator. The owner's own judged restore recovered
  5 of 12 such items in our measurement; the operator path is the reliable
  remedy ([evidence](docs/evidence/v1.17-219c-restore-2026-10-03.md)).

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

- **"Discussed with X" now counts as a telling event (#225 widened).** "Discussed
  with X", "agreed with X", "decided with X", "met with X" and "in our sync with
  X", with the contributor present, are telling events in #225's informant and
  conduct checks (the philosopher's ruling, 2026-10-03). A bare "as discussed,
  …" names nobody and still fails. Hearsay from a joint verb is recorded at that
  verb's strength ("In discussion with X: …"), never as "X says".

- **A decline for "no one spoke" now gets one re-check, and can become context
  (attribution over-decline).** When an ordinary judgment declines a
  contribution for manufactured attribution, a second targeted judge call names
  the contribution's actual ground: a rendered directive or publication it
  quotes, a named outside party's publishing act, a telling event the
  contributor was in, or the contributor's own first-hand claim. The engine
  verifies that answer against the contribution's text. Only a verified ground
  reverses the decline, and only ever to context, never to a directive. Every
  other decline ground still stands, and any failure leaves the first decline
  unchanged. A rescue must ground every claim the contribution attributes to its
  source. Not covered: aliases ("the purchasing team"), the batch path, and a
  conduct observation that names another scope
  ([evidence](docs/evidence/v1.17-attribution-recheck-2026-10-03.md)).

### Added

- **Optional provider pinning for an OpenRouter judge, `JUDGE_PROVIDER` /
  `STRATA_JUDGE_PROVIDER` (#224).** OpenRouter routes the default judge
  across roughly ten backing providers; the same item on the same build
  has measured anywhere from 10/10 to 5/10 across runs, plausibly from the
  provider mix. Setting this pins every judge call to one named provider
  (e.g. `Alibaba`) via `extra_body={"provider": {"order": [...],
  "allow_fallbacks": false}}`, ignored entirely on a non-OpenRouter judge
  endpoint. Not a behaviour change while unset (the default): every judge
  call's request is byte-identical to today's. `strata doctor`'s judge line
  now shows whether a configured provider is actually pinned or ignored.

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
  - **Detection surface:** `strata record <scope> --swept` and the Console's
    new "Correction withdrawals" tab (under Publications) both list every
    correction withdrawal in a scope (verbatim, judged `carries`, or relay),
    with the refuted claim and correcting content side by side, how it was
    withdrawn, the reader count, and #219 C's own unresolved/overflow rows,
    flagged. The Console tab adds a Restore button (the operator path) and a
    "keep withdrawn" acknowledge.
  - Not a behaviour change for anything already shipped: unused unless a
    withdrawal is actually restored.
