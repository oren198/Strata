"""v1.16 P5 — the reporter-side "raised to <issuer> as <id>" derivation.

Derived at read time from `raised_from` (the RAISED contribution's own pointer
back to the outcome that caused it) — no new field on the outcome contribution
itself. Covers the RecordStore reverse-lookup helpers, `strata_read_contribution`'s
`raised_to`, and the `strata record` CLI line, for both a scope-issuer raise and
an operator-issuer raise.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from strata.__main__ import main
from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.operator import operator_publish
from strata.record_store import (
    ContributorRef,
    OperatorEvidenceInput,
    RaisedContributionInput,
    RecordStore,
)
from tests.test_mcp_server import _make_fleet_yaml, _record_reader


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from strata.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_FLEET_YAML = """\
strata:
  - id: L0
    name: Executive
    ordinal: 0
  - id: L1
    name: Function
    ordinal: 1
scopes:
  - id: g_arch
    name: Architect
    stratum_id: L0
  - id: g_backend
    name: Backend
    stratum_id: L1
edges:
  - from: g_backend
    to: g_arch
"""


def _contributor(scope_id: str) -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id, skill="engineer", session_id="sess_test", ts="2026-09-26T00:00:00Z"
    )


def _seed_directive(rs: RecordStore, scope_id: str) -> str:
    """A real, admitted directive contribution — `acted_on`'s FK requires one."""
    directive_id = rs.append_contribution(
        scope_id=scope_id,
        content="All services must use TLS 1.3 or later.",
        proposed_classification="directive",
        subject="tls",
        supersedes=None,
        contributor=_contributor(scope_id),
    ).id
    rs.record_judgment(
        contribution_id=directive_id, decision="accept_as_directive", judged_by="scope-manager"
    )
    return directive_id


def _seed_outcome(db_path: str, scope_id: str) -> tuple[str, RecordStore]:
    rs = RecordStore(db_path)
    outcome_id = rs.append_contribution(
        scope_id=scope_id,
        content="Tried the directive; it failed.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_contributor(scope_id),
    ).id
    return outcome_id, rs


def _quick_db(tmp_path: Path) -> tuple[str, RecordStore]:
    db_path = str(tmp_path / "test.db")
    run_migrations(db_path)
    return db_path, RecordStore(db_path)


# --- RecordStore reverse lookups -----------------------------------------------------


def test_get_raised_contribution_returns_none_when_nothing_was_raised(tmp_path: Path) -> None:
    db_path, rs = _quick_db(tmp_path)
    outcome_id, _ = _seed_outcome(db_path, "g_backend")
    assert rs.get_raised_contribution(outcome_id) is None
    assert rs.get_raised_operator_evidence(outcome_id) is None


def test_get_raised_contribution_finds_the_raise(tmp_path: Path) -> None:
    db_path, rs = _quick_db(tmp_path)
    directive_id = _seed_directive(rs, "g_arch")
    outcome_id, _ = _seed_outcome(db_path, "g_backend")
    _judgment, raised = rs.record_judgment_and_raise(
        contribution_id=outcome_id,
        decision="accept_as_context",
        judged_by="scope-manager",
        raise_contribution=RaisedContributionInput(
            scope_id="g_arch",
            content=f"evidence from g_backend: following {directive_id} went wrong: it failed.",
            subject="tls",
            contributor=_contributor("g_backend"),
            acted_on=directive_id,
            raised_from=outcome_id,
        ),
    )
    found = rs.get_raised_contribution(outcome_id)
    assert found is not None
    assert found.id == raised.id
    assert found.scope_id == "g_arch"
    assert rs.get_raised_operator_evidence(outcome_id) is None


def test_get_raised_operator_evidence_finds_the_raise(tmp_path: Path) -> None:
    db_path, rs = _quick_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet = FleetConfig.model_validate(
        {
            "strata": [{"id": "L0", "name": "Executive", "ordinal": 0}],
            "scopes": [{"id": "g_arch", "name": "Architect", "stratum_id": "L0"}],
            "edges": [],
        }
    )
    item = operator_publish(
        "g_arch",
        "Never deploy on a Friday.",
        "deploy-window",
        record_store=rs,
        summaries_dir=summaries_dir,
        fleet=fleet,
    )
    outcome_id, _ = _seed_outcome(db_path, "g_backend")
    rs.record_judgment_and_raise(
        contribution_id=outcome_id,
        decision="accept_as_context",
        judged_by="scope-manager",
        raise_operator_evidence=OperatorEvidenceInput(
            operator_item_id=item.id,
            raised_from=outcome_id,
            reporter=_contributor("g_backend"),
            content=f"evidence from g_backend: following {item.id} went wrong: it failed.",
        ),
    )
    evidence = rs.get_raised_operator_evidence(outcome_id)
    assert evidence is not None
    assert evidence.operator_item_id == item.id
    assert rs.get_raised_contribution(outcome_id) is None


# --- system-raised counts (CEO stats add) ---------------------------------------------


def test_count_raised_by_issuer_is_empty_when_nothing_was_raised(tmp_path: Path) -> None:
    db_path, rs = _quick_db(tmp_path)
    assert rs.count_raised_by_issuer() == {}
    assert rs.count_raised_operator_evidence() == 0


