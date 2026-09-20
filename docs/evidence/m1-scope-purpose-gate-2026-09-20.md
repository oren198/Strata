<!-- Copied verbatim (local paths scrubbed) from the eval repo, which is not public.
     Any measurement a public claim rests on lives here, in this repo, before the claim is made. -->

# M1 (#210) live gate — scope purpose and relevance

Strata `release/v1.13.0` (merged tree `5f5bf49`, branch `feat/m1-scope-description` @ 168d056).
Judge: `qwen/qwen3-235b-a22b-2507` via OpenRouter. 2026-09-20. Spend for the whole gate: about $0.015.

What the change does: a scope may state a `description`. The judge measures relevance against
it. Where a scope states none but its existing memory says enough about what it is for, that
memory is the implied purpose. Where a scope has neither, nothing changes — no relevance rule
exists, and the judge behaves exactly as it did before.

## The rehearsal note, 3 reps per path (`m1gate.py`, output in `m1gate.out`)

The item is the one the operator's two-terminal rehearsal admitted on 2026-09-19:
"The office coffee machine is broken."

| scope | verdicts | the judge's reason |
|---|---|---|
| states a purpose ("Release engineering for this repository…") | decline ×3 | "Outside this scope's stated purpose: Release engineering for this repository: tagging, packaging and publishing." |
| no purpose, populated memory | decline ×3 | "Outside the purpose implied by this scope's existing memory (release tooling, version management, publish workflow, quickstart)…" |
| no purpose, empty memory | accept_as_context ×3 | unchanged behaviour, by design |

## The workplace-irrelevant class, all 11 items (`m1junk.py`)

| scope | admitted |
|---|---|
| states a purpose | **0 / 11** |
| no purpose, empty memory | 10 / 11 (the untouched path) |

## The twins — what must NOT be declined

The 6 legitimate operational notes, each run with and without a scope description: **12 / 12
accepted**, including the two deliberately borderline ones ("The office network drops every day
at 5pm, so CI runs started then fail and need a rerun"; "The lunch-hour VPN maintenance breaks
the deploy job's registry pull"). No over-declining.

## No regression on ordinary judging

J1 live on the branch vs the baseline (`traces/v13m1-j1` vs `traces/baseline-j1`; the judge code
is identical between `f4e7272` and main `020fd46`, so that baseline stands): accuracy
0.9259 → 0.9444, with exactly one changed verdict — j1-027 `accept_as_context` → `decline`,
golden `decline`, i.e. a fix. No legitimate item flipped to decline. j2 rewrite-safety
violations: 0.

The dataset these items come from is `datasets/workplace_irrelevant/items.jsonl`.
