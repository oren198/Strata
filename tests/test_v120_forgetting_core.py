"""v1.20 forgetting core: the retire act, the date-lapse sweep, the owner's confirmation of a
judged end condition, candidates, and the readers' notice.

The fleet is the #243 test world: g_org owns two directives, g_web and g_api read from it."""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from strata import forgetting
from strata.forgetting import (
    ENGINE_ACTOR,
    PositionGateRefusal,
    compute_retirement_candidates,
    confirm_judged_end_condition,
    dismiss_candidate,
    retire_directive,
    sweep_date_lapses,
    visible_candidates,
)
from strata.perspective import compose_perspective
from strata.record_store import ContributorRef
from strata.scope_manager import ScopeManager
from strata.summary_store import Directive, EndCondition, ScopeSummary
from tests import test_v18_243_publication_exception as t243
from tests.test_scope_manager import SCOPE, STRATUM, _fake_response

world = t243.world

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
OWNER = ContributorRef(scope_id="g_org", skill=None, session_id="s_org", ts="t")
READER = ContributorRef(scope_id="g_web", skill=None, session_id="s_web", ts="t")
NOTICE = "The test-suite rule was replaced by the release checklist."


def _kw(world):  # noqa: ANN001, ANN202
    fleet, store, summaries = world
    return {"fleet": fleet, "record_store": store, "summary_store": summaries}


def _org_ids(world) -> list[str]:  # noqa: ANN001
    return [d.id for d in world[2].read("g_org").directives]


def _set_end(world, directive_id: str, end: EndCondition, set_by: str = "c_setter") -> None:  # noqa: ANN001
    summaries = world[2]
    summary = summaries.read("g_org")
    summary = summary.model_copy(
        update={
            "directives": [
                d.model_copy(update={"end_condition": end, "set_by": set_by})
                if d.id == directive_id
                else d
                for d in summary.directives
            ]
        }
    )
    summaries.write("g_org", summary)


def _perspective(world, scope_id: str) -> dict:  # noqa: ANN001
    fleet, store, summaries = world
    return compose_perspective(
        scope_id,
        fleet=fleet,
        summary_store=summaries,
        change_event_reader=lambda s: store.list_change_events(scope_id=s),
    )


def _binding_ids(perspective: dict) -> set[str]:
    return {
        d["id"] if isinstance(d, dict) else d.id
        for layer in perspective["layers"]
        if layer.get("binding")
        for d in layer.get("directives") or []
    }


# -- the retire act ---------------------------------------------------------------------


def test_the_public_act_takes_no_cause_end_condition_or_evidence() -> None:
    params = set(inspect.signature(retire_directive).parameters)
    assert params == {
        "scope_id",
        "directive_id",
        "kind",
        "notice",
        "actor",
        "fleet",
        "record_store",
        "summary_store",
    }


def test_a_retire_removes_the_directive_records_it_and_creates_nothing(world) -> None:  # noqa: ANN001
    before = set(_org_ids(world))
    retirement = retire_directive("g_org", "c_suite", "currency", NOTICE, OWNER, **_kw(world))
    assert set(_org_ids(world)) == before - {"c_suite"}
    assert retirement.kind == "currency" and retirement.cause == "act"
    assert retirement.notice == NOTICE and retirement.actor_session_id == "s_org"
    (row,) = world[1].list_retirements(scope_id="g_org")
    assert row.id == retirement.id


@pytest.mark.parametrize(
    "actor",
    [
        READER,
        ContributorRef(scope_id="g_org", skill=None, session_id=ENGINE_ACTOR, ts="t"),
        ContributorRef(scope_id=ENGINE_ACTOR, skill=None, session_id=ENGINE_ACTOR, ts="t"),
    ],
)
def test_a_non_owner_or_the_engine_actor_is_refused_and_nothing_changes(world, actor) -> None:  # noqa: ANN001
    before = world[2].read("g_org")
    with pytest.raises(PositionGateRefusal):
        retire_directive("g_org", "c_suite", "currency", NOTICE, actor, **_kw(world))
    assert world[2].read("g_org") == before
    assert world[1].list_retirements(scope_id="g_org") == []


