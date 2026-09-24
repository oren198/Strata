"""#204 — a judge response with no `reasoning` must never fail a judgment.

Reproduced against the real record: contribution c_5c4ebf81929307c6 (g_ceo,
2026-09-19) recorded judgment_attempt ja_9fecf1d657bd5305 — error_class
`KeyError`, message `'reasoning'`, outcome `judge_failed` — and a later
`strata_rejudge` on the same contribution succeeded. `_parse_judgment` read
`raw["reasoning"]` unguarded; a payload that omits the field crashes instead
of triggering the one corrective re-ask every other protocol slip gets.

Fix: missing reasoning is a protocol slip like the others (issue #201) — one
corrective re-ask, using the SAME machinery. Unlike the others, a second
miss must not propagate: the contributor's judgment stands, with reasoning
recorded empty and a note. Reasoning is never the thing being judged.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from strata.fleet_config import Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import ScopeManager
from strata.summary_store import ScopeSummary

STRATUM = Stratum(id="L0", name="executive", ordinal=0)
SCOPE = Scope(id="g_ceo", name="CEO", stratum_id="L0")
CONTRIBUTOR = ContributorRef(
    scope_id=SCOPE.id, skill=None, session_id="sess_auto_6908", ts="2026-09-19T13:00:26+00:00"
)
CONTRIBUTION = Contribution(
    id="c_5c4ebf81929307c6",
    scope_id=SCOPE.id,
    content="Strata MVP, ratified by the CEO...",
    proposed_classification="directive",
    subject="strata-mvp",
    supersedes=None,
    contributor=CONTRIBUTOR,
    created_at="2026-09-19T13:00:26+00:00",
)
SUMMARY = ScopeSummary(
    scope_id=SCOPE.id, directives=[], context="", updated_at="2026-09-01T00:00:00Z"
)


def _resp(**payload) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.input = payload
    r = MagicMock()
    r.content = [block]
    return r


def _accept_no_reasoning(**extra) -> dict:
    """The exact shape that crashed: a decision, no `reasoning` key at all."""
    return {
        "decision": "accept_as_directive",
        "directive_ops": [],
        "new_context": None,
        **extra,
    }


def _judge(*payloads: dict):
    client = MagicMock()
    client.messages.create.side_effect = [_resp(**p) for p in payloads]
    judgment = ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=SUMMARY,
        recent_contributions=[],
        new_contribution=CONTRIBUTION,
    )
    return judgment, client


def _followup_text(client: MagicMock) -> str:
    content = client.messages.create.call_args_list[1].kwargs["messages"][-1]["content"]
    return " ".join(b["text"] for b in content if b.get("type") == "text")


# --- reproduction, against _parse_judgment directly (no retry, no network) ----------------


def test_parse_judgment_no_longer_raises_keyerror_on_missing_reasoning() -> None:
    """Direct reproduction of ja_9fecf1d657bd5305's shape: this must not be a KeyError."""
    block = _resp(**_accept_no_reasoning()).content[0]
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 — pinning the type, not swallowing it
        ScopeManager._parse_judgment(
            scope=SCOPE,
            tool_use_block=block,
            current_summary=SUMMARY,
            new_contribution=CONTRIBUTION,
        )
    assert not isinstance(excinfo.value, KeyError)
    assert isinstance(excinfo.value, ValueError)


# --- end-to-end: the one re-ask fixes it when the retry supplies reasoning ----------------


def test_missing_reasoning_earns_one_corrective_reask_naming_the_field() -> None:
    j, client = _judge(
        _accept_no_reasoning(),
        _accept_no_reasoning(reasoning="Ratified by the CEO; binds the fleet."),
    )
    assert client.messages.create.call_count == 2
    text = _followup_text(client)
    assert "`reasoning`" in text
    assert j.decision == "accept_as_directive"
    assert j.reasoning == "Ratified by the CEO; binds the fleet."
    assert j.new_summary is not None
    assert "Corrective re-ask" in j.record_notes


# --- the contributor never loses their judgment, even if the retry ALSO omits it ----------


def test_still_missing_after_the_reask_the_judgment_stands_with_an_empty_reasoning_note() -> None:
    """This is #204's actual guarantee: never fail the contribution over an explanation."""
    j, client = _judge(_accept_no_reasoning(), _accept_no_reasoning())
    assert client.messages.create.call_count == 2  # one re-ask, never a crash, never a third call
    assert j.decision == "accept_as_directive"
    assert j.new_summary is not None  # the amendment still applies
    assert j.reasoning == ""
    assert "no reasoning" in j.record_notes.lower() or "empty reasoning" in j.record_notes.lower()


def test_a_blank_or_whitespace_reasoning_counts_as_missing_too() -> None:
    j, client = _judge(_accept_no_reasoning(reasoning="   "), _accept_no_reasoning())
    assert client.messages.create.call_count == 2
    assert j.reasoning == ""


def test_a_non_string_reasoning_counts_as_missing_too() -> None:
    j, client = _judge(_accept_no_reasoning(reasoning=123), _accept_no_reasoning())
    assert client.messages.create.call_count == 2


# --- a decline that omits reasoning is fixed the same way, no amendment either way --------


def test_a_decline_with_no_reasoning_is_fixed_the_same_way() -> None:
    j, client = _judge(
        {"decision": "decline", "directive_ops": None, "new_context": None},
        {
            "decision": "decline",
            "directive_ops": None,
            "new_context": None,
            "reasoning": "Duplicate.",
        },
    )
    assert client.messages.create.call_count == 2
    assert j.decision == "decline"
    assert j.reasoning == "Duplicate."
    assert j.new_summary is None


# --- shares the protocol re-ask's one-retry budget: never a third call -------------------


def test_a_prior_protocol_slip_still_shares_the_one_retry_with_a_missing_reasoning_case() -> None:
    """A NoToolUseBlock already spent the re-ask; a still-missing reasoning after it must
    not crash either, and must not cost a third call."""
    no_tool_response = MagicMock()
    no_tool_response.content = []
    client = MagicMock()
    client.messages.create.side_effect = [no_tool_response, _resp(**_accept_no_reasoning())]
    judgment = ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=SUMMARY,
        recent_contributions=[],
        new_contribution=CONTRIBUTION,
    )
    assert client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_directive"
    assert judgment.reasoning == ""


# --- batch path: symmetric hardening, same exception family, no KeyError -----------------


def test_batch_verdict_missing_reasoning_is_a_valueerror_not_a_keyerror() -> None:
    from strata.scope_manager import _parse_batch_verdicts

    with pytest.raises(ValueError) as excinfo:  # noqa: PT011
        _parse_batch_verdicts(
            [{"contribution_id": "c1", "decision": "accept_as_context"}], batch_ids=["c1"]
        )
    assert not isinstance(excinfo.value, KeyError)


# --- publication judging: the same raw-indexing bug, same tolerant fix -------------------


def test_judge_publication_does_not_keyerror_on_missing_reasoning(monkeypatch) -> None:
    block = MagicMock()
    block.type = "tool_use"
    block.input = {"decision": "accept"}
    response = MagicMock()
    response.content = [block]
    client = MagicMock()
    client.messages.create.return_value = response

    manager = ScopeManager(client=client)
    result = manager.judge_publication(
        scope=SCOPE,
        act_kind="publish",
        current_summary=SUMMARY,
        current_publication=[],
        content="A published fact.",
        kind="context",
        anchors=["a1"],
    )
    assert result.reasoning == ""
    assert result.decision == "accept"
