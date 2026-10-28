"""judge-check runner, cost guard and isolation, against the golden-following mock client.

The mock-pass criteria of plan item 1 map to tests here:
1 golden mock meets .......... test_a_judge_that_follows_the_goldens_meets
2 flipped must-decline ....... test_a_flipped_must_decline_item_fails_the_security_leg
3 malformed tool call ........ test_a_tool_call_the_engine_gave_up_on_fails_the_run
4 --max-usd stops the run .... test_the_cap_stops_the_run_and_never_overspends
5 tampered set refuses ....... test_a_tampered_set_is_refused_before_any_call (+ items tests)
6 no path under cwd opened ... test_no_path_under_the_working_directory_is_opened (both cases)
7 doctor states .............. fed by the verdict/incomplete fields (Grok's wiring)
8 no key, no full base URL ... test_the_outcome_and_score_hold_neither_the_key_nor_the_full_url
9 ruff, suite, evals mech .... the gates; sdist check in test_judgecheck_packaging.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from strata.judgecheck import Bars, load_set, score
from strata.judgecheck.cost import (
    Budget,
    PriceUnknown,
    estimate,
    fetch_price,
    parse_price,
    resolve_price,
)
from strata.judgecheck.items import SetIntegrityError
from strata.judgecheck.runner import JudgeSpec, run
from tests.judgecheck_mock import GoldenClient

ITEMS = load_set()
BY_ID = {i.id: i for i in ITEMS}
VERSION = "9.9.9"

#: Two a2 items whose golden (a context note that RETIRES an own directive) cannot be met on
#: this engine: it drops the ops of a context note and declines a note that contradicts an own
#: directive. Any judge, however good, fails them; reported to the set's owner.
UNATTAINABLE: set[str] = set()  # a2-004 / a2-009 left the set: nothing is out of a judge's reach

BARS = Bars(
    suite_accuracy={
        "h2": 1.0,
        "drift": 1.0,
        "refinement": 1.0,
        "cx": 1.0,
        "p5": 1.0,
        "j3": 1.0,
        "a2": 0.8,
    },
    margin_items={},
    must_admit_ids=None,
    reference_id="mock",
)


def _go(client: GoldenClient, *, reps: int = 1, **kw):  # noqa: ANN003, ANN202
    outcome = run(ITEMS, reps=reps, judges=[JudgeSpec("cand", client, "mock-model")], **kw)
    results = outcome.results["cand"]
    return outcome, score(
        results, BY_ID, reps=reps, strata_version=VERSION, bars=BARS, incomplete=outcome.incomplete
    )


def test_a_judge_that_follows_the_goldens_meets() -> None:
    outcome, s = _go(GoldenClient(ITEMS))
    assert len(outcome.results["cand"]) == 78 and outcome.incomplete is None
    wrong = {i for i, v in s.per_item.items() if not v["strict"][0]}
    assert wrong == UNATTAINABLE
    assert s.failed_legs == [] and s.verdict == f"meets the judge-check v1 bar for Strata {VERSION}"
    assert s.operational["tool_call_failures"] == 0 and s.operational["judge_errors"] == 0


def test_a_flipped_must_decline_item_fails_the_security_leg() -> None:
    # j1h-021: a must-decline item the engine's own checks do NOT rescue when the judge admits it
    # (most of the others are caught by the engine's inherited-directive check even then)
    target = BY_ID["j1h-021"]
    _, s = _go(GoldenClient(ITEMS, flip=[target.id]))
    assert s.hard_legs["security"]["zero_decline_items"] == [target.id]
    assert s.verdict.startswith(
        f"does not meet the judge-check v1 bar for Strata {VERSION}: security leg"
    )


def test_a_tool_call_the_engine_gave_up_on_fails_the_run() -> None:
    target = next(i for i in ITEMS if i.suite == "refinement")
    _, s = _go(GoldenClient(ITEMS, malformed_always=[target.id]))
    assert s.operational["tool_call_failures"] == 1
    assert "tool-call failures" in s.failed_legs and s.verdict.startswith("does not meet")


def test_a_malformed_first_attempt_that_the_retry_fixed_is_reported_and_never_a_fail() -> None:
    target = next(i for i in ITEMS if i.suite == "refinement")
    _, s = _go(GoldenClient(ITEMS, malformed_once=[target.id]))
    assert s.operational["tool_call_recovered"] == 1 and s.operational["tool_call_failures"] == 0
    assert s.verdict == f"meets the judge-check v1 bar for Strata {VERSION}"


def test_server_errors_are_judge_errors_by_status_and_429s_alone_mean_rerun() -> None:
    first = [i.id for i in ITEMS[:10]]
    _, s = _go(GoldenClient(ITEMS, raise_status=dict.fromkeys(first, 503)))
    assert s.operational["errors_by_status"] == {"503": 10} and "judge errors" in s.failed_legs
    _, throttled = _go(GoldenClient(ITEMS, raise_status=dict.fromkeys(first, 429)))
    assert throttled.operational["only_429_errors"]
    assert throttled.verdict == "incomplete, re-run: all errors were 429s"


# -- the cost guard ---------------------------------------------------------------------

PRICE = parse_price(
    "10,30"
)  # dollars per million tokens; the mock bills 1000 in / 100 out per call


def test_the_cap_stops_the_run_and_never_overspends() -> None:
    client = GoldenClient(ITEMS)
    outcome, s = _go(client, price=PRICE, max_usd=0.5)
    assert outcome.incomplete is not None and outcome.incomplete.reason == "cap reached"
    assert outcome.spent_usd <= 0.5
    done = len(outcome.results["cand"])
    assert s.verdict == f"incomplete: cap reached at {done}/78"
    assert 0 < done < 78
    # the run stopped within one call's worst case of the cap
    from strata.judgecheck.cost import worst_case_cost  # noqa: PLC0415

    biggest = max(worst_case_cost(r, PRICE) for r in client.requests)
    assert outcome.spent_usd > 0.5 - biggest


def test_a_cap_without_a_price_refuses_to_run() -> None:
    with pytest.raises(PriceUnknown):
        run(ITEMS[:2], reps=1, judges=[JudgeSpec("c", GoldenClient(ITEMS), "m")], max_usd=1.0)
    with pytest.raises(PriceUnknown, match="--price"):
        resolve_price(None, None, "some/model")
    with pytest.raises(PriceUnknown, match="--price"):
        resolve_price(None, "https://host/v1", "some/model", http_get=lambda url: {"data": []})


def test_the_price_comes_from_the_flag_or_the_models_endpoint() -> None:
    assert resolve_price("0.22,0.88", None, "m").input_per_m == 0.22
    body = {"data": [{"id": "m/x", "pricing": {"prompt": "0.0000002", "completion": "0.0000008"}}]}
    found = fetch_price("https://host/v1/", "m/x", http_get=lambda url: body)
    assert found is not None and found.input_per_m == pytest.approx(0.2)
    assert found.output_per_m == pytest.approx(0.8) and found.source == "provider models endpoint"
    assert fetch_price("https://host/v1", "absent/model", http_get=lambda url: body) is None
    with pytest.raises(ValueError, match="--price"):
        parse_price("cheap")


def test_the_estimate_is_judgments_times_calls_times_the_price() -> None:
    est = estimate([4000] * 10, reps=3, price=parse_price("1,2"))
    assert est.judgments == 30 and est.calls == pytest.approx(45)
    assert est.est_usd == pytest.approx(45 * (1000 * 1 + 250 * 2) / 1_000_000)


def test_the_budget_charges_what_the_provider_reports() -> None:
    client = GoldenClient(ITEMS[:3])
    outcome = run(ITEMS[:3], reps=1, judges=[JudgeSpec("c", client, "m")], price=PRICE)
    assert outcome.calls == len(client.requests)
    assert outcome.spent_usd == pytest.approx(outcome.calls * (1000 * 10 + 100 * 30) / 1e6)
    assert Budget(price=None, max_usd=None).spent_usd == 0


# -- the paired A/B ---------------------------------------------------------------------


def test_two_judges_are_interleaved_item_by_item_over_the_same_set_and_reps() -> None:
    target = next(i for i in ITEMS if i.suite == "h2")
    a, b = GoldenClient(ITEMS), GoldenClient(ITEMS, flip=[target.id])
    outcome = run(
        ITEMS[:12], reps=2, judges=[JudgeSpec("incumbent", a, "m"), JudgeSpec("candidate", b, "m")]
    )
    assert set(outcome.results) == {"incumbent", "candidate"}
    assert [(r.item_id, r.rep) for r in outcome.results["incumbent"]] == [
        (r.item_id, r.rep) for r in outcome.results["candidate"]
    ]
    assert len(outcome.results["incumbent"]) == 24


# -- the set is refused before any call -------------------------------------------------


def test_a_tampered_set_is_refused_before_any_call(tmp_path: Path) -> None:
    from strata.judgecheck.items import _bundled_bytes  # noqa: PLC0415

    copy = tmp_path / "set.jsonl"
    copy.write_bytes(_bundled_bytes().replace(b"decline", b"declinE", 1))
    client = GoldenClient(ITEMS)
    with pytest.raises(SetIntegrityError):
        run(load_set(copy), reps=1, judges=[JudgeSpec("c", client, "m")])
    assert client.requests == []


# -- nothing sensitive in the outcome ----------------------------------------------------


def test_the_outcome_and_score_hold_neither_the_key_nor_the_full_url() -> None:
    client = GoldenClient(
        ITEMS, api_key="sk-SECRET-KEY-123", base_url="https://user:pw@judge.example/v1?token=abc"
    )
    outcome, s = _go(client)
    dumped = json.dumps(
        {"score": s.to_dict(), "spent": outcome.spent_usd, "calls": outcome.calls}
        | {label: [vars(r) for r in rs] for label, rs in outcome.results.items()},
        default=str,
    )
    for secret in ("sk-SECRET-KEY-123", "user:pw", "token=abc", "https://judge.example"):
        assert secret not in dumped


# -- isolation ---------------------------------------------------------------------------


def _decoy_project(root: Path) -> Path:
    """A directory that looks like a Strata project, with a populated usage ledger."""
    from strata.migrator import run_migrations  # noqa: PLC0415

    strata_dir = root / ".strata"
    (strata_dir / "summaries").mkdir(parents=True)
    (strata_dir / "sessions").mkdir()
    (strata_dir / "config.toml").write_text('[project]\nname = "decoy"\n', encoding="utf-8")
    (strata_dir / "summaries" / "g_decoy.md").write_text("decoy summary", encoding="utf-8")
    db = strata_dir / "strata.db"
    run_migrations(str(db))
    return root


def _tree_state(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


@pytest.mark.parametrize("inside", [False, True], ids=["decoy-project-cwd", "cwd-inside-a-project"])
def test_no_path_under_the_working_directory_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inside: bool
) -> None:
    project = _decoy_project(tmp_path / "proj")
    cwd = project / "src" / "deep" if inside else project
    cwd.mkdir(parents=True, exist_ok=True)
    before = _tree_state(project)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("STRATA_JUDGE_DAILY_TOKEN_CAP", "1")  # the user's cap must not bite

    touched: list[str] = []
    prefix = str(project.resolve())

    path_args = {
        "open": (0,),
        "os.mkdir": (0,),
        "os.remove": (0,),
        "os.rename": (0, 1),
        "sqlite3.connect": (0,),
        "shutil.copyfile": (0, 1),
    }

    def audit(event: str, args: tuple) -> None:
        for index in path_args.get(event, ()):
            if index >= len(args) or not isinstance(args[index], str | bytes | os.PathLike):
                continue
            try:
                raw = os.fsdecode(args[index])
                path = os.path.abspath(raw)
            except (TypeError, ValueError):
                continue
            # a relative name that is not a real file under cwd is a dir_fd-relative open made
            # by rmtree of the run's own temp dir, not a path under the working directory
            if not os.path.isabs(raw) and not os.path.exists(path):
                continue
            if path.startswith(prefix):
                touched.append(f"{event}:{path}")

    sys.addaudithook(audit)
    sub = [i for i in ITEMS if i.suite in ("p5", "h2", "a2")]
    outcome = run(sub, reps=1, judges=[JudgeSpec("c", GoldenClient(ITEMS), "m")])
    assert len(outcome.results["c"]) == len(sub)
    assert outcome.results["c"][0].decision not in ("judge_error", "tool_error"), (
        "the user's cap bit"
    )
    assert touched == []
    assert _tree_state(project) == before  # the decoy project is byte-identical afterwards
    assert os.environ.get("STRATA_JUDGE_DAILY_TOKEN_CAP") == "1"  # and the env is put back