def test_a_blank_notice_an_unknown_kind_and_an_unknown_directive_are_refused(world) -> None:  # noqa: ANN001
    with pytest.raises(ValueError):
        retire_directive("g_org", "c_suite", "currency", "  ", OWNER, **_kw(world))
    with pytest.raises(ValueError):
        retire_directive("g_org", "c_suite", "forgotten", NOTICE, OWNER, **_kw(world))
    with pytest.raises(KeyError):
        retire_directive("g_org", "c_nope", "currency", NOTICE, OWNER, **_kw(world))


def test_r1_no_delay_and_every_reader_is_notified(world) -> None:  # noqa: ANN001
    assert "c_suite" in _binding_ids(_perspective(world, "g_web"))
    retire_directive("g_org", "c_suite", "currency", NOTICE, OWNER, **_kw(world))
    for reader in ("g_web", "g_api"):
        perspective = _perspective(world, reader)
        assert "c_suite" not in _binding_ids(perspective)
        (change,) = [c for c in perspective["input_changes"] if c["item_id"] == "c_suite"]
        assert change["kind"] == "directive_retired"
        assert change["retirement"]["label"] == "retired"
        assert change["retirement"]["notice"] == NOTICE


def test_a_correction_is_labelled_retracted_and_otherwise_the_same_event(world) -> None:  # noqa: ANN001
    retire_directive("g_org", "c_suite", "correction", NOTICE, OWNER, **_kw(world))
    retire_directive("g_org", "c_tokens", "currency", NOTICE, OWNER, **_kw(world))
    perspective = _perspective(world, "g_web")
    by_item = {c["item_id"]: c for c in perspective["input_changes"]}
    assert by_item["c_suite"]["retirement"]["label"] == "retracted"
    assert by_item["c_tokens"]["retirement"]["label"] == "retired"
    # R3: one path, two labels. Same event kind, same audience, same shape.
    assert by_item["c_suite"]["kind"] == by_item["c_tokens"]["kind"] == "directive_retired"

    def strip(c: dict) -> dict:
        skip = ("retirement", "item_id", "change_id", "created_at", "contribution_id", "before")
        return {k: v for k, v in c.items() if k not in skip}

    assert strip(by_item["c_suite"]) == strip(by_item["c_tokens"])


def test_r3_release_and_retraction_reach_the_same_scopes(world) -> None:  # noqa: ANN001
    retire_directive("g_org", "c_suite", "currency", NOTICE, OWNER, **_kw(world))
    retire_directive("g_org", "c_tokens", "correction", NOTICE, OWNER, **_kw(world))
    store = world[1]
    audience = {
        item: sorted(
            e.scope_id
            for scope in ("g_org", "g_web", "g_api")
            for e in store.list_change_events(scope_id=scope)
            if e.item_id == item
        )
        for item in ("c_suite", "c_tokens")
    }
    assert audience["c_suite"] == audience["c_tokens"] != []


def test_f2_a_retire_does_not_touch_a_readers_context_or_directives(world) -> None:  # noqa: ANN001
    before = {s: world[2].read(s) for s in ("g_web", "g_api")}
    retire_directive("g_org", "c_suite", "currency", NOTICE, OWNER, **_kw(world))
    assert {s: world[2].read(s) for s in ("g_web", "g_api")} == before


def test_the_notice_has_no_clock_it_stays_until_the_readers_own_pass(world) -> None:  # noqa: ANN001
    retire_directive("g_org", "c_suite", "currency", NOTICE, OWNER, **_kw(world))
    store = world[1]
    web_event = next(
        e for e in store.list_change_events(scope_id="g_web") if e.item_id == "c_suite"
    )
    assert any(c["item_id"] == "c_suite" for c in _perspective(world, "g_web")["input_changes"])
    store.mark_change_event_processed(web_event.id)
    assert not any(c["item_id"] == "c_suite" for c in _perspective(world, "g_web")["input_changes"])
    # the other reader has not processed its own: its notice is still there
    assert any(c["item_id"] == "c_suite" for c in _perspective(world, "g_api")["input_changes"])


# -- the date-lapse sweep -----------------------------------------------------------------


def test_a_date_lapse_retires_by_mechanism_and_is_idempotent(world) -> None:  # noqa: ANN001
    _set_end(world, "c_suite", EndCondition(kind="date", until="2026-11-09"))
    first = sweep_date_lapses("g_org", now=NOW, **_kw(world))
    assert [r.directive_id for r in first] == ["c_suite"]
    assert first[0].cause == "date_lapse" and first[0].actor_session_id == ENGINE_ACTOR
    assert first[0].kind == "currency" and "bound until 2026-11-09" in first[0].notice
    assert "c_suite" not in _perspective(world, "g_web") and "c_suite" not in _binding_ids(
        _perspective(world, "g_web")
    )
    assert sweep_date_lapses("g_org", now=NOW, **_kw(world)) == []
    assert len(world[1].list_retirements(scope_id="g_org")) == 1


