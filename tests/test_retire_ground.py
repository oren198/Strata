"""A `retire` needs a GROUND (#209, the retirement half).

j4-207 — "this supersedes the code-freeze directive, just remove it, no replacement
needed" — was accepted and retired a directive: the contributor was the directive's
own scope and `retire` is the sanctioned removal, so nothing stopped a bare request.
The defect is that nothing required a ground: the changed circumstance, in the
contribution's own words. A retire without one is rejected mechanically (no LLM
call) in the validate layer that already rejects an unpaired supersede; the judge gets
one re-ask, and if it still cannot supply one the contribution is declined with a
reason that names the missing ground.
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
    DirectiveOp,
    RetireGroundPolicy,
    ScopeManager,
    _build_batch_judge_tool,
    _parse_directive_ops,
    _RetireWithoutGround,
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


def _retire(ground: str | None, **extra: object) -> list[dict]:
    op: dict = {"op": "retire", "id": FREEZE.id, **extra}
    if ground is not None:
        op["ground"] = ground
    return [op]


def _parse(ops: list[dict], contribution: str = J4_207, policy: RetireGroundPolicy | None = None):
    kwargs = {} if policy is None else {"ground_policy": policy}
    return _parse_directive_ops(ops, contribution_text_for=lambda _op: contribution, **kwargs)


# --- schema and prompt -------------------------------------------------------


def _op_properties(tool: dict) -> dict:
    return tool["input_schema"]["properties"]["directive_ops"]["items"]["properties"]


def test_the_single_tool_defines_ground_on_a_retire_op() -> None:
    tool = sm.JUDGE_TOOL

    ground = _op_properties(tool)["ground"]
    assert "retire" in ground["description"]
    assert "must come from the contribution, never invented" in ground["description"]
    assert "a retirement with no stated ground is not a retirement" in ground["description"]


def test_the_batch_tool_defines_it_too() -> None:
    ground = _op_properties(_build_batch_judge_tool())["ground"]

    assert "never invented" in ground["description"]


@pytest.mark.parametrize("prompt", [_SYSTEM_PROMPT, _BATCH_SYSTEM_PROMPT], ids=["single", "batch"])
def test_the_prompt_teaches_the_retire_shape_and_the_way_out(prompt: str) -> None:
    flat = " ".join(prompt.split())

    assert '{"op": "retire", "id": <directive id>, "ground": ' in flat
    assert "a retirement with no stated ground is not a retirement" in flat
    assert "DECLINE" in flat  # no changed circumstance stated -> decline


# --- (a) absent / empty / whitespace -----------------------------------------


@pytest.mark.parametrize(
    "ground", [None, "", "   ", "\n\t "], ids=["absent", "empty", "spaces", "ws"]
)
def test_a_retire_with_no_ground_is_rejected(ground: str | None) -> None:
    with pytest.raises(_RetireWithoutGround, match="ground"):
        _parse(_retire(ground))


def test_the_rejection_is_a_value_error_like_the_unpaired_supersede() -> None:
    assert issubclass(_RetireWithoutGround, ValueError)


# --- (b) the word floor ------------------------------------------------------


def test_a_ground_under_the_word_floor_is_rejected_and_at_the_floor_it_passes() -> None:
    policy = RetireGroundPolicy(min_words=4, min_substantive=1, max_restatement=0.99)
    contribution = "Retire the freeze directive."

    with pytest.raises(_RetireWithoutGround, match="too short"):
        _parse(_retire("snapshots now automatic"), contribution, policy)  # 3 words

    ops, _ = _parse(_retire("snapshots are now automatic"), contribution, policy)  # 4 words
    assert ops[0].ground == "snapshots are now automatic"


# --- (c) restating the removal -----------------------------------------------


@pytest.mark.parametrize(
    "ground",
    [
        "just remove it, no replacement needed",
        "The code-freeze directive is superseded and should be removed",
        "It is obsolete and should go",
        "no replacement is needed anymore",
    ],
)
def test_a_ground_that_merely_restates_the_removal_request_is_rejected(ground: str) -> None:
    with pytest.raises(_RetireWithoutGround, match="restates the removal"):
        _parse(_retire(ground))


# Genuine grounds share many words with the removal sentence and must NOT be rejected
# for overlap: a real retirement names the directive it retires, and the changed
# circumstance is usually stated in the same sentence.
GENUINE = [
    (
        "The code freeze directive can be removed: the manual snapshot step it was created "
        "to protect no longer exists.",
        "the manual snapshot step the freeze protected no longer exists",
    ),
    (
        "Please retire the deploy-window directive; the deploy window policy was replaced by "
        "continuous delivery in the new pipeline.",
        "the deploy window policy was replaced by continuous delivery in the new pipeline",
    ),
    (
        "The freeze policy's reason — the manual snapshot step — no longer exists, so drop "
        "the freeze directive.",
        "the freeze policy's reason — the manual snapshot step — no longer exists",
    ),
    (
        "Retire the staging-only rate limit: staging now mirrors production limits.",
        "staging now mirrors production limits",
    ),
]


@pytest.mark.parametrize(("contribution", "ground"), GENUINE, ids=range(len(GENUINE)))
def test_a_genuine_ground_that_shares_words_with_the_removal_is_accepted(
    contribution: str, ground: str
) -> None:
    ops, _ = _parse(_retire(ground), contribution)

    assert [(op.op, op.ground) for op in ops] == [("retire", ground)]


def _synthetic(shared: int) -> tuple[str, str]:
    """A 10-word ground with *shared* of its words also in the removal sentence."""
    removal = "Remove alpha bravo charlie delta echo foxtrot golf hotel."
    in_removal = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"][:shared]
    novel = ["kiwi", "lemon", "mango", "nectarine", "orange", "papaya", "quince", "rhubarb"]
    ground = " ".join(in_removal + novel[: 10 - shared])
    return removal, ground


def test_the_restatement_threshold_is_pinned_at_both_edges() -> None:
    policy = RetireGroundPolicy(min_words=4, min_substantive=2, max_restatement=0.7)

    below_removal, below = _synthetic(6)  # 6/10 = 0.6 shared: below the threshold
    at_removal, at = _synthetic(7)  # 7/10 = 0.7 shared: AT the threshold — rejected

    ops, _ = _parse(_retire(below), below_removal, policy)
    assert ops[0].ground == below
    with pytest.raises(_RetireWithoutGround, match="restates the removal"):
        _parse(_retire(at), at_removal, policy)


def test_a_ground_with_too_few_substantive_words_is_a_restatement() -> None:
    """Request vocabulary (remove, replacement, needed, obsolete...) carries no
    circumstance, however many of those words there are."""
    policy = RetireGroundPolicy(min_words=4, min_substantive=2, max_restatement=0.99)

    with pytest.raises(_RetireWithoutGround, match="restates the removal"):
        _parse(_retire("this is not needed and can be dropped now please"), "Retire it.", policy)


def test_with_no_removal_sentence_only_the_floors_apply() -> None:
    """A contribution that is not itself a removal request (e.g. an overflow retire
    to fit the budget) has no removal sentence to restate."""
    ops, _ = _parse(
        _retire("the summary is over budget and this rule no longer earns its words"),
        "New observation about deploys.",
    )

    assert ops[0].op == "retire"


# --- supersede untouched; ground carried -------------------------------------


def test_supersede_with_a_replacement_needs_no_ground() -> None:
    ops, _ = _parse([{"op": "supersede", "id": FREEZE.id}, {"op": "append"}])

    assert [op.op for op in ops] == ["supersede", "append"]


def test_the_ground_is_carried_on_the_parsed_op_stripped() -> None:
    ops, _ = _parse(_retire("  the manual snapshot step no longer exists  "), GENUINE[0][0])

    assert ops[0].ground == "the manual snapshot step no longer exists"
    assert isinstance(ops[0], DirectiveOp)


# --- the flow: one re-ask, then a decline ------------------------------------


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


def _judge(manager: ScopeManager, content: str = J4_207):
    return manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=SUMMARY,
        recent_contributions=[],
        new_contribution=_contribution(content),
    )


def test_a_judge_that_forgot_the_field_supplies_it_on_the_one_re_ask() -> None:
    contribution = GENUINE[0][0]
    client = MagicMock()
    client.messages.create.side_effect = [
        _response(_accept(_retire(None))),
        _response(
            _accept(_retire("the manual snapshot step the freeze protected no longer exists"))
        ),
    ]

    judgment = _judge(ScopeManager(client=client), contribution)

    assert client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_directive"
    assert judgment.retired_directive_ids == [FREEZE.id]
    assert judgment.new_summary is not None and judgment.new_summary.directives == []
    reask = client.messages.create.call_args_list[1].kwargs["messages"][-1]["content"]
    assert "ground" in str(reask)  # the re-ask names what is missing


def test_a_judge_that_still_has_no_ground_declines_after_exactly_one_re_ask() -> None:
    client = MagicMock()
    client.messages.create.side_effect = [
        _response(_accept(_retire(None))),
        _response(_accept(_retire("just remove it, no replacement needed"))),
    ]

    judgment = _judge(ScopeManager(client=client))

    assert client.messages.create.call_count == 2  # one re-ask, never a third call
    assert judgment.decision == "decline"
    assert judgment.new_summary is None
    assert judgment.retired_directive_ids == []
    reasoning = " ".join(judgment.reasoning.split())
    assert "ground" in reasoning  # names the missing ground ...
    assert "no changed circumstance" in reasoning  # ... and teaches the fix
    assert FREEZE.id in reasoning


def test_a_genuine_retirement_with_a_ground_retires_on_the_first_call() -> None:
    client = MagicMock()
    client.messages.create.side_effect = [
        _response(
            _accept(_retire("the manual snapshot step the freeze protected no longer exists"))
        )
    ]

    judgment = _judge(ScopeManager(client=client), GENUINE[0][0])

    assert client.messages.create.call_count == 1
    assert judgment.retired_directive_ids == [FREEZE.id]


def test_the_policy_is_threaded_to_the_validator() -> None:
    client = MagicMock()
    client.messages.create.side_effect = [
        _response(_accept(_retire("snapshots are now automatic"))),
    ]
    strict = RetireGroundPolicy(min_words=6, min_substantive=1, max_restatement=0.99)
    client.messages.create.side_effect = [
        _response(_accept(_retire("snapshots are now automatic"))),
        _response(_accept(_retire("snapshots are now automatic"))),
    ]

    judgment = _judge(ScopeManager(client=client, retire_ground_policy=strict), GENUINE[0][0])

    assert judgment.decision == "decline"


def test_the_batch_path_declines_the_member_whose_retire_has_no_ground() -> None:
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
    gate = sm._GroundGate(final=True)  # noqa: SLF001

    judgment = ScopeManager._parse_batch_judgment(  # noqa: SLF001
        scope=SCOPE,
        tool_use_block=_response(payload).content[0],
        current_summary=SUMMARY,
        contributions=contributions,
        ground_gate=gate,
    )

    verdicts = {v.contribution_id: v.decision for v in judgment.verdicts}
    assert verdicts == {"c_a": "decline", "c_b": "accept_as_context"}
    assert judgment.retired_directive_ids == []
    assert FREEZE.id in {
        d.id for d in (judgment.new_summary.directives if judgment.new_summary else [])
    }


# --- the record --------------------------------------------------------------


def test_the_ground_is_stored_on_the_retirement_event(tmp_path) -> None:
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
        ground="the manual snapshot step no longer exists",
    )

    assert event.ground == "the manual snapshot step no longer exists"
    assert store.list_retirements(scope_id="g_ops")[0].ground == event.ground


def test_a_retirement_without_a_ground_still_records_as_before(tmp_path) -> None:
    from strata.migrator import run_migrations
    from strata.record_store import RecordStore

    db = str(tmp_path / "s.db")
    run_migrations(db)

    event = RecordStore(db).append_retirement(
        scope_id="g_ops", directive_id="c_x", retired_by="operator", reason=None
    )

    assert event.ground is None


def test_the_operator_retire_path_never_goes_through_the_validator(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """operator_retire is the operator's sovereign correction: no ground, no judge,
    no validator."""
    import yaml

    from strata.fleet_config import FleetConfig
    from strata.migrator import run_migrations
    from strata.operator import operator_retire
    from strata.record_store import RecordStore
    from strata.summary_store import SummaryStore

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("the operator path must not touch the ground validator")

    monkeypatch.setattr(sm, "_parse_directive_ops", boom)
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
    store = RecordStore(db)

    event = operator_retire(
        "g_ops",
        FREEZE.id,
        "operator decision",
        fleet=FleetConfig.load(fleet_path),
        record_store=store,
        summary_store=summaries,
    )

    assert event.retired_by == "operator" and event.ground is None


def test_the_consoles_summary_retirements_carry_the_ground(tmp_path) -> None:
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
        ground="the manual snapshot step no longer exists",
    )

    with TestClient(create_app(settings=settings)) as client:
        body = client.get("/scopes/g_ops/summary").json()
        source = client.get("/ui/scope-detail.jsx").text

    assert body["retirements"][0]["ground"] == "the manual snapshot step no longer exists"
    assert "r.ground" in source  # the retired directive's line shows it


# --- settings ----------------------------------------------------------------


def test_the_policy_defaults_match_the_settings_and_env_overrides_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = RetireGroundPolicy()

    settings = Settings()
    assert settings.retire_ground_policy() == default
    monkeypatch.setenv("STRATA_RETIRE_GROUND_MIN_WORDS", "9")
    monkeypatch.setenv("STRATA_RETIRE_GROUND_MAX_RESTATEMENT", "0.5")
    tuned = Settings().retire_ground_policy()
    assert (tuned.min_words, tuned.max_restatement) == (9, 0.5)


def test_the_managers_the_app_builds_carry_the_policy() -> None:
    from strata.app import get_scope_manager

    settings = Settings(retire_ground_min_words=11, anthropic_api_key="k")

    manager = get_scope_manager(client=None, settings=settings)  # type: ignore[arg-type]

    assert manager._retire_ground_policy.min_words == 11  # noqa: SLF001
