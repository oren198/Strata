"""A process death between the summary file write and the after-write steps no longer loses them.

Each case lets the amendment write its intent, then "kills the process" (a BaseException that no
``except Exception`` swallows) just after a step. Recovery (``recover_amendment_intents``, which the
drain and the server start run under the scope lock) then finishes the work. Expected, every time:
the directive is retired once, the readers are told once, the judgment row is stamped, the intent is
done, and a second recovery does nothing. A death before the file write abandons the intent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strata import app as app_module
from strata import review
from strata.app import recover_amendment_intents
from strata.locks import scope_lock
from strata.record_store import RecordStore
from tests.v121_126_support import World, directive_answer


class Crash(BaseException):  # noqa: N818 — a process death, not an error
    pass


def _once(target, name: str, monkeypatch: pytest.MonkeyPatch) -> dict:  # noqa: ANN001
    """Make ``target.name`` raise Crash on its first call, then behave."""
    real = getattr(target, name)
    state = {"calls": 0}

    def flaky(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        state["calls"] += 1
        if state["calls"] == 1:
            raise Crash(name)
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, flaky)
    return state


#: (case, where the death lands). Each is "after the step before it".
CASES = [
    ("after_the_file_write", RecordStore, "stamp_summary_version"),
    ("after_the_stamp", app_module, "_record_lineage"),
    ("after_the_diagnostics", RecordStore, "append_retirement"),
    ("after_the_retirement_row", app_module, "propagate_directive_removals"),
    ("after_the_withdrawals", app_module, "_emit_directive_set_change_ids"),
    ("after_the_events", RecordStore, "mark_amendment_intent"),
]


def _events(w: World) -> list[tuple[str, str, str]]:
    """The notices readers were sent about c_tokens: (reader scope, item, kind)."""
    rows = []
    for scope_id in ("g_web", "g_api"):
        for e in w.store.list_change_events(scope_id=scope_id):
            if e.item_id == "c_tokens":
                rows.append((scope_id, e.item_id, e.kind))
    return sorted(rows)


def _recover(w: World) -> list[str]:
    with scope_lock("g_org"):
        return recover_amendment_intents("g_org", **w.kw())


def _assert_finished_exactly_once(w: World, contribution_id: str) -> None:
    assert "c_tokens" not in w.org_ids()
    assert len(w.store.list_retirements(scope_id="g_org")) == 1
    assert _events(w) == [
        ("g_api", "c_tokens", "directive_retired"),
        ("g_web", "c_tokens", "directive_retired"),
    ]
    assert w.store.get_judgment(contribution_id).summary_version is not None  # stamped
    (intent,) = w.store.list_amendment_intents(scope_id="g_org")
    assert intent.state == "done"
    # a second recovery, and a replay of a done intent, change nothing
    assert _recover(w) == []
    assert len(w.store.list_retirements(scope_id="g_org")) == 1 and len(_events(w)) == 2


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


RETIRE = directive_answer([{"op": "retire", "id": "c_tokens", "changed_circumstance": "No more."}])


@pytest.mark.parametrize(("case", "target", "name"), CASES, ids=[c[0] for c in CASES])
def test_level_zero_a_death_after_the_step_is_finished_by_recovery(
    world0: World,
    case: str,
    target,
    name: str,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    _once(target, name, monkeypatch)
    with pytest.raises(Crash):
        world0.propose(RETIRE)
    contribution = next(
        c for c in world0.store.list_contributions(scope_id="g_org") if c.subject is None
    )
    (intent,) = world0.store.list_amendment_intents(scope_id="g_org")
    assert intent.state == "pending"
    assert "c_tokens" not in world0.org_ids()  # the file was written; readers already see it

    assert _recover(world0) == [intent.id]
    _assert_finished_exactly_once(world0, contribution.id)


@pytest.mark.parametrize(("case", "target", "name"), CASES, ids=[c[0] for c in CASES])
def test_level_one_a_death_after_the_step_is_finished_and_the_next_approve_is_recorded(
    world1: World,
    case: str,
    target,
    name: str,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    held = world1.propose(RETIRE)
    assert "c_tokens" in world1.org_ids()  # held, nothing applied
    _once(target, name, monkeypatch)
    with pytest.raises(Crash):
        review.approve(held.contribution_id, operator="oren", **world1.kw())
    assert world1.store.get_judgment(held.contribution_id).disposition == "held"

    # the next approve runs recovery under its own lock, finds the intent done, and records it
    act = review.approve(held.contribution_id, operator="oren", **world1.kw())

    assert act.action == "approve"
    assert world1.store.get_judgment(held.contribution_id).disposition == "approved"
    _assert_finished_exactly_once(world1, held.contribution_id)


def test_a_death_before_the_file_write_abandons_the_intent_and_owes_nothing(
    world0: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from strata.summary_store import SummaryStore  # noqa: PLC0415

    _once(SummaryStore, "write", monkeypatch)
    with pytest.raises(Crash):
        world0.propose(RETIRE)
    before = world0.summaries.read("g_org")
    (intent,) = world0.store.list_amendment_intents(scope_id="g_org")
    assert intent.state == "pending" and "c_tokens" in world0.org_ids()

    assert _recover(world0) == []  # nothing to replay
    assert world0.store.get_amendment_intent(intent.id).state == "abandoned"
    assert world0.summaries.read("g_org") == before
    assert world0.store.list_retirements(scope_id="g_org") == [] and _events(world0) == []


def test_an_intent_from_the_future_is_not_replayed_and_is_marked_suspect(world0: World) -> None:
    intent_id = world0.store.create_amendment_intent(
        scope_id="g_org", expected_version=-1, payload_json="{}"
    )
    assert _recover(world0) == []
    assert world0.store.get_amendment_intent(intent_id).state == "suspect"


# -- recovery runs at drain and at server start, and never waits on a lock --------------------


def _crash_a_retire(w: World, monkeypatch: pytest.MonkeyPatch) -> str:
    _once(RecordStore, "stamp_summary_version", monkeypatch)
    with pytest.raises(Crash):
        w.propose(RETIRE)
    (intent,) = w.store.list_amendment_intents(scope_id="g_org")
    assert intent.state == "pending"
    return intent.id


def test_the_drain_finishes_a_pending_intent_before_anything_else(
    world0: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent_id = _crash_a_retire(world0, monkeypatch)
    app_module.drain_scope("g_org", scope_manager=None, summary_max_words=500, **world0.kw())
    assert world0.store.get_amendment_intent(intent_id).state == "done"
    assert len(world0.store.list_retirements(scope_id="g_org")) == 1


def test_a_drain_that_cannot_get_the_lock_defers_the_replay_and_does_not_wait(
    world0: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading  # noqa: PLC0415
    import time  # noqa: PLC0415

    intent_id = _crash_a_retire(world0, monkeypatch)
    held, release = threading.Event(), threading.Event()

    def hold_the_lock() -> None:
        with scope_lock("g_org"):
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold_the_lock)
    holder.start()
    assert held.wait(5)
    try:
        start = time.monotonic()
        outcome = app_module.drain_scope(
            "g_org", scope_manager=None, summary_max_words=500, lock_timeout=0.2, **world0.kw()
        )
        assert time.monotonic() - start < 2.0  # bounded: it gave up instead of waiting
        assert outcome.judged is False
        assert world0.store.get_amendment_intent(intent_id).state == "pending"  # deferred
    finally:
        release.set()
        holder.join(10)
    app_module.drain_scope("g_org", scope_manager=None, summary_max_words=500, **world0.kw())
    assert world0.store.get_amendment_intent(intent_id).state == "done"  # replayed next time


def test_server_start_recovers_every_scopes_pending_intents(
    world0: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent_id = _crash_a_retire(world0, monkeypatch)
    replayed = app_module.recover_all_amendment_intents(**world0.kw())
    assert replayed == {"g_org": [intent_id]}
    assert world0.store.get_amendment_intent(intent_id).state == "done"


def test_the_mcp_server_main_runs_the_recovery_before_serving() -> None:
    import inspect  # noqa: PLC0415

    from strata.mcp import server  # noqa: PLC0415

    source = inspect.getsource(server.main)
    assert source.index("recover_all_amendment_intents") < source.index(
        'mcp.run(transport="stdio")'
    )


# -- the payload holds only what a replay needs ----------------------------------------------------


def test_the_intent_payload_holds_only_ids_targets_reasons_and_change_ids(
    world0: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json  # noqa: PLC0415

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-not-in-the-journal")
    monkeypatch.setenv("STRATA_FAKE_ENV_MARKER", "env-marker-value")
    _crash_a_retire(world0, monkeypatch)
    (intent,) = world0.store.list_amendment_intents(scope_id="g_org")
    payload = json.loads(intent.payload_json)
    assert set(payload) == {
        "judged_ids",
        "change_ids",
        "hop",
        "previous_ids",
        "new_ids",
        "retirements",
        "removals",
        "removed_directive_ids",
        "withdraw_published",
        "withdraw_reasoning",
        "withdraw_notice_kind",
        "withdraw_correcting_after",
        "withdraw_correcting_claim_id",
    }
    text = intent.payload_json
    assert "sk-secret" not in text and "env-marker-value" not in text
    assert "messages" not in payload and "tool" not in text.lower()  # no judge transcript
    # the retirement reason is what the record already stores on the judgment, nothing more
    (retirement,) = payload["retirements"]
    assert retirement["directive_id"] == "c_tokens"
