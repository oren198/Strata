"""`strata doctor` names the judge it will use, in every case, and says so when it can't be reached.

The probe is injected (conftest stubs ``strata.__main__._probe_judge``); nothing here
touches the network.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from strata import __main__ as cli
from strata.__main__ import cmd_doctor, cmd_register

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


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    rc = cmd_register(
        argparse.Namespace(
            path=str(tmp_path), diff=False, bootstrap_venv=False, harness=None, yes=True
        )
    )
    assert rc == 0
    from strata.migrator import run_migrations

    run_migrations(str(tmp_path / ".strata" / "strata.db"))
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_root")
    monkeypatch.setenv("STRATA_AGENT_SKILL", "strata-worker")
    monkeypatch.setenv("STRATA_AGENT_SESSION_ID", "sess_test")
    return tmp_path


def _doctor(capsys: pytest.CaptureFixture) -> tuple[int, str]:
    capsys.readouterr()
    rc = cmd_doctor(argparse.Namespace())
    c = capsys.readouterr()
    return rc, c.out + c.err


def test_doctor_names_the_default_judge(project, monkeypatch, capsys) -> None:
    monkeypatch.setenv("JUDGE_API_KEY", "sk-or-abc")
    rc, out = _doctor(capsys)
    assert rc == 0
    assert (
        "Judge: qwen/qwen3-235b-a22b-2507 @ openrouter.ai/api (default, measured 2026-09-20)" in out
    )


def test_doctor_names_the_kept_anthropic_judge(project, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abc")
    rc, out = _doctor(capsys)
    assert rc == 0
    assert (
        "Judge: claude-haiku-4-5 @ api.anthropic.com (kept: an Anthropic key is set and no "
        "JUDGE_MODEL/JUDGE_BASE_URL; "
        'measured 2026-09-20 — see "Choosing a judge" in the README)'
    ) in out


def test_doctor_names_a_configured_judge(project, monkeypatch, capsys) -> None:
    monkeypatch.setenv("JUDGE_API_KEY", "k")
    monkeypatch.setenv("JUDGE_MODEL", "my/model")
    monkeypatch.setenv("JUDGE_BASE_URL", "https://gw.example/v1")
    rc, out = _doctor(capsys)
    assert rc == 0
    assert "Judge: my/model @ gw.example/v1 (configured via JUDGE_*" in out


def test_doctor_names_the_judge_even_with_no_key(project, capsys) -> None:
    rc, out = _doctor(capsys)
    assert rc == 0
    assert (
        "Judge: qwen/qwen3-235b-a22b-2507 @ openrouter.ai/api (default, measured 2026-09-20)" in out
    )
    assert "no judge key found" in out


def test_doctor_reads_the_project_dotenv_for_the_judge(project, capsys) -> None:
    (project / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-x\n")
    _, out = _doctor(capsys)
    assert "Judge: claude-haiku-4-5 @ api.anthropic.com (kept:" in out


def test_doctor_probes_the_resolved_judge_and_says_so_when_it_fails(
    project, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("JUDGE_API_KEY", "sk-or-abc")
    seen = []

    def fake_probe(resolved):
        seen.append((resolved.model, resolved.base_url))
        return "the endpoint is unreachable (connection refused)"

    monkeypatch.setattr(cli, "_probe_judge", fake_probe)
    rc, out = _doctor(capsys)

    assert seen == [("qwen/qwen3-235b-a22b-2507", "https://openrouter.ai/api")]
    assert rc == 0, "an unreachable judge is a soft failure — it never flips the exit code"
    assert "Judge: qwen/qwen3-235b-a22b-2507 @ openrouter.ai/api" in out
    assert "the endpoint is unreachable (connection refused)" in out
    # The override lines a stranger can paste into .env:
    assert "JUDGE_API_KEY=" in out
    assert "JUDGE_MODEL=" in out
    assert "JUDGE_BASE_URL=" in out


def test_doctor_model_id_not_resolving_prints_the_override_lines(
    project, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("JUDGE_API_KEY", "sk-or-abc")
    monkeypatch.setattr(
        cli,
        "_probe_judge",
        lambda r: "the endpoint does not serve model id 'qwen/qwen3-235b-a22b-2507'",
    )
    _, out = _doctor(capsys)
    assert "does not serve model id 'qwen/qwen3-235b-a22b-2507'" in out
    assert "JUDGE_MODEL=" in out and "JUDGE_BASE_URL=" in out


def test_doctor_no_probe_without_a_key(project, monkeypatch, capsys) -> None:
    called = []
    monkeypatch.setattr(cli, "_probe_judge", lambda r: called.append(r) or "boom")
    _, out = _doctor(capsys)
    assert called == []
    assert "boom" not in out


def test_doctor_reachable_judge_adds_no_failure(project, monkeypatch, capsys) -> None:
    monkeypatch.setenv("JUDGE_API_KEY", "sk-or-abc")
    monkeypatch.setattr(cli, "_probe_judge", lambda r: None)
    rc, out = _doctor(capsys)
    assert rc == 0
    assert "JUDGE_BASE_URL=" not in out


# --- the real probe, against a fake client ------------------------------------------------


def _resolved(**kw):
    from strata.settings import ResolvedJudge

    base = dict(model="m", base_url="https://h.example", api_key="k", reason="default")
    base.update(kw)
    return ResolvedJudge(**base)


class _Client:
    def __init__(self, exc: Exception | None) -> None:
        self._exc = exc
        self.calls: list[dict] = []
        outer = self

        class _Messages:
            def create(self, **kw):
                outer.calls.append(kw)
                if outer._exc is not None:
                    raise outer._exc
                return object()

        self.messages = _Messages()


def test_probe_live_success_returns_none_and_sends_one_token(monkeypatch) -> None:
    client = _Client(None)
    monkeypatch.setattr(cli, "_build_probe_client", lambda resolved: client)
    assert cli._probe_judge_live(_resolved()) is None
    assert client.calls[0]["max_tokens"] == 1
    assert client.calls[0]["model"] == "m"


def test_probe_live_classifies_failures(monkeypatch) -> None:
    import anthropic
    import httpx

    req = httpx.Request("POST", "https://h.example/v1/messages")

    def status(code: int, cls):
        return cls("x", response=httpx.Response(code, request=req), body=None)

    cases = [
        (anthropic.APIConnectionError(request=req), "unreachable"),
        (status(404, anthropic.NotFoundError), "does not serve model id 'm'"),
        (status(401, anthropic.AuthenticationError), "rejected the key"),
        (status(500, anthropic.InternalServerError), "returned an error (HTTP 500)"),
        (RuntimeError("weird"), "could not be checked"),
    ]
    for exc, needle in cases:
        monkeypatch.setattr(cli, "_build_probe_client", lambda resolved, e=exc: _Client(e))
        reason = cli._probe_judge_live(_resolved())
        assert reason is not None and needle in reason, (exc, reason)


def test_doctor_label_is_not_doubled(project, monkeypatch, capsys) -> None:
    monkeypatch.setenv("JUDGE_API_KEY", "sk-or-abc")
    _, out = _doctor(capsys)
    assert "Judge: qwen/" in out
    assert "Judge: judge:" not in out


def test_doctor_kept_text_is_true_for_an_sk_ant_judge_key(project, monkeypatch, capsys) -> None:
    """JUDGE_API_KEY IS set here (holding an Anthropic key); the text must not say otherwise."""
    monkeypatch.setenv("JUDGE_API_KEY", "sk-ant-api03-zzz")
    _, out = _doctor(capsys)
    assert "kept: an Anthropic key is set and no JUDGE_MODEL/JUDGE_BASE_URL" in out
    assert "ANTHROPIC_API_KEY set and no JUDGE_*" not in out


def test_doctor_register_written_defaults_still_read_as_the_default(
    project, monkeypatch, capsys
) -> None:
    """register writes JUDGE_MODEL/JUDGE_BASE_URL beside the key; that is still the default."""
    (project / ".env").write_text(
        "JUDGE_API_KEY=sk-or-abc\n"
        "JUDGE_MODEL=qwen/qwen3-235b-a22b-2507\n"
        "JUDGE_BASE_URL=https://openrouter.ai/api\n"
    )
    _, out = _doctor(capsys)
    assert "(default, measured 2026-09-20)" in out
    assert "configured via JUDGE_*" not in out
