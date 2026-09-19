"""Strict Stop-hook enforcement is on by default; register records it, `--no-strict`
opts out, and the setting lives in `.strata/config.toml` so every harness's hook
reads the same per-project answer (Codex's `config.toml` is global, so baking it
into the Codex hook command would flip every project on the machine)."""

from __future__ import annotations

from pathlib import Path

import pytest

from strata import install
from strata.__main__ import _build_parser, cmd_register
from strata.project_config import read_freshness_strict


@pytest.fixture(autouse=True)
def _no_judge_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    for var in (
        "JUDGE_API_KEY",
        "ANTHROPIC_API_KEY",
        "STRATA_JUDGE_API_KEY",
        "STRATA_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _register(project: Path, *flags: str) -> int:
    args = _build_parser().parse_args(["register", str(project), *flags])
    return cmd_register(args)


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_register_turns_strict_on(tmp_path: Path) -> None:
    project = _project(tmp_path)

    assert _register(project) == 0

    assert read_freshness_strict(project) is True
    assert "[freshness]" in (project / ".strata" / "config.toml").read_text(encoding="utf-8")


def test_register_no_strict_opts_out(tmp_path: Path) -> None:
    project = _project(tmp_path)

    assert _register(project, "--no-strict") == 0

    assert read_freshness_strict(project) is False


def test_re_register_adds_strict_to_a_pre_m3_project(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _register(project)
    config = project / ".strata" / "config.toml"
    config.write_text(
        'db = ".strata/strata.db"\nfleet_yaml = ".strata/fleet.yaml"\n'
        'summaries_dir = ".strata/summaries"\n',
        encoding="utf-8",
    )
    assert read_freshness_strict(project) is None

    _register(project)

    assert read_freshness_strict(project) is True


def test_re_register_keeps_an_opt_out_but_no_strict_flips_an_existing_setting(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _register(project, "--no-strict")

    _register(project)  # plain re-register must not silently undo the opt-out
    assert read_freshness_strict(project) is False

    (tmp_path / "second").mkdir()
    project2 = _project(tmp_path / "second")
    _register(project2)
    _register(project2, "--no-strict")
    assert read_freshness_strict(project2) is False


def test_register_diff_mode_writes_nothing(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _register(project)
    config = project / ".strata" / "config.toml"
    config.write_text(config.read_text(encoding="utf-8").replace("true", "false"), encoding="utf-8")

    args = _build_parser().parse_args(["register", str(project), "--diff"])
    cmd_register(args)

    assert read_freshness_strict(project) is False


# --- the text helpers ---------------------------------------------------------


def test_set_freshness_strict_appends_a_table_and_preserves_everything_else() -> None:
    text = '# my comment\ndb = "x"\n\n[launch]\ndefault_harness = "codex"\n'

    out = install.set_freshness_strict(text, True)

    assert out.startswith(text)
    assert out.endswith("[freshness]\nstrict = true\n")
    assert install.read_freshness_strict_from_text(out) is True


def test_set_freshness_strict_replaces_in_place_and_never_duplicates() -> None:
    once = install.set_freshness_strict('db = "x"\n', True)
    twice = install.set_freshness_strict(once, False)

    assert twice.count("[freshness]") == 1
    assert twice.count("strict") == 1
    assert install.read_freshness_strict_from_text(twice) is False


def test_set_freshness_strict_keeps_crlf_line_endings() -> None:
    out = install.set_freshness_strict('db = "x"\r\n', True)

    assert "\r\n[freshness]\r\nstrict = true\r\n" in out
    assert "\n" not in out.replace("\r\n", "")


def test_read_freshness_strict_is_none_when_unset_or_invalid() -> None:
    assert install.read_freshness_strict_from_text('db = "x"\n') is None
    assert install.read_freshness_strict_from_text("[freshness]\nstrict = 'yes'\n") is None
    assert install.read_freshness_strict_from_text("not toml [[") is None
