"""`strata doctor` checks that the `strata` on PATH is the install that registered.

Every registered hook calls bare `strata` (issue #207). If an older install (say a
pipx 1.10.5) is first on PATH, the hooks silently run old code — no error, just
missing behaviour. Register records which install ran it (path and version) in
`.strata/config.toml`; doctor compares that with what `strata` resolves to now and
fails loudly when they differ.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from strata import __version__, install
from strata.__main__ import cmd_doctor, cmd_register
from strata.project_config import read_install_record


@pytest.fixture(autouse=True)
def _clear_judge_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "JUDGE_API_KEY",
        "ANTHROPIC_API_KEY",
        "STRATA_JUDGE_API_KEY",
        "STRATA_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _fake_strata(directory: Path, version: str) -> Path:
    """A stand-in `strata` executable that reports *version*."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "strata"
    script.write_text(f'#!/bin/sh\necho "strata {version}"\n', encoding="utf-8")
    script.chmod(0o755)
    return script


def _registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    args = argparse.Namespace(path=str(tmp_path), diff=False, bootstrap_venv=False)
    assert cmd_register(args) == 0
    from strata.migrator import run_migrations

    run_migrations(str(tmp_path / ".strata" / "strata.db"))
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_root")
    monkeypatch.setenv("STRATA_AGENT_SKILL", "strata-worker")
    monkeypatch.setenv("STRATA_AGENT_SESSION_ID", "sess_test")
    capsys.readouterr()
    return tmp_path


def _record(project: Path, executable: Path, version: str) -> None:
    config = project / ".strata" / "config.toml"
    config.write_text(
        install.set_install_record(config.read_text(encoding="utf-8"), str(executable), version),
        encoding="utf-8",
    )


def _doctor(capsys) -> tuple[int, str]:
    rc = cmd_doctor(argparse.Namespace())
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


# --- the text helpers ---------------------------------------------------------


def test_the_install_record_round_trips_and_is_replaced_in_place() -> None:
    text = 'db = "x"\n\n[freshness]\nstrict = true\n'

    once = install.set_install_record(text, "/opt/a/bin/strata", "1.12.0")
    twice = install.set_install_record(once, "/opt/b/bin/strata", "1.13.0")

    assert install.read_install_record_from_text(once) == ("/opt/a/bin/strata", "1.12.0")
    assert install.read_install_record_from_text(twice) == ("/opt/b/bin/strata", "1.13.0")
    assert twice.count("[install]") == 1
    assert twice.startswith(text)  # everything else is untouched


def test_no_install_record_reads_as_none() -> None:
    assert install.read_install_record_from_text('db = "x"\n') is None
    assert install.read_install_record_from_text("not toml [[") is None


# --- register records it ------------------------------------------------------


def test_register_records_the_install_that_ran_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    project = _registered(tmp_path, monkeypatch, capsys)

    recorded = read_install_record(project)

    assert recorded is not None
    executable, version = recorded
    assert version == __version__
    assert os.path.isabs(executable)


# --- doctor -------------------------------------------------------------------


def test_doctor_passes_when_the_strata_on_path_is_the_registering_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    project = _registered(tmp_path, monkeypatch, capsys)
    good = _fake_strata(tmp_path / "good", __version__)
    _record(project, good, __version__)
    monkeypatch.setenv("PATH", f"{good.parent}{os.pathsep}{os.environ['PATH']}")

    rc, output = _doctor(capsys)

    assert rc == 0
    assert "strata on PATH" in output


def test_doctor_fails_loudly_when_an_older_strata_is_first_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    project = _registered(tmp_path, monkeypatch, capsys)
    registering = _fake_strata(tmp_path / "new", "1.12.0")
    older = _fake_strata(tmp_path / "old", "1.10.5")
    _record(project, registering, "1.12.0")
    monkeypatch.setenv("PATH", f"{older.parent}{os.pathsep}{os.environ['PATH']}")

    rc, output = _doctor(capsys)

    assert rc == 1
    assert "strata on PATH" in output
    assert str(older) in output and str(registering) in output
    assert "1.10.5" in output and "1.12.0" in output
    assert "hooks" in output  # says what breaks: the registered hooks call bare `strata`


def test_doctor_fails_when_there_is_no_strata_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    project = _registered(tmp_path, monkeypatch, capsys)
    _record(project, _fake_strata(tmp_path / "new", "1.12.0"), "1.12.0")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    rc, output = _doctor(capsys)

    assert rc == 1
    assert "no `strata` on PATH" in output


def test_doctor_only_warns_when_register_predates_the_install_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    project = _registered(tmp_path, monkeypatch, capsys)
    config = project / ".strata" / "config.toml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text[: text.index("[install]")].rstrip() + "\n", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{_fake_strata(tmp_path / 'any', __version__).parent}")

    rc, output = _doctor(capsys)

    assert rc == 0
    assert "re-run `strata register`" in output


def test_doctor_notes_an_in_place_upgrade_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Same install, newer version than the one that registered: not a mismatch."""
    project = _registered(tmp_path, monkeypatch, capsys)
    upgraded = _fake_strata(tmp_path / "bin", "1.13.0")
    _record(project, upgraded, "1.12.0")
    monkeypatch.setenv("PATH", f"{upgraded.parent}{os.pathsep}{os.environ['PATH']}")

    rc, output = _doctor(capsys)

    assert rc == 0
    assert "1.12.0" in output and "1.13.0" in output
