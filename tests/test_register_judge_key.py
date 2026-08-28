"""Tests for `strata register`'s end-of-run judge-key capture.

Motivated by a live failure: a project registered without a judge key, so
its first contribution sat unjudged with no obvious next step. At the end of
a successful, interactive `strata register` — reusing the markerless
prompt's TTY-check pattern — this now offers to capture the judge key and
store it in `.env`.

Covered here:

1. Safety gate: GITIGNORE_BLOCK covers `.env` before any write path exists,
   and `gitignore_covers_dotenv` / `write_env_judge_key` from
   `strata.install`.
2. The prompt appears only when interactive + no --yes + no key already
   visible; a non-empty answer is written to `.env` (create/append/replace);
   an empty answer (or EOF) prints the how-to-add-it-later note.
3. --yes and non-interactive never prompt; both print the same note when no
   key is visible.
4. A key already visible (env or this project's own .env) skips the prompt
   and prints "judge key: found".
5. `.env` byte-preservation around existing content.
6. The write-time gate: a project whose .gitignore doesn't cover `.env`
   (e.g. registered before this feature existed) still gets the key
   written, with a loud warning.
7. `strata doctor`'s new soft "Judge key" check, both states.

Vocabulary: scope, fleet, skill, scope-manager.
"""

from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata import install
from strata.__main__ import cmd_doctor, cmd_register

# ---------------------------------------------------------------------------
# Isolation: clear the four judge/anthropic key env-var spellings on every
# test in this file so "already visible" behaves the same regardless of
# whoever's shell runs the suite. getpass.getpass is NOT defaulted here —
# each test that reaches the prompt patches it explicitly, since the whole
# point of this file is exercising that prompt.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_judge_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "JUDGE_API_KEY",
        "ANTHROPIC_API_KEY",
        "STRATA_JUDGE_API_KEY",
        "STRATA_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _make_args(
    path: str | None = None,
    diff: bool = False,
    bootstrap_venv: bool = False,
    yes: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        path=path,
        diff=diff,
        bootstrap_venv=bootstrap_venv,
        harness=None,
        yes=yes,
    )