def test_until_is_inclusive_through_the_day_and_nothing_lapses_without_a_date(world) -> None:  # noqa: ANN001
    _set_end(world, "c_suite", EndCondition(kind="date", until="2026-11-10"))
    _set_end(world, "c_tokens", EndCondition(kind="judged", condition="the audit closes"))
    assert sweep_date_lapses("g_org", now=NOW, **_kw(world)) == []
    late = datetime(2026, 11, 10, 23, 59, 59, tzinfo=UTC)
    assert sweep_date_lapses("g_org", now=late, **_kw(world)) == []
    assert [
        r.directive_id
        for r in sweep_date_lapses("g_org", now=late + timedelta(seconds=1), **_kw(world))
    ] == ["c_suite"]
    assert "c_tokens" in _org_ids(world)


def test_the_sweep_runs_inside_a_judgment_without_deadlocking_on_the_scope_lock(world) -> None:  # noqa: ANN001
    from strata.app import _judge_and_record  # noqa: PLC0415, F401 — imported to prove the symbol
    from strata.locks import scope_lock  # noqa: PLC0415

    _set_end(world, "c_suite", EndCondition(kind="date", until="2026-01-01"))
    with scope_lock("g_org"):  # a judgment holds this; the inner variant must not retake it
        done = forgetting.sweep_date_lapses_locked("g_org", **_kw(world))
    assert [r.directive_id for r in done] == ["c_suite"]


# -- R2: only the owner confirms a judged end condition -----------------------------------


def _fulfils_contribution(world, scope_id: str, session: str = "s_org"):  # noqa: ANN001, ANN202
    store = world[1]
    return store.append_contribution(
        scope_id=scope_id,
        content="The audit closed on 9 November.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(scope_id=scope_id, skill=None, session_id=session, ts="t"),
        fulfils="c_suite",
    )


def _confirm(world, contribution, evidence, judging="g_org"):  # noqa: ANN001, ANN202
    return confirm_judged_end_condition(
        "g_org",
        "c_suite",
        contribution,
        evidence,
        judging_scope_id=judging,
        **_kw(world),
    )


def test_the_owners_judge_retires_a_judged_condition_with_the_honest_reason(world) -> None:  # noqa: ANN001
    _set_end(world, "c_suite", EndCondition(kind="judged", condition="the audit closes"))
    contribution = _fulfils_contribution(world, "g_org")
    retirement = _confirm(world, contribution, "The note says the audit closed on 9 November.")
    assert retirement is not None
    assert retirement.cause == "condition_judged" and retirement.kind == "currency"
    assert retirement.notice == (
        'end condition judged met: "the audit closes" / '
        "The note says the audit closed on 9 November."
    )
    assert "verified" not in retirement.notice
    assert retirement.evidence_contribution_id == contribution.id
    assert "c_suite" not in _org_ids(world)


@pytest.mark.parametrize(
    "case",
    ["other_scope_contribution", "other_judging_scope", "no_fulfils", "verified", "blank"],
)
def test_nobody_else_retires_a_judged_condition(world, case) -> None:  # noqa: ANN001
    _set_end(world, "c_suite", EndCondition(kind="judged", condition="the audit closes"))
    contribution = _fulfils_contribution(
        world, "g_web" if case == "other_scope_contribution" else "g_org", "s_x"
    )
    evidence = "The note says it closed."
    judging = "g_org"
    if case == "other_judging_scope":
        judging = "g_web"
    if case == "no_fulfils":
        contribution = replace(contribution, fulfils=None)
    if case == "verified":
        evidence = "I verified it closed."
    if case == "blank":
        evidence = "  "
    assert _confirm(world, contribution, evidence, judging) is None
    assert "c_suite" in _org_ids(world)
    assert world[1].list_retirements(scope_id="g_org") == []


def test_fulfils_judged_elsewhere_opens_a_candidate_never_a_retirement(world) -> None:  # noqa: ANN001
    _set_end(world, "c_suite", EndCondition(kind="judged", condition="the audit closes"))
    contribution = _fulfils_contribution(world, "g_web", "s_web")
    assert _confirm(world, contribution, "It closed.", judging="g_web") is None
    (candidate,) = world[1].list_retirement_candidates(scope_id="g_org")
    assert candidate.ground == "condition_evidence" and candidate.directive_id == "c_suite"
    assert contribution.id in candidate.evidence_ids


