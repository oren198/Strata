"""Engine notes are not pending judgments (issue #196, item 2).

A ``change-emission-failed`` row is the engine's own record that a notice
could not be delivered. The read tool reports state ``note``. ``strata_rejudge``
refuses it. Pending counts leave it out. The recency window, which the judge
digest renders, still says ``pending``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from strata.fleet_config import FleetConfig
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import ScopeManagerJudgment
from tests.test_mcp_server import _load_mcp_module, _make_db, _make_fleet_yaml

SCOPE = "g_backend"


def _contributor() -> ContributorRef:
    return ContributorRef(
        scope_id=SCOPE,
        skill="scope-manager",
        session_id="change-event",
        ts="2026-10-21T00:00:00+00:00",
    )


def _append(store: RecordStore, *, subject: str, content: str) -> str:
    contribution = store.append_contribution(
        scope_id=SCOPE,
        content=content,
        proposed_classification="context",
        subject=subject,
        supersedes=None,
        contributor=_contributor(),
    )
    return contribution.id


def _seed(db_path: str) -> tuple[str, str]:
    with RecordStore(db_path) as store:
        note_id = _append(
            store,
            subject="change-emission-failed",
            content=(
                "[Change chg_lost could not be delivered to scope g_other: "
                "withdrawn of pub_1. That scope has NOT been told.]"
            ),
        )
        pending_id = _append(
            store,
            subject="a real claim",
            content="The dock pumps were switched off at 20:00.",
        )
    return note_id, pending_id


async def test_the_read_tool_shows_an_engine_note(tmp_path: Path) -> None:
    """strata_read_contribution reports state note. The digest row stays pending."""
    db_path = _make_db(tmp_path)
    fleet_path = _make_fleet_yaml(tmp_path)
    note_id, _pending_id = _seed(db_path)
    mod = _load_mcp_module(db_path, str(tmp_path / "summaries"), str(fleet_path))
    mod._record_store = RecordStore(db_path)
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_AGENT_SCOPE", SCOPE),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        result = await mod.strata_read_contribution(note_id)
        page = await mod.strata_read_scope_record(SCOPE)

    assert result["state"]["state"] == "note"
    assert result["judgment"] is None
    states = {row["contribution_id"]: row["state"] for row in page["contribution_states"]}
    assert states[note_id] == "note"

    with RecordStore(db_path) as store:
        window = store.list_recent_contributions(scope_id=SCOPE, limit=20)
    note_row = next(row for row in window if row.contribution.id == note_id)
    assert note_row.state == "pending"


async def test_strata_rejudge_refuses_an_engine_note(tmp_path: Path) -> None:
    """The refusal is the message, and the row is not sent to a judge."""
    db_path = _make_db(tmp_path)
    fleet_path = _make_fleet_yaml(tmp_path)
    note_id, _pending_id = _seed(db_path)
    mod = _load_mcp_module(db_path, str(tmp_path / "summaries"), str(fleet_path))
    mod._record_store = RecordStore(db_path)
    fleet = FleetConfig.load(fleet_path)
    judge = MagicMock()
    judge.judge.return_value = ScopeManagerJudgment(
        decision="decline", reasoning="Should not be asked.", new_summary=None
    )

    with (
        patch.object(mod, "_AGENT_SCOPE", SCOPE),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_build_scope_manager", return_value=judge),
        pytest.raises(RuntimeError, match="does not judge engine notes"),
    ):
        await mod.strata_rejudge(note_id)

    judge.judge.assert_not_called()
    with RecordStore(db_path) as store:
        assert store.get_judgment(note_id) is None


def test_pending_counters_exclude_engine_notes(tmp_path: Path) -> None:
    """A real pending row still counts. The engine note does not."""
    db_path = _make_db(tmp_path)
    note_id, pending_id = _seed(db_path)

    with RecordStore(db_path) as store:
        states = store.list_contribution_states(scope_id=SCOPE)
        page = store.page_record(scope_id=SCOPE)

    by_id = {state.contribution_id: state.state for state in states}
    assert by_id[note_id] == "note"
    assert by_id[pending_id] == "pending"
    assert sum(state.state == "pending" for state in states) == 1
    paged = {state.contribution_id: state.state for state in page.contribution_states}
    assert sum(state == "pending" for state in paged.values()) == 1
    assert paged[note_id] == "note"
