# `strata judge-check`

`strata judge-check` measures the configured judge. It runs the bundled, frozen judge-check set (set v1) through the engine and compares the engine's decision on each item with that item's golden label. The comparison is string and field equality. No second model grades the run. The command is a measurement: it does not change the user's project, and every run uses a fresh temporary store.

the public set will be tuned against; a pass shows the model meets this set, not that it is good in general.

The reference bars shipped with this build were measured with `qwen/qwen3-235b-a22b-2507` on Parasail, five repetitions on the frozen set. A run reports whether the configured judge meets those stored bars. The verdict text is the engine's sentence, for example `meets the judge-check v1 bar for Strata <version>` or `does not meet the judge-check v1 bar for Strata <version>: <legs>`.

## What a run measures

- Per-suite strict accuracy against the stored bar (the reference accuracy, less that suite's margin).
- The security leg: judge-dependent must-decline items in the h2, drift, and j3 portions of the set. The engine reports engine-side defences separately, as `engine defences held: x/y`. That line is not part of the verdict.
- The must-admit leg and the p5 label leg.
- Operational counts: tool-call failures, judge errors after the engine's retries, timeouts, and dollars spent.
- Relation re-asks, printed as `relation re-asks: N calls, E errors; main-call errors: M`. Those counts are reported only. They have no bar and they do not change the verdict.

Reason honesty beyond the regex check is not measured in v1. The regex check is a stored reason that says "hearsay" on an item labelled first-hand. A wider reading, such as a reason that asserts authority the run could not establish, is not part of judge-check v1. The text report says so.

## Cost guard

Before the run, the command prints the engine's estimate (items, repetitions, calls per judgment, and the model's per-token price) and asks `y/N`. `--yes` skips the question. Any other answer does not start the run and exits 3.

`--max-usd X` is a hard cap enforced in the process during the run. When the next call would pass the cap, the run stops. The report's verdict is the engine's line `incomplete: cap reached at N/M`, and the command exits 2.

Prices come from the provider's models endpoint when it has one. On OpenRouter that price is the pinned provider's endpoint, or the dearest endpoint when the run is not pinned. `--price IN,OUT` (dollars per million tokens; an optional third field is the cache-read price) wins over the listing. When no price is known, the command prints the engine's `PriceUnknown` message verbatim and exits 3. A run does not start unpriced. A tampered set file is refused the same way.

## Exit codes

| Code | When |
|---|---|
| 0 | The verdict meets the judge-check v1 bar. |
| 1 | The verdict does not meet the bar. |
| 2 | The run is incomplete, or unmeasured (no reference bars in this build). |
| 3 | Refused before a measured verdict: no price, a tampered set, or a config error. Declining the y/N question is also exit 3. |

## Report

The JSON report is the engine score (`judgecheck_score_v1`, from `JudgeCheckScore.to_dict()`) plus a run header. The header carries the model, the provider, the base URL's host, the engine version, the set version and sha256, the date, the repetitions, and the dollars spent.

The header records the host only. It does not record an API key, the full base URL, a query string, or any other environment value.

Reports are written under the user state directory, `$XDG_STATE_HOME/strata/judge-check` or `~/.local/state/strata/judge-check`, keyed by engine version, model, provider, and host. They are not written under the working directory's `.strata/`. `--out PATH` also writes the same JSON there. `--compare REPORT` compares this run with that saved report using the strict flags already stored on each `per_item`. The saved report is not graded again. The exit code still follows the run just measured.

## `strata doctor`

`strata doctor` prints one extra line, from the report saved for this engine version and the configured judge:

- `Judge check: meets the v1 bar (report <date>)`
- `Judge check: does not meet (<legs>)`
- `Judge check: incomplete (report <date>); re-run`
- `Judge check: unmeasured for this engine version`
- `Judge check: report not understood (report <date>); re-run`
- `Judge check: could not read the report (<ExceptionClass>)`

The incomplete line is for a saved verdict that starts with `incomplete`, including `incomplete: cap reached at N/M` and `incomplete, re-run: all errors were 429s`. The unmeasured line is only when no report file exists for this engine version, or for the verdict `unmeasured: no reference bars in this build`. A verdict that is missing or not a string, and any other verdict string doctor does not recognise, is `Judge check: report not understood (report <date>); re-run`. When a line names the report date and that date is missing or not a single line, the date text is `date unknown`. A report file that exists but cannot be read or parsed is `Judge check: could not read the report (<ExceptionClass>)`: the exception class name only, not the exception message. A doctor message that contains a newline is an unrecognised report and uses the report-not-understood line. One state is not reworded into another.

The line does not call the judge, does not ask a question, and does not change `strata doctor`'s exit status.

## Known limits

- Reason honesty beyond the regex check is not measured in v1.
- a context note cannot retire a directive (its ops are dropped), so that case is not tested.
