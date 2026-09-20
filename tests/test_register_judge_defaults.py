"""`strata register` writes the judge's model and endpoint beside the key it captures.

Never overwrites existing JUDGE_* lines; never pairs an Anthropic key with the OpenRouter
default (that would flip an existing install's judge — the no-silent-switch rule).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from strata import install
from strata.__main__ import cmd_register

_ENV_VARS = (
    "JUDGE_API_KEY",
    "STRATA_JUDGE_API_KEY",
    "JUDGE_BASE_URL",
    "STRATA_JUDGE_BASE_URL",
    "JUDGE_MODEL",
    "STRATA_MANAGER_MODEL",
    "ANTHROPIC_API_KEY",
    "STRATA_ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _register(tmp_path: Path, yes: bool = False) -> int:
    (tmp_path / ".git").mkdir(exist_ok=True)
    return cmd_register(
        argparse.Namespace(path=str(tmp_path), diff=False, bootstrap_venv=False, harness=None, yes=yes)
    )


def _tty(monkeypatch: pytest.MonkeyPatch, answer: str, prompts: list[str] | None = None) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def fake(prompt: str = "") -> str:
        if prompts is not None:
            prompts.append(prompt)
        return answer

    monkeypatch.setattr("getpass.getpass", fake)


def _lines(env: Path) -> list[str]:
    return env.read_text().splitlines()


# --- install helper --------------------------------------------------------------------------


def test_write_env_judge_defaults_appends_both_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("JUDGE_API_KEY=sk-or-1\n")
    written = install.write_env_judge_defaults(env)
    assert written == ["JUDGE_MODEL", "JUDGE_BASE_URL"]
    assert _lines(env) == [
        "JUDGE_API_KEY=sk-or-1",
        "JUDGE_MODEL=qwen/qwen3-235b-a22b-2507",
        "JUDGE_BASE_URL=https://openrouter.ai/api",
    ]


@pytest.mark.parametrize(
    "existing,expect_written",
    [
        ("JUDGE_MODEL=mine\n", ["JUDGE_BASE_URL"]),
        ("STRATA_MANAGER_MODEL=mine\n", ["JUDGE_BASE_URL"]),
        ("JUDGE_BASE_URL=https://gw.example\n", ["JUDGE_MODEL"]),
        ("STRATA_JUDGE_BASE_URL=https://gw.example\n", ["JUDGE_MODEL"]),
        ("JUDGE_MODEL=a\nJUDGE_BASE_URL=b\n", []),
    ],
)
def test_write_env_judge_defaults_never_overwrites_existing_lines(
    tmp_path: Path, existing: str, expect_written: list[str]
) -> None:
    env = tmp_path / ".env"
    env.write_text(existing)
    assert install.write_env_judge_defaults(env) == expect_written
    for line in existing.splitlines():
        assert line in _lines(env)


# --- register, interactive ---------------------------------------------------------------------


def test_interactive_openrouter_key_writes_model_and_endpoint_beside_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    prompts: list[str] = []
    _tty(monkeypatch, "sk-or-v1-abc", prompts)
    assert _register(tmp_path) == 0
    assert _lines(tmp_path / ".env") == [
        "JUDGE_API_KEY=sk-or-v1-abc",
        "JUDGE_MODEL=qwen/qwen3-235b-a22b-2507",
        "JUDGE_BASE_URL=https://openrouter.ai/api",
    ]
    # The prompt says which provider the key must be for.
    assert "OpenRouter" in prompts[0]
    out = capsys.readouterr().out
    assert "qwen/qwen3-235b-a22b-2507 @ openrouter.ai/api" in out


def test_interactive_anthropic_key_writes_only_the_key(tmp_path: Path, monkeypatch, capsys) -> None:
    """An sk-ant- key stays on the Anthropic endpoint: no OpenRouter model/endpoint beside it."""
    _tty(monkeypatch, "sk-ant-api03-zzz")
    assert _register(tmp_path) == 0
    assert _lines(tmp_path / ".env") == ["JUDGE_API_KEY=sk-ant-api03-zzz"]
    assert "claude-haiku-4-5 @ api.anthropic.com" in capsys.readouterr().out


def test_interactive_never_overwrites_existing_judge_lines(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("JUDGE_MODEL=my/model\nJUDGE_BASE_URL=https://gw.example\n")
    _tty(monkeypatch, "sk-or-v1-abc")
    assert _register(tmp_path) == 0
    lines = _lines(tmp_path / ".env")
    assert "JUDGE_MODEL=my/model" in lines
    assert "JUDGE_BASE_URL=https://gw.example" in lines
    assert "JUDGE_API_KEY=sk-or-v1-abc" in lines
    assert not any(l.startswith("JUDGE_MODEL=qwen") for l in lines)
    assert len(lines) == 3


def test_interactive_prompt_names_the_default_provider_and_how_to_use_anthropic(
    tmp_path: Path, monkeypatch
) -> None:
    prompts: list[str] = []
    _tty(monkeypatch, "", prompts)
    assert _register(tmp_path) == 0
    assert "OpenRouter" in prompts[0]
    assert "ANTHROPIC_API_KEY" in prompts[0]
    assert not (tmp_path / ".env").exists()


# --- register, non-interactive -----------------------------------------------------------------


def test_non_interactive_writes_no_judge_lines_and_names_the_default(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _register(tmp_path, yes=True) == 0
    assert not (tmp_path / ".env").exists()
    out = capsys.readouterr().out
    assert "OpenRouter" in out
    assert "JUDGE_API_KEY" in out


def test_key_already_visible_writes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    """An existing install (ANTHROPIC_API_KEY only) is never given JUDGE_* lines by register."""
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-old\n")
    _tty(monkeypatch, "should-not-be-asked")
    assert _register(tmp_path) == 0
    assert _lines(tmp_path / ".env") == ["ANTHROPIC_API_KEY=sk-ant-old"]
    assert "judge key: found" in capsys.readouterr().out


def test_re_register_after_capture_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    _tty(monkeypatch, "sk-or-v1-abc")
    assert _register(tmp_path) == 0
    before = (tmp_path / ".env").read_text()
    _tty(monkeypatch, "sk-or-v1-other")
    assert _register(tmp_path) == 0
    assert (tmp_path / ".env").read_text() == before
