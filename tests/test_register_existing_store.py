"""`strata register` must not seed a fresh store over an existing one (#178).

Live incident 2026-08-31: register was run on a project that already had a
working store at its root (a 7-scope ``fleet.yaml``, a 692KB ``strata.db``,
a populated ``summaries/``). Register seeded a starter fleet and an empty
database under ``.strata/`` and wrote ``.strata/config.toml`` pointing at
them. That flipped storage resolution from the env fallback to the project
branch, so the original store became unreachable — and ``strata doctor``
then reported all-green against the new empty one.

These tests pin the three cases: adopt when unambiguous, refuse when the
choice is genuinely the operator's, and never adopt anything on ``--fresh``.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.__main__ import cmd_register

_FLEET_7 = (
    "strata:\n"
    "  - id: L0\n    name: Executive\n    ordinal: 0\n"
    "  - id: L1\n    name: Eng\n    ordinal: 1\n"
    "scopes:\n"
    + "".join(f"  - id: g_s{i}\n    name: S{i}\n    stratum_id: L1\n" for i in range(7))
    + "edges: []\n"
)


@pytest.fixture(autouse=True)
def _no_judge_key_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    for var in (
        "JUDGE_API_KEY",
        "ANTHROPIC_API_KEY",
        "STRATA_JUDGE_API_KEY",
        "STRATA_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _args(tmp_path: Path, *, adopt: str | None = None, fresh: bool = False):
    return argparse.Namespace(
        path=str(tmp_path),
        diff=False,
        bootstrap_venv=False,
        harness=None,
        yes=False,
        adopt=adopt,
        fresh=fresh,
    )


def _seed_root_store(tmp_path: Path, *, summaries: int = 3) -> None:
    """The layout from the live incident: a real store at the project root."""
    (tmp_path / "fleet.yaml").write_text(_FLEET_7, encoding="utf-8")
    (tmp_path / "strata.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 4096)
    summaries_dir = tmp_path / "summaries"
    summaries_dir.mkdir()
    for i in range(summaries):
        (summaries_dir / f"g_s{i}.md").write_text("# real memory\n", encoding="utf-8")


def _config(tmp_path: Path) -> dict:
    return tomllib.loads((tmp_path / ".strata" / "config.toml").read_text(encoding="utf-8"))


def test_adopts_the_existing_store_instead_of_seeding(tmp_path, capsys):
    """One existing store, no ambiguity: point config.toml at what is there."""
    (tmp_path / ".git").mkdir()
    _seed_root_store(tmp_path)

    rc = cmd_register(_args(tmp_path))

    assert rc == 0
    cfg = _config(tmp_path)
    assert cfg["fleet_yaml"] == "fleet.yaml"
    assert cfg["db"] == "strata.db"
    assert cfg["summaries_dir"] == "summaries"
    assert "adopted" in capsys.readouterr().out.lower()


def test_adopting_does_not_seed_a_second_fleet(tmp_path):
    """The starter fleet must not appear beside the store being adopted."""
    (tmp_path / ".git").mkdir()
    _seed_root_store(tmp_path)

    cmd_register(_args(tmp_path))

    assert not (tmp_path / ".strata" / "fleet.yaml").exists()


def test_existing_store_is_left_byte_for_byte_untouched(tmp_path):
    (tmp_path / ".git").mkdir()
    _seed_root_store(tmp_path)
    before_fleet = (tmp_path / "fleet.yaml").read_bytes()
    before_db = (tmp_path / "strata.db").read_bytes()

    cmd_register(_args(tmp_path))

    assert (tmp_path / "fleet.yaml").read_bytes() == before_fleet
    assert (tmp_path / "strata.db").read_bytes() == before_db


def test_two_stores_refuses_and_names_both(tmp_path, capsys):
    """Which store is 'yours' is the operator's call, never register's."""
    (tmp_path / ".git").mkdir()
    _seed_root_store(tmp_path)
    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    (strata_dir / "fleet.yaml").write_text(
        "strata:\n  - id: L0\n    name: root\n    ordinal: 0\n"
        "scopes:\n  - id: g_root\n    name: Root\n    stratum_id: L0\nedges: []\n",
        encoding="utf-8",
    )
    (strata_dir / "summaries").mkdir()
    (strata_dir / "summaries" / "g_root.md").write_text("# other\n", encoding="utf-8")

    rc = cmd_register(_args(tmp_path))
    out = capsys.readouterr().out

    assert rc == 1
    assert not (strata_dir / "config.toml").exists()
    assert "fleet.yaml" in out
    assert "--adopt" in out


def test_adopt_flag_picks_a_named_store(tmp_path):
    (tmp_path / ".git").mkdir()
    _seed_root_store(tmp_path)
    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    (strata_dir / "fleet.yaml").write_text(
        "strata:\n  - id: L0\n    name: root\n    ordinal: 0\n"
        "scopes:\n  - id: g_root\n    name: Root\n    stratum_id: L0\nedges: []\n",
        encoding="utf-8",
    )
    (strata_dir / "summaries").mkdir()
    (strata_dir / "summaries" / "g_root.md").write_text("# other\n", encoding="utf-8")

    rc = cmd_register(_args(tmp_path, adopt=str(tmp_path)))

    assert rc == 0
    assert _config(tmp_path)["fleet_yaml"] == "fleet.yaml"


def test_adopt_rejects_a_path_that_is_not_a_store(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    _seed_root_store(tmp_path)
    (tmp_path / "elsewhere").mkdir()

    rc = cmd_register(_args(tmp_path, adopt=str(tmp_path / "elsewhere")))

    assert rc == 1
    assert "elsewhere" in capsys.readouterr().out


def test_fresh_seeds_a_new_store_beside_the_old_one(tmp_path):
    """The dangerous path stays available, but only when asked for by name."""
    (tmp_path / ".git").mkdir()
    _seed_root_store(tmp_path)

    rc = cmd_register(_args(tmp_path, fresh=True))

    assert rc == 0
    assert (tmp_path / ".strata" / "fleet.yaml").exists()
    assert _config(tmp_path)["fleet_yaml"] == ".strata/fleet.yaml"


def test_bare_project_still_seeds_exactly_as_before(tmp_path):
    """No existing store: the ordinary brownfield path is unchanged."""
    (tmp_path / ".git").mkdir()

    rc = cmd_register(_args(tmp_path))

    assert rc == 0
    assert (tmp_path / ".strata" / "fleet.yaml").exists()
    assert _config(tmp_path)["fleet_yaml"] == ".strata/fleet.yaml"


def test_already_registered_project_is_untouched(tmp_path):
    """An existing config.toml is the registration marker — it always wins."""
    (tmp_path / ".git").mkdir()
    _seed_root_store(tmp_path)
    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    (strata_dir / "config.toml").write_text(
        'db = ".strata/strata.db"\n'
        'fleet_yaml = ".strata/fleet.yaml"\n'
        'summaries_dir = ".strata/summaries"\n',
        encoding="utf-8",
    )

    rc = cmd_register(_args(tmp_path))

    assert rc == 0
    assert _config(tmp_path)["fleet_yaml"] == ".strata/fleet.yaml"


def test_already_registered_project_with_fleet_elsewhere_is_not_seeded(tmp_path, capsys):
    """Live incident 2026-08-31 (#184): config.toml already points somewhere
    other than ``.strata/fleet.yaml`` — an absolute path outside the project
    tree, in the reported case. Register must ask the resolver where the
    fleet actually lives, not assume the default layout, before deciding
    whether to seed.
    """
    (tmp_path / ".git").mkdir()
    external_root = tmp_path.parent / (tmp_path.name + "_external")
    external_root.mkdir()
    external_fleet = external_root / "fleet.yaml"
    external_fleet.write_text(_FLEET_7, encoding="utf-8")

    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    (strata_dir / "config.toml").write_text(
        f'db = "{external_root / "strata.db"}"\n'
        f'fleet_yaml = "{external_fleet}"\n'
        f'summaries_dir = "{external_root / "summaries"}"\n',
        encoding="utf-8",
    )

    rc = cmd_register(_args(tmp_path))
    out = capsys.readouterr().out

    assert rc == 0
    # The bug: register seeded a starter fleet under .strata/ even though
    # config.toml pointed elsewhere.
    assert not (strata_dir / "fleet.yaml").exists()
    # The external fleet must be untouched.
    assert external_fleet.read_text(encoding="utf-8") == _FLEET_7
    # The closing advice must name the fleet the project actually uses, not
    # the file register would have invented, and must not claim single-scope
    # auto-bind for a 7-scope fleet.
    assert str(external_fleet) in out
    assert ".strata/fleet.yaml" not in out
    assert "g_root" not in out


def test_reseeds_at_the_configured_path_when_its_parent_dir_is_gone(tmp_path):
    """Recovery path `strata start` promises: "fleet.yaml missing ... Re-run
    `strata register` ... to re-seed it." That must actually re-seed at the
    resolved path — including creating the parent directory if it, too, was
    removed — not crash and not fall back to the default `.strata/fleet.yaml`
    layout.
    """
    (tmp_path / ".git").mkdir()
    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    missing_parent = tmp_path / "custom"  # deliberately not created
    (strata_dir / "config.toml").write_text(
        f'db = "{missing_parent / "db.sqlite"}"\n'
        f'fleet_yaml = "{missing_parent / "fleet.yaml"}"\n'
        f'summaries_dir = "{missing_parent / "summaries"}"\n',
        encoding="utf-8",
    )

    rc = cmd_register(_args(tmp_path))

    assert rc == 0
    assert (missing_parent / "fleet.yaml").exists()
    assert not (strata_dir / "fleet.yaml").exists()
