"""``strata judge-check``: wire the engine's runner, price, score and compare.

The command prints the engine's estimate, asks y/N (``--yes`` skips the question),
runs :func:`strata.judgecheck.run`, and renders :meth:`JudgeCheckScore.to_dict`
without scoring the run a second time. ``--compare`` is handed to
:func:`strata.judgecheck.compare`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from strata.judgecheck.scoring import ItemResult


def _results_from_stored_per_item(report: dict[str, Any]) -> list[ItemResult]:
    """Item results that carry the saved report's decisions, for the engine's compare.

    Decisions come from ``per_item`` in the saved ``to_dict``. This does not call
    ``score``.
    """
    per_item = report.get("per_item")
    if not isinstance(per_item, dict):
        return []
    label = str(report.get("judge_label") or "incumbent")
    rows: list[ItemResult] = []
    for item_id, entry in per_item.items():
        if not isinstance(entry, dict):
            continue
        decisions = entry.get("decisions") or []
        suite = str(entry.get("suite") or "")
        if not isinstance(decisions, list):
            continue
        for rep, decision in enumerate(decisions):
            rows.append(
                ItemResult(
                    item_id=str(item_id),
                    suite=suite,
                    rep=rep,
                    judge_label=label,
                    decision=str(decision),
                )
            )
    return rows


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
            diff = judgecheck.compare(
                _results_from_stored_per_item(saved_compare),
                outcome.results[spec.label],
                by_id,
            )
        except Exception as exc:  # noqa: BLE001 — a bad compare file is a config refusal
            return _refuse(f"config error: compare failed: {exc}")
        print(json.dumps(diff, indent=2, sort_keys=True))

    return _exit_for_verdict(scored.verdict)
