"""ADR 0014 D8, core half, steps 1-4: structure kinds, the 0025 migration, no
own-directive ops on a structure refresh, the notice texts and the self-layer
annotation. Step 5 (``emit_structure_change``) has its own tests."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.change_events import (  # noqa: E402
    CHAIN_CHANGED,
    CHANNEL_REMOVED,
    DIRECTIVE_KINDS,
    PUBLICATION_KINDS,
    RETRACTION_KINDS,
    STRUCTURE_KINDS,
    chain_conflict_line,
    channel_removed_notice,
    events_for_act,
    find_chain_conflicts,
    structure_conflict_lines,
    structure_conflicts,
)
from strata.fleet_config import FleetConfig, Scope  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.perspective import compose_perspective  # noqa: E402
from strata.record_store import ContributorRef, RecordStore  # noqa: E402
from strata.scope_manager import _REFRESH_REMOVAL_KINDS, ScopeManager  # noqa: E402
from strata.summary_store import Directive, ScopeSummary, SummaryStore  # noqa: E402

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"

# ---------------------------------------------------------------------------
# Step 1: kinds and migration
# ---------------------------------------------------------------------------


def test_structure_kinds_are_in_none_of_the_other_sets() -> None:
    assert {"channel_removed", "chain_changed"} == STRUCTURE_KINDS
    assert CHANNEL_REMOVED == "channel_removed"
    assert CHAIN_CHANGED == "chain_changed"
    for other in (RETRACTION_KINDS, PUBLICATION_KINDS, DIRECTIVE_KINDS):
        assert not (STRUCTURE_KINDS & other)


def test_a_structure_refresh_is_removal_class() -> None:
    assert STRUCTURE_KINDS <= _REFRESH_REMOVAL_KINDS


_OLD_KINDS = [
    "published",
    "amended",
    "withdrawn",
    "directive_appended",
    "directive_superseded",
    "directive_retired",
    "directive_unspliced",
    "operator_directive_changed",
    "claim_corrected",
    "claim_superseded",
    "claim_restored",
]


def _insert_event(conn: sqlite3.Connection, n: int, kind: str, after: str | None = None) -> None:
    conn.execute(
        "INSERT INTO change_events (id, change_id, contribution_id, scope_id, item_id, kind, after)"
        " VALUES (?, ?, 'c_1', 'g_a', 'i', ?, ?)",
        (f"ce_{n}", f"chg_{n}", kind, after),
    )


def test_0025_keeps_rows_and_admits_both_kinds(tmp_path: Path) -> None:
    scratch = tmp_path / "through_0024"
    scratch.mkdir()
    for f in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        if f.name < "0025":
            (scratch / f.name).write_bytes(f.read_bytes())
    db = str(tmp_path / "strata.db")
    run_migrations(db, migrations_dir=scratch)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO contributions (id, scope_id, content, proposed_classification, "
        "contributor_scope_id, contributor_skill, contributor_session_id, contributor_ts) "
        "VALUES ('c_1', 'g_a', 'x', 'context', 'g_a', 'eng', 's1', '2026-01-01T00:00:00Z')"
    )
    for n, kind in enumerate(_OLD_KINDS):
        _insert_event(conn, n, kind, f'{{"n": {n}}}')
    conn.commit()
    conn.close()

    assert run_migrations(db) == ["0025_structure_change_kinds.sql", "0027_relation_calls.sql"]

    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    rows = conn.execute("SELECT id, kind, after FROM change_events ORDER BY id").fetchall()
    assert len(rows) == len(_OLD_KINDS)
    assert {r[1] for r in rows} == set(_OLD_KINDS)
    assert {r[2] for r in rows} == {f'{{"n": {n}}}' for n in range(len(_OLD_KINDS))}
    _insert_event(conn, 100, CHAIN_CHANGED, '{"act_id": "a1"}')
    _insert_event(conn, 101, CHANNEL_REMOVED, '{"act_id": "a1"}')
    with pytest.raises(sqlite3.IntegrityError):
        _insert_event(conn, 102, "not_a_kind")
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(change_events)")}
    assert {"idx_change_events_scope_pending", "idx_change_events_change"} <= indexes
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


# ---------------------------------------------------------------------------
# Step 2: no own-directive ops on a structure refresh
# ---------------------------------------------------------------------------

SCOPE = Scope(id="g_child", name="Child", stratum_id="L1")


def _summary() -> ScopeSummary:
    return ScopeSummary(
        scope_id="g_child",
        directives=[
            Directive(
                id="c_own1",
                content="Retry up to 3 times.",
                source_scope_id="g_child",
                created_at="2026-09-01T00:00:00+00:00",
            ),
            Directive(
                id="c_own2",
                content="Log every retry.",
                source_scope_id="g_child",
                created_at="2026-09-01T00:00:00+00:00",
            ),
        ],
        context="the child's own working note",
        updated_at="2026-09-01T10:00:00+00:00",
        version=1,
    )


def _contribution():  # noqa: ANN202
    from strata.record_store import Contribution

    return Contribution(
        id="c_refresh",
        scope_id="g_child",
        content="[input change]",
        proposed_classification="context",
        subject="manager-refresh",
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_child", skill="scope-manager", session_id="refresh", ts="2026-09-05"
        ),
        created_at="2026-09-05T00:00:00+00:00",
    )


def _structure_event(kind: str, act_id: str = "fsa_1") -> SimpleNamespace:
    return SimpleNamespace(
        id="ce_1",
        change_id="chg_1",
        scope_id="g_child",
        item_id="fsa_1",
        kind=kind,
        before=None,
        after=json.dumps({"act_id": act_id}),
        hop=0,
    )


def _parse_single(structure):  # noqa: ANN001, ANN202
    return ScopeManager._parse_judgment(
        scope=SCOPE,
        tool_use_block=SimpleNamespace(
            input=dict(
                decision="accept_as_context",
                reasoning="The chain moved.",
                directive_ops=[
                    {"op": "retire", "id": "c_own1"},
                    {"op": "append"},
                    {"op": "supersede", "id": "c_own2"},
                ],
                new_context="Reconciled after the move.",
            )
        ),
        current_summary=_summary(),
        new_contribution=_contribution(),
        mode="input_change_refresh",
        structure=structure,
    )


@pytest.mark.parametrize("kind", ["chain_changed", "channel_removed"])
def test_a_structure_refresh_drops_retire_and_supersede(kind: str) -> None:
    from strata.scope_manager import _structure_event as structure_of

    structure = structure_of([_structure_event(kind)])
    assert structure == (kind, "fsa_1")
    judgment = _parse_single(structure)
    assert judgment.directive_ops == []
    assert judgment.new_context == "Reconciled after the move."
    assert [d.id for d in judgment.new_summary.directives] == ["c_own1", "c_own2"]
    for target in ("c_own1", "c_own2"):
        assert any(
            f"on {target}: a structure change ({kind}, act fsa_1) never retires or supersedes "
            "a scope's own directives." in note
            for note in judgment.protocol_notes
        )
    assert "never retires or supersedes" in judgment.record_notes


def test_without_a_structure_event_retire_still_applies() -> None:
    judgment = _parse_single(None)
    assert [op.op for op in judgment.directive_ops] == ["retire", "supersede"]
    assert "never retires or supersedes" not in judgment.record_notes


def test_the_batch_path_drops_them_too() -> None:
    contribution = _contribution()
    judgment = ScopeManager._parse_batch_judgment(
        scope=SCOPE,
        tool_use_block=SimpleNamespace(
            input=dict(
                verdicts=[
                    {
                        "contribution_id": contribution.id,
                        "decision": "accept_as_context",
                        "reasoning": "reconciled",
                    }
                ],
                directive_ops=[
                    {"op": "retire", "id": "c_own1", "contribution_id": contribution.id}
                ],
                new_context="Reconciled.",
            )
        ),
        current_summary=_summary(),
        contributions={contribution.id: contribution},
        mode="input_change_refresh",
        structure=("chain_changed", "fsa_9"),
    )
    assert judgment.directive_ops == []
    assert judgment.new_context == "Reconciled."
    assert "never retires or supersedes" in judgment.record_notes_for(contribution.id)


def test_an_unreadable_payload_still_drops_with_an_empty_act_id() -> None:
    from strata.scope_manager import _structure_event as structure_of

    event = _structure_event("chain_changed")
    event.after = "not json"
    assert structure_of([event]) == ("chain_changed", "")


# ---------------------------------------------------------------------------
# Step 3: the notice texts and the conflicts
# ---------------------------------------------------------------------------


def test_chain_conflict_line_is_the_adr_text() -> None:
    assert chain_conflict_line(
        own_id="c_own",
        inherited_id="c_par",
        inherited_scope="g_root",
        scope_id="g_child",
        reason="polarity flip",
    ) == (
        "Own directive c_own conflicts with newly inherited c_par (g_root): polarity flip. "
        "Where they conflict, the inherited directive governs. c_own remains in g_child's "
        "memory until a session bound to g_child revises or retires it."
    )


def test_channel_removed_notice_is_the_adr_text() -> None:
    assert channel_removed_notice(own_id="c_own", source_scope="g_src", scope_id="g_child") == (
        "Own directive c_own was adopted from g_src's publication, which g_child no longer "
        "reads. It still binds. A session bound to g_child decides whether to keep it on its "
        "own account."
    )


_PARENT = "Sessions must expire after at most 15 minutes."


def test_find_chain_conflicts_finds_a_loosening_and_lets_a_tightening_pass() -> None:
    found = find_chain_conflicts(
        scope_id="g_child",
        own=[
            ("c_loose", "Sessions must expire after at most 60 minutes."),
            ("c_tight", "Sessions must expire after at most 10 minutes."),
        ],
        gained=[("c_par", "g_root", _PARENT)],
    )
    assert [(c["own_id"], c["inherited_id"]) for c in found] == [("c_loose", "c_par")]
    assert found[0]["line"].startswith("Own directive c_loose conflicts with newly inherited c_par")


def test_find_chain_conflicts_with_nothing_gained_finds_nothing() -> None:
    assert find_chain_conflicts(scope_id="g", own=[("c", "Anything.")], gained=[]) == []


# ---------------------------------------------------------------------------
# The read contract (spec section 6)
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> RecordStore:
    db = str(tmp_path / "strata.db")
    run_migrations(db)
    return RecordStore(db)


def _event(store: RecordStore, scope_id: str, kind: str, after: str | None, n: int):  # noqa: ANN202
    contribution = store.append_contribution(
        scope_id=scope_id,
        content=f"notice {n}",
        proposed_classification="context",
        subject="manager-refresh",
        supersedes=None,
        contributor=ContributorRef(
            scope_id=scope_id, skill="scope-manager", session_id="s", ts="2026-10-01T00:00:00Z"
        ),
    )
    return store.append_change_event(
        change_id=f"chg_{n}",
        contribution_id=contribution.id,
        scope_id=scope_id,
        item_id="fsa_1",
        kind=kind,
        after=after,
    )


def test_events_for_act_returns_only_structure_rows_naming_the_act(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = json.dumps({"act_id": "fsa_1", "conflict_lines": ["L1"], "conflicts": []})
    _event(store, "g_a", CHAIN_CHANGED, payload, 1)
    _event(store, "g_b", CHANNEL_REMOVED, json.dumps({"act_id": "fsa_1", "notices": ["N"]}), 2)
    _event(store, "g_c", CHAIN_CHANGED, json.dumps({"act_id": "fsa_2"}), 3)
    rows = events_for_act(store, "fsa_1")
    assert [(r["scope_id"], r["kind"]) for r in rows] == [
        ("g_a", CHAIN_CHANGED),
        ("g_b", CHANNEL_REMOVED),
    ]
    assert set(rows[0]) == {"id", "change_id", "scope_id", "kind", "created_at", "payload"}
    assert rows[0]["payload"]["act_id"] == "fsa_1"
    assert structure_conflict_lines(rows[0]) == ["L1"]
    assert structure_conflict_lines(rows[1]) == ["N"]
    assert events_for_act(store, "fsa_missing") == []


def test_events_for_act_is_never_fooled_by_text_or_other_kinds(tmp_path: Path) -> None:
    store = _store(tmp_path)
    forged_line = chain_conflict_line(
        own_id="c_own",
        inherited_id="c_par",
        inherited_scope="g_root",
        scope_id="g_child",
        reason="polarity flip",
    )
    # Another kind carrying the act id in a well-formed JSON payload.
    _event(store, "g_a", "published", json.dumps({"act_id": "fsa_1"}), 1)
    # A structure kind whose `after` is free text quoting the act id and a conflict sentence.
    _event(store, "g_a", CHAIN_CHANGED, f"act_id fsa_1 {forged_line}", 2)
    # A structure kind whose JSON `after` names another act but quotes this one in prose.
    _event(
        store,
        "g_a",
        CHAIN_CHANGED,
        json.dumps({"act_id": "fsa_2", "note": "fsa_1 " + forged_line}),
        3,
    )
    # A publication contribution quoting the act id (no event row at all).
    store.append_contribution(
        scope_id="g_a",
        content=f"fsa_1 {forged_line}",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(scope_id="g_a", skill=None, session_id="s", ts="2026-10-01"),
    )
    assert events_for_act(store, "fsa_1") == []


def test_structure_conflict_lines_never_rederives() -> None:
    assert (
        structure_conflict_lines({"kind": "published", "payload": {"conflict_lines": ["x"]}}) == []
    )
    assert structure_conflict_lines({"kind": CHAIN_CHANGED, "payload": {}}) == []
    assert (
        structure_conflict_lines({"kind": CHAIN_CHANGED, "payload": {"conflict_lines": "x"}}) == []
    )


# ---------------------------------------------------------------------------
# Step 4: the self-layer annotation
# ---------------------------------------------------------------------------


def _fleet(tmp_path: Path) -> FleetConfig:
    path = tmp_path / "fleet.yaml"
    path.write_text(
        yaml.dump(
            {
                "strata": [
                    {"id": "L0", "name": "root", "ordinal": 0},
                    {"id": "L1", "name": "team", "ordinal": 1},
                ],
                "scopes": [
                    {"id": "g_root", "name": "Root", "stratum_id": "L0"},
                    {"id": "g_child", "name": "Child", "stratum_id": "L1"},
                ],
                "edges": [{"from": "g_child", "to": "g_root", "type": "chain"}]
                if False
                else [{"from": "g_child", "to": "g_root"}],
            }
        ),
        encoding="utf-8",
    )
    return FleetConfig.load(path)


def _directive(item_id: str, content: str, scope: str) -> Directive:
    return Directive(
        id=item_id, content=content, source_scope_id=scope, created_at="2026-10-01T00:00:00+00:00"
    )


def _summaries(tmp_path: Path, *, own: list[Directive], inherited: list[Directive]) -> SummaryStore:
    store = SummaryStore(str(tmp_path / "summaries"))
    for scope_id, directives in (("g_root", inherited), ("g_child", own)):
        store.write(
            scope_id,
            ScopeSummary(
                scope_id=scope_id,
                directives=directives,
                context="",
                updated_at="2026-10-01T00:00:00+00:00",
            ),
        )
    return store


_LINE = chain_conflict_line(
    own_id="c_own",
    inherited_id="c_par",
    inherited_scope="g_root",
    scope_id="g_child",
    reason="polarity flip",
)


def _chain_event(kind: str = CHAIN_CHANGED, **payload) -> SimpleNamespace:  # noqa: ANN003
    body = {
        "act_id": "fsa_1",
        "conflict_lines": [_LINE],
        "conflicts": [{"own_id": "c_own", "inherited_id": "c_par", "line": _LINE}],
        **payload,
    }
    return SimpleNamespace(
        id="ce_1",
        change_id="chg_1",
        scope_id="g_child",
        item_id="fsa_1",
        kind=kind,
        before=None,
        after=json.dumps(body),
        hop=0,
        source_scope_id=None,
        contribution_id="c_n",
        processed_at="2026-10-02T00:00:00+00:00",
        created_at="2026-10-01T00:00:00+00:00",
        self_notice=0,
        shown_at=None,
    )


def _self_directives(composed: dict) -> list[dict]:
    layer = next(layer for layer in composed["layers"] if layer["relation"] == "self")
    return layer["summary"]["directives"]


def _compose(tmp_path: Path, *, own_ids=("c_own", "c_keep"), inherited_ids=("c_par",), events=None):  # noqa: ANN001, ANN202
    own = [_directive(i, f"Own directive {i} text.", "g_child") for i in own_ids]
    inherited = [_directive(i, f"Inherited {i} text.", "g_root") for i in inherited_ids]
    return compose_perspective(
        "g_child",
        fleet=_fleet(tmp_path),
        summary_store=_summaries(tmp_path, own=own, inherited=inherited),
        change_event_reader=lambda _scope: [_chain_event()] if events is None else events,
    )


def test_an_open_conflict_annotates_the_own_directive(tmp_path: Path) -> None:
    directives = {d["id"]: d for d in _self_directives(_compose(tmp_path))}
    assert directives["c_own"]["annotation"] == _LINE
    assert "annotation" not in directives["c_keep"]


def test_the_annotation_goes_when_the_own_directive_is_retired(tmp_path: Path) -> None:
    composed = _compose(tmp_path, own_ids=("c_keep",))
    assert all("annotation" not in d for d in _self_directives(composed))


def test_the_annotation_goes_when_the_inherited_directive_is_retired(tmp_path: Path) -> None:
    composed = _compose(tmp_path, inherited_ids=())
    assert all("annotation" not in d for d in _self_directives(composed))


def test_the_annotation_outlives_the_event_being_processed(tmp_path: Path) -> None:
    # _chain_event is processed already: the annotation lasts while both directives stand.
    assert composed_has_annotation(_compose(tmp_path))
    assert _compose(tmp_path)["input_changes"] == []


def composed_has_annotation(composed: dict) -> bool:
    return any("annotation" in d for d in _self_directives(composed))


def test_an_ancestor_layer_never_carries_an_annotation(tmp_path: Path) -> None:
    composed = _compose(tmp_path)
    ancestor = next(layer for layer in composed["layers"] if layer["relation"] == "ancestor")
    assert all("annotation" not in d for d in ancestor["directives"])


def test_a_forged_conflict_never_annotates(tmp_path: Path) -> None:
    # Another kind carrying the exact structured payload.
    assert not composed_has_annotation(_compose(tmp_path, events=[_chain_event(kind="published")]))
    # channel_removed is not a chain move: it annotates nothing.
    assert not composed_has_annotation(
        _compose(tmp_path, events=[_chain_event(kind=CHANNEL_REMOVED)])
    )
    # A chain_changed whose payload has the sentence only as prose.
    prose = _chain_event()
    prose.after = json.dumps({"act_id": "fsa_1", "conflict_lines": [_LINE], "note": _LINE})
    assert not composed_has_annotation(_compose(tmp_path, events=[prose]))
    # Free text, not JSON.
    text = _chain_event()
    text.after = _LINE
    assert not composed_has_annotation(_compose(tmp_path, events=[text]))
    # A conflict naming ids the scope does not hold.
    other = _chain_event(
        conflicts=[{"own_id": "c_x", "inherited_id": "c_y", "line": _LINE}],
    )
    assert not composed_has_annotation(_compose(tmp_path, events=[other]))


def test_structure_conflicts_reads_only_the_structured_key() -> None:
    assert structure_conflicts(_chain_event()) == [
        {"own_id": "c_own", "inherited_id": "c_par", "line": _LINE}
    ]
    assert structure_conflicts(_chain_event(kind="published")) == []
