"""The chain parent's PUBLICATION must reach the judge (ADR 0014, Phase A finding 1).

``_read_judge_inputs`` built peer publications from REFERENCED peers only, so
the chain parent's outward face — which ADR 0013 D2 composes into every child's
perspective — was the one composed input no judge had ever seen. An input-change
refresh triggered by a parent publication change would then have nothing to
judge against: the notice would say "item p_x was withdrawn" and the judge could
not see what the parent's face now says.

Rendered as its own PARENT PUBLICATION block beside REFERENCED PEER
PUBLICATIONS: non-binding either way, same "according to <scope>" citation
rule. The parent's CONTEXT stays invisible (ADR 0013 D1, see
``test_judge_parent_context.py``) — a publication is a curated outward face,
which is exactly what a context digest is not.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.app import _read_judge_inputs
from strata.fleet_config import FleetConfig, Scope, Stratum
from strata.migrator import run_migrations
from strata.publication import PublishedItem
from strata.record_store import RecordStore
from strata.scope_manager import _build_judge_preamble, _rendered_publication_item_ids
from strata.summary_store import ScopeSummary, SummaryStore

PARENT_PUBLISHED = "PARENT-PUBLISHED all deploys ship behind a flag"

SCOPE = Scope(id="g_child", name="Child", stratum_id="L1")
STRATUM = Stratum(id="L1", name="Team", ordinal=1)


def _item(item_id: str = "p_parent_1") -> PublishedItem:
    return PublishedItem(
        id=item_id,
        kind="directive",
        content=PARENT_PUBLISHED,
        subject="deploys",
        anchors=["c_parent_1"],
        published_at="2026-09-01T10:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _preamble(**kwargs) -> str:  # noqa: ANN003
    return _build_judge_preamble(
        scope=SCOPE,
        stratum=STRATUM,
        parent_summary=None,
        current_summary=ScopeSummary(
            scope_id="g_child",
            directives=[],
            context="the child's own working note",
            updated_at="2026-09-01T10:00:00+00:00",
        ),
        recent_contributions=[],
        judged_contribution_ids=[],
        **kwargs,
    )


def test_parent_publication_renders_as_its_own_block() -> None:
    text = _preamble(parent_publication=("g_parent", [_item()]))
    assert "PARENT PUBLICATION" in text
    assert PARENT_PUBLISHED in text
    assert "g_parent" in text


def test_parent_publication_block_is_omitted_when_there_is_no_parent() -> None:
    assert "PARENT PUBLICATION" not in _preamble(parent_publication=None)


def test_an_empty_parent_face_still_renders_honestly() -> None:
    """An honestly empty face is visible, exactly as this scope's own is."""
    text = _preamble(parent_publication=("g_parent", []))
    assert "PARENT PUBLICATION" in text
    assert "(none yet)" in text


def test_parent_publication_items_count_as_rendered_for_context_sources() -> None:
    """ADR 0014 D3: a declared source may name anything the judge was shown."""
    assert _rendered_publication_item_ids(
        None, None, parent_publication=("g_parent", [_item()])
    ) == ["p_parent_1"]


# ---------------------------------------------------------------------------
# Wiring — _read_judge_inputs actually reads it
# ---------------------------------------------------------------------------


def _fleet(root: Path) -> FleetConfig:
    fleet = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "team", "ordinal": 1},
        ],
        "scopes": [
            {"id": "g_parent", "name": "Parent", "stratum_id": "L0"},
            {"id": "g_child", "name": "Child", "stratum_id": "L1"},
        ],
        "edges": [{"from": "g_child", "to": "g_parent", "kind": "chain"}],
    }
    root.mkdir(parents=True, exist_ok=True)
    path = root / "fleet.yaml"
    path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return FleetConfig.load(path)


def test_read_judge_inputs_reads_the_parents_publication(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path)
    db_path = str(tmp_path / "test.db")
    run_migrations(db_path)
    summary_store = SummaryStore(str(tmp_path / "summaries"))

    from strata.publication import _write_publication

    _write_publication("g_parent", [_item()], summaries_dir=str(summary_store.summaries_dir))

    with RecordStore(db_path) as rs:
        inputs = _read_judge_inputs(
            scope=fleet.get_scope("g_child"),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
        )

    assert inputs.parent_publication is not None
    scope_id, items = inputs.parent_publication
    assert scope_id == "g_parent"
    assert [i.content for i in items] == [PARENT_PUBLISHED]


def test_a_root_scope_has_no_parent_publication(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path)
    db_path = str(tmp_path / "test.db")
    run_migrations(db_path)
    summary_store = SummaryStore(str(tmp_path / "summaries"))

    with RecordStore(db_path) as rs:
        inputs = _read_judge_inputs(
            scope=fleet.get_scope("g_parent"),
            fleet=fleet,
            record_store=rs,
            summary_store=summary_store,
        )

    assert inputs.parent_publication is None
