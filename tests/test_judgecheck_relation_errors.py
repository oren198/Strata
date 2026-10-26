"""judge-check counts relation re-ask errors apart from main-call errors (reported, no bar)."""

from __future__ import annotations

from typing import Any

from strata.judgecheck import load_set, score
from strata.judgecheck.runner import RELATION_CALL_KINDS, JudgeSpec, run
from tests.judgecheck_mock import GoldenClient, status_error

ITEMS = load_set()
BY_ID = {i.id: i for i in ITEMS}
RELATION_TOOLS = ("classify_inherited_relation", "classify_own_directive_relation")


class RelationFlaky(GoldenClient):
    """Raises a 503 on every relation re-ask whose user message mentions a marked item."""

    def __init__(self, items, fail_for: set[str], **kw: Any) -> None:  # noqa: ANN001
        super().__init__(items, **kw)
        self.fail_for = fail_for

    def create(self, **kwargs: Any) -> Any:
        if kwargs["tool_choice"]["name"] in RELATION_TOOLS:
            item = self._item_for(kwargs["messages"][0]["content"])
            if item.id in self.fail_for:
                self.requests.append(kwargs)
                raise status_error(503)
        return super().create(**kwargs)


def _score(client: GoldenClient):  # noqa: ANN202
    outcome = run(ITEMS, reps=1, judges=[JudgeSpec("cand", client, "mock-model")])
    return score(outcome.results["cand"], BY_ID, reps=1, strata_version="9.9.9")


def _relation_item_ids(n: int = 3) -> set[str]:
    probe = GoldenClient(ITEMS)
    run(ITEMS, reps=1, judges=[JudgeSpec("cand", probe, "mock-model")])
    seen = []
    for req in probe.requests:
        if req["tool_choice"]["name"] in RELATION_TOOLS:
            item = probe._item_for(req["messages"][0]["content"])  # noqa: SLF001
            if item.id not in seen:
                seen.append(item.id)
    assert len(seen) >= n
    return set(seen[:n])


def test_a_clean_run_reports_relation_calls_and_no_errors() -> None:
    op = _score(GoldenClient(ITEMS)).operational
    assert op["relation_calls"] > 0 and op["relation_calls"] < op["calls"]
    assert op["relation_errors"] == 0 and op["main_call_errors"] == 0


def test_relation_reask_failures_are_counted_apart_from_main_call_errors() -> None:
    marked = _relation_item_ids()
    op = _score(RelationFlaky(ITEMS, marked)).operational
    assert op["relation_errors"] >= len(marked)
    assert op["main_call_errors"] == 0


def test_a_failed_main_call_is_a_main_call_error_not_a_relation_error() -> None:
    target = next(i.id for i in ITEMS if i.suite == "refinement")
    op = _score(GoldenClient(ITEMS, raise_status={target: 503})).operational
    assert op["main_call_errors"] >= 1 and op["relation_errors"] == 0


def test_relation_errors_never_fail_the_run_on_their_own() -> None:
    s = _score(RelationFlaky(ITEMS, _relation_item_ids()))
    assert "relation" not in " ".join(s.failed_legs)


def test_the_relation_kinds_are_the_engines_relation_re_asks() -> None:
    assert "inherited_relation_recheck" in RELATION_CALL_KINDS
    assert "own_directive_relation" in RELATION_CALL_KINDS
