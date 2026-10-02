# Changelog

This repository has not kept a changelog file before now; prior release
notes live in each release's PR description (`dev` → `main`) and its GitHub
Release body, per `docs/releasing.md`. This file starts with v1.17.0 and is
meant to be folded into that PR's own notes at release time, not to replace
them.

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
  - Three cases are declined **by design**, not promised fixed: a flat rule
    stated as fact with no contributor in it; a real telling event that
    names no addressee; and an overheard telling that names the scope
    (admissible in theory under ADR 0016 D2, declined here) — each decline
    reason tells the contributor what to say instead.
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
  correction with any overflow recorded, never silently dropped.
