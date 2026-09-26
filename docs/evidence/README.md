# Evidence

Measurements a public claim rests on live here, verbatim, before the claim is made.

The eval repository is not public, so a README or release note that links to it is a 404 for
every reader — the 1.11.0 notes hit exactly that. So a measurement is copied into this
directory, with local paths and key references scrubbed and nothing else changed, and the claim
links here.

| file | what it measures |
|---|---|
| `judge-baseline-2026-09-20.md` | six judge models on the same suites: junk admission, J1 accuracy, J4 attack success, error rate, re-asks, latency, cost — plus the #210 gate and the #209 required-field check per model |
| `m1-scope-purpose-gate-2026-09-20.md` | the scope-purpose relevance gate: the rehearsal office note across the three purpose paths, the 11-item workplace class, the 6 legitimate operational twins, and the J1 baseline diff |
| `v1.15-p1-adoption-2026-09-24.md` | outcome-loop adoption BEFORE item ids were shown: fresh Claude Code and Codex sessions with `acted_on` available, neutral prompts — 0 of 6 set it (the 3 Codex sessions had no working sandbox, #218) |
| `v1.15-p1b-adoption-2026-09-25.md` | the same protocol AFTER the perspective lists context item ids and the nudge names them (Claude Code only) — 6 of 6 set it; read with the P1 file as one before/after pair, caveats inside |
| `v1.15-outcome-loop-gate-2026-09-25.md` | the v1.15 outcome-loop gate. P3: the judge's four dispositions on acted_on reports (echo hard stop 10/10, strict per-item scores, the input-identity no-regression proof with J1/J4 as noise context). P4: correction fan-out (exactly-once per reader, owner notified cross-scope, supersession silent, negative controls). Plus the found-and-fixed list and the two open limits (#219, #220) |
| `v1.16-judge-prompt-examples-2026-09-26.md` | v1.16 items 1b/1c: the acted_on block's worked example leaking into reasons (fixed: fabricated quotes 3/70 → 0/70); the ADR 0016 conduct example and two failed wording fixes (reverted); findings: an unstable conduct/interior boundary, interior assertions admitted via an invented informant (#225), unpinned runs can't gate wording (#224) |

**Caveat for every unpinned before/after in v1.15–v1.16 (added 2026-09-26):** OpenRouter's
routing appears to follow the prompt itself. Two builds that differed in one prompt line, run
interleaved in the same session, were served by very different provider mixes. Any two prompts
compared without a pinned provider may therefore have been served by different providers (#224).

A file here is a record of what was run, not a summary of what we wish it showed: failures,
crashes and incidents stay in.
