"""``strata judge-check``: wire the engine's runner, price and score.

The command prints the engine's estimate, asks y/N (``--yes`` skips the question),
runs :func:`strata.judgecheck.run`, and renders :meth:`JudgeCheckScore.to_dict`
without scoring the run a second time. ``--compare`` reads strict flags already
stored on each report's ``per_item``. It does not call :func:`strata.judgecheck.compare`
and it does not grade the saved report again.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _decision_at(entry: dict[str, Any], rep: int) -> str | None:
    decisions = entry.get("decisions")
    if isinstance(decisions, list) and rep < len(decisions) and decisions[rep] is not None:
        return str(decisions[rep])
    return None


def _compare_stored_per_item(saved_per_item: object, candidate_per_item: object) -> dict[str, Any]:
    """Strict flips over common ``(item, rep)`` pairs.

    Incumbent strictness is ``per_item[id].strict[rep]`` from the saved report.
    Candidate strictness is this run's ``score(...).to_dict()["per_item"]``.
    Neither side is graded here.
    """
    if not isinstance(saved_per_item, dict):
        raise ValueError("compare report has no per_item")
    if not isinstance(candidate_per_item, dict):
        raise ValueError("this run has no per_item")
    flips: list[dict[str, Any]] = []
    suites: dict[str, dict[str, int]] = {}
    for item_id, inc in saved_per_item.items():
        cand = candidate_per_item.get(item_id)
        if not isinstance(inc, dict) or not isinstance(cand, dict):
            continue
        inc_strict = inc.get("strict")
        cand_strict = cand.get("strict")
        if not isinstance(inc_strict, list) or not isinstance(cand_strict, list):
            continue
        suite = inc.get("suite")
        if not isinstance(suite, str) or not suite:
            suite = cand.get("suite") if isinstance(cand.get("suite"), str) else ""
        for rep in range(min(len(inc_strict), len(cand_strict))):
            left = inc_strict[rep]
            right = cand_strict[rep]
            if not isinstance(left, bool) or not isinstance(right, bool):
                continue
            entry = suites.setdefault(
                suite, {"judgments": 0, "incumbent_strict": 0, "candidate_strict": 0}
            )
            entry["judgments"] += 1
            entry["incumbent_strict"] += int(left)
            entry["candidate_strict"] += int(right)
            if left != right:
                flips.append(
                    {
                        "item_id": str(item_id),
                        "rep": rep,
                        "suite": suite,
                        "incumbent_strict": left,
                        "candidate_strict": right,
                        "decisions": {
                            "incumbent": _decision_at(inc, rep),
                            "candidate": _decision_at(cand, rep),
                        },
                    }
                )
    flips.sort(key=lambda row: (row["suite"], row["item_id"], row["rep"]))
    return {"flips": flips, "suites": suites}


def _exit_for_verdict(verdict: str) -> int:
    if verdict.startswith("meets the judge-check v1 bar"):
        return 0
    if verdict.startswith("does not meet"):
        return 1
    return 2


def _refuse(message: str) -> int:
    print(message, file=sys.stderr)
    return 3


def _confirm(estimate_line: str, *, assume_yes: bool) -> bool:
    print(estimate_line)
    if assume_yes:
        return True
    try:
        answer = input("Proceed? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() in {"y", "yes"}:
        return True
    print("Not running.")
    return False


def cmd_judge_check(args: Any) -> int:
    """Run the frozen set with the configured judge and write the report."""
    import strata.judgecheck as judgecheck
    from strata import __version__
    from strata.judgecheck.items import SET_V1_SHA256
    from strata.judgecheck.report import build_report, render_text, run_header, write_report
    from strata.settings import Settings

    reps = args.reps
    if not isinstance(reps, int) or reps < 1:
        return _refuse("config error: --reps must be an integer >= 1")
    max_usd = args.max_usd
    if max_usd is not None and max_usd < 0:
        return _refuse("config error: --max-usd must not be negative")

    saved_compare: dict[str, Any] | None = None
    if args.compare:
        compare_path = Path(args.compare)
        if not compare_path.is_file():
            return _refuse(f"config error: compare report not found: {args.compare}")
        try:
            loaded = json.loads(compare_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return _refuse(f"config error: compare report is not JSON: {exc}")
        if not isinstance(loaded, dict):
            return _refuse("config error: compare report must be a JSON object")
        saved_compare = loaded

    out_path: Path | None = None
    if args.out:
        out_path = Path(args.out)
        if out_path.exists() and out_path.is_dir():
            return _refuse(f"config error: --out is a directory: {args.out}")

    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 — a settings failure is a config refusal
        return _refuse(f"config error: {exc}")

    try:
        price = judgecheck.resolve_price(
            args.price,
            settings.judge_base_url,
            settings.manager_model,
            provider=settings.judge_provider,
        )
    except judgecheck.PriceUnknown as exc:
        return _refuse(str(exc))
    except ValueError as exc:
        return _refuse(str(exc))

    try:
        items = judgecheck.load_set()
    except judgecheck.SetIntegrityError as exc:
        return _refuse(str(exc))

    estimate = judgecheck.estimate(
        [judgecheck.item_prompt_chars(item) for item in items], reps, price
    )
    if not _confirm(
        "estimate: " + json.dumps(estimate.to_dict(), sort_keys=True),
        assume_yes=bool(args.yes),
    ):
        return 3

    try:
        spec = judgecheck.judge_spec_from_settings(settings)
    except Exception as exc:  # noqa: BLE001 — the judge client could not be built
        return _refuse(f"config error: {exc}")

    outcome = judgecheck.run(
        items,
        reps=reps,
        judges=[spec],
        price=price,
        max_usd=max_usd,
    )
    by_id = {item.id: item for item in items}
    scored = judgecheck.score(
        outcome.results[spec.label],
        by_id,
        reps=reps,
        strata_version=__version__,
        bars=judgecheck.BARS_V1,
        incomplete=outcome.incomplete,
        judge_label=spec.label,
    )
    described = judgecheck.describe_judge(settings)
    header = run_header(
        described,
        engine_version=__version__,
        set_sha256=SET_V1_SHA256,
        reps=reps,
        spent_usd=outcome.spent_usd,
    )
    report = build_report(scored.to_dict(), header)
    text = render_text(report)
    write_report(report, text, out=out_path)
    sys.stdout.write(text)

    if saved_compare is not None:
        try:
            diff = _compare_stored_per_item(
                saved_compare.get("per_item"),
                scored.to_dict()["per_item"],
            )
        except Exception as exc:  # noqa: BLE001 — a bad compare file is a config refusal
            return _refuse(f"config error: compare failed: {exc}")
        print(json.dumps(diff, indent=2, sort_keys=True))

    return _exit_for_verdict(scored.verdict)