def test_a_directive_without_a_judged_condition_is_never_confirmed(world) -> None:  # noqa: ANN001
    contribution = _fulfils_contribution(world, "g_org")
    assert _confirm(world, contribution, "It closed.") is None
    assert "c_suite" in _org_ids(world)


# -- (f) a plain judged retire cannot bypass R2 --------------------------------------------

_JUDGED_SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[
        Directive(
            id="c_cond",
            content="Freeze deploys.",
            source_scope_id=SCOPE.id,
            created_at="2026-10-01T09:00:00+00:00",
            end_condition=EndCondition(kind="judged", condition="the audit closes"),
            set_by="c_cond",
        ),
        Directive(
            id="c_plain",
            content="Tag releases.",
            source_scope_id=SCOPE.id,
            created_at="2026-10-01T09:00:00+00:00",
        ),
    ],
    context="ctx",
    updated_at="2026-10-01T00:00:00+00:00",
)


def _judge_retire(target: str, *, fulfils: str | None, evidence: str):  # noqa: ANN202
    from tests.test_scope_manager import NEW_CONTRIBUTION  # noqa: PLC0415

    client = MagicMock()
    client.messages.create.return_value = _fake_response(
        {
            "decision": "accept_as_directive",
            "reasoning": "The condition holds.",
            "directive_ops": [{"op": "retire", "id": target, "changed_circumstance": evidence}],
            "new_context": None,
        }
    )
    contribution = replace(NEW_CONTRIBUTION, proposed_classification="directive", fulfils=fulfils)
    return ScopeManager(client=client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=_JUDGED_SUMMARY,
        recent_contributions=[],
        new_contribution=contribution,
    )


def test_a_plain_judged_retire_of_a_judged_condition_is_dropped() -> None:
    judgment = _judge_retire("c_cond", fulfils=None, evidence="it seems over")
    assert judgment.directive_ops == []
    assert "c_cond" in [d.id for d in judgment.new_summary.directives]
    assert any("retire of c_cond refused" in n for n in judgment.protocol_notes)


def test_a_judged_retire_with_fulfils_and_evidence_stands_with_the_engines_reason() -> None:
    judgment = _judge_retire(
        "c_cond", fulfils="c_cond", evidence="The note reports the audit closed."
    )
    assert [op.op for op in judgment.directive_ops] == ["retire"]
    assert judgment.directive_ops[0].changed_circumstance == (
        'end condition judged met: "the audit closes" / The note reports the audit closed.'
    )
    assert "c_cond" not in [d.id for d in judgment.new_summary.directives]


def test_a_judged_retire_claiming_verification_is_dropped() -> None:
    judgment = _judge_retire("c_cond", fulfils="c_cond", evidence="I verified the audit closed.")
    assert judgment.directive_ops == []


def test_a_plain_retire_of_an_ordinary_directive_keeps_todays_path() -> None:
    judgment = _judge_retire("c_plain", fulfils=None, evidence="no longer true")
    assert [op.op for op in judgment.directive_ops] == ["retire"]
    assert judgment.directive_ops[0].changed_circumstance == "no longer true"


# -- candidates: prompts, never retirements ------------------------------------------------


def _backdate(world, directive_id: str, days: int) -> None:  # noqa: ANN001
    summaries = world[2]
    summary = summaries.read("g_org")
    stamp = (NOW - timedelta(days=days)).isoformat()
    summaries.write(
        "g_org",
        summary.model_copy(
            update={
                "directives": [
                    d.model_copy(update={"created_at": stamp}) if d.id == directive_id else d
                    for d in summary.directives
                ]
            }
        ),
    )


def test_age_and_disuse_open_candidates_and_retire_nothing(world) -> None:  # noqa: ANN001
    _backdate(world, "c_suite", 120)
    before = world[2].read("g_org")
    opened = compute_retirement_candidates(
        "g_org", record_store=world[1], summary_store=world[2], now=NOW, force=True
    )
    grounds = {(c.directive_id, c.ground) for c in opened}
    assert ("c_suite", "age") in grounds and ("c_suite", "disuse") in grounds
    assert world[2].read("g_org") == before
    assert world[1].list_retirements(scope_id="g_org") == []
    # recomputing never duplicates an open candidate
    again = compute_retirement_candidates(
        "g_org", record_store=world[1], summary_store=world[2], now=NOW, force=True
    )
    assert [c for c in again if c.directive_id == "c_suite"] == []


