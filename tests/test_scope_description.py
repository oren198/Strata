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
from strata.summary_store import Directive, ScopeSummary

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
#
# Relevance is judged only where there is something to measure it against: a stated
# purpose (a description), or — with none — a scope whose existing memory is enough
# to tell what it is about. A scope with neither keeps today's behaviour exactly, so
# NO relevance wording may appear anywhere in its prompt (system prompt included).


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


_POPULATED_CONTEXT = (
    "The billing service issues invoices, retries failed card payments three times and "
    "reconciles payouts nightly. Invoices are stored in the ledger database and exported "
    "to the finance team as CSV every month. Refunds are approved by support leads and "
    "recorded against the original invoice. Tax rates come from the pricing service and "
    "are cached for a day. Payment provider webhooks arrive out of order, so handlers "
    "must be idempotent."
)


def _summary(context: str = "", directives: list[Directive] | None = None) -> ScopeSummary:
    return ScopeSummary(
        scope_id="a",
        directives=directives or [],
        context=context,
        updated_at="2026-09-20T00:00:00+00:00",
        version=1,
    )


def _message(
    description: str | None = None,
    summary: ScopeSummary | None = None,
    *,
    min_words: int | None = None,
    mode: str = "ordinary",
) -> str:
    extra = {} if min_words is None else {"implied_purpose_min_words": min_words}
    return _build_user_message(
        scope=Scope(id="a", name="A", stratum_id="L1", description=description),
        stratum=_STRATUM,
        ancestor_directives=None,
        current_summary=summary,
        recent_contributions=[],
        new_contribution=_contribution(),
        mode=mode,  # type: ignore[arg-type]
        **extra,
    )


def _flat(text: str) -> str:
    return " ".join(text.split())


_RELEVANCE_MARKERS = ("RELEVANCE", "SCOPE PURPOSE", "stated purpose", "implied by")


def test_with_a_description_relevance_is_judged_against_it() -> None:
    message = _flat(_message("Billing service: invoices and payouts."))

    assert "SCOPE PURPOSE: Billing service: invoices and payouts." in message
    assert "RELEVANCE" in message
    assert 'begin "Outside this scope\'s stated purpose: <the purpose as stated>."' in message


def test_a_described_scope_still_admits_on_purpose_work() -> None:
    """Must-still-accept twin, description path."""
    message = _flat(_message("Billing service: invoices and payouts."))

    assert "on-purpose" in message
    assert "when in doubt, admit" in message.lower()


def test_the_batch_message_carries_the_purpose_and_rule_too() -> None:
    message = _flat(
        _build_batch_user_message(
            scope=Scope(id="a", name="A", stratum_id="L1", description="Billing purpose."),
            stratum=_STRATUM,
            ancestor_directives=None,
            current_summary=None,
            recent_contributions=[],
            new_contributions=[_contribution()],
        )
    )

    assert "SCOPE PURPOSE: Billing purpose." in message
    assert "Outside this scope's stated purpose:" in message


@pytest.mark.parametrize(
    "summary",
    [None, _summary(), _summary("Coffee is upstairs."), _summary("A short note.")],
    ids=["no-summary", "empty-summary", "few-words", "near-empty"],
)
def test_a_scope_with_no_description_and_no_real_memory_carries_no_relevance_rule(
    summary: ScopeSummary | None,
) -> None:
    """Today's behaviour, exactly: a scope with nothing in it must not start declining
    things it accepts today, so nothing about relevance is in its prompt — not the
    per-call message, not the system prompt."""
    prompts = [_message(None, summary), _SYSTEM_PROMPT, _BATCH_SYSTEM_PROMPT]

    for prompt in prompts:
        for marker in _RELEVANCE_MARKERS:
            assert marker not in prompt, marker
    assert "None" not in _message(None, summary).split("NEW CONTRIBUTION TO JUDGE")[0]


def test_a_populated_scope_with_no_description_gets_an_implied_purpose_rule() -> None:
    message = _flat(_message(None, _summary(_POPULATED_CONTEXT)))

    assert "RELEVANCE" in message
    assert "SCOPE PURPOSE" not in message  # no purpose is stated — it is implied
    assert "implied by this scope's existing memory" in message
    # A decline on this path says the purpose was implied and names what it read.
    assert "Outside the purpose implied by this scope's existing memory (" in message
    assert "name what you read" in message


def test_directives_count_toward_a_populated_scope() -> None:
    directives = [
        Directive(
            id="c_1",
            content=_POPULATED_CONTEXT,
            subject=None,
            source_scope_id="a",
            source_skill=None,
            created_at="2026-09-20T00:00:00+00:00",
        )
    ]

    assert "implied by this scope's existing memory" in _flat(
        _message(None, _summary("", directives))
    )


def test_a_populated_scope_still_admits_on_topic_work() -> None:
    """Must-still-accept twin, implied-purpose path."""
    message = _flat(_message(None, _summary(_POPULATED_CONTEXT)))

    assert "extends, corrects, or sits alongside" in message
    assert "when in doubt, admit" in message.lower()


def test_the_words_needed_to_imply_a_purpose_are_tunable() -> None:
    assert "implied by" not in _message(None, _summary("Coffee is upstairs on the third floor."))
    assert "implied by" in _message(
        None, _summary("Coffee is upstairs on the third floor."), min_words=3
    )


def test_an_input_change_refresh_has_nothing_to_judge_for_relevance() -> None:
    message = _message(
        "Billing purpose.", _summary(_POPULATED_CONTEXT), mode="input_change_refresh"
    )

    assert "RELEVANCE" not in message


def test_the_scope_manager_threads_its_threshold_to_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    import strata.scope_manager as sm

    seen: dict = {}

    def spy(**kwargs: object) -> str:
        seen.update(kwargs)
        raise RuntimeError("stop after the prompt is built")

    monkeypatch.setattr(sm, "_build_user_message", spy)
    manager = sm.ScopeManager(client=MagicMock(), implied_purpose_min_words=7)

    with pytest.raises(RuntimeError, match="stop after"):
        manager.judge(
            scope=Scope(id="a", name="A", stratum_id="L1"),
            stratum=_STRATUM,
            current_summary=None,
            recent_contributions=[],
            new_contribution=_contribution(),
        )

    assert seen["implied_purpose_min_words"] == 7


def test_the_threshold_is_a_setting_with_an_engine_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from strata.scope_manager import IMPLIED_PURPOSE_MIN_WORDS

    assert Settings().implied_purpose_min_words == IMPLIED_PURPOSE_MIN_WORDS
    monkeypatch.setenv("STRATA_IMPLIED_PURPOSE_MIN_WORDS", "12")
    assert Settings().implied_purpose_min_words == 12


def test_the_managers_the_app_builds_carry_the_setting() -> None:
    from strata.app import get_scope_manager

    settings = Settings(implied_purpose_min_words=17, anthropic_api_key="k")

    manager = get_scope_manager(client=None, settings=settings)  # type: ignore[arg-type]

    assert manager._implied_purpose_min_words == 17


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
