"""v1.20 forgetting core: the end condition on a directive (model, summary file, validation)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from strata.forgetting import CONDITION_MAX_CHARS, validate_ends
from strata.scope_manager import (
    DirectiveOp,
    ScopeManager,
    _apply_amendment,
    _apply_batch_amendment,
)
from strata.summary_store import Directive, EndCondition, ScopeSummary, SummaryStore
from tests.test_scope_manager import NEW_CONTRIBUTION, SCOPE, STRATUM, _fake_response

NOW = datetime(2026, 11, 30, 10, 0, tzinfo=UTC)


def _directive(did: str, **kw) -> Directive:  # noqa: ANN003
    return Directive(
        id=did,
        content=f"Rule {did}.",
        source_scope_id="g_a",
        created_at="2026-10-01T09:00:00+00:00",
        **kw,
    )


def _roundtrip(tmp_path, directives):  # noqa: ANN001, ANN202
    store = SummaryStore(str(tmp_path))
    store.write(
        "g_a",
        ScopeSummary(scope_id="g_a", directives=directives, context="ctx", updated_at="t"),
    )
    return store.read("g_a"), (tmp_path / "g_a.md").read_text(encoding="utf-8")


def test_a_date_end_condition_round_trips_on_its_own_line(tmp_path) -> None:  # noqa: ANN001
    d = _directive("c_1", end_condition=EndCondition(kind="date", until="2026-11-30"), set_by="c_1")
    back, text = _roundtrip(tmp_path, [d])
    assert "- ends: until=2026-11-30 · set_by=c_1" in text
    assert back.directives[0].end_condition == d.end_condition
    assert back.directives[0].set_by == "c_1"


def test_a_judged_condition_round_trips_with_quotes_and_unicode(tmp_path) -> None:  # noqa: ANN001
    cond = 'the "1.19" release ships — and the tag is pushed'
    d = _directive("c_1", end_condition=EndCondition(kind="judged", condition=cond), set_by="c_9")
    back, _ = _roundtrip(tmp_path, [d])
    assert back.directives[0].end_condition.condition == cond
    assert back.directives[0].set_by == "c_9"


def test_a_directive_without_one_renders_no_ends_line_and_reads_back_none(tmp_path) -> None:  # noqa: ANN001
    back, text = _roundtrip(tmp_path, [_directive("c_1")])
    assert "ends:" not in text
    assert back.directives[0].end_condition is None and back.directives[0].set_by is None


def test_a_neighbour_without_one_is_not_given_the_previous_directives_condition(tmp_path) -> None:  # noqa: ANN001
    ended = _directive("c_1", end_condition=EndCondition(kind="date", until="2026-12-01"))
    back, _ = _roundtrip(tmp_path, [ended, _directive("c_2")])
    assert back.directives[0].end_condition is not None
    assert back.directives[1].end_condition is None


def test_validate_ends_accepts_today_and_the_future() -> None:
    assert validate_ends({"until": "2026-11-30"}, now=NOW).until == "2026-11-30"
    assert validate_ends({"until": "2027-01-01"}, now=NOW).kind == "date"


def test_validate_ends_rejects_a_past_date_and_a_bad_one() -> None:
    with pytest.raises(ValueError, match="past"):
        validate_ends({"until": "2026-11-29"}, now=NOW)
    with pytest.raises(ValueError, match="not a date"):
        validate_ends({"until": "next week"}, now=NOW)


def test_validate_ends_judged_condition_bounds() -> None:
    assert validate_ends({"when": "  the 1.19 release ships  "}, now=NOW).condition == (
        "the 1.19 release ships"
    )
    for bad in ("", "   ", "x" * (CONDITION_MAX_CHARS + 1)):
        with pytest.raises(ValueError):
            validate_ends({"when": bad}, now=NOW)
    assert validate_ends({"when": "x" * CONDITION_MAX_CHARS}, now=NOW) is not None


@pytest.mark.parametrize(
    "bad", [{}, {"until": "2027-01-01", "when": "x"}, {"by": "x"}, "2027-01-01"]
)
def test_validate_ends_rejects_any_other_shape(bad) -> None:  # noqa: ANN001
    with pytest.raises(ValueError):
        validate_ends(bad, now=NOW)


def test_none_means_no_end_condition() -> None:
    assert validate_ends(None) is None


# -- an end condition survives every judged write (the judge never re-emits directives) ------

_ENDED = _directive(
    "c_ended",
    end_condition=EndCondition(kind="judged", condition="the 1.19 release ships"),
    set_by="c_ended",
)
_OTHER = _directive("c_other")
_SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[_ENDED, _OTHER],
    context="Old context line.",
    updated_at="2026-10-01T00:00:00+00:00",
)


def _ended(summary: ScopeSummary) -> Directive:
    return next(d for d in summary.directives if d.id == "c_ended")


@pytest.mark.parametrize(
    "ops",
    [
        [DirectiveOp(op="append")],
        [DirectiveOp(op="publish", content="A new rule.", supersedes="c_other")],
        [DirectiveOp(op="retire", id="c_other", changed_circumstance="no longer true")],
        [DirectiveOp(op="append"), DirectiveOp(op="supersede", id="c_other")],
        [],
    ],
)
def test_an_amendment_that_names_another_directive_leaves_the_end_condition_alone(ops) -> None:  # noqa: ANN001
    summary = _apply_amendment(
        scope=SCOPE,
        current_summary=_SUMMARY,
        contribution=NEW_CONTRIBUTION,
        ops=ops,
        new_context="A condensed context.",
    )
    assert _ended(summary) == _ENDED


def test_a_batch_amendment_that_retires_another_leaves_the_end_condition_alone() -> None:
    batch = _apply_batch_amendment(
        scope=SCOPE,
        current_summary=_SUMMARY,
        contributions={NEW_CONTRIBUTION.id: NEW_CONTRIBUTION},
        ops=[DirectiveOp(op="retire", id="c_other")],
        new_context="A condensed context.",
    )
    assert [d.id for d in batch.directives] == ["c_ended"]
    assert _ended(batch) == _ENDED


def test_a_full_judgment_that_condenses_keeps_the_end_condition() -> None:
    client = MagicMock()
    client.messages.create.return_value = _fake_response(
        {
            "decision": "accept_as_context",
            "reasoning": "Condensed.",
            "directive_ops": [{"op": "retire", "id": "c_other", "changed_circumstance": "gone"}],
            "new_context": "Short.",
        }
    )
    contribution = replace(NEW_CONTRIBUTION, proposed_classification="context")
    judgment = ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=_SUMMARY,
        recent_contributions=[],
        new_contribution=contribution,
    )
    assert judgment.new_summary is not None
    assert _ended(judgment.new_summary) == _ENDED