def _init_project(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()


def _run_register(tmp_path: Path, **kwargs) -> int:
    return cmd_register(_make_args(path=str(tmp_path), **kwargs))


# ---------------------------------------------------------------------------
# 1. Safety gate — must exist BEFORE any code path can write a key.
# ---------------------------------------------------------------------------


def test_gitignore_block_covers_dotenv() -> None:
    """The .gitignore block register seeds must ignore `.env`."""
    assert install.gitignore_covers_dotenv(install.GITIGNORE_BLOCK)


def test_register_seeds_gitignore_covering_env(tmp_path: Path, monkeypatch) -> None:
    """A fresh `strata register` run leaves .env covered by .gitignore."""
    _init_project(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    rc = _run_register(tmp_path, yes=True)

    assert rc == 0
    gitignore_text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert install.gitignore_covers_dotenv(gitignore_text)


def test_gitignore_covers_dotenv_exact_line_only() -> None:
    assert install.gitignore_covers_dotenv(".env\n")
    assert install.gitignore_covers_dotenv("*.log\n.env\nnode_modules/\n")
    assert not install.gitignore_covers_dotenv("# uses .env for secrets\n")
    assert not install.gitignore_covers_dotenv(".env.local\n")
    assert not install.gitignore_covers_dotenv("")


# ---------------------------------------------------------------------------
# 2. write_env_judge_key — byte-level create/append/replace.
# ---------------------------------------------------------------------------


def test_write_env_judge_key_creates_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    action = install.write_env_judge_key(env_path, "sk-ant-abc123")

    assert action == "created"
    assert env_path.read_bytes() == b"JUDGE_API_KEY=sk-ant-abc123\n"


def test_write_env_judge_key_appends_preserving_existing_content(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"OTHER_VAR=hello\nANOTHER=world\n")

    action = install.write_env_judge_key(env_path, "sk-ant-xyz")

    assert action == "appended"
    assert env_path.read_bytes() == (b"OTHER_VAR=hello\nANOTHER=world\nJUDGE_API_KEY=sk-ant-xyz\n")


def test_write_env_judge_key_appends_adds_missing_trailing_newline(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"OTHER_VAR=hello")  # no trailing newline

    install.write_env_judge_key(env_path, "sk-ant-xyz")

    assert env_path.read_bytes() == b"OTHER_VAR=hello\nJUDGE_API_KEY=sk-ant-xyz\n"


def test_write_env_judge_key_replaces_existing_judge_line_in_place(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"BEFORE=1\nJUDGE_API_KEY=\nAFTER=2\n")

    action = install.write_env_judge_key(env_path, "sk-ant-new")

    assert action == "replaced"
    assert env_path.read_bytes() == b"BEFORE=1\nJUDGE_API_KEY=sk-ant-new\nAFTER=2\n"


def test_write_env_judge_key_replaces_existing_anthropic_line_in_place(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"BEFORE=1\nANTHROPIC_API_KEY=\nAFTER=2\n")

    action = install.write_env_judge_key(env_path, "sk-ant-new")

    assert action == "replaced"
    assert env_path.read_bytes() == b"BEFORE=1\nJUDGE_API_KEY=sk-ant-new\nAFTER=2\n"


def test_write_env_judge_key_does_not_touch_commented_line(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"# JUDGE_API_KEY=example\nOTHER=1\n")

    action = install.write_env_judge_key(env_path, "sk-ant-new")

    assert action == "appended"
    assert env_path.read_bytes() == (
        b"# JUDGE_API_KEY=example\nOTHER=1\nJUDGE_API_KEY=sk-ant-new\n"
    )


def test_write_env_judge_key_created_file_is_owner_only(tmp_path: Path) -> None:
    """A freshly-created .env holds a secret — it must not inherit the
    umask's world/group-readable default (commonly 0644); it must be 0600."""
    env_path = tmp_path / ".env"

    action = install.write_env_judge_key(env_path, "sk-ant-secret")

    assert action == "created"
    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600


def test_write_env_judge_key_leaves_existing_file_perms_alone(tmp_path: Path) -> None:
    """Appending/replacing into a pre-existing .env must not touch its
    permissions — that file is the user's own, with whatever mode they set."""
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"OTHER=1\n")
    env_path.chmod(0o644)

    action = install.write_env_judge_key(env_path, "sk-ant-secret")

    assert action == "appended"
    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o644


# ---------------------------------------------------------------------------
# 3. End-to-end register: prompt appears only in the right conditions.
# ---------------------------------------------------------------------------


def test_interactive_no_key_prompts_and_writes_env(tmp_path: Path, monkeypatch, capsys) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    prompts = []

    def _fake_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return "sk-ant-secret"

    monkeypatch.setattr("getpass.getpass", _fake_getpass)

    rc = _run_register(tmp_path)

    assert rc == 0
    assert len(prompts) == 1
    assert "Judge key" in prompts[0]
    assert "Anthropic" in prompts[0]
    env_path = tmp_path / ".env"
    assert env_path.exists()
    assert env_path.read_bytes() == b"JUDGE_API_KEY=sk-ant-secret\n"
    captured = capsys.readouterr()
    assert "judge key: wrote .env" in captured.out


def test_interactive_empty_answer_skips_write_and_prints_note(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")

    rc = _run_register(tmp_path)

    assert rc == 0
    assert not (tmp_path / ".env").exists()
    captured = capsys.readouterr()
    assert "JUDGE_API_KEY" in captured.out
    assert ".env" in captured.out
    assert "unjudged" in captured.out


def test_interactive_eof_on_getpass_treated_as_skip(tmp_path: Path, monkeypatch, capsys) -> None:
    """EOF on the hidden-input prompt (stdin closed mid-session) must not crash."""
    _init_project(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def _raise_eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("getpass.getpass", _raise_eof)

    rc = _run_register(tmp_path)

    assert rc == 0
    assert not (tmp_path / ".env").exists()
    captured = capsys.readouterr()
    assert "unjudged" in captured.out


def test_interactive_ctrl_c_on_getpass_treated_as_skip(tmp_path: Path, monkeypatch, capsys) -> None:
    """Ctrl-C during the hidden-input prompt must not raise an unhandled
    KeyboardInterrupt traceback — treated as skip, same as EOF, matching the
    markerless prompt's graceful pattern."""
    _init_project(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def _raise_keyboard_interrupt(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("getpass.getpass", _raise_keyboard_interrupt)

    rc = _run_register(tmp_path)

    assert rc == 0
    assert not (tmp_path / ".env").exists()
    captured = capsys.readouterr()
    assert "unjudged" in captured.out


def test_key_already_visible_via_env_var_skips_prompt(tmp_path: Path, monkeypatch, capsys) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("JUDGE_API_KEY", "sk-ant-already-set")
    called = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": called.append(prompt) or "x")

    rc = _run_register(tmp_path)

    assert rc == 0
    assert called == [], "getpass must not be called when a key is already visible"
    assert not (tmp_path / ".env").exists()
    captured = capsys.readouterr()
    assert "judge key: found" in captured.out


def test_key_already_visible_via_existing_dotenv_skips_prompt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _init_project(tmp_path)
    (tmp_path / ".env").write_text("JUDGE_API_KEY=sk-ant-pre-existing\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    called = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": called.append(prompt) or "x")

    rc = _run_register(tmp_path)

    assert rc == 0
    assert called == [], "getpass must not be called when .env already has a key"
    captured = capsys.readouterr()
    assert "judge key: found" in captured.out
    # The pre-existing .env content must be left untouched.
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "JUDGE_API_KEY=sk-ant-pre-existing\n"


def test_yes_flag_never_prompts_prints_note(tmp_path: Path, monkeypatch, capsys) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    called = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": called.append(prompt) or "x")

    rc = _run_register(tmp_path, yes=True)

    assert rc == 0
    assert called == [], "getpass must not be called with --yes"
    assert not (tmp_path / ".env").exists()
    captured = capsys.readouterr()
    assert "JUDGE_API_KEY" in captured.out
    assert "unjudged" in captured.out


def test_non_interactive_never_prompts_prints_note(tmp_path: Path, monkeypatch, capsys) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    called = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": called.append(prompt) or "x")

    rc = _run_register(tmp_path, yes=True)

    assert rc == 0
    assert called == [], "getpass must not be called non-interactively"
    assert not (tmp_path / ".env").exists()
    captured = capsys.readouterr()
    assert "JUDGE_API_KEY" in captured.out
    assert "unjudged" in captured.out


def test_diff_mode_never_prompts_or_writes(tmp_path: Path, monkeypatch, capsys) -> None:
    _init_project(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    called = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="": called.append(prompt) or "x")

    rc = _run_register(tmp_path, diff=True)

    assert rc == 0
    assert called == [], "getpass must not be called in --diff mode"
    assert not (tmp_path / ".env").exists()


# ---------------------------------------------------------------------------
# 4. Write-time gate: .gitignore not covering .env still writes, but warns.
# ---------------------------------------------------------------------------


def test_write_warns_loudly_when_gitignore_does_not_cover_env(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Simulates a project registered before this feature existed (or one
    whose .gitignore was hand-edited to drop the `.env` line): the key is
    still written — skipping it would strand the operator worse than a
    warning would — but the warning fires loudly on stderr."""
    _init_project(tmp_path)
    # A .gitignore that does NOT cover .env (the pre-feature / edited case).
    (tmp_path / ".gitignore").write_text(
        "# Strata — managed by `strata register` — do not remove this line\n"
        ".strata/.venv/\n.strata/strata.db*\n.strata/summaries/\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sk-ant-risky")

    from strata.__main__ import _offer_judge_key_capture

    _offer_judge_key_capture(tmp_path, skip_prompt=False)

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert ".env" in captured.err
    assert (tmp_path / ".env").read_bytes() == b"JUDGE_API_KEY=sk-ant-risky\n"


def test_write_no_warning_when_gitignore_covers_env(tmp_path: Path, monkeypatch, capsys) -> None:
    """The mirror case: a .gitignore that does cover .env writes silently
    (no WARNING on stderr)."""
    _init_project(tmp_path)
    (tmp_path / ".gitignore").write_text(install.GITIGNORE_BLOCK, encoding="utf-8")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sk-ant-safe")

    from strata.__main__ import _offer_judge_key_capture

    _offer_judge_key_capture(tmp_path, skip_prompt=False)

    captured = capsys.readouterr()
    assert "WARNING" not in captured.err
    assert (tmp_path / ".env").read_bytes() == b"JUDGE_API_KEY=sk-ant-safe\n"


# ---------------------------------------------------------------------------
# 5. `strata doctor` — soft Judge key check, both states.
# ---------------------------------------------------------------------------


def _registered_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    rc = cmd_register(_make_args(path=str(tmp_path), yes=True))
    assert rc == 0

    from strata.migrator import run_migrations

    run_migrations(str(tmp_path / ".strata" / "strata.db"))
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_root")
    monkeypatch.setenv("STRATA_AGENT_SKILL", "strata-worker")
    monkeypatch.setenv("STRATA_AGENT_SESSION_ID", "sess_test")
    return tmp_path


def test_doctor_warns_but_passes_when_no_judge_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _registered_project(tmp_path, monkeypatch)
    capsys.readouterr()

    rc = cmd_doctor(argparse.Namespace())
    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert rc == 0, "a missing judge key must never flip doctor's exit code"
    lower = output.lower()
    assert "judge key" in lower
    assert "JUDGE_API_KEY" in output
    assert ".env" in output


def test_doctor_reports_pass_line_when_judge_key_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _registered_project(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_API_KEY", "sk-ant-present")
    capsys.readouterr()

    rc = cmd_doctor(argparse.Namespace())
    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert rc == 0
    lower = output.lower()
    assert "judge key" in lower
    assert "resolved" in lower
