"""The doctor's directive-integrity line (v1.22 P2): report only, never repairs, never fails."""

from __future__ import annotations

from pathlib import Path

import pytest

from strata import app as app_module
from strata.__main__ import _check_directive_integrity
from strata.integrity import missing_directives
from strata.locks import scope_lock
from strata.record_store import RecordStore
from tests.test_v122_amendment_intents_crash import Crash, _once
from tests.v121_126_support import World, directive_answer


@pytest.fixture
def world(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path)
    yield w
    w.close()


def _paths(w: World):  # noqa: ANN202
    from types import SimpleNamespace  # noqa: PLC0415

    return SimpleNamespace(
        db_path=str(w.tmp_path / "strata.db"),
        fleet_yaml_path=str(w.tmp_path / "fleet.yaml"),
        summaries_dir=str(w.tmp_path / "summaries"),
    )


def _lose_a_retirement(w: World, monkeypatch: pytest.MonkeyPatch) -> str:
    """Admit a directive, then retire it with a death before the retirement row is written."""
    admitted = w.propose(directive_answer([{"op": "append"}]))
    _once(RecordStore, "append_retirement", monkeypatch)
    with pytest.raises(Crash):
        w.propose(directive_answer([{"op": "retire", "id": admitted.contribution_id}]), content="x")
    return admitted.contribution_id


def test_a_healthy_record_reports_nothing(world: World) -> None:
    world.propose(directive_answer([{"op": "append"}]))
    assert missing_directives(**_kw(world)) == []
    check = _check_directive_integrity(_paths(world))
    assert check.passed is True and "no admitted directive is missing" in check.message


def _kw(w: World) -> dict:
    return {"fleet": w.fleet, "record_store": w.store, "summary_store": w.summaries}


def test_a_directive_missing_with_no_retirement_is_reported_and_nothing_is_repaired(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lost = _lose_a_retirement(world, monkeypatch)
    before = (world.summaries.read("g_org"), world.store.list_retirements())

    found = missing_directives(**_kw(world))
    assert [(m.scope_id, m.directive_id) for m in found] == [("g_org", lost)]
    check = _check_directive_integrity(_paths(world))
    assert lost in check.message and "directive missing with no retirement" in check.message
    assert (world.summaries.read("g_org"), world.store.list_retirements()) == before  # no repair


def test_the_line_never_fails_doctor_even_when_it_reports(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lose_a_retirement(world, monkeypatch)
    assert _check_directive_integrity(_paths(world)).passed is True


def test_after_recovery_the_directive_has_its_retirement_and_is_no_longer_reported(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _lose_a_retirement(world, monkeypatch)
    with scope_lock("g_org"):
        app_module.recover_amendment_intents("g_org", **_kw(world))
    assert missing_directives(**_kw(world)) == []
