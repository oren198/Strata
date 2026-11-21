"""#126 / ADR 0018 D4, D6, D8 and the #145 contract: the operator disposes of held items.

``strata.review`` has no scope manager parameter, so an approval can never call the judge; every
test below also patches ``ScopeManager.judge`` to raise.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.v121_126_support import NEW_RULE, TOKENS, World, directive_answer


@pytest.fixture
def world(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path, {"g_org": 1})
    yield w
    w.close()


@contextmanager
def _judge_must_not_run():  # noqa: ANN202
    with patch("strata.scope_manager.ScopeManager.judge", side_effect=AssertionError("judge ran")):
        yield


def _review():  # noqa: ANN202
    from strata import review  # noqa: PLC0415

    return review


# -- list ---------------------------------------------------------------------------------------


def test_list_shows_the_held_count_and_the_age_of_the_oldest(world: World) -> None:
    world.propose(directive_answer([{"op": "append"}]))
    world.propose(directive_answer([{"op": "retire", "id": "c_tokens"}]), content="Drop tokens.")
    listing = _review().list_held(record_store=world.store)
    assert listing.count == 2
    assert listing.oldest_age_seconds is not None and listing.oldest_age_seconds >= 0
    assert {item.scope_id for item in listing.items} == {"g_org"}


def test_list_on_nothing_held_says_zero(world: World) -> None:
    listing = _review().list_held(record_store=world.store)
    assert listing.count == 0 and listing.oldest_age_seconds is None


def test_the_cli_lists_the_held_count_and_the_oldest_age(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from strata.__main__ import main  # noqa: PLC0415
    from strata.settings import get_settings  # noqa: PLC0415

    world.propose(directive_answer([{"op": "append"}]))
    monkeypatch.chdir(world.tmp_path)
    monkeypatch.setenv("STRATA_FLEET_CONFIG", str(world.tmp_path / "fleet.yaml"))
    monkeypatch.setenv("STRATA_DB_PATH", str(world.tmp_path / "strata.db"))
    monkeypatch.setenv("STRATA_SUMMARIES_DIR", str(world.tmp_path / "summaries"))
    get_settings.cache_clear()
    assert main(["operator", "review", "list"]) == 0
    out = capsys.readouterr().out
    assert "1 held" in out and "oldest" in out


# -- approve ------------------------------------------------------------------------------------


def test_approving_an_append_applies_the_judges_ops_unchanged_as_the_scopes_own_directive(
    world: World,
) -> None:
    outcome = world.propose(directive_answer([{"op": "append"}]))
    with _judge_must_not_run():
        act = _review().approve(outcome.contribution_id, operator="oren", **world.kw())
    directive = next(d for d in world.summaries.read("g_org").directives if d.content == NEW_RULE)
    assert directive.source_scope_id == "g_org"  # the scope's own directive
    assert act.actor == "oren" and act.action == "approve" and act.directive_id == directive.id
    assert not any(
        NEW_RULE in str(i)
        for i in world.perspective("g_org")["layers"]
        if i.get("stratum_id") == "operator"
    )
    judgment = world.store.get_judgment(outcome.contribution_id)
    assert judgment.decision == "accept_as_directive" and judgment.disposition == "approved"
    state = world.store.get_record_entry(outcome.contribution_id).state
    assert state.accepted is True  # #145: a held accept_as_directive is not admitted until approved
    assert NEW_RULE in world.binding_contents("g_web")  # descendants now inherit it
    assert "approved" in str(world.perspective("g_org")) and directive.id in str(
        world.perspective("g_org")
    )


def test_approving_a_judged_retire_applies_it_and_notifies_readers(world: World) -> None:
    outcome = world.propose(
        directive_answer(
            [{"op": "retire", "id": "c_tokens", "changed_circumstance": "No longer so."}]
        )
    )
    assert world.store.list_retirements(scope_id="g_org") == []
    with _judge_must_not_run():
        _review().approve(outcome.contribution_id, operator="oren", **world.kw())
    assert "c_tokens" not in world.org_ids()
    (retirement,) = world.store.list_retirements(scope_id="g_org")
    assert retirement.directive_id == "c_tokens"
    assert TOKENS not in world.binding_contents("g_web")
    assert any(c["item_id"] == "c_tokens" for c in world.perspective("g_web")["input_changes"])


def test_approving_a_held_session_retirement_request_enacts_kind_and_notice_only_now(
    world: World,
) -> None:
    request = _review().request_retirement(
        "g_org",
        "c_tokens",
        "correction",
        "It was never right.",
        actor_session_id="sess_a",
        **world.kw(),
    )
    assert world.org_ids() == ["c_suite", "c_tokens"]
    assert world.perspective("g_web")["input_changes"] == []  # no notice at request time
    with _judge_must_not_run():
        act = _review().approve(request.id, operator="oren", **world.kw())
    assert "c_tokens" not in world.org_ids()
    (retirement,) = world.store.list_retirements(scope_id="g_org")
    assert retirement.kind == "correction" and retirement.notice == "It was never right."
    assert retirement.actor_session_id == "sess_a"  # the requester
    assert act.actor == "oren"  # the enactor
    changes = world.perspective("g_web")["input_changes"]
    assert [c["item_id"] for c in changes] == ["c_tokens"]
    assert "retracted" in str(changes[0])  # labelled by the recorded kind


# -- reject -------------------------------------------------------------------------------------


def test_rejecting_records_the_reason_and_nothing_enters(world: World) -> None:
    outcome = world.propose(directive_answer([{"op": "append"}]))
    before = world.summaries.read("g_org")
    with _judge_must_not_run():
        act = _review().reject(
            outcome.contribution_id, "Too broad for this scope.", operator="oren", **world.kw()
        )
    assert act.action == "reject" and act.reason == "Too broad for this scope."
    assert world.summaries.read("g_org") == before
    judgment = world.store.get_judgment(outcome.contribution_id)
    assert judgment.decision == "accept_as_directive" and judgment.disposition == "rejected"
    assert world.store.get_record_entry(outcome.contribution_id).state.accepted is False
    assert "rejected: Too broad for this scope." in str(world.perspective("g_org"))
    assert NEW_RULE not in world.binding_contents("g_web")


def test_a_reject_needs_a_reason(world: World) -> None:
    outcome = world.propose(directive_answer([{"op": "append"}]))
    with pytest.raises(ValueError, match="reason"):
        _review().reject(outcome.contribution_id, "  ", operator="oren", **world.kw())


# -- 6. the mechanical re-check before apply ------------------------------------------------------


def test_a_held_retire_request_lapses_when_its_directive_was_retired_meanwhile(
    world: World,
) -> None:
    request = _review().request_retirement(
        "g_org", "c_tokens", "currency", "Not needed.", actor_session_id="sess_a", **world.kw()
    )
    from strata.operator import operator_retire  # noqa: PLC0415

    operator_retire("g_org", "c_tokens", "in person", **world.kw())
    with _judge_must_not_run():
        _review().approve(request.id, operator="oren", **world.kw())
    (stored,) = world.store.list_retirement_requests(scope_id="g_org")
    assert stored.disposition == "lapsed"
    assert len(world.store.list_retirements(scope_id="g_org")) == 1  # only the operator's own


def test_a_held_retire_lapses_when_its_directive_was_superseded_meanwhile(world: World) -> None:
    outcome = world.propose(directive_answer([{"op": "retire", "id": "c_tokens"}]))
    world.replace_directive("c_tokens", "c_tokens_v2", "Tokens expire after 10 minutes.")
    with _judge_must_not_run():
        _review().approve(outcome.contribution_id, operator="oren", **world.kw())
    assert world.store.get_judgment(outcome.contribution_id).disposition == "lapsed"
    assert "Tokens expire after 10 minutes." in [
        d.content for d in world.summaries.read("g_org").directives
    ]


def test_a_held_supersede_whose_target_is_gone_is_refused_with_the_reason(world: World) -> None:
    outcome = world.propose(
        directive_answer([{"op": "supersede", "id": "c_tokens"}, {"op": "append"}])
    )
    from strata.operator import operator_retire  # noqa: PLC0415

    operator_retire("g_org", "c_tokens", "in person", **world.kw())
    with _judge_must_not_run(), pytest.raises(_review().ReviewRefusal, match="c_tokens"):
        _review().approve(outcome.contribution_id, operator="oren", **world.kw())
    assert NEW_RULE not in [d.content for d in world.summaries.read("g_org").directives]
    assert world.store.get_judgment(outcome.contribution_id).disposition == "held"  # still held


def test_a_held_append_that_now_conflicts_with_a_newly_inherited_directive_is_refused(
    tmp_path: Path,
) -> None:
    """g_web is the review scope. When the proposal was judged, g_org held no directive; by
    approval time g_org binds a rule the proposal contradicts (Concept 5: even in person)."""
    w = World(tmp_path, {"g_web": 1})
    try:
        w.summaries.write("g_org", w.summaries.read("g_org").model_copy(update={"directives": []}))
        outcome = w.propose(
            directive_answer([{"op": "append"}]),
            scope_id="g_web",
            content="Releases may be published without running the full test suite.",
        )
        w.summaries.write(
            "g_org",
            w.summaries.read("g_org").model_copy(
                update={
                    "directives": [
                        __import__("strata.summary_store", fromlist=["Directive"]).Directive(
                            id="c_suite",
                            content="Run the full test suite before publishing a release.",
                            source_scope_id="g_org",
                            created_at="2026-11-21T00:00:00+00:00",
                        )
                    ]
                }
            ),
        )
        with _judge_must_not_run(), pytest.raises(_review().ReviewRefusal, match="c_suite"):
            _review().approve(outcome.contribution_id, operator="oren", **w.kw())
        assert w.summaries.read("g_web").directives == []
        assert w.store.get_judgment(outcome.contribution_id).disposition == "held"
    finally:
        w.close()


# -- the #145 contract ---------------------------------------------------------------------------


def test_a_held_accept_as_directive_is_not_accepted_until_it_is_approved(world: World) -> None:
    import strata  # noqa: PLC0415

    outcome = world.propose(directive_answer([{"op": "append"}]))
    state = world.store.get_record_entry(outcome.contribution_id).state
    assert strata.verdict_admits(state.decision) is True  # the verdict alone admits
    assert state.accepted is False  # the contribution was not admitted
    _review().approve(outcome.contribution_id, operator="oren", **world.kw())
    assert world.store.get_record_entry(outcome.contribution_id).state.accepted is True
