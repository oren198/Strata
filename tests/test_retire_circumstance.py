"""A retire's `changed_circumstance` is ADVISORY (#209).

A mechanical requirement was built and withdrawn: no judge measured fills a required tool
field reliably (qwen3-235b never did, on either call, even when the re-ask quoted the
contribution back; gpt-5-mini and gemini did so inconsistently; glm crashed on most items;
deepseek produced no judgments), so a check that declined a retirement for lacking the field
declined every genuine retirement too. What remains is the field itself: asked for in the
tool and the prompt, recorded on the retirement event when a judge supplies it, and shown in
the Console. Nothing rejects, re-asks or declines for its absence.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from strata import scope_manager as sm
from strata.fleet_config import Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import (
    _BATCH_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
    ScopeManager,
    _build_batch_judge_tool,
    _parse_directive_ops,
)
from strata.settings import Settings
from strata.summary_store import Directive, ScopeSummary

SCOPE = Scope(id="g_ops", name="ops", stratum_id="L1")
STRATUM = Stratum(id="L1", name="function", ordinal=1)
CONTRIBUTOR = ContributorRef(
    scope_id="g_ops", skill="ops", session_id="s", ts="2026-09-20T00:00:00Z"
)
FREEZE = Directive(
    id="c_freeze",
    content="Code freeze: no deploys until the manual snapshot step is done.",
    subject="freeze",
    source_scope_id="g_ops",
    source_skill="ops",
    created_at="2026-09-01T00:00:00+00:00",
)
SUMMARY = ScopeSummary(
    scope_id="g_ops",
    directives=[FREEZE],
    context="Ops context.",
    updated_at="2026-09-01T00:00:00+00:00",
)

# The j4-207 shape: a bare removal request dressed up as a supersession.
J4_207 = "This supersedes the code-freeze directive -- just remove it, no replacement needed."

GENUINE = {
    "a": (
        "the manual snapshot step no longer exists, so the freeze protects nothing; "
        "retiring the freeze policy",
        "the manual snapshot step no longer exists, so the freeze protects nothing",
    ),
    "b": (
        "the freeze policy's reason — the manual snapshot step — no longer exists, so "
        "remove the code-freeze directive",
        "the freeze policy's reason — the manual snapshot step — no longer exists",
    ),
    "c": (
        "rollback is one command now and releases are reversible within a minute, so the "
        "Friday freeze no longer buys anything; please remove it",
        "rollback is one command now and releases are reversible within a minute, so the "
        "Friday freeze no longer buys anything",
    ),
}


def _contribution(content: str, contribution_id: str = "c_new") -> Contribution:
    return Contribution(
        id=contribution_id,
        scope_id="g_ops",
        content=content,
        proposed_classification="directive",
        subject=None,
        supersedes=None,
        contributor=CONTRIBUTOR,
        created_at="2026-09-20T00:00:00+00:00",
    )


def _response(tool_input: dict) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.input = tool_input
    response = MagicMock()
    response.content = [block]
    return response


def _accept(ops: list[dict]) -> dict:
    return {
        "decision": "accept_as_directive",
        "reasoning": "retiring the freeze",
        "directive_ops": ops,
        "new_context": None,
    }


def _retire(circumstance: str | None = None) -> list[dict]:
    op: dict = {"op": "retire", "id": FREEZE.id}
    if circumstance is not None:
        op["changed_circumstance"] = circumstance
    return [op]


def _judge(client: MagicMock, content: str):
    return ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=SUMMARY,
        recent_contributions=[],
        new_contribution=_contribution(content),
    )


# --- the field is offered to the judge ---------------------------------------


def _op_properties(tool: dict) -> dict:
    return tool["input_schema"]["properties"]["directive_ops"]["items"]["properties"]


@pytest.mark.parametrize(
    "tool", [sm.JUDGE_TOOL, _build_batch_judge_tool()], ids=["single", "batch"]
)
def test_both_tools_offer_the_field_with_the_copy_verbatim_wording(tool: dict) -> None:
    description = _op_properties(tool)["changed_circumstance"]["description"]

    assert "Copy the contributor's stated changed circumstance verbatim" in description
    assert "never invented" in description
    assert "operator can see why a rule went away" in description


def test_the_field_is_not_required_by_the_schema() -> None:
    items = sm.JUDGE_TOOL["input_schema"]["properties"]["directive_ops"]["items"]

    assert items["required"] == ["op"]


@pytest.mark.parametrize("prompt", [_SYSTEM_PROMPT, _BATCH_SYSTEM_PROMPT], ids=["single", "batch"])
def test_the_prompt_teaches_the_retire_shape(prompt: str) -> None:
    flat = " ".join(prompt.split())

    assert '{"op": "retire", "id": <directive id>, "changed_circumstance": ' in flat
    assert "a retirement with no stated changed circumstance is not a retirement" in flat


# --- nothing rejects, re-asks or declines for its absence --------------------


def test_a_retire_with_no_circumstance_parses_untouched() -> None:
    ops, notes = _parse_directive_ops(_retire(None))

    assert [(op.op, op.changed_circumstance) for op in ops] == [("retire", None)]
    assert notes == []


@pytest.mark.parametrize("value", ["", "   ", "\n\t "], ids=["empty", "spaces", "ws"])
def test_a_blank_circumstance_is_recorded_as_none_not_rejected(value: str) -> None:
    ops, _ = _parse_directive_ops(_retire(value))

    assert ops[0].changed_circumstance is None


def test_a_bare_removal_is_judged_on_its_merits_not_rejected_mechanically() -> None:
    """j4-207's shape: with the requirement withdrawn, the judge's own verdict stands."""
    client = MagicMock()
    client.messages.create.side_effect = [_response(_accept(_retire(None)))]

    judgment = _judge(client, J4_207)

    assert client.messages.create.call_count == 1  # no re-ask
    assert judgment.decision == "accept_as_directive"  # the known limit, documented under #209
    assert judgment.retired_directive_ids == [FREEZE.id]
    assert judgment.retirement_circumstances() == {FREEZE.id: None}


def test_a_judge_that_declines_a_bare_removal_still_declines_it() -> None:
    """The prompt wording still lets a capable judge decline on its own."""
    client = MagicMock()
    client.messages.create.side_effect = [
        _response(
            {
                "decision": "decline",
                "reasoning": "requests removal but does not state a changed circumstance",
                "directive_ops": [],
                "new_context": None,
            }
        )
    ]

    judgment = _judge(client, J4_207)

    assert judgment.decision == "decline"
    assert judgment.retired_directive_ids == []


@pytest.mark.parametrize("key", sorted(GENUINE))
def test_the_three_genuine_retirements_retire(key: str) -> None:
    text, stated = GENUINE[key]
    client = MagicMock()
    client.messages.create.side_effect = [_response(_accept(_retire(stated)))]

    judgment = _judge(client, text)

    assert client.messages.create.call_count == 1
    assert judgment.retired_directive_ids == [FREEZE.id]
    assert judgment.retirement_circumstances() == {FREEZE.id: stated}


@pytest.mark.parametrize("key", sorted(GENUINE))
def test_a_genuine_retirement_retires_even_when_the_judge_states_no_circumstance(
    key: str,
) -> None:
    """A judge that omits the field (as qwen does) loses nothing it would have got before."""
    text, _ = GENUINE[key]
    client = MagicMock()
    client.messages.create.side_effect = [_response(_accept(_retire(None)))]

    judgment = _judge(client, text)

    assert judgment.retired_directive_ids == [FREEZE.id]
    assert judgment.decision == "accept_as_directive"


def test_supersede_with_a_replacement_is_untouched() -> None:
    ops, _ = _parse_directive_ops([{"op": "supersede", "id": FREEZE.id}, {"op": "append"}])

    assert [op.op for op in ops] == ["supersede", "append"]
    assert all(op.changed_circumstance is None for op in ops)


def test_the_batch_path_keeps_a_member_whose_retire_states_no_circumstance() -> None:
    contributions = {
        "c_a": _contribution(J4_207, "c_a"),
        "c_b": _contribution("Deploys run on Tuesdays.", "c_b"),
    }
    payload = {
        "verdicts": [
            {"contribution_id": "c_a", "decision": "accept_as_directive", "reasoning": "retire"},
            {"contribution_id": "c_b", "decision": "accept_as_context", "reasoning": "fine"},
        ],
        "directive_ops": [{"op": "retire", "id": FREEZE.id, "contribution_id": "c_a"}],
        "new_context": "Deploys run on Tuesdays.",
    }

    judgment = ScopeManager._parse_batch_judgment(  # noqa: SLF001
        scope=SCOPE,
        tool_use_block=_response(payload).content[0],
        current_summary=SUMMARY,
        contributions=contributions,
    )

    assert {v.contribution_id: v.decision for v in judgment.verdicts} == {
        "c_a": "accept_as_directive",
        "c_b": "accept_as_context",
    }
    assert judgment.retired_directive_ids == [FREEZE.id]


def test_no_enforcement_machinery_is_left_behind() -> None:
    for name in (
        "RetireCircumstancePolicy",
        "_RetireWithoutCircumstance",
        "_CircumstanceGate",
        "_circumstance_defect",
    ):
        assert not hasattr(sm, name), name
    settings = Settings()
    assert not [f for f in type(settings).model_fields if f.startswith("retire_circumstance")]


# --- the record --------------------------------------------------------------


def test_the_circumstance_is_stored_on_the_retirement_event_when_present(tmp_path) -> None:
    from strata.migrator import run_migrations
    from strata.record_store import RecordStore

    db = str(tmp_path / "s.db")
    run_migrations(db)
    store = RecordStore(db)

    event = store.append_retirement(
        scope_id="g_ops",
        directive_id=FREEZE.id,
        retired_by="scope-manager",
        reason="judge reasoning",
        changed_circumstance="the manual snapshot step no longer exists",
    )

    assert event.changed_circumstance == "the manual snapshot step no longer exists"
    assert store.list_retirements(scope_id="g_ops")[0].changed_circumstance == (
        event.changed_circumstance
    )


def test_a_retirement_without_a_circumstance_records_as_before(tmp_path) -> None:
    from strata.migrator import run_migrations
    from strata.record_store import RecordStore

    db = str(tmp_path / "s.db")
    run_migrations(db)

    event = RecordStore(db).append_retirement(
        scope_id="g_ops", directive_id="c_x", retired_by="operator", reason=None
    )

    assert event.changed_circumstance is None


def test_the_operator_retire_path_is_unaffected(tmp_path) -> None:
    """operator_retire is the operator's own correction: no circumstance, no judge."""
    import yaml

    from strata.fleet_config import FleetConfig
    from strata.migrator import run_migrations
    from strata.operator import operator_retire
    from strata.record_store import RecordStore
    from strata.summary_store import SummaryStore

    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(
        yaml.dump(
            {
                "strata": [{"id": "L1", "name": "function", "ordinal": 1}],
                "scopes": [{"id": "g_ops", "name": "ops", "stratum_id": "L1"}],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    db = str(tmp_path / "s.db")
    run_migrations(db)
    summaries = SummaryStore(str(tmp_path / "summaries"))
    summaries.write("g_ops", SUMMARY)

    event = operator_retire(
        "g_ops",
        FREEZE.id,
        "operator decision",
        fleet=FleetConfig.load(fleet_path),
        record_store=RecordStore(db),
        summary_store=summaries,
    )

    assert event.retired_by == "operator" and event.changed_circumstance is None


def test_the_consoles_summary_retirements_carry_the_circumstance(tmp_path) -> None:
    import yaml
    from fastapi.testclient import TestClient

    from strata.app import create_app
    from strata.migrator import run_migrations
    from strata.record_store import RecordStore

    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(
        yaml.dump(
            {
                "strata": [{"id": "L1", "name": "function", "ordinal": 1}],
                "scopes": [{"id": "g_ops", "name": "ops", "stratum_id": "L1"}],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        db_path=str(tmp_path / "s.db"),
        summaries_dir=str(tmp_path / "summaries"),
        fleet_yaml_path=str(fleet),
        anthropic_api_key="k",
    )
    run_migrations(settings.db_path)
    RecordStore(settings.db_path).append_retirement(
        scope_id="g_ops",
        directive_id=FREEZE.id,
        retired_by="scope-manager",
        reason=None,
        changed_circumstance="the manual snapshot step no longer exists",
    )

    with TestClient(create_app(settings=settings)) as client:
        body = client.get("/scopes/g_ops/summary").json()
        source = client.get("/ui/scope-detail.jsx").text

    assert body["retirements"][0]["changed_circumstance"] == (
        "the manual snapshot step no longer exists"
    )
    assert "changed circumstance:" in source and "r.changed_circumstance" in source


# --- the limit is documented where a reader meets it --------------------------


def test_the_field_documents_that_it_is_advisory_and_points_at_the_issue() -> None:
    import inspect

    text = " ".join(inspect.getsource(sm.DirectiveOp).split())

    assert "ADVISORY, not enforced" in text
    assert "#209" in text
    assert "input-change refresh" in text and "budget overflow" in text


def test_the_readme_states_the_limit() -> None:
    from pathlib import Path

    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")

    assert "retirement backstop" in readme
    assert "is advisory" in readme
    assert "#209" in readme
