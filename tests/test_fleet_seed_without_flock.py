"""A missing fleet.yaml can be seeded where fcntl.flock does not exist.

Replacing an existing file still refuses. ``strata export-fleet`` onto an
existing file prints that refusal and exits 1, with no traceback.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from strata.fleet_config import FLEET_FILE_LOCK_UNAVAILABLE
from strata.migrator import run_migrations
from strata.project_config import StoragePaths

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"


def _no_fcntl(monkeypatch: pytest.MonkeyPatch) -> None:
    import strata.fleet_config as fleet_config

    monkeypatch.setattr(fleet_config, "fcntl", None)


def _build_v1_db(db_path: str, tmp_path: Path) -> None:
    only_0001 = tmp_path / "migrations_0001_only"
    only_0001.mkdir()
    (only_0001 / "0001_initial.sql").write_text(
        (_MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    run_migrations(db_path, migrations_dir=only_0001)


def _seed_fleet(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L0', 'Executive', 0)")
    conn.execute("INSERT INTO strata (id, name, ordinal) VALUES ('L1', 'Function', 1)")
    conn.execute("INSERT INTO scopes (id, name, stratum_id) VALUES ('s_ceo', 'CEO', 'L0')")
    conn.execute("INSERT INTO scopes (id, name, stratum_id) VALUES ('s_eng', 'Engineering', 'L1')")
    conn.execute(
        "INSERT INTO edges (id, from_scope_id, to_scope_id) VALUES ('e1', 's_ceo', 's_eng')"
    )
    conn.commit()
    conn.close()


def test_create_fleet_file_leaves_an_existing_file_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O_EXCL: a fleet.yaml that is already there is not rewritten, even with no flock."""
    from strata.fleet_config import create_fleet_file

    _no_fcntl(monkeypatch)
    path = tmp_path / "fleet.yaml"
    path.write_bytes(b"keep me\n")
    assert create_fleet_file(path, b"replacement\n") is False
    assert path.read_bytes() == b"keep me\n"


def test_register_seeds_fleet_when_fcntl_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fresh ``strata register`` seeds fleet.yaml and exits 0 when fcntl is missing."""
    _no_fcntl(monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    (tmp_path / ".git").mkdir()

    from strata.__main__ import main

    rc = main(["register", str(tmp_path), "--yes"])
    captured = capsys.readouterr()

    assert rc == 0
    fleet = tmp_path / ".strata" / "fleet.yaml"
    assert fleet.is_file()
    assert fleet.stat().st_size > 0
    assert (tmp_path / ".strata" / "config.toml").is_file()
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert FLEET_FILE_LOCK_UNAVAILABLE not in captured.err


def test_start_seeds_fleet_when_fcntl_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Env-driven ``strata start`` seeds a missing fleet.yaml when fcntl is missing."""
    _no_fcntl(monkeypatch)
    db_path = str(tmp_path / "strata.db")
    fleet_path = tmp_path / "fleet.yaml"

    from strata.__main__ import main

    with (
        patch("uvicorn.run"),
        patch(
            "strata.__main__._storage_paths",
            return_value=StoragePaths(
                db_path=db_path,
                summaries_dir=str(tmp_path / "summaries"),
                fleet_yaml_path=str(fleet_path),
                source="env",
                project_root=None,
            ),
        ),
    ):
        rc = main(["start", "--skip-preflight"])
    captured = capsys.readouterr()

    assert rc == 0
    assert fleet_path.is_file()
    assert fleet_path.stat().st_size > 0
    assert "seeded fleet.yaml from the default template; edit to suit" in captured.out
    assert "Traceback" not in captured.err
    assert FLEET_FILE_LOCK_UNAVAILABLE not in captured.err


def test_register_seed_refusal_does_not_leave_a_half_registered_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A seed refusal is said before config.toml, and a re-run then finishes.

    The first register fails while reading the seed template. It must exit 1
    with that reason and no traceback, and it must not leave ``.strata/config.toml``.
    The second register, with the refusal gone, exits 0 and writes both files.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    (tmp_path / ".git").mkdir()

    real_read_bytes = Path.read_bytes
    armed = True

    def _read_bytes(self: Path) -> bytes:
        nonlocal armed
        if armed and self.name == "minimal.yaml":
            armed = False
            raise OSError("simulated seed refusal")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)

    from strata.__main__ import main

    rc = main(["register", str(tmp_path), "--yes"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "Refusing to register: simulated seed refusal" in captured.err
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert not (tmp_path / ".strata" / "config.toml").exists()

    rc = main(["register", str(tmp_path), "--yes"])
    captured = capsys.readouterr()

    assert rc == 0
    fleet = tmp_path / ".strata" / "fleet.yaml"
    assert fleet.is_file()
    assert fleet.stat().st_size > 0
    assert (tmp_path / ".strata" / "config.toml").is_file()
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert "Refusing to register" not in captured.err


def test_export_fleet_existing_file_refuses_without_fcntl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``export-fleet --force`` onto an existing file prints the lock refusal and exits 1.

    ``--force`` is the replace path. Without it the CLI still refuses with the
    existing-file hint, before any lock. The file on disk is left as it was,
    and the refusal is the message itself, not a traceback.
    """
    _no_fcntl(monkeypatch)
    db_path = str(tmp_path / "v1.db")
    _build_v1_db(db_path, tmp_path)
    _seed_fleet(db_path)
    out = tmp_path / "fleet.yaml"
    original = b"already here\n"
    out.write_bytes(original)

    from strata.__main__ import main

    rc = main(["export-fleet", "--db", db_path, "--out", str(out), "--force"])
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.err.strip() == FLEET_FILE_LOCK_UNAVAILABLE
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert out.read_bytes() == original
