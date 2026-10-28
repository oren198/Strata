"""#195: operator primitives tell the attachment scope's subtree even without `fleet`.

Called without `fleet`, a primitive reads the fleet from beside the store it was handed (and
nowhere else); when none can be found it raises before any write. Every project here is a
temp directory; the process environment and working directory are made unrelated on purpose.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.operator import (
    OperatorFleetUnresolved,
    _operator_layer_path,
    operator_publish,
    operator_retire_item,
    operator_supersede_item,
)
from strata.record_store import RecordStore


def _fleet_dict(prefix: str) -> dict:
    return {
        "strata": [
            {"id": "L0", "name": "top", "ordinal": 0},
            {"id": "L1", "name": "middle", "ordinal": 1},
            {"id": "L2", "name": "bottom", "ordinal": 2},
        ],
        "scopes": [
            {"id": f"{prefix}_top", "name": "Top", "stratum_id": "L0"},
            {"id": f"{prefix}_mid", "name": "Mid", "stratum_id": "L1"},
            {"id": f"{prefix}_low", "name": "Low", "stratum_id": "L2"},
        ],
        "edges": [
            {"from": f"{prefix}_mid", "to": f"{prefix}_top"},
            {"from": f"{prefix}_low", "to": f"{prefix}_mid"},
        ],
    }


def _write_fleet(path: Path, prefix: str) -> FleetConfig:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(_fleet_dict(prefix), default_flow_style=False), encoding="utf-8")
    return FleetConfig.load(path)


class Project:
    """A temp project: its store, its summaries dir, and the directory that holds the db."""

    def __init__(self, root: Path, prefix: str, *, seeded: bool, fleet_file: bool = True):
        self.prefix = prefix
        self.here = root / ".strata" if seeded else root
        self.here.mkdir(parents=True, exist_ok=True)
        self.db = str(self.here / "strata.db")
        run_migrations(self.db)
        self.summaries = str(root / "summaries")
        self.fleet = _write_fleet(self.here / "fleet.yaml", prefix) if fleet_file else None
        if seeded:
            (self.here / "config.toml").write_text(
                'db = ".strata/strata.db"\n'
                'fleet_yaml = ".strata/fleet.yaml"\n'
                'summaries_dir = "summaries"\n',
                encoding="utf-8",
            )
        self.store = RecordStore(self.db)
        self.attach = f"{prefix}_top"
        self.scopes = [f"{prefix}_top", f"{prefix}_mid", f"{prefix}_low"]

    def events(self):
        return {s: self.store.list_change_events(scope_id=s) for s in self.scopes}

    def shape(self):
        """The events with the per-run ids left out."""
        return {
            s: [(e.kind, e.source_scope_id, e.before, e.after) for e in evs]
            for s, evs in self.events().items()
        }


@pytest.fixture(autouse=True)
def _unrelated_environment(tmp_path, monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("STRATA_"):
            monkeypatch.delenv(name)
    elsewhere = tmp_path / "unrelated-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)


def _call(primitive: str, project: Project, **extra):
    """Run *primitive* on *project*; return nothing. supersede/retire act on a first item."""
    common = {"record_store": project.store, "summaries_dir": project.summaries}
    if primitive == "publish":
        operator_publish(project.attach, "Ship on Fridays.", **common, **extra)
        return
    first = operator_publish(project.attach, "first", **common, fleet=project.fleet)
    if primitive == "supersede":
        operator_supersede_item(project.attach, first.id, "second", **common, **extra)
    else:
        operator_retire_item(project.attach, first.id, **common, **extra)


PRIMITIVES = ["publish", "supersede", "retire"]


@pytest.fixture(params=[False, True], ids=["beside-db", "dot-strata-config"])
def layout(request):
    return request.param


@pytest.mark.parametrize("primitive", PRIMITIVES)
def test_a_call_without_fleet_still_emits_the_same_events(tmp_path, layout, primitive):
    implicit = Project(tmp_path / "implicit", "pa", seeded=layout)
    explicit = Project(tmp_path / "explicit", "pa", seeded=layout)

    _call(primitive, implicit)
    _call(primitive, explicit, fleet=explicit.fleet)

    assert implicit.shape() == explicit.shape()
    for scope in implicit.scopes:
        assert implicit.events()[scope], f"no event for {scope}"


@pytest.mark.parametrize("primitive", PRIMITIVES)
def test_the_environment_and_cwd_never_pick_the_fleet(tmp_path, monkeypatch, primitive):
    first = Project(tmp_path / "first", "pa", seeded=False)
    second = Project(tmp_path / "second", "pb", seeded=True)
    monkeypatch.setenv("STRATA_FLEET_CONFIG", str(second.here / "fleet.yaml"))
    monkeypatch.setenv("STRATA_DB_PATH", str(second.db))
    monkeypatch.setenv("STRATA_SUMMARIES_DIR", str(second.summaries))
    monkeypatch.chdir(second.here.parent)

    _call(primitive, first)

    expected = 1 if primitive == "publish" else 2  # supersede and retire follow a first publish
    assert [len(first.events()[s]) for s in first.scopes] == [expected] * 3
    assert not any(second.events()[s] for s in second.scopes)
    for event in (e for evs in first.events().values() for e in evs):
        assert event.scope_id in first.scopes
        assert event.source_scope_id == first.attach


def test_a_config_that_points_at_another_db_is_ignored(tmp_path):
    project = Project(tmp_path, "pa", seeded=True, fleet_file=False)
    _write_fleet(project.here / "named-by-config.yaml", "pa")
    (project.here / "config.toml").write_text(
        'db = ".strata/other.db"\n'
        'fleet_yaml = ".strata/named-by-config.yaml"\n'
        'summaries_dir = "summaries"\n',
        encoding="utf-8",
    )
    common = {"record_store": project.store, "summaries_dir": project.summaries}

    with pytest.raises(OperatorFleetUnresolved):
        operator_publish(project.attach, "x", **common)

    _write_fleet(project.here / "fleet.yaml", "pa")  # the file beside the db is used instead
    operator_publish(project.attach, "x", **common)
    assert all(project.events()[s] for s in project.scopes)


@pytest.mark.parametrize("primitive", PRIMITIVES)
def test_an_unresolvable_fleet_raises_and_writes_nothing(tmp_path, primitive):
    project = Project(tmp_path, "pa", seeded=False)
    first = operator_publish(
        project.attach,
        "first",
        record_store=project.store,
        summaries_dir=project.summaries,
        fleet=project.fleet,
    )
    (project.here / "fleet.yaml").unlink()
    layer = _operator_layer_path(project.summaries, project.attach)
    layer_before = layer.read_bytes()
    acts_before = len(project.store.list_operator_acts())
    events_before = project.shape()
    common = {"record_store": project.store, "summaries_dir": project.summaries}

    with pytest.raises(OperatorFleetUnresolved) as raised:
        if primitive == "publish":
            operator_publish(project.attach, "second", **common)
        elif primitive == "supersede":
            operator_supersede_item(project.attach, first.id, "second", **common)
        else:
            operator_retire_item(project.attach, first.id, **common)

    assert project.db in str(raised.value) or "fleet=" in str(raised.value)
    assert len(project.store.list_operator_acts()) == acts_before
    assert layer.read_bytes() == layer_before
    assert project.shape() == events_before


@pytest.mark.parametrize("primitive", PRIMITIVES)
def test_an_explicit_fleet_wins_over_the_file_beside_the_store(tmp_path, primitive):
    project = Project(tmp_path, "pa", seeded=False)
    passed = _write_fleet(tmp_path / "passed.yaml", "pq")
    passed_scopes = ["pq_top", "pq_mid", "pq_low"]

    _call_with(primitive, project, passed)

    for scope in passed_scopes:
        assert project.store.list_change_events(scope_id=scope), scope
    assert not any(project.events()[s] for s in project.scopes)


def _call_with(primitive, project, fleet):
    common = {"record_store": project.store, "summaries_dir": project.summaries}
    if primitive == "publish":
        operator_publish("pq_top", "x", **common, fleet=fleet)
        return
    first = operator_publish("pq_top", "first", **common, fleet=fleet)
    if primitive == "supersede":
        operator_supersede_item("pq_top", first.id, "second", **common, fleet=fleet)
    else:
        operator_retire_item("pq_top", first.id, **common, fleet=fleet)
