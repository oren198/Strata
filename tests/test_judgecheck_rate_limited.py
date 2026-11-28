"""A judge-check run in which any call was answered 429 is incomplete: those judgments are not
scored, the verdict is never "meets", and the CLI exit code is nonzero."""

from __future__ import annotations

from typing import Any

from strata.judgecheck import Bars, load_set, score
from strata.judgecheck.cli import _exit_for_verdict
from strata.judgecheck.runner import JudgeSpec, run
from tests.judgecheck_mock import GoldenClient, status_error

ITEMS = load_set()
BY_ID = {i.id: i for i in ITEMS}
VERSION = "9.9.9"
RELATION_TOOLS = ("classify_inherited_relation", "classify_own_directive_relation")
BARS = Bars(
    suite_accuracy={s: 0.0 for s in ("h2", "drift", "refinement", "cx", "p5", "j3", "a2")},
    margin_items={},
    must_admit_ids=None,
    reference_id="mock",
)


class RelationThrottled(GoldenClient):
    """Answers 429 to every relation re-ask about one marked item."""

    def __init__(self, items, target: str, **kw: Any) -> None:  # noqa: ANN001
        super().__init__(items, **kw)
        self.target = target

    def create(self, **kwargs: Any) -> Any:
        asked_about = self._item_for(kwargs["messages"][0]["content"]).id
        if kwargs["tool_choice"]["name"] in RELATION_TOOLS and asked_about == self.target:
            self.requests.append(kwargs)
            raise status_error(429)
        return super().create(**kwargs)


def _go(client: GoldenClient):  # noqa: ANN202
    outcome = run(ITEMS, reps=1, judges=[JudgeSpec("cand", client, "mock-model")])
    return outcome, score(outcome.results["cand"], BY_ID, reps=1, strata_version=VERSION, bars=BARS)


def _relation_item() -> str:
    probe = GoldenClient(ITEMS)
    run(ITEMS, reps=1, judges=[JudgeSpec("cand", probe, "mock-model")])
    for req in probe.requests:
        if req["tool_choice"]["name"] in RELATION_TOOLS:
            return probe._item_for(req["messages"][0]["content"]).id  # noqa: SLF001
    raise AssertionError("no relation re-ask in the set")


def test_a_clean_run_still_meets() -> None:
    _, s = _go(GoldenClient(ITEMS))
    assert s.verdict == f"meets the judge-check v1 bar for Strata {VERSION}"
    assert s.incomplete is None


def test_one_throttled_judgment_is_incomplete_not_scored_and_exits_nonzero() -> None:
    victim = ITEMS[0].id  # one 429 in the whole run: under the 2% error rate
    outcome, s = _go(GoldenClient(ITEMS, raise_status={victim: 429}))
    assert s.verdict.startswith("incomplete")
    assert "meets" not in s.verdict
    assert "429" in s.verdict and s.operational["rate_limited_judgments"] == 1
    assert s.operational["meets"] is False
    assert victim not in s.per_item  # the 429'd judgment was not scored as a judgment
    assert sum(len(v["decisions"]) for v in s.per_item.values()) == len(outcome.results["cand"]) - 1
    assert _exit_for_verdict(s.verdict) != 0


def test_a_throttled_relation_reask_is_incomplete_and_exits_nonzero() -> None:
    victim = _relation_item()
    _, s = _go(RelationThrottled(ITEMS, victim))
    assert s.verdict.startswith("incomplete")
    assert victim not in s.per_item
    assert _exit_for_verdict(s.verdict) != 0
