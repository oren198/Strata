"""The retire act goes through the amendment intent journal (v1.22).

``forgetting._retire_locked`` is the one retire path of three callers: a bound session's own
``strata_retire`` (level 0), the date-lapse sweep, and the approval of a held ``rr_`` request
(level 1). Before, it wrote the retirement row, then the summary file, then the withdrawals and
the reader notice, with nothing to finish them after a death in between. Now an intent comes
first, the summary file second, and the rest is idempotent and replayed on recovery.

Each case kills the "process" just after a step (a BaseException nothing swallows), then recovers.
Expected, every time: c_tokens is gone, exactly one retirement row, exactly one notice per reader,
the intent is done, and a second recovery changes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from strata import forgetting, review
from strata import publication as publication_module
from strata.record_store import ContributorRef, RecordStore
from strata.summary_store import EndCondition, SummaryStore
from tests.test_v122_amendment_intents_crash import Crash, _events, _once, _recover
from tests.v121_126_support import World

NOTICE = "The tide table changed, so this no longer binds."
SESSION = ContributorRef(
    scope_id="g_org", skill=None, session_id="s_org", ts="2026-11-23T09:00:00Z"
)

#: where the death lands: each is "just after the step before it".
POINTS = [
    ("after_the_file_write", RecordStore, "append_retirement"),
    ("after_the_retirement_row", publication_module, "propagate_directive_removals"),
    ("after_the_withdrawals", forgetting, "emit_change_event"),
    ("after_the_notice", RecordStore, "list_retirement_candidates"),
    ("after_the_last_step", RecordStore, "mark_amendment_intent"),
]


@pytest.fixture
def world0(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path)
    yield w
    w.close()


@pytest.fixture
def world1(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path, {"g_org": 1})
    yield w
    w.close()


def _retire_as_a_session(w: World) -> None:
    forgetting.retire_directive("g_org", "c_tokens", "currency", NOTICE, SESSION, **w.kw())


def _make_c_tokens_due(w: World) -> None:
    summary = w.summaries.read("g_org")
    w.summaries.write(
        "g_org",
        summary.model_copy(
            update={
                "directives": [
                    d.model_copy(
                        update={"end_condition": EndCondition(kind="date", until="2026-11-09")}
                    )
                    if d.id == "c_tokens"
                    else d
                    for d in summary.directives
                ]
            }
        ),
    )


def _lapse(w: World) -> None:
    forgetting.sweep_date_lapses_locked(
        "g_org", now=datetime(2026, 11, 10, 12, tzinfo=UTC), **w.kw()
    )


def _assert_retired_exactly_once(w: World, cause: str) -> None:
    assert "c_tokens" not in w.org_ids()
    (retirement,) = w.store.list_retirements(scope_id="g_org")
    assert retirement.directive_id == "c_tokens" and retirement.cause == cause
    assert _events(w) == [
        ("g_api", "c_tokens", "directive_retired"),
        ("g_web", "c_tokens", "directive_retired"),
    ]
    (intent,) = w.store.list_amendment_intents(scope_id="g_org")
    assert intent.state == "done"
    assert _recover(w) == []  # a second recovery changes nothing
    assert len(w.store.list_retirements(scope_id="g_org")) == 1 and len(_events(w)) == 2


# -- 1. a bound session's own strata_retire (level 0) ----


@pytest.mark.parametrize(("case", "target", "name"), POINTS, ids=[p[0] for p in POINTS])
def test_a_session_retire_after_a_death_is_finished_by_recovery(
    world0: World,
    case: str,
    target,
    name: str,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    _once(target, name, monkeypatch)
    with pytest.raises(Crash):
        _retire_as_a_session(world0)
    (intent,) = world0.store.list_amendment_intents(scope_id="g_org")
    assert intent.state == "pending" and "c_tokens" not in world0.org_ids()
    assert _recover(world0) == [intent.id]
    _assert_retired_exactly_once(world0, "act")


# -- 2. the date-lapse sweep ----


@pytest.mark.parametrize(("case", "target", "name"), POINTS, ids=[p[0] for p in POINTS])
def test_a_date_lapse_after_a_death_is_finished_by_recovery(
    world0: World,
    case: str,
    target,
    name: str,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    _make_c_tokens_due(world0)
    _once(target, name, monkeypatch)
    with pytest.raises(Crash):
        _lapse(world0)
    (intent,) = world0.store.list_amendment_intents(scope_id="g_org")
    assert intent.state == "pending" and "c_tokens" not in world0.org_ids()
    assert _recover(world0) == [intent.id]
    _assert_retired_exactly_once(world0, "date_lapse")


# -- 3. the approval of a held rr_ request (level 1) ----


@pytest.mark.parametrize(("case", "target", "name"), POINTS, ids=[p[0] for p in POINTS])
def test_an_approved_request_after_a_death_is_finished_and_recorded_as_approved(
    world1: World,
    case: str,
    target,
    name: str,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    request = review.request_retirement(
        "g_org", "c_tokens", "currency", NOTICE, actor_session_id="s_org", **world1.kw()
    )
    assert "c_tokens" in world1.org_ids()  # held: it still binds
    _once(target, name, monkeypatch)
    with pytest.raises(Crash):
        review.approve(request.id, operator="oren", **world1.kw())

    act = review.approve(request.id, operator="oren", **world1.kw())  # recovers, then records

    assert act.action == "approve"  # never "lapse"
    (stored,) = world1.store.list_retirement_requests(scope_id="g_org")
    assert stored.disposition == "approved"
    _assert_retired_exactly_once(world1, "act")


# -- a death before the file write owes nothing ----


def test_a_death_before_the_file_write_abandons_the_intent(
    world0: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _once(SummaryStore, "write", monkeypatch)
    with pytest.raises(Crash):
        _retire_as_a_session(world0)
    (intent,) = world0.store.list_amendment_intents(scope_id="g_org")
    assert _recover(world0) == []
    assert world0.store.get_amendment_intent(intent.id).state == "abandoned"
    assert "c_tokens" in world0.org_ids()
    assert world0.store.list_retirements(scope_id="g_org") == [] and _events(world0) == []
    _retire_as_a_session(world0)  # and the retire can simply be made again
    assert "c_tokens" not in world0.org_ids()


def test_the_payload_holds_only_what_the_replay_needs(
    world0: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json  # noqa: PLC0415

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-not-in-the-journal")
    _once(RecordStore, "append_retirement", monkeypatch)
    with pytest.raises(Crash):
        _retire_as_a_session(world0)
    (intent,) = world0.store.list_amendment_intents(scope_id="g_org")
    payload = json.loads(intent.payload_json)
    assert payload["type"] == "retire_act" and payload["directive_id"] == "c_tokens"
    assert payload["notice"] == NOTICE
    assert "sk-secret" not in intent.payload_json
