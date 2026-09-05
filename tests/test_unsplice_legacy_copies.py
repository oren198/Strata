"""Legacy spliced rows are unspliced, mechanically, once (ADR 0015 D5).

Summaries written before this release carry rows the splice copied out of an
ancestor's summary. They were never judged into that scope — the splice was
mechanical — so ADR 0014 D7's "no stored state is rewritten" does not protect
them; it protected JUDGED state.

The detection is exact, and it is the exact inverse of the splice: a directive
id IS a contribution id, and a contribution is appended to exactly one scope's
record. So a row whose contribution lives in ANOTHER scope's record is a copy,
and a row whose contribution lives in this scope's record — or is unknown to
the record at all — is not, and is left alone. ``source_scope_id`` is not used
for detection: the HTTP route lets a contributor's scope differ from the
target scope, so it is a claim about who typed, not about where the row was
admitted.

Vocabulary follows CONTEXT.md verbatim: scope, scope summary, directive,
record, contribution, refresh, change event.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from strata.app import drain_scope
from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.record_store import ContributorRef, RecordStore
from strata.summary_store import Directive, ScopeSummary, SummaryStore


def _fleet(tmp_path: Path) -> FleetConfig:
    path = tmp_path / "fleet.yaml"
    path.write_text(
        yaml.dump(
            {
                "strata": [
                    {"id": "L0", "name": "executive", "ordinal": 0},
                    {"id": "L1", "name": "team", "ordinal": 1},
                ],
                "scopes": [
                    {"id": "g_root", "name": "Root", "stratum_id": "L0"},
                    {"id": "g_child", "name": "Child", "stratum_id": "L1"},
                ],
                "edges": [{"from": "g_child", "to": "g_root"}],
            }
        ),
        encoding="utf-8",
    )
    return FleetConfig.load(path)


def _contributor(scope_id: str) -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id,
        skill="strata-developer",
        session_id="sess_test",
        ts="2026-09-01T09:00:00+00:00",
    )


def _directive(directive_id: str, *, source_scope_id: str) -> Directive:
    return Directive(
        id=directive_id,
        content="Every service ships with a runbook.",
        subject="ops",
        source_scope_id=source_scope_id,
        source_skill="scope-manager",
        created_at="2026-09-01T09:00:00+00:00",
    )


@pytest.fixture()
def env(tmp_path: Path):
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    record_store = RecordStore(db_path)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    yield _fleet(tmp_path), record_store, summary_store
    record_store.close()


def _drain(fleet, record_store, summary_store):
    return drain_scope(
        "g_child",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        # No judge: the unsplice is mechanical and owes one nothing.
        scope_manager=None,
        summary_max_words=500,
    )


def _seed(record_store, summary_store, *, rows) -> None:
    summary_store.write(
        "g_child",
        ScopeSummary(
            scope_id="g_child",
            directives=list(rows),
            context="Child working note.",
            updated_at="2026-09-01T10:00:00+00:00",
        ),
    )


def _own_row(record_store) -> Directive:
    """A directive minted from a contribution in g_child's OWN record."""
    contribution = record_store.append_contribution(
        scope_id="g_child",
        content="Runbooks live in the repo.",
        proposed_classification="directive",
        subject="ops",
        supersedes=None,
        contributor=_contributor("g_child"),
    )
    return _directive(contribution.id, source_scope_id="g_child")


def _copied_row(record_store) -> Directive:
    """A row the splice copied out of g_root's record — the legacy shape."""
    contribution = record_store.append_contribution(
        scope_id="g_root",
        content="Every service ships with a runbook.",
        proposed_classification="directive",
        subject="ops",
        supersedes=None,
        contributor=_contributor("g_root"),
    )
    return _directive(contribution.id, source_scope_id="g_root")


def test_a_copied_row_is_removed_on_the_first_drain(env) -> None:
    """The exact inverse of the splice (ADR 0015 D5)."""
    fleet, record_store, summary_store = env
    copied = _copied_row(record_store)
    _seed(record_store, summary_store, rows=[copied])

    _drain(fleet, record_store, summary_store)

    written = summary_store.read("g_child")
    assert [d.id for d in written.directives] == []


def test_the_removal_is_named_in_the_record(env) -> None:
    """A summary rewritten with nothing in the record saying so is a silent edit."""
    fleet, record_store, summary_store = env
    copied = _copied_row(record_store)
    _seed(record_store, summary_store, rows=[copied])

    _drain(fleet, record_store, summary_store)

    notes = [
        c
        for c in record_store.list_contributions(scope_id="g_child")
        if c.subject == "manager-refresh"
    ]
    assert len(notes) == 1
    assert copied.id in notes[0].content
    assert "ADR 0015 D5" in notes[0].content


def test_the_note_is_recorded_never_drained(env) -> None:
    """It reports work already done — it is not a refresh anyone still owes."""
    fleet, record_store, summary_store = env
    _seed(record_store, summary_store, rows=[_copied_row(record_store)])

    _drain(fleet, record_store, summary_store)

    assert record_store.list_change_events(scope_id="g_child", unprocessed_only=True) == []
    assert record_store.list_change_events(scope_id="g_child") != []


