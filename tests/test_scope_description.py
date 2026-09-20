"""A scope's optional ``description`` (#210): the stated purpose the judge measures
relevance against, and every surface that carries it.

Covers the model and its fleet.yaml round trip, `strata register` (flag and prompt),
the scope-manager prompts, `strata doctor`, and the Console (API and UI).
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import strata.__main__ as cli
from strata.__main__ import _build_parser, cmd_doctor, cmd_register
from strata.app import create_app
from strata.fleet_config import FleetConfig, Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import (
    _BATCH_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
    _build_batch_user_message,
    _build_user_message,
)
from strata.settings import Settings

_STRATUM = Stratum(id="L1", name="function", ordinal=1)


def _write_fleet(path: Path, scopes: list[dict]) -> Path:
    path.write_text(
        yaml.dump(
            {
                "strata": [{"id": "L1", "name": "function", "ordinal": 1}],
                "scopes": [{"stratum_id": "L1", **s} for s in scopes],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    return path


# --- the model and its round trip -------------------------------------------


def test_description_defaults_to_none_and_existing_fleets_load_unchanged(tmp_path: Path) -> None:
    fleet = FleetConfig.load(_write_fleet(tmp_path / "fleet.yaml", [{"id": "a", "name": "A"}]))

    assert fleet.scopes[0].description is None


def test_description_loads_from_fleet_yaml(tmp_path: Path) -> None:
    path = _write_fleet(
        tmp_path / "fleet.yaml",
        [{"id": "a", "name": "A", "description": "Billing service: invoices and payouts."}],
    )

    assert FleetConfig.load(path).scopes[0].description == "Billing service: invoices and payouts."


def test_a_blank_description_is_no_description() -> None:
    assert Scope(id="a", name="A", stratum_id="L1", description="   ").description is None


def test_a_description_survives_later_edits_and_add_scope_writes_one(tmp_path: Path) -> None:
    path = _write_fleet(
        tmp_path / "fleet.yaml", [{"id": "a", "name": "A", "description": "Purpose A."}]
    )
    fleet = FleetConfig.load(path)

    fleet.add_scope(id="b", name="B", stratum_id="L1", description="Purpose B.")
    reloaded = FleetConfig.load(path)

    assert {s.id: s.description for s in reloaded.scopes} == {"a": "Purpose A.", "b": "Purpose B."}
    fleet.add_scope(id="c", name="C", stratum_id="L1")  # no description: key not written
    written = yaml.safe_load(path.read_text())["scopes"][-1]
    assert "description" not in written


# --- strata register ---------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    for var in ("JUDGE_API_KEY", "ANTHROPIC_API_KEY", "STRATA_JUDGE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def _register(project: Path, *flags: str) -> int:
    (project / ".git").mkdir(exist_ok=True)
    args = _build_parser().parse_args(
        ["register", str(project), "--harness", "claude-code", *flags]
    )
    return cmd_register(args)


def _seeded_description(project: Path) -> str | None:
    return FleetConfig.load(project / ".strata" / "fleet.yaml").scopes[0].description


def test_register_writes_the_description_flag_to_fleet_yaml(tmp_path: Path) -> None:
    assert _register(tmp_path, "--description", "Billing: invoices, payouts.") == 0

    assert _seeded_description(tmp_path) == "Billing: invoices, payouts."


def test_register_escapes_a_description_that_needs_quoting(tmp_path: Path) -> None:
    tricky = 'Ops: "runbooks" & on-call # not a comment'

    assert _register(tmp_path, "--description", tricky) == 0

    assert _seeded_description(tmp_path) == tricky


def test_register_without_a_description_never_prompts_when_not_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_input(prompt: str = "") -> str:
        raise AssertionError("register prompted in a non-interactive run")

    monkeypatch.setattr("builtins.input", no_input)

    assert _register(tmp_path) == 0
    assert _seeded_description(tmp_path) is None


def test_register_asks_for_the_description_when_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "Payments platform work.")

    assert _register(tmp_path) == 0
    assert _seeded_description(tmp_path) == "Payments platform work."


def test_a_blank_answer_leaves_the_description_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "   ")

    assert _register(tmp_path) == 0
    assert _seeded_description(tmp_path) is None


def test_yes_and_eof_never_block_register(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)

    def eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)

    assert _register(tmp_path, "--yes") == 0  # --yes: never asks
    other = tmp_path / "other"
    other.mkdir()
    assert _register(other) == 0  # asked, stdin closed: falls through to blank
    assert _seeded_description(other) is None


def test_an_existing_fleet_is_never_rewritten_by_the_description_flag(tmp_path: Path) -> None:
    assert _register(tmp_path) == 0
    fleet_path = tmp_path / ".strata" / "fleet.yaml"
    before = fleet_path.read_text(encoding="utf-8")

    assert _register(tmp_path, "--description", "Late purpose.") == 0

    assert fleet_path.read_text(encoding="utf-8") == before


# --- the judge prompts -------------------------------------------------------


def _contribution() -> Contribution:
    return Contribution(
        id="c_new",
        scope_id="a",
        content="The office coffee machine is broken.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="a", skill=None, session_id="s", ts="2026-09-20T00:00:00+00:00"
        ),
        created_at="2026-09-20 00:00:00",
    )


def _message(description: str | None) -> str:
    return _build_user_message(
        scope=Scope(id="a", name="A", stratum_id="L1", description=description),
        stratum=_STRATUM,
        ancestor_directives=None,
        current_summary=None,
        recent_contributions=[],
        new_contribution=_contribution(),
    )


def test_the_prompt_carries_the_description_as_the_scopes_stated_purpose() -> None:
    message = _message("Billing service: invoices and payouts.")

    assert "SCOPE PURPOSE: Billing service: invoices and payouts." in message


def test_the_prompt_has_no_placeholder_when_there_is_no_description() -> None:
    message = _message(None)

    assert "SCOPE PURPOSE" not in message
    assert "None" not in message.split("NEW CONTRIBUTION TO JUDGE")[0]


def test_the_batch_message_carries_the_purpose_too() -> None:
    message = _build_batch_user_message(
        scope=Scope(id="a", name="A", stratum_id="L1", description="Billing purpose."),
        stratum=_STRATUM,
        ancestor_directives=None,
        current_summary=None,
        recent_contributions=[],
        new_contributions=[_contribution()],
    )

    assert "SCOPE PURPOSE: Billing purpose." in message


@pytest.mark.parametrize("prompt", [_SYSTEM_PROMPT, _BATCH_SYSTEM_PROMPT], ids=["single", "batch"])
def test_the_relevance_rule_names_which_rule_applied_in_the_decline_reason(prompt: str) -> None:
    prompt = " ".join(prompt.split())  # the prompt is hard-wrapped
    assert "RELEVANCE" in prompt
    # Both decline reasons are pinned, so the declines view teaches the operator to
    # add a description when there is none.
    assert "no stated purpose; not about the project's work" in prompt.lower()
    assert "outside this scope's stated purpose:" in prompt.lower()
    # Absent description: project work is the bar.
    assert "code" in prompt and "decisions" in prompt and "operations" in prompt
    assert "tooling" in prompt


@pytest.mark.parametrize("prompt", [_SYSTEM_PROMPT, _BATCH_SYSTEM_PROMPT], ids=["single", "batch"])
def test_the_relevance_rule_keeps_on_purpose_observations_admissible(prompt: str) -> None:
    """The must-still-accept twins: irrelevance is a named decline ground, and a
    project observation is explicitly on-purpose — the rule is not a licence to turn
    away well-formed context."""
    lowered = " ".join(prompt.split()).lower()
    assert "never a reason to decline" in lowered  # the existing observation rule stays
    assert "not relevant to this scope (relevance" in lowered  # ... and lists relevance
    assert "always on-purpose" in lowered  # the twin guard
    assert "flaky" in lowered  # a concrete on-purpose example


# --- strata doctor -----------------------------------------------------------


def _doctor_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scopes: list[dict]) -> None:
    assert _register(tmp_path) == 0
    _write_fleet(tmp_path / ".strata" / "fleet.yaml", scopes)
    monkeypatch.chdir(tmp_path)


def _doctor(capsys: pytest.CaptureFixture) -> tuple[int, str]:
    rc = cmd_doctor(argparse.Namespace())
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def test_doctor_warns_once_per_scope_without_a_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _doctor_project(
        tmp_path,
        monkeypatch,
        [
            {"id": "g_a", "name": "A"},
            {"id": "g_b", "name": "B", "description": "Has a purpose."},
            {"id": "g_c", "name": "C"},
        ],
    )
    capsys.readouterr()

    rc, output = _doctor(capsys)

    lines = [line for line in output.splitlines() if "description" in line.lower() and "g_" in line]
    assert len(lines) == 2
    assert any("g_a" in line for line in lines) and any("g_c" in line for line in lines)
    assert all(
        "can't tell what's irrelevant to it; add one in fleet.yaml" in line for line in lines
    )
    assert not any("g_b" in line for line in lines)
    assert rc == 0 or "✗" not in "".join(line for line in lines)  # a warning, never the failure


def test_doctor_description_warning_does_not_flip_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _doctor_project(tmp_path, monkeypatch, [{"id": "g_a", "name": "A"}])
    capsys.readouterr()
    without = _doctor(capsys)[0]
    _write_fleet(
        tmp_path / ".strata" / "fleet.yaml", [{"id": "g_a", "name": "A", "description": "Purpose."}]
    )
    with_description = _doctor(capsys)[0]

    assert without == with_description


# --- the Console -------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    fleet = _write_fleet(
        tmp_path / "fleet.yaml",
        [
            {"id": "g_a", "name": "A", "description": "Billing: invoices."},
            {"id": "g_b", "name": "B"},
        ],
    )
    settings = Settings(
        db_path=str(tmp_path / "t.db"),
        summaries_dir=str(tmp_path / "summaries"),
        fleet_yaml_path=str(fleet),
        anthropic_api_key="test-key",
    )
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


def test_the_scopes_api_carries_the_description(client: TestClient) -> None:
    scopes = {s["id"]: s for s in client.get("/scopes").json()["scopes"]}

    assert scopes["g_a"]["description"] == "Billing: invoices."
    assert scopes["g_b"]["description"] is None


def test_the_scope_header_shows_the_description_or_no_description(client: TestClient) -> None:
    source = client.get("/ui/scope-detail.jsx").text

    assert "scope.description" in source
    assert "no description" in source