def test_a_date_end_within_a_week_opens_date_near_and_the_lapse_still_happens(world) -> None:  # noqa: ANN001
    _set_end(world, "c_suite", EndCondition(kind="date", until="2026-11-14"))
    opened = compute_retirement_candidates(
        "g_org", record_store=world[1], summary_store=world[2], now=NOW, force=True
    )
    assert ("c_suite", "date_near") in {(c.directive_id, c.ground) for c in opened}
    assert "c_suite" in _org_ids(world)


def test_candidates_are_shown_to_the_owning_scope_only(world) -> None:  # noqa: ANN001
    _backdate(world, "c_suite", 120)
    compute_retirement_candidates(
        "g_org", record_store=world[1], summary_store=world[2], now=NOW, force=True
    )
    assert visible_candidates("g_org", OWNER, record_store=world[1]) != []
    assert visible_candidates("g_org", READER, record_store=world[1]) == []


def test_dismiss_snoozes_and_changes_nothing_else(world) -> None:  # noqa: ANN001
    _backdate(world, "c_suite", 120)
    compute_retirement_candidates(
        "g_org", record_store=world[1], summary_store=world[2], now=NOW, force=True
    )
    store = world[1]
    candidate = next(
        c for c in store.list_retirement_candidates(scope_id="g_org") if c.ground == "age"
    )
    with pytest.raises(PositionGateRefusal):
        dismiss_candidate(candidate.id, reason="keep", days=30, actor=READER, record_store=store)
    with pytest.raises(ValueError):
        dismiss_candidate(candidate.id, reason=" ", days=30, actor=OWNER, record_store=store)
    before = world[2].read("g_org")
    dismiss_candidate(candidate.id, reason="still wanted", days=30, actor=OWNER, record_store=store)
    assert world[2].read("g_org") == before
    row = store.get_retirement_candidate(candidate.id)
    assert row.state == "dismissed" and row.note == "still wanted"
    # while snoozed (real clock: +30 days from now), a recompute does not reopen it
    compute_retirement_candidates("g_org", record_store=store, summary_store=world[2], force=True)
    assert [
        c for c in store.list_retirement_candidates(scope_id="g_org") if c.ground == "age"
    ] == []


def test_retiring_marks_its_open_candidates_acted(world) -> None:  # noqa: ANN001
    _backdate(world, "c_suite", 120)
    compute_retirement_candidates(
        "g_org", record_store=world[1], summary_store=world[2], now=NOW, force=True
    )
    retire_directive("g_org", "c_suite", "currency", NOTICE, OWNER, **_kw(world))
    assert [
        c
        for c in world[1].list_retirement_candidates(scope_id="g_org")
        if c.directive_id == "c_suite"
    ] == []
    acted = world[1].list_retirement_candidates(scope_id="g_org", states=("acted",))
    assert {c.directive_id for c in acted} == {"c_suite"}


# -- ends / fulfils at the write boundary ---------------------------------------------------


def test_a_contribution_round_trips_ends_and_fulfils_through_the_record(world) -> None:  # noqa: ANN001
    c = world[1].append_contribution(
        scope_id="g_org",
        content="Freeze deploys.",
        proposed_classification="directive",
        subject=None,
        supersedes=None,
        contributor=OWNER,
        ends='{"kind":"date","until":"2026-12-01","condition":null}',
        fulfils="c_suite",
    )
    again = world[1].get_contribution(c.id)
    assert again.ends == c.ends and again.fulfils == "c_suite"


def test_a_directive_is_minted_with_the_end_condition_its_contribution_carries() -> None:
    from strata.scope_manager import DirectiveOp, _mint_directive  # noqa: PLC0415
    from tests.test_scope_manager import NEW_CONTRIBUTION  # noqa: PLC0415

    contribution = replace(
        NEW_CONTRIBUTION, ends='{"kind":"date","until":"2026-12-01","condition":null}'
    )
    minted = _mint_directive(DirectiveOp(op="append"), contribution)
    assert minted.end_condition == EndCondition(kind="date", until="2026-12-01")
    assert minted.set_by == contribution.id
    plain = _mint_directive(DirectiveOp(op="append"), NEW_CONTRIBUTION)
    assert plain.end_condition is None and plain.set_by is None


