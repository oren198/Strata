"""#210 re-ruling: the own-memory implied purpose may only SUGGEST a description, report-only.

doctor and the Console say "no description; your scope's memory suggests: ..." when a scope
has no description anywhere in its chain and enough memory to suggest one. Nothing is ever
declined or written from it. A scope that inherits a description is not warned about.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from strata.__main__ import _build_parser, cmd_doctor, cmd_register
from strata.app import create_app
from strata.scope_manager import IMPLIED_PURPOSE_MIN_WORDS, suggest_description
from strata.settings import Settings
from strata.summary_store import Directive, ScopeSummary, SummaryStore

_MEMORY = (
    "The billing service issues invoices, retries failed card payments three times and "
    "reconciles payouts nightly. Invoices are stored in the ledger database and exported "
    "to the finance team as CSV every month. Refunds are approved by support leads and "
    "recorded against the original invoice. Tax rates come from the pricing service and "
    "are cached for a day. Payment provider webhooks arrive out of order, so handlers "
    "must be idempotent."
)


def _summary(context: str = "", directives: list[Directive] | None = None) -> ScopeSummary:
    return ScopeSummary(
        scope_id="g_leaf",
        directives=directives or [],
        context=context,
        updated_at="2026-11-23T00:00:00+00:00",
        version=1,
    )


def _directive(content: str) -> Directive:
    return Directive(
        id="c_1",
        content=content,
        subject=None,
        source_scope_id="g_leaf",
        source_skill=None,
        created_at="2026-11-23T00:00:00+00:00",
    )


# --- the suggestion itself ----------------------------------------------------------


def test_too_little_memory_suggests_nothing() -> None:
    assert suggest_description(_summary("Coffee is upstairs."), IMPLIED_PURPOSE_MIN_WORDS) is None
    assert suggest_description(None, IMPLIED_PURPOSE_MIN_WORDS) is None


def test_enough_memory_suggests_its_opening_words() -> None:
    got = suggest_description(_summary(_MEMORY), IMPLIED_PURPOSE_MIN_WORDS)
    assert got is not None
    assert got.startswith("The billing service issues invoices")
    assert len(got.split()) <= 25


def test_directive_only_memory_suggests_from_the_directive() -> None:
    got = suggest_description(_summary("", [_directive(_MEMORY)]), IMPLIED_PURPOSE_MIN_WORDS)
    assert got is not None and got.startswith("The billing service")


# --- doctor -------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
    for var in ("JUDGE_API_KEY", "ANTHROPIC_API_KEY", "STRATA_JUDGE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def _fleet(path: Path, scopes: list[dict], edges: list[dict] | None = None) -> None:
    path.write_text(
        yaml.dump(
            {
                "strata": [
                    {"id": "L0", "name": "exec", "ordinal": 0},
                    {"id": "L1", "name": "function", "ordinal": 1},
                ],
                "scopes": scopes,
                "edges": edges or [],
            }
        ),
        encoding="utf-8",
    )


def _doctor_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scopes, edges=None) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    args = _build_parser().parse_args(["register", str(tmp_path), "--harness", "claude-code"])
    assert cmd_register(args) == 0
    _fleet(tmp_path / ".strata" / "fleet.yaml", scopes, edges)
    monkeypatch.chdir(tmp_path)


def _doctor(capsys: pytest.CaptureFixture) -> tuple[int, list[str]]:
    capsys.readouterr()
    rc = cmd_doctor(argparse.Namespace())
    out = capsys.readouterr()
    return rc, [line for line in (out.out + out.err).splitlines() if "description" in line.lower()]


def test_doctor_suggests_a_description_from_the_scopes_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _doctor_project(tmp_path, monkeypatch, [{"id": "g_a", "name": "A", "stratum_id": "L1"}])
    SummaryStore(str(tmp_path / ".strata" / "summaries")).write("g_a", _summary(_MEMORY))
    _, lines = _doctor(capsys)
    flat = " ".join(lines)
    assert "no description; your scope's memory suggests:" in flat
    assert "The billing service issues invoices" in flat


def test_doctor_without_memory_keeps_the_plain_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _doctor_project(tmp_path, monkeypatch, [{"id": "g_a", "name": "A", "stratum_id": "L1"}])
    _, lines = _doctor(capsys)
    flat = " ".join(lines)
    assert "g_a" in flat and "memory suggests" not in flat


def test_doctor_does_not_warn_about_a_scope_that_inherits_a_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _doctor_project(
        tmp_path,
        monkeypatch,
        [
            {"id": "g_root", "name": "Root", "stratum_id": "L0", "description": "Payments."},
            {"id": "g_leaf", "name": "Leaf", "stratum_id": "L1"},
        ],
        [{"from": "g_leaf", "to": "g_root", "kind": "chain"}],
    )
    _, lines = _doctor(capsys)
    assert not any("g_leaf" in line for line in lines)


def test_the_suggestion_never_changes_doctors_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _doctor_project(tmp_path, monkeypatch, [{"id": "g_a", "name": "A", "stratum_id": "L1"}])
    plain, _ = _doctor(capsys)
    SummaryStore(str(tmp_path / ".strata" / "summaries")).write("g_a", _summary(_MEMORY))
    suggested, _ = _doctor(capsys)
    assert plain == suggested


# --- the Console API ----------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    fleet = tmp_path / "fleet.yaml"
    _fleet(
        fleet,
        [
            {"id": "g_root", "name": "Root", "stratum_id": "L0", "description": "Payments."},
            {"id": "g_a", "name": "A", "stratum_id": "L1"},
            {"id": "g_b", "name": "B", "stratum_id": "L1", "description": "Own purpose."},
            {"id": "g_c", "name": "C", "stratum_id": "L0"},
        ],
        [{"from": "g_a", "to": "g_root", "kind": "chain"}],
    )
    summaries = tmp_path / "summaries"
    store = SummaryStore(str(summaries))
    store.write(
        "g_c",
        ScopeSummary(
            scope_id="g_c",
            directives=[],
            context=_MEMORY,
            updated_at="2026-11-23T00:00:00+00:00",
            version=1,
        ),
    )
    settings = Settings(
        db_path=str(tmp_path / "t.db"),
        summaries_dir=str(summaries),
        fleet_yaml_path=str(fleet),
        anthropic_api_key="test-key",
    )
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


def _scopes(client: TestClient) -> dict:
    return {s["id"]: s for s in client.get("/scopes").json()["scopes"]}


def test_the_api_names_an_inherited_description_and_its_source(client: TestClient) -> None:
    a = _scopes(client)["g_a"]
    assert a["description"] is None
    assert a["effective_description"] == {"text": "Payments.", "source_scope_id": "g_root"}
    assert a["description_suggestion"] is None


def test_the_api_suggests_for_a_scope_with_none_in_its_chain(client: TestClient) -> None:
    c = _scopes(client)["g_c"]
    assert c["effective_description"] is None
    assert c["description_suggestion"].startswith("The billing service issues invoices")


def test_the_api_has_no_suggestion_for_a_described_scope(client: TestClient) -> None:
    b = _scopes(client)["g_b"]
    assert b["effective_description"] == {"text": "Own purpose.", "source_scope_id": "g_b"}
    assert b["description_suggestion"] is None


def test_the_console_shows_the_suggestion_text(client: TestClient) -> None:
    source = client.get("/ui/scope-detail.jsx").text
    assert "memory suggests" in source
    assert "description_suggestion" in source
