"""The adoption link: a scope-bound session adopts a held proposal.

A contribution from a session bound to another scope cannot change a scope's
directives (the position gate) — it is admitted as an attributed proposal. A
session bound to that scope adopts the proposal by an ORDINARY own-scope
contribution carrying ``adopted_from=<the proposal's id>``. The write boundary
validates the link, the judge is shown what is adopted, and the directive it
admits carries the link as provenance. Nothing renders when the link is unset.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager, validate_adopted_from
from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.record_store import Contribution, ContributorRef, RecordStore
from strata.scope_manager import (
    ScopeManager,
    _build_batch_user_message,
    _build_user_message,
    _render_contribution_block,
    _render_directives_only,
    _with_held_note,
    is_held_note,
)
from strata.settings import Settings
from strata.summary_store import Directive, ScopeSummary, SummaryStore, _parse_summary
from strata.summary_store import _render_summary as _render_summary_markdown
from tests.test_mcp_server import _load_mcp_module, _make_db, _make_fleet_yaml
from tests.test_scope_manager import (
    NEW_CONTRIBUTION,
    SCOPE,
    SECOND_CONTRIBUTION,
    STRATUM,
    _batch_input,
    _fake_response,
    _judge_batch,
)

PARENT = "g_adopt_par"
CHILD = "g_adopt_kid"

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "src" / "strata" / "_migrations"

_APPEND_DIRECTIVE = {
    "decision": "accept_as_directive",
    "reasoning": "A clear, enforceable rule for this scope.",
    "directive_ops": [{"op": "append"}],
    "new_context": "Retries are bounded across the service.",
}

_PROPOSAL_TEXT = "Cap every outbound retry at three attempts."


def _write_fleet(tmp_path: Path) -> str:
    path = tmp_path / "fleet.yaml"
    path.write_text(
        yaml.dump(
            {
                "strata": [
                    {"id": "L0", "name": "executive", "ordinal": 0},
                    {"id": "L1", "name": "team", "ordinal": 1},
                ],
                "scopes": [
                    {"id": PARENT, "name": "Parent", "stratum_id": "L0"},
                    {"id": CHILD, "name": "Child", "stratum_id": "L1"},
                ],
                "edges": [{"from": CHILD, "to": PARENT}],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture()
def client(tmp_path: Path):  # noqa: ANN201
    """A TestClient over the real app, judged by a real ScopeManager whose
    Anthropic client is mocked: each test queues the tool inputs it returns."""
    db_path = str(tmp_path / "strata.db")
    summaries_dir = str(tmp_path / "summaries")
    run_migrations(db_path)
    settings = Settings(
        db_path=db_path,
        summaries_dir=summaries_dir,
        fleet_yaml_path=_write_fleet(tmp_path),
        manager_model="claude-haiku-4-5",
        anthropic_api_key="test-key",
    )
    application = create_app(settings=settings)
    mock_client = MagicMock()
    queued: list[dict] = []
    mock_client.messages.create.side_effect = lambda **_kw: _fake_response(queued.pop(0))
    manager = ScopeManager(client=mock_client)
    application.dependency_overrides[get_scope_manager] = lambda: manager
    with TestClient(application) as tc:
        tc.queued = queued  # type: ignore[attr-defined]
        tc.mock_client = mock_client  # type: ignore[attr-defined]
        tc.db_path = db_path  # type: ignore[attr-defined]
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        yield tc


def _post(
    client: TestClient,
    *,
    as_scope: str,
    content: str,
    skill: str = "lead",
    **fields: object,
):  # noqa: ANN202
    return client.post(
        "/contribute",
        json={
            "scope_id": PARENT,
            "content": content,
            "proposed_classification": "directive",
            "contributor": {
                "scope_id": as_scope,
                "skill": skill,
                "session_id": f"sess_{as_scope}",
                "ts": "2026-10-03T09:00:00+00:00",
            },
            **fields,
        },
    )


def _child_proposal(client: TestClient) -> str:
    """A child-bound directive proposal to the parent — held by the position gate."""
    client.queued.append(_APPEND_DIRECTIVE)  # type: ignore[attr-defined]
    resp = _post(client, as_scope=CHILD, content=_PROPOSAL_TEXT, skill="shift-engineer")
    assert resp.status_code == 200, resp.text
    assert resp.json()["judgment"]["decision"] == "accept_as_context"
    return resp.json()["contribution_id"]


def _user_message(mock_client: MagicMock, index: int) -> str:
    return str(mock_client.messages.create.call_args_list[index].kwargs["messages"])


# ---------------------------------------------------------------------------
# End to end through POST /contribute
# ---------------------------------------------------------------------------


def test_a_parent_bound_session_adopts_a_held_child_proposal(client: TestClient) -> None:
    proposal_id = _child_proposal(client)
    store = SummaryStore(client.summaries_dir)  # type: ignore[attr-defined]
    held = store.read(PARENT)
    assert held is not None
    assert held.directives == []
    assert f"[{proposal_id}] shift-engineer ({CHILD}) proposes: {_PROPOSAL_TEXT}" in held.context
    assert "adopts proposal" not in _user_message(client.mock_client, 0)  # type: ignore[attr-defined]

    client.queued.append(_APPEND_DIRECTIVE)  # type: ignore[attr-defined]
    resp = _post(client, as_scope=PARENT, content=_PROPOSAL_TEXT, adopted_from=proposal_id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["judgment"]["decision"] == "accept_as_directive"
    adopting_id = resp.json()["contribution_id"]

    # The judge saw what is adopted, in the NEW CONTRIBUTION block.
    assert (
        f"- adopts proposal {proposal_id} by shift-engineer ({CHILD}): {_PROPOSAL_TEXT}"
        in _user_message(client.mock_client, 1)  # type: ignore[attr-defined]
    )

    # The minted directive carries the link, and it survives the on-disk round trip.
    summary = store.read(PARENT)
    assert summary is not None
    assert [(d.id, d.adopted_from) for d in summary.directives] == [(adopting_id, proposal_id)]
    on_disk = store.path_for(PARENT).read_text(encoding="utf-8")
    assert f"(adopted from proposal {proposal_id})" in on_disk

    # The record keeps the link on every read path.
    with RecordStore(client.db_path) as records:  # type: ignore[attr-defined]
        assert records.get_contribution(adopting_id).adopted_from == proposal_id
        entry = records.get_record_entry(adopting_id)
        assert entry is not None
        assert entry.contribution.adopted_from == proposal_id
        listed = {c.id: c.adopted_from for c in records.list_contributions(scope_id=PARENT)}
        assert listed == {proposal_id: None, adopting_id: proposal_id}


# ---------------------------------------------------------------------------
# Validation at the write boundary
# ---------------------------------------------------------------------------


def _assert_rejected(resp, match: str) -> None:  # noqa: ANN001
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "adopted_from_invalid"
    assert match in detail["detail"]


def _append_raw(db_path: str, *, scope_id: str, as_scope: str) -> str:
    with RecordStore(db_path) as records:
        return records.append_contribution(
            scope_id=scope_id,
            content="Some proposal.",
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=ContributorRef(
                scope_id=as_scope, skill="x", session_id="s", ts="2026-10-03T00:00:00Z"
            ),
        ).id


def _judged_raw(db_path: str, *, decision: str, notes: str | None, as_scope: str = CHILD) -> str:
    """A contribution to the parent from *as_scope*, with the given verdict on record."""
    cid = _append_raw(db_path, scope_id=PARENT, as_scope=as_scope)
    with RecordStore(db_path) as records:
        records.record_judgment(
            contribution_id=cid, decision=decision, judged_by="scope-manager", notes=notes
        )
    return cid


_HELD_NOTES = _with_held_note("A proposal from a child scope.", [])


def test_adopted_from_must_reference_an_existing_contribution(client: TestClient) -> None:
    resp = _post(client, as_scope=PARENT, content="Rule.", adopted_from="c_missing")
    _assert_rejected(resp, "does not reference an existing contribution")


def test_adopted_from_must_live_in_the_target_scopes_record(client: TestClient) -> None:
    elsewhere = _append_raw(client.db_path, scope_id=CHILD, as_scope=CHILD)  # type: ignore[attr-defined]
    resp = _post(client, as_scope=PARENT, content="Rule.", adopted_from=elsewhere)
    _assert_rejected(resp, f"is a contribution to {CHILD!r}, not to {PARENT!r}")


def test_adopted_from_must_come_from_another_position(client: TestClient) -> None:
    own = _append_raw(client.db_path, scope_id=PARENT, as_scope=PARENT)  # type: ignore[attr-defined]
    resp = _post(client, as_scope=PARENT, content="Rule.", adopted_from=own)
    _assert_rejected(resp, "it is not a proposal from another position")


def test_only_a_session_bound_to_the_target_scope_adopts(client: TestClient) -> None:
    proposal = _append_raw(client.db_path, scope_id=PARENT, as_scope=CHILD)  # type: ignore[attr-defined]
    resp = _post(client, as_scope=CHILD, content="Rule.", adopted_from=proposal)
    _assert_rejected(resp, f"only a session bound to {PARENT!r} can adopt")


def test_adopted_from_with_acted_on_is_rejected(client: TestClient) -> None:
    proposal = _append_raw(client.db_path, scope_id=PARENT, as_scope=CHILD)  # type: ignore[attr-defined]
    resp = _post(client, as_scope=PARENT, content="Rule.", adopted_from=proposal, acted_on=proposal)
    _assert_rejected(resp, "adopted_from and acted_on cannot both be set")


def test_rejections_write_nothing_and_call_no_judge(client: TestClient) -> None:
    proposal = _append_raw(client.db_path, scope_id=PARENT, as_scope=CHILD)  # type: ignore[attr-defined]
    _post(client, as_scope=CHILD, content="Rule.", adopted_from=proposal)
    with RecordStore(client.db_path) as records:  # type: ignore[attr-defined]
        assert [c.id for c in records.list_contributions(scope_id=PARENT)] == [proposal]
    client.mock_client.messages.create.assert_not_called()  # type: ignore[attr-defined]


def test_adopted_from_with_supersedes_is_allowed(tmp_path: Path) -> None:
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    proposal = _judged_raw(db_path, decision="accept_as_context", notes=_HELD_NOTES)
    with RecordStore(db_path) as records:
        # Validation takes no `supersedes` at all: adopting may replace a directive.
        validate_adopted_from(
            records, adopted_from=proposal, acted_on=None, scope_id=PARENT, agent_scope=PARENT
        )
        # And unset is a no-op whatever else is passed.
        validate_adopted_from(
            records, adopted_from=None, acted_on="c_x", scope_id=PARENT, agent_scope=CHILD
        )


def test_an_unjudged_proposal_cannot_be_adopted(client: TestClient) -> None:
    pending = _append_raw(client.db_path, scope_id=PARENT, as_scope=CHILD)  # type: ignore[attr-defined]
    resp = _post(client, as_scope=PARENT, content="Rule.", adopted_from=pending)
    _assert_rejected(resp, "not yet judged")


def test_a_declined_proposal_cannot_be_adopted(client: TestClient) -> None:
    declined = _judged_raw(
        client.db_path,  # type: ignore[attr-defined]
        decision="decline",
        notes="Duplicates an existing rule.",
    )
    resp = _post(client, as_scope=PARENT, content="Rule.", adopted_from=declined)
    _assert_rejected(resp, "was declined, so there is nothing held to adopt")


@pytest.mark.parametrize(
    ("decision", "notes"),
    [
        ("accept_as_context", "Plain context, nothing held."),
        ("accept_as_context", None),
        # The held note must END the notes: quoted mid-reasoning it is not a hold.
        ("accept_as_context", _HELD_NOTES + " And more reasoning after it."),
        ("accept_as_directive", _HELD_NOTES),
    ],
)
def test_a_proposal_admitted_without_a_hold_cannot_be_adopted(
    client: TestClient, decision: str, notes: str | None
) -> None:
    admitted = _judged_raw(client.db_path, decision=decision, notes=notes)  # type: ignore[attr-defined]
    resp = _post(client, as_scope=PARENT, content="Rule.", adopted_from=admitted)
    _assert_rejected(resp, "was not held as a proposal by the position gate")


def test_is_held_note_matches_exactly_what_with_held_note_writes() -> None:
    reasoning = "Informative, from a child scope."
    targeted = _with_held_note(reasoning, ["c_old001", "c_old002"])
    new_directive = _with_held_note(reasoning, [])
    assert is_held_note(targeted)
    assert is_held_note(new_directive)
    assert not is_held_note(reasoning)
    assert not is_held_note(_with_held_note(reasoning, None))
    assert not is_held_note(None)
    assert not is_held_note(targeted + " [Some later note.]")


def test_with_held_note_output_bytes_are_unchanged() -> None:
    tail = (
        ". The contributor is not bound to this scope, so this is admitted as an "
        "attributed proposal; a session bound to this scope may adopt it.]"
    )
    assert _with_held_note("R.", ["c_a", "c_b"]) == (
        "R. [Held: would change directive c_a, c_b, which stands" + tail
    )
    assert _with_held_note("R.", []) == "R. [Held: proposed a new directive, not adopted" + tail
    assert _with_held_note("R.", None) == "R."


async def test_the_mcp_tool_rejects_an_upward_adoption(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    fleet_path = _make_fleet_yaml(tmp_path)
    module = _load_mcp_module(db_path, str(tmp_path / "summaries"), str(fleet_path))
    proposal = _append_raw(db_path, scope_id="g_arch", as_scope="g_backend")
    with (
        patch.object(module, "_AGENT_SCOPE", "g_backend"),
        patch.object(module, "_AGENT_SKILL", "strata-developer"),
        patch.object(module, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(module, "_load_fleet", return_value=FleetConfig.load(fleet_path)),
        patch("anthropic.Anthropic", return_value=MagicMock()),
        patch("strata.scope_manager.ScopeManager.judge") as judge,
        pytest.raises(RuntimeError, match="only a session bound to 'g_arch' can adopt"),
    ):
        await module.strata_contribute(
            scope_id="g_arch",
            content="Adopt it.",
            proposed_classification="directive",
            adopted_from=proposal,
        )
    judge.assert_not_called()


# ---------------------------------------------------------------------------
# Migration 0022 on a populated 0021 database
# ---------------------------------------------------------------------------


def _migrations_dir_up_to(tmp_path: Path, last_name: str) -> Path:
    scratch = tmp_path / f"migrations_through_{last_name}"
    scratch.mkdir()
    for f in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        (scratch / f.name).write_bytes(f.read_bytes())
        if f.name == last_name:
            break
    return scratch


def test_migration_0022_applies_on_a_populated_0021_database(tmp_path: Path) -> None:
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path, migrations_dir=_migrations_dir_up_to(tmp_path, "0021_restore_act.sql"))
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for cid, acted_on in (("c_1", None), ("c_2", "c_1")):
            conn.execute(
                "INSERT INTO contributions (id, scope_id, content, proposed_classification, "
                "contributor_scope_id, contributor_skill, contributor_session_id, "
                "contributor_ts, acted_on) VALUES (?, 'g_x', 'Port is 8443.', 'context', "
                "'g_x', 'eng', 's1', '2026-01-01T00:00:00Z', ?)",
                (cid, acted_on),
            )
        conn.execute(
            "INSERT INTO judgments (id, contribution_id, decision, judged_by) "
            "VALUES ('j_1', 'c_1', 'accept_as_context', 'scope-manager')"
        )
        conn.commit()
    finally:
        conn.close()

    assert run_migrations(db_path) == [
        "0022_adopted_from.sql",
        "0023_fleet_structure_act.sql",
        "0024_judge_usage.sql",
    ]

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        rows = conn.execute(
            "SELECT id, acted_on, adopted_from FROM contributions ORDER BY id"
        ).fetchall()
        assert rows == [("c_1", None, None), ("c_2", "c_1", None)]
        assert conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    with RecordStore(db_path) as records:
        assert records.get_contribution("c_2").adopted_from is None


# ---------------------------------------------------------------------------
# Input identity: nothing renders when the link is unset
# ---------------------------------------------------------------------------

_CONTRIBUTOR = ContributorRef(
    scope_id=SCOPE.id, skill="code-writer", session_id="sess_001", ts="2026-05-01T10:00:00+00:00"
)
_PLAIN = Contribution(
    id="c_plain1",
    scope_id=SCOPE.id,
    content="All new modules must include type annotations.",
    proposed_classification="directive",
    subject="type-annotations",
    supersedes=None,
    contributor=_CONTRIBUTOR,
    created_at="2026-05-01T10:00:00+00:00",
)
_DIRECTIVE = Directive(
    id="c_old001",
    content="Use snake_case for all identifiers.",
    subject="naming",
    source_scope_id=SCOPE.id,
    source_skill="architect",
    created_at="2026-04-01T09:00:00+00:00",
)


def test_a_contribution_without_adopted_from_renders_exactly_as_before() -> None:
    expected = (
        "- id: c_plain1\n"
        "- proposed classification: directive\n"
        "- subject: type-annotations\n"
        "- supersedes: (none)\n"
        f"- contributor: skill=code-writer scope={SCOPE.id} at=2026-05-01T10:00:00+00:00\n"
        "- content:\n"
        "    All new modules must include type annotations.\n"
    )
    assert _render_contribution_block(_PLAIN) == expected
    assert _render_contribution_block(_PLAIN, None) == expected


def test_the_user_message_is_unchanged_without_an_adopted_proposal() -> None:
    kwargs = {
        "scope": SCOPE,
        "stratum": STRATUM,
        "ancestor_directives": None,
        "current_summary": None,
        "recent_contributions": [],
        "new_contribution": _PLAIN,
    }
    assert _build_user_message(**kwargs) == _build_user_message(**kwargs, adopted_proposal=None)
    assert "adopts proposal" not in _build_user_message(**kwargs)


def test_a_directive_without_adopted_from_renders_exactly_as_before() -> None:
    summary = ScopeSummary(
        scope_id=SCOPE.id,
        directives=[_DIRECTIVE],
        context="",
        updated_at="2026-04-01T09:00:00+00:00",
    )
    rendered = _render_summary_markdown(summary)
    assert (
        "### [c_old001] Use snake_case for all identifiers.\n"
        "- subject: naming\n"
        f"- source: scope={SCOPE.id} · skill=architect · at=2026-04-01T09:00:00+00:00\n"
        "\n"
        "> Use snake_case for all identifiers.\n"
    ) in rendered
    assert "adopted from" not in rendered
    assert _render_directives_only([_DIRECTIVE]) == (
        "### [c_old001] Use snake_case for all identifiers.\n"
        "- subject: naming\n"
        f"- source: scope={SCOPE.id} · at=2026-04-01T09:00:00+00:00"
    )
    assert _parse_summary(rendered).directives == [_DIRECTIVE]


def test_an_adopted_directive_round_trips_its_link() -> None:
    adopted = _DIRECTIVE.model_copy(update={"adopted_from": "c_prop01"})
    skill_less = adopted.model_copy(update={"id": "c_old002", "source_skill": None})
    summary = ScopeSummary(
        scope_id=SCOPE.id,
        directives=[adopted, skill_less],
        context="",
        updated_at="2026-04-01T09:00:00+00:00",
    )
    rendered = _render_summary_markdown(summary)
    assert "at=2026-04-01T09:00:00+00:00 (adopted from proposal c_prop01)" in rendered
    parsed = _parse_summary(rendered).directives
    assert parsed == [adopted, skill_less]
    assert parsed[0].created_at == "2026-04-01T09:00:00+00:00"
    assert _render_directives_only([adopted]).endswith(
        "at=2026-04-01T09:00:00+00:00 (adopted from proposal c_prop01)"
    )


def test_a_batch_renders_the_adoption_under_the_adopting_member_only() -> None:
    proposal = replace(
        _PLAIN,
        id="c_prop01",
        content="Cap retries at three.",
        contributor=ContributorRef(
            scope_id="g_kid", skill="shift-engineer", session_id="s", ts="t"
        ),
    )
    adopting = replace(_PLAIN, id="c_adopt1", adopted_from="c_prop01")
    kwargs = {
        "scope": SCOPE,
        "stratum": STRATUM,
        "ancestor_directives": None,
        "current_summary": None,
        "recent_contributions": [],
        "new_contributions": [_PLAIN, adopting],
    }
    plain = _build_batch_user_message(**kwargs)
    message = _build_batch_user_message(**kwargs, adopted_proposals={"c_adopt1": proposal})
    line = "- adopts proposal c_prop01 by shift-engineer (g_kid): Cap retries at three.\n"
    assert "adopts proposal" not in plain
    assert message.count(line) == 1
    assert message.index("CONTRIBUTION 2 OF 2") < message.index(line)
    assert message.replace(line, "") == plain


# ---------------------------------------------------------------------------
# A directive proposal with no ops is still recorded as held (and adoptable)
# ---------------------------------------------------------------------------

_DIRECTIVE_NO_OPS = {
    "decision": "accept_as_directive",
    "reasoning": "A binding rule for this scope.",
    "directive_ops": [],
    "new_context": "Retries are bounded across the service.",
}


def test_a_foreign_directive_with_no_ops_is_held_with_the_note_and_adoptable(
    client: TestClient,
) -> None:
    client.queued.append(_DIRECTIVE_NO_OPS)  # type: ignore[attr-defined]
    resp = _post(client, as_scope=CHILD, content=_PROPOSAL_TEXT, skill="shift-engineer")
    assert resp.status_code == 200, resp.text
    assert resp.json()["judgment"]["decision"] == "accept_as_context"
    proposal_id = resp.json()["contribution_id"]
    with RecordStore(client.db_path) as records:  # type: ignore[attr-defined]
        notes = records.get_judgment(proposal_id).notes
    assert notes.endswith("[Held: proposed a new directive, not adopted" + _HELD_TAIL)
    assert is_held_note(notes)

    client.queued.append(_APPEND_DIRECTIVE)  # type: ignore[attr-defined]
    resp = _post(client, as_scope=PARENT, content=_PROPOSAL_TEXT, adopted_from=proposal_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["judgment"]["decision"] == "accept_as_directive"


def test_an_own_scope_directive_with_no_ops_has_no_held_note(client: TestClient) -> None:
    client.queued.append(_DIRECTIVE_NO_OPS)  # type: ignore[attr-defined]
    resp = _post(client, as_scope=PARENT, content=_PROPOSAL_TEXT)
    assert resp.status_code == 200, resp.text
    with RecordStore(client.db_path) as records:  # type: ignore[attr-defined]
        notes = records.get_judgment(resp.json()["contribution_id"]).notes
    assert "[Held:" not in notes
    assert not is_held_note(notes)


def test_a_batch_records_the_held_note_for_a_foreign_directive_with_no_ops(
    tmp_path: Path,
) -> None:
    foreign = replace(
        NEW_CONTRIBUTION,
        contributor=ContributorRef(
            scope_id="g_child01", skill="shift-engineer", session_id="s", ts="t"
        ),
    )
    own = SECOND_CONTRIBUTION
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        _batch_input(
            verdicts=[
                {
                    "contribution_id": foreign.id,
                    "decision": "accept_as_directive",
                    "reasoning": "binding",
                },
                {
                    "contribution_id": own.id,
                    "decision": "accept_as_directive",
                    "reasoning": "also binding",
                },
            ],
            directive_ops=[],
        )
    )
    judgment = _judge_batch(mock_client, contributions=[foreign, own])

    assert judgment.held_by_contribution == {foreign.id: []}
    foreign_notes = judgment.record_notes_for(foreign.id)
    assert foreign_notes.endswith("[Held: proposed a new directive, not adopted" + _HELD_TAIL)
    assert is_held_note(foreign_notes)
    assert not is_held_note(judgment.record_notes_for(own.id))

    # The recorded row makes the proposal adoptable by the scope's own session.
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    with RecordStore(db_path) as records:
        cid = records.append_contribution(
            scope_id=SCOPE.id,
            content=foreign.content,
            proposed_classification="directive",
            subject=None,
            supersedes=None,
            contributor=foreign.contributor,
        ).id
        records.record_judgment(
            contribution_id=cid,
            decision="accept_as_context",
            judged_by="scope-manager",
            notes=foreign_notes,
        )
        validate_adopted_from(
            records, adopted_from=cid, acted_on=None, scope_id=SCOPE.id, agent_scope=SCOPE.id
        )


_HELD_TAIL = (
    ". The contributor is not bound to this scope, so this is admitted as an "
    "attributed proposal; a session bound to this scope may adopt it.]"
)