# -- reads, the judge's view, and the write boundary ---------------------------------------


def test_a_descendants_read_lapses_an_ancestors_over_date_and_opens_its_own_candidates(
    world,
) -> None:  # noqa: ANN001
    _set_end(world, "c_suite", EndCondition(kind="date", until="2026-11-09"))
    done = forgetting.sweep_for_read("g_web", now=NOW, **_kw(world))
    assert [r.directive_id for r in done] == ["c_suite"]
    assert "c_suite" not in _binding_ids(_perspective(world, "g_web"))
    assert any(c["item_id"] == "c_suite" for c in _perspective(world, "g_web")["input_changes"])
    assert forgetting.sweep_for_read("g_web", now=NOW, **_kw(world)) == []


def test_the_judge_sees_the_end_condition_the_fulfils_and_the_retire_not_publish_line() -> None:
    from strata.scope_manager import _render_contribution_block  # noqa: PLC0415
    from tests.test_scope_manager import NEW_CONTRIBUTION  # noqa: PLC0415

    plain = _render_contribution_block(NEW_CONTRIBUTION)
    assert "binds" not in plain and "fulfils" not in plain and "retire" not in plain
    both = replace(
        NEW_CONTRIBUTION,
        ends='{"kind":"judged","until":null,"condition":"the audit closes"}',
        fulfils="c_x",
        supersedes="c_y",
    )
    text = _render_contribution_block(both)
    assert "- binds until: the audit closes\n" in text
    assert "- fulfils: c_x\n" in text
    assert "answer `retire`, not `publish`" in text


def test_the_directive_block_shown_to_a_judge_carries_the_ends_line() -> None:
    from strata.scope_manager import _render_directives_only  # noqa: PLC0415

    d = Directive(
        id="c_1",
        content="Freeze deploys.",
        source_scope_id="g_a",
        created_at="2026-10-01T09:00:00+00:00",
        end_condition=EndCondition(kind="date", until="2026-12-01"),
        set_by="c_1",
    )
    assert "- ends: until=2026-12-01 · set_by=c_1" in _render_directives_only([d])
    assert "ends:" not in _render_directives_only([replace_ends(d)])


def replace_ends(d: Directive) -> Directive:
    return d.model_copy(update={"end_condition": None, "set_by": None})


def test_ends_and_fulfils_are_checked_before_anything_is_appended(world) -> None:  # noqa: ANN001
    from strata.app import run_contribution  # noqa: PLC0415
    from strata.fleet_config import Stratum  # noqa: PLC0415

    fleet, store, summaries = world
    scope = fleet.get_scope("g_org")
    base = dict(
        scope=scope,
        stratum=MagicMock(spec=Stratum),
        content="Freeze deploys.",
        subject=None,
        supersedes=None,
        contributor=OWNER,
        fleet=fleet,
        record_store=store,
        summary_store=summaries,
        scope_manager=MagicMock(),
        summary_max_words=500,
    )
    with pytest.raises(ValueError, match="past"):
        run_contribution(proposed_classification="directive", ends={"until": "2020-01-01"}, **base)
    with pytest.raises(ValueError, match="only meaningful"):
        run_contribution(proposed_classification="context", ends={"until": "2999-01-01"}, **base)
    with pytest.raises(ValueError):
        run_contribution(proposed_classification="context", fulfils="c_suite", **base)
    assert store.list_contributions(scope_id="g_org") == []


def test_a_retirement_is_found_through_its_directive_not_through_the_event(world) -> None:  # noqa: ANN001
    store = world[1]
    assert store.retirement_for_directive("c_suite") is None
    retirement = retire_directive("g_org", "c_suite", "correction", NOTICE, OWNER, **_kw(world))
    assert store.retirement_for_directive("c_suite").id == retirement.id
    assert store.retirement_for_directive("c_suite", scope_id="g_web") is None
    assert "retirement_id" not in {
        row[1] for row in store._conn.execute("pragma table_info(change_events)")
    }
    # an operator correction (no kind) keeps its event shape: no retirement object
    store.append_retirement(
        scope_id="g_org", directive_id="c_tokens", retired_by="operator", reason="gone"
    )
    plain = next(e for e in store.list_change_events(scope_id="g_web") if e.item_id == "c_suite")
    assert plain.retirement.label == "retracted"
