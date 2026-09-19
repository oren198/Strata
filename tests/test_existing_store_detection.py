"""Tests for :func:`strata.install.find_existing_stores` (issue #178).

`strata register` used to seed a fresh starter store under `.strata/` even
when the project already had a working one, then point `.strata/config.toml`
at the new empty store — silently shadowing the old one. These tests cover
the detection half: what counts as an existing store, and what does not.
"""

from __future__ import annotations

from strata.install import find_existing_stores

_FLEET = (
    "strata:\n"
    "  - id: L0\n"
    "    name: root\n"
    "    ordinal: 0\n"
    "scopes:\n"
    "  - id: g_root\n"
    "    name: Root\n"
    "    stratum_id: L0\n"
    "edges: []\n"
)


def _seed_store(root, *, fleet=True, db=False, summaries=0, prefix=""):
    """Write a store-shaped layout under *root*, returning *root*.

    *prefix* places the store in a subdirectory (e.g. ``.strata``), matching
    the two layouts register has to tell apart.
    """
    base = root / prefix if prefix else root
    base.mkdir(parents=True, exist_ok=True)
    if fleet:
        (base / "fleet.yaml").write_text(_FLEET, encoding="utf-8")
    if db:
        (base / "strata.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    if summaries:
        summaries_dir = base / "summaries"
        summaries_dir.mkdir(exist_ok=True)
        for i in range(summaries):
            (summaries_dir / f"g_scope{i}.md").write_text("# summary\n", encoding="utf-8")
    return base


def test_bare_project_has_no_store(tmp_path):
    assert find_existing_stores(tmp_path) == []


def test_fleet_plus_populated_db_is_a_store(tmp_path):
    _seed_store(tmp_path, db=True)
    found = find_existing_stores(tmp_path)
    assert [s.root for s in found] == [tmp_path]


def test_fleet_plus_summaries_is_a_store(tmp_path):
    _seed_store(tmp_path, summaries=2)
    found = find_existing_stores(tmp_path)
    assert [s.root for s in found] == [tmp_path]


def test_lone_fleet_yaml_is_not_a_store(tmp_path):
    """A stray fleet.yaml with no memory beside it must never be adopted."""
    _seed_store(tmp_path, fleet=True)
    assert find_existing_stores(tmp_path) == []


def test_memory_without_a_fleet_is_not_a_store(tmp_path):
    _seed_store(tmp_path, fleet=False, db=True, summaries=1)
    assert find_existing_stores(tmp_path) == []


def test_unloadable_fleet_is_not_a_store(tmp_path):
    _seed_store(tmp_path, db=True)
    (tmp_path / "fleet.yaml").write_text("scopes: [oops\n", encoding="utf-8")
    assert find_existing_stores(tmp_path) == []


def test_empty_db_alone_is_not_a_store(tmp_path):
    """A zero-byte strata.db is what register itself creates — not memory."""
    _seed_store(tmp_path, fleet=True)
    (tmp_path / "strata.db").write_bytes(b"")
    assert find_existing_stores(tmp_path) == []


def test_empty_summaries_dir_alone_is_not_a_store(tmp_path):
    _seed_store(tmp_path, fleet=True, summaries=0)
    (tmp_path / "summaries").mkdir()
    assert find_existing_stores(tmp_path) == []


def test_finds_both_root_and_dotstrata_layouts(tmp_path):
    _seed_store(tmp_path, db=True)
    _seed_store(tmp_path, prefix=".strata", summaries=1)
    found = find_existing_stores(tmp_path)
    assert [s.root for s in found] == [tmp_path, tmp_path / ".strata"]


def test_candidate_reports_evidence_for_the_operator(tmp_path):
    """A candidate has to explain itself — the operator picks between them."""
    _seed_store(tmp_path, db=True, summaries=3)
    (candidate,) = find_existing_stores(tmp_path)
    assert candidate.scopes == 1
    assert candidate.summary_count == 3
    assert candidate.db_bytes > 0
    assert candidate.fleet_yaml == tmp_path / "fleet.yaml"
