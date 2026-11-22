"""A suspect amendment intent is reported, not only logged (v1.22).

Recovery finds a summary version AHEAD of an intent's expected version. That cannot happen under
the scope lock, so it is not replayed (replaying onto a state it was not written for could invent a
notice). It is marked suspect, and now also leaves an engine note in the record, rendered as `note`
and kept out of pending counts, and the doctor counts suspect intents (report only).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from strata import app as app_module
from strata.__main__ import _check_suspect_intents
from strata.locks import scope_lock
from strata.record_store import ENGINE_NOTE_SUBJECT
from tests.v121_126_support import World


@pytest.fixture
def world(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path)
    yield w
    w.close()


def _suspect_intent(w: World) -> str:
    """An intent that expects a summary version the scope is already past."""
    return w.store.create_amendment_intent(
        scope_id="g_org", expected_version=0, payload_json='{"judged_ids": []}'
    )


def _recover(w: World) -> list[str]:
    with scope_lock("g_org"):
        return app_module.recover_amendment_intents("g_org", **w.kw())


def _notes(w: World) -> list:  # noqa: ANN202
    return [
        c for c in w.store.list_contributions(scope_id="g_org") if c.subject == ENGINE_NOTE_SUBJECT
    ]


def test_a_future_version_is_not_replayed_and_leaves_one_engine_note(world: World) -> None:
    intent_id = _suspect_intent(world)
    assert _recover(world) == []  # not replayed
    assert world.store.get_amendment_intent(intent_id).state == "suspect"
    (note,) = _notes(world)
    assert intent_id in note.content and "not replayed" in note.content
    assert "version" in note.content
    assert world.store.list_retirements(scope_id="g_org") == []  # nothing was invented


def test_the_note_renders_as_a_note_and_stays_out_of_pending_counts(world: World) -> None:
    _suspect_intent(world)
    _recover(world)
    (note,) = _notes(world)
    state = world.store.get_record_entry(note.id).state
    assert state.state == "note"
    states = world.store.list_contribution_states(scope_id="g_org")
    assert sum(s.state == "pending" for s in states) == 0


def test_recovering_again_does_not_write_a_second_note(world: World) -> None:
    _suspect_intent(world)
    _recover(world)
    _recover(world)
    assert len(_notes(world)) == 1


def test_the_note_carries_no_payload_content(world: World) -> None:
    world.store.create_amendment_intent(
        scope_id="g_org",
        expected_version=0,
        payload_json='{"judged_ids": [], "withdraw_reasoning": "a-private-reason-text"}',
    )
    _recover(world)
    (note,) = _notes(world)
    assert "a-private-reason-text" not in note.content


# -- the doctor line ------------------------------------------------------------------------------


def _paths(w: World) -> SimpleNamespace:
    return SimpleNamespace(
        db_path=str(w.tmp_path / "strata.db"),
        fleet_yaml_path=str(w.tmp_path / "fleet.yaml"),
        summaries_dir=str(w.tmp_path / "summaries"),
    )


def test_the_doctor_says_none_when_no_intent_is_suspect(world: World) -> None:
    check = _check_suspect_intents(_paths(world))
    assert check.passed is True and "no amendment intent is suspect" in check.message


def test_the_doctor_counts_suspect_intents_and_never_fails(world: World) -> None:
    first, second = _suspect_intent(world), _suspect_intent(world)
    _recover(world)
    check = _check_suspect_intents(_paths(world))
    assert check.passed is True  # report only: the exit status is the other checks' alone
    assert "2 amendment intents are suspect" in check.message
    assert first in check.message and second in check.message
    # reporting changes nothing
    assert world.store.get_amendment_intent(first).state == "suspect"