def test_count_raised_by_issuer_groups_by_the_issuing_scope(tmp_path: Path) -> None:
    db_path, rs = _quick_db(tmp_path)
    directive_id = _seed_directive(rs, "g_arch")
    outcome_a, _ = _seed_outcome(db_path, "g_backend")
    rs.record_judgment_and_raise(
        contribution_id=outcome_a,
        decision="accept_as_context",
        judged_by="scope-manager",
        raise_contribution=RaisedContributionInput(
            scope_id="g_arch",
            content=f"evidence from g_backend: following {directive_id} went wrong: it failed.",
            subject=None,
            contributor=_contributor("g_backend"),
            acted_on=directive_id,
            raised_from=outcome_a,
        ),
    )
    outcome_b, _ = _seed_outcome(db_path, "g_backend")
    rs.record_judgment_and_raise(
        contribution_id=outcome_b,
        decision="accept_as_context",
        judged_by="scope-manager",
        raise_contribution=RaisedContributionInput(
            scope_id="g_arch",
            content=(
                f"evidence from g_backend: following {directive_id} went wrong: it failed again."
            ),
            subject=None,
            contributor=_contributor("g_backend"),
            acted_on=directive_id,
            raised_from=outcome_b,
        ),
    )
    assert rs.count_raised_by_issuer() == {"g_arch": 2}
    assert rs.count_raised_operator_evidence() == 0


# --- strata_read_contribution's raised_to ---------------------------------------------


async def test_strata_read_contribution_shows_raised_to_a_scope_issuer(tmp_path: Path) -> None:
    fleet_path = _make_fleet_yaml(tmp_path)  # g_backend -> g_arch
    mod, db_path = _record_reader(tmp_path, fleet_path)
    directive_id = _seed_directive(RecordStore(db_path), "g_arch")
    outcome_id, rs = _seed_outcome(db_path, "g_backend")
    _judgment, raised = rs.record_judgment_and_raise(
        contribution_id=outcome_id,
        decision="accept_as_context",
        judged_by="scope-manager",
        raise_contribution=RaisedContributionInput(
            scope_id="g_arch",
            content=f"evidence from g_backend: following {directive_id} went wrong: it failed.",
            subject=None,
            contributor=_contributor("g_backend"),
            acted_on=directive_id,
            raised_from=outcome_id,
        ),
    )
    mod._record_store = RecordStore(db_path)
    fleet = FleetConfig.load(fleet_path)
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        result = await mod.strata_read_contribution(outcome_id)

    assert result["raised_to"] == {"scope_id": "g_arch", "contribution_id": raised.id}


async def test_strata_read_contribution_shows_raised_to_the_operator(tmp_path: Path) -> None:
    fleet_path = _make_fleet_yaml(tmp_path)
    mod, db_path = _record_reader(tmp_path, fleet_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_for_publish = FleetConfig.load(fleet_path)
    rs_seed = RecordStore(db_path)
    item = operator_publish(
        "g_arch",
        "Never deploy on a Friday.",
        "deploy-window",
        record_store=rs_seed,
        summaries_dir=summaries_dir,
        fleet=fleet_for_publish,
    )
    outcome_id, rs = _seed_outcome(db_path, "g_backend")
    rs.record_judgment_and_raise(
        contribution_id=outcome_id,
        decision="accept_as_context",
        judged_by="scope-manager",
        raise_operator_evidence=OperatorEvidenceInput(
            operator_item_id=item.id,
            raised_from=outcome_id,
            reporter=_contributor("g_backend"),
            content=f"evidence from g_backend: following {item.id} went wrong: it failed.",
        ),
    )
    evidence = rs.get_raised_operator_evidence(outcome_id)
    mod._record_store = RecordStore(db_path)
    fleet = FleetConfig.load(fleet_path)
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        result = await mod.strata_read_contribution(outcome_id)

    assert result["raised_to"] == {"operator": True, "evidence_id": evidence.id}


async def test_strata_read_contribution_raised_to_is_none_for_an_ordinary_contribution(
    tmp_path: Path,
) -> None:
    fleet_path = _make_fleet_yaml(tmp_path)
    mod, db_path = _record_reader(tmp_path, fleet_path)
    outcome_id, _rs = _seed_outcome(db_path, "g_backend")
    mod._record_store = RecordStore(db_path)
    fleet = FleetConfig.load(fleet_path)
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        result = await mod.strata_read_contribution(outcome_id)

    assert result["raised_to"] is None


# --- strata record CLI ----------------------------------------------------------------


def test_cmd_record_prints_the_raised_to_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(_FLEET_YAML, encoding="utf-8")
    db_path = tmp_path / "test.db"
    summaries_dir = tmp_path / "summaries"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRATA_FLEET_CONFIG", str(fleet_path))
    monkeypatch.setenv("STRATA_DB_PATH", str(db_path))
    monkeypatch.setenv("STRATA_SUMMARIES_DIR", str(summaries_dir))
    from strata.settings import get_settings

    get_settings.cache_clear()
    run_migrations(str(db_path))

    directive_id = _seed_directive(RecordStore(str(db_path)), "g_arch")
    outcome_id, rs = _seed_outcome(str(db_path), "g_backend")
    _judgment, raised = rs.record_judgment_and_raise(
        contribution_id=outcome_id,
        decision="accept_as_context",
        judged_by="scope-manager",
        raise_contribution=RaisedContributionInput(
            scope_id="g_arch",
            content=f"evidence from g_backend: following {directive_id} went wrong: it failed.",
            subject=None,
            contributor=_contributor("g_backend"),
            acted_on=directive_id,
            raised_from=outcome_id,
        ),
    )

    rc = main(["record", "g_backend"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"raised to g_arch as {raised.id}" in out