def test_a_row_minted_from_this_scopes_own_contribution_survives(env) -> None:
    """The scope admitted it; it is the scope's own memory, whatever it says."""
    fleet, record_store, summary_store = env
    own = _own_row(record_store)
    _seed(record_store, summary_store, rows=[own, _copied_row(record_store)])

    _drain(fleet, record_store, summary_store)

    assert [d.id for d in summary_store.read("g_child").directives] == [own.id]


def test_a_row_unknown_to_the_record_survives(env) -> None:
    """Never remove on absence of evidence.

    A row whose contribution the record has never heard of proves nothing
    about where it was admitted — an operator-attached row, a hand-migrated
    one — and removing it would destroy memory on a guess.
    """
    fleet, record_store, summary_store = env
    stranger = _directive("c_unknown_to_the_record", source_scope_id="g_root")
    _seed(record_store, summary_store, rows=[stranger])

    _drain(fleet, record_store, summary_store)

    assert [d.id for d in summary_store.read("g_child").directives] == [stranger.id]


def test_the_second_drain_removes_nothing_and_writes_nothing(env) -> None:
    """Once, not every read: the unsplice is a migration, not a filter."""
    fleet, record_store, summary_store = env
    _seed(record_store, summary_store, rows=[_own_row(record_store), _copied_row(record_store)])

    _drain(fleet, record_store, summary_store)
    after_first = summary_store.read("g_child")

    _drain(fleet, record_store, summary_store)
    after_second = summary_store.read("g_child")

    assert after_second.version == after_first.version
    assert (
        len(
            [
                c
                for c in record_store.list_contributions(scope_id="g_child")
                if c.subject == "manager-refresh"
            ]
        )
        == 1
    )


def test_a_scope_with_no_summary_yet_is_untouched(env) -> None:
    """Nothing on disk, nothing to unsplice — and no summary conjured for it."""
    fleet, record_store, summary_store = env

    _drain(fleet, record_store, summary_store)

    assert summary_store.read("g_child") is None


# ---------------------------------------------------------------------------
# The read path must actually reach the unsplice (ADR 0015 D5 vs D6)
# ---------------------------------------------------------------------------
#
# D6 reduces `drain_is_noop` to "nothing pending", and the MCP read path skips
# a no-op drain lock-free. On its own that leaves the scope this migration
# exists for — a legacy summary carrying copies, with an empty queue, which is
# the settled state of every scope that has not been written to since the
# upgrade — never unspliced by a read at all. A migration nothing triggers is
# not a migration. So a pending unsplice is work, and `drain_is_noop` says so.


def test_a_legacy_copy_makes_the_drain_not_a_noop(env) -> None:
    """Nothing pending, and there is still work: the copies are still here."""
    from strata.app import drain_is_noop

    fleet, record_store, summary_store = env
    _seed(record_store, summary_store, rows=[_copied_row(record_store)])

    assert record_store.list_change_events(scope_id="g_child", unprocessed_only=True) == []
    assert (
        drain_is_noop(
            "g_child", fleet=fleet, record_store=record_store, summary_store=summary_store
        )
        is False
    )


def test_the_drain_is_a_noop_again_once_the_copies_are_gone(env) -> None:
    """Once, not every read: after the unsplice the check finds nothing."""
    from strata.app import drain_is_noop

    fleet, record_store, summary_store = env
    _seed(record_store, summary_store, rows=[_own_row(record_store), _copied_row(record_store)])

    _drain(fleet, record_store, summary_store)

    assert (
        drain_is_noop(
            "g_child", fleet=fleet, record_store=record_store, summary_store=summary_store
        )
        is True
    )


def test_a_scope_holding_only_its_own_rows_is_a_noop_from_the_start(env) -> None:
    """A fleet that never ran the splice never pays for the check's verdict."""
    from strata.app import drain_is_noop

    fleet, record_store, summary_store = env
    _seed(record_store, summary_store, rows=[_own_row(record_store)])

    assert (
        drain_is_noop(
            "g_child", fleet=fleet, record_store=record_store, summary_store=summary_store
        )
        is True
    )


def test_the_mcp_read_path_unsplices_a_quiet_legacy_scope(env, monkeypatch) -> None:
    """The end the hole was at: a read of a settled legacy scope cleans it up.

    `_drain_for_read` skips a no-op drain without taking the scope lock, so
    this passes only if `drain_is_noop` reports the pending unsplice as work.
    """
    import strata.mcp.server as server

    fleet, record_store, summary_store = env
    copied = _copied_row(record_store)
    _seed(record_store, summary_store, rows=[copied])

    monkeypatch.setattr(server, "_record_store", record_store)
    monkeypatch.setattr(server, "_summary_store", summary_store)
    # No judge key: the unsplice is mechanical and must run without one.
    monkeypatch.setattr(
        server,
        "_settings",
        SimpleNamespace(
            judge_api_key=None,
            anthropic_api_key=None,
            summary_max_words=500,
            window_verbatim_tail=2,
            recency_window_size=20,
        ),
    )

    pending = server._drain_for_read(fleet, "g_child")

    assert pending == 0
    assert [d.id for d in summary_store.read("g_child").directives] == []
