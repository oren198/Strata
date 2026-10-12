"""#244: inherited-rule matching on the shipping judge. Fix 1: the comparator."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.scope_manager import _tighten_value_check, inherited_conflict  # noqa: E402

_TOKENS = "Access tokens expire after at most 15 minutes."
_LOGS = "Application logs are retained for at most 14 days."
_PUMPS = "Pumps are switched off at or before 20:00."


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        (_TOKENS, "Platform-issued access tokens expire after 10 minutes."),
        (_TOKENS, "Platform-issued access tokens expire after ten minutes."),
        (_TOKENS, "Platform-issued access tokens expire after at most 10 minutes."),
        (_LOGS, "The platform team retains application logs for 7 days."),
        (_LOGS, "The platform team retains application logs for seven days."),
        (_PUMPS, "Pumps in hall 2 are switched off at 19:00."),
        ("Tokens expire after at least 10 minutes.", "Tokens expire after 20 minutes."),
    ],
)
def test_a_bare_value_inside_the_bound_is_a_tightening(parent: str, child: str) -> None:
    assert inherited_conflict(child, parent) is None


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        (_TOKENS, "Platform-issued access tokens expire after 20 minutes."),
        (_TOKENS, "Platform tokens expire after at least 10 minutes."),
        (_LOGS, "The platform team retains application logs for 30 days."),
        (_PUMPS, "Pumps in hall 2 are switched off at 21:00."),
        ("Tokens expire after at least 10 minutes.", "Tokens expire after 5 minutes."),
    ],
)
def test_a_bare_value_outside_the_bound_or_under_another_comparator_still_conflicts(
    parent: str, child: str
) -> None:
    assert inherited_conflict(child, parent) is not None


def test_two_different_same_unit_values_are_not_guessed() -> None:
    # Ambiguous: which one is the retention? Fails toward the rule.
    ok, _reason = _tighten_value_check(_LOGS, "Logs are kept 7 days locally and 30 days offsite.")
    assert not ok


def test_the_bare_value_must_have_the_parents_unit() -> None:
    ok, _reason = _tighten_value_check(_TOKENS, "Tokens expire after 10 hours.")
    assert not ok


# ---------------------------------------------------------------------------
# Fix 2: the engine locates the kept value; the model's span is a hint
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock  # noqa: E402

from strata.scope_manager import (  # noqa: E402
    relation_decline_scope_trigger,
    verify_relation_ground,
)

from .test_v17_relation_recheck import (  # noqa: E402
    ANCESTOR_DIRECTIVE,
    ANCESTOR_DIRECTIVES,
    _judge,
    _ordinary_decline,
    _reask_response,
)

_ALERT_PARENT = ANCESTOR_DIRECTIVE.content  # "... at or below 500 ms p99 latency."


def _tightens(kept_span: str) -> dict:
    return {
        "relation": "tightens",
        "other_grounds_clear": True,
        "parent_id": "p",
        "tighten_kind": "rule",
        "kept_span": kept_span,
    }


@pytest.mark.parametrize(
    "kept_span",
    [
        "Alerting escalates to SEV-1 at or below 500 ms p99 latency.",  # quotes the parent
        "alerts go to SEV-1 once latency is under 400 ms",  # paraphrase
    ],
)
def test_the_engine_locates_the_kept_value_when_the_models_span_is_not_verbatim(
    kept_span: str,
) -> None:
    content = "The platform team escalates to SEV-1 at 400 ms p99 latency."
    ok, result, classification, _ctx = verify_relation_ground(
        _tightens(kept_span), _ALERT_PARENT, content, "directive"
    )
    assert ok is True, result
    assert classification == "directive"
    assert "located by the engine" in result


def test_a_non_verbatim_span_over_a_looser_value_still_declines() -> None:
    content = "The platform team escalates to SEV-1 at 900 ms p99 latency."
    ok, result, _c, _x = verify_relation_ground(
        _tightens("escalates at 400 ms"), _ALERT_PARENT, content, "directive"
    )
    assert ok is False
    assert "kept_span not verbatim" in result


def test_a_non_verbatim_span_with_no_value_in_the_text_declines() -> None:
    ok, result, _c, _x = verify_relation_ground(
        _tightens("something else entirely"),
        _ALERT_PARENT,
        "The platform team escalates quickly.",
        "directive",
    )
    assert ok is False


def test_a_non_verbatim_span_does_not_bypass_the_exemption_guards() -> None:
    content = "The platform team escalates to SEV-1 at 400 ms p99 latency, except for batch jobs."
    ok, result, _c, _x = verify_relation_ground(
        _tightens("not in the text"), _ALERT_PARENT, content, "directive"
    )
    assert ok is False
    assert "exemption" in result


def test_a_verbatim_span_that_truncates_a_number_still_declines() -> None:
    content = "Escalates to SEV-1 at 400 ms p99 latency."
    ok, result, _c, _x = verify_relation_ground(
        _tightens("at 4"), _ALERT_PARENT, content, "directive"
    )
    assert ok is False
    assert "truncates" in result


def test_the_whole_flow_rescues_a_paraphrased_span() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_decline(f"Contradicts {ANCESTOR_DIRECTIVE.id}."),
        _reask_response(
            relation="tightens",
            tighten_kind="rule",
            kept_span="Alerting escalates to SEV-1 at or below 500 ms p99 latency",
            classification="directive",
        ),
    ]
    judgment, _manager = _judge(mock_client, "The platform team escalates to SEV-1 at 400 ms.")
    assert judgment.decision == "accept_as_directive"
    assert judgment.relation_recheck["relation"] == "tightens"


# ---------------------------------------------------------------------------
# Fix 3: a decline naming the ancestor SCOPE, not the directive id
# ---------------------------------------------------------------------------


def test_scope_trigger_picks_the_directive_sharing_the_subject() -> None:
    reasoning = "The ancestor scope g_relparent has already established this."
    assert (
        relation_decline_scope_trigger(
            reasoning, "Alerting escalates at 400 ms.", ANCESTOR_DIRECTIVES
        )
        == ANCESTOR_DIRECTIVE.id
    )


@pytest.mark.parametrize(
    ("reasoning", "content"),
    [
        ("No scope is named here.", "Alerting escalates at 400 ms."),
        ("The ancestor scope g_relparent says so.", "The standup is at 09:30."),
        ("The ancestor scope g_relparentX says so.", "Alerting escalates at 400 ms."),
        (None, "Alerting escalates at 400 ms."),
    ],
)
def test_scope_trigger_needs_both_the_scope_and_a_shared_word(
    reasoning: str | None, content: str
) -> None:
    assert relation_decline_scope_trigger(reasoning, content, ANCESTOR_DIRECTIVES) is None


def test_the_whole_flow_fires_the_reask_on_a_scope_naming_decline() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_decline("The ancestor scope g_relparent has already established this rule."),
        _reask_response(
            relation="tightens",
            tighten_kind="rule",
            kept_span="x",
            classification="directive",
        ),
    ]
    judgment, _manager = _judge(mock_client, "Alerting escalates to SEV-1 at 400 ms p99 latency.")
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_directive"


def test_no_reask_when_the_decline_names_neither_id_nor_scope() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [_ordinary_decline("Not relevant to this scope.")]
    judgment, _manager = _judge(mock_client, "Alerting escalates to SEV-1 at 400 ms p99 latency.")
    assert mock_client.messages.create.call_count == 1
    assert judgment.decision == "decline"
