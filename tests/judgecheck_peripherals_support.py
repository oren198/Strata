"""Shared fakes for the judge-check CLI and doctor tests.

The runner is replaced with an in-process result builder. No test in this lane
calls a live judge.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from strata.judgecheck import RunOutcome
from tests.judgecheck_support import golden_result


class RunBox:
    """Stand-in for ``strata.judgecheck.run``. Records the call and returns goldens."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.flip_ids: set[str] = set()
        self.tool_error_ids: set[str] = set()
        self.hearsay_ids: set[str] = set()
        self.incomplete: Any = None
        self.relation_calls = 0
        self.relation_errors = 0
        self.main_call_errors = 0
        self.spent_usd = 1.0

    def __call__(
        self,
        items: Any,
        *,
        reps: int,
        judges: Any,
        price: Any,
        max_usd: float | None = None,
        seed: int = 0,
        on_progress: Any = None,
    ) -> RunOutcome:
        rows_in = list(items)
        self.calls.append(
            {
                "reps": reps,
                "max_usd": max_usd,
                "n_items": len(rows_in),
                "price_source": price.source,
                "input_per_m": price.input_per_m,
                "output_per_m": price.output_per_m,
                "labels": [spec.label for spec in judges],
                "providers": [spec.provider for spec in judges],
                "models": [spec.model for spec in judges],
            }
        )
        results: dict[str, list[Any]] = {}
        for spec in judges:
            built = []
            for item in rows_in:
                for rep in range(reps):
                    row = golden_result(item, rep=rep, label=spec.label)
                    row = replace(
                        row,
                        calls=1 + self.relation_calls,
                        relation_calls=self.relation_calls,
                        relation_errors=self.relation_errors,
                        main_call_errors=self.main_call_errors,
                    )
                    if item.id in self.flip_ids:
                        row = replace(row, decision="accept")
                    if item.id in self.tool_error_ids:
                        row = replace(row, decision="tool_error")
                    if item.id in self.hearsay_ids:
                        row = replace(row, reasoning="Hearsay: labelled first-hand")
                    built.append(row)
            results[spec.label] = built
        return RunOutcome(
            results=results,
            spent_usd=self.spent_usd,
            calls=sum(row.calls for grouped in results.values() for row in grouped),
            reps=reps,
            incomplete=self.incomplete,
        )


def install_offline_guards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[RunBox, Path]:
    """Patch the runner and the price fetch, and point state at ``tmp_path``.

    ``fetch_price`` raises if a test reaches the network. Tests that need the
    "no listing" path replace this patch.
    """
    state = tmp_path / "xdg-state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("JUDGE_MODEL", "qwen/qwen3-235b-a22b-2507")
    monkeypatch.setenv("JUDGE_BASE_URL", "https://openrouter.ai/api")
    for name in (
        "JUDGE_PROVIDER",
        "STRATA_JUDGE_PROVIDER",
        "JUDGE_API_KEY",
        "STRATA_JUDGE_API_KEY",
        "ANTHROPIC_API_KEY",
        "STRATA_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    def fetch_must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("fetch_price tried to reach the network")

    monkeypatch.setattr("strata.judgecheck.cost.fetch_price", fetch_must_not_run)
    box = RunBox()
    monkeypatch.setattr("strata.judgecheck.run", box)
    return box, state


def invoke(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    """Run ``strata`` in-process. An argparse ``SystemExit`` becomes its code."""
    from strata.__main__ import main

    try:
        rc = main(argv)
    except SystemExit as exc:
        code = exc.code
        rc = code if isinstance(code, int) else 1
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def report_json(state: Path) -> Path:
    found = sorted(state.rglob("report.json"))
    assert len(found) == 1, [str(path) for path in found]
    return found[0]


def judge_must_decline_id() -> str:
    from strata.judgecheck import load_set

    for item in load_set():
        if (
            item.suite in {"h2", "drift", "j3"}
            and item.golden["decision"] == "decline"
            and item.data.get("judgecheck_security") != "engine"
        ):
            return item.id
    raise AssertionError("the set has no judge-dependent must-decline item")


def first_hand_id() -> str:
    from strata.judgecheck import load_set
    from strata.telling import has_telling_act

    for item in load_set():
        payload = item.data.get("contribution") or item.data.get("proposal") or {}
        if not has_telling_act(str(payload.get("content", ""))):
            return item.id
    raise AssertionError("the set has no first-hand item")
