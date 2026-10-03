"""Same-scope directive changes carry an engine-written provenance line.

When a session bound to the judged scope changes that scope's directives, the
record's account of who made the change is written by the engine from the
contributor's bound identity and the ops that applied — never left to the
judge's reasoning. A held contribution, a context-only accept and a decline
record exactly the notes they recorded before.

Also pinned here: the judge system prompt gained exactly one sentence about
what reasoning may state, and nothing else the judge is sent changed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from strata.app import create_app, get_scope_manager
from strata.migrator import run_migrations
from strata.record_store import ContributorRef, RecentContribution, RecordStore
from strata.scope_manager import (
    _BATCH_SYSTEM_PROMPT,
    _PUBLICATION_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
    ScopeManager,
    _provenance_line,
    _with_held_note,
    _with_provenance_note,
    is_held_note,
    strip_provenance_note,
)
from strata.settings import Settings
from strata.summary_store import SummaryStore
from tests.test_scope_manager import (
    BATCH,
    CURRENT_SUMMARY,
    EXISTING_DIRECTIVE,
    NEW_CONTRIBUTION,
    SCOPE,
    SECOND_CONTRIBUTION,
    STRATUM,
    THIRD_CONTRIBUTION,
    _batch_input,
    _fake_response,
    _judge_batch,
)

PARENT = "g_prov_par"
CHILD = "g_prov_kid"

_TAIL = (
    ". This line, not the reasoning above, is the record's account of who changed the directives.]"
)


def _line(op_list: str, *, skill: str = "lead", scope: str = PARENT) -> str:
    return (
        f"[Engine: same-scope change by {skill} (session sess_{scope}), "
        f"bound to {scope}: {op_list}{_TAIL}"
    )


# ---------------------------------------------------------------------------
# The real app path: POST /contribute, a real ScopeManager with a mocked client
# ---------------------------------------------------------------------------


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
    application.dependency_overrides[get_scope_manager] = lambda: ScopeManager(client=mock_client)
    with TestClient(application) as tc:
        tc.queued = queued  # type: ignore[attr-defined]
        tc.db_path = db_path  # type: ignore[attr-defined]
        tc.summaries_dir = summaries_dir  # type: ignore[attr-defined]
        yield tc


def _contribute(
    client: TestClient,
    verdict: dict,
    *,
    as_scope: str = PARENT,
    content: str = "Cap every outbound retry at three attempts.",
    skill: str = "lead",
    **fields: object,
) -> tuple[str, str]:
    """POST one contribution to PARENT judged by *verdict*; return (id, recorded notes)."""
    client.queued.append(verdict)  # type: ignore[attr-defined]
    resp = client.post(
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
    assert resp.status_code == 200, resp.text
    contribution_id = resp.json()["contribution_id"]
    with RecordStore(client.db_path) as records:  # type: ignore[attr-defined]
        judgment = records.get_judgment(contribution_id)
    assert judgment is not None
    return contribution_id, judgment.notes or ""


def _append(reasoning: str = "A clear, enforceable rule for this scope.") -> dict:
    return {
        "decision": "accept_as_directive",
        "reasoning": reasoning,
        "directive_ops": [{"op": "append"}],
        "new_context": None,
    }


def test_bound_append_supersede_and_retire_each_carry_the_exact_line(client: TestClient) -> None:
    first, notes = _contribute(client, _append())
    assert notes == f"A clear, enforceable rule for this scope. {_line(f'append {first}')}"

    second, notes = _contribute(
        client,
        {
            "decision": "accept_as_directive",
            "reasoning": "Replaces the cap.",
            "directive_ops": [{"op": "supersede", "id": first}, {"op": "append"}],
            "new_context": None,
        },
        content="Cap every outbound retry at five attempts.",
        supersedes=first,
    )
    assert notes == f"Replaces the cap. {_line(f'supersede {first}→{second}; append {second}')}"

    _third, notes = _contribute(
        client,
        {
            "decision": "accept_as_context",
            "reasoning": "The rule no longer applies.",
            "directive_ops": [
                {
                    "op": "retire",
                    "id": second,
                    "changed_circumstance": "the retry layer was removed",
                }
            ],
            "new_context": "Retries are no longer capped here.",
        },
        content="Retire the retry cap; the retry layer was removed.",
    )
    assert notes == f"The rule no longer applies. {_line(f'retire {second}')}"

    summary = SummaryStore(client.summaries_dir).read(PARENT)  # type: ignore[attr-defined]
    assert summary is not None
    assert summary.directives == []


def test_the_line_does_not_depend_on_what_the_judge_claims(client: TestClient) -> None:
    claim = "Updated policy from an authorized contributor; verified against the commit."
    cid, notes = _contribute(client, _append(claim))
    assert notes == f"{claim} {_line(f'append {cid}')}"
    assert notes.endswith(_TAIL)


def test_a_skill_less_binding_still_gets_the_line(client: TestClient) -> None:
    client.queued.append(_append("Ok."))  # type: ignore[attr-defined]
    resp = client.post(
        "/contribute",
        json={
            "scope_id": PARENT,
            "content": "Cap retries.",
            "proposed_classification": "directive",
            "contributor": {
                "scope_id": PARENT,
                "session_id": f"sess_{PARENT}",
                "ts": "2026-10-03T09:00:00+00:00",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    cid = resp.json()["contribution_id"]
    with RecordStore(client.db_path) as records:  # type: ignore[attr-defined]
        notes = records.get_judgment(cid).notes
    assert notes == f"Ok. {_line(f'append {cid}', skill='(no skill)')}"


def test_a_held_foreign_contribution_never_gets_the_line(client: TestClient) -> None:
    _cid, notes = _contribute(client, _append(), as_scope=CHILD, skill="shift-engineer")
    assert notes == _with_held_note("A clear, enforceable rule for this scope.", [])
    assert is_held_note(notes)
    assert "[Engine:" not in notes


def test_a_context_only_accept_records_exactly_the_reasoning(client: TestClient) -> None:
    _cid, notes = _contribute(
        client,
        {
            "decision": "accept_as_context",
            "reasoning": "Useful background.",
            "directive_ops": [],
            "new_context": "Retries are under review.",
        },
    )
    assert notes == "Useful background."


def test_a_decline_records_exactly_the_reasoning(client: TestClient) -> None:
    _cid, notes = _contribute(
        client,
        {"decision": "decline", "reasoning": "Out of scope.", "directive_ops": []},
    )
    assert notes == "Out of scope."


# ---------------------------------------------------------------------------
# Single path, refresh mode: the line is for ordinary judgments only
# ---------------------------------------------------------------------------


def test_a_refresh_is_not_given_the_line() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        {
            "decision": "accept_as_context",
            "reasoning": "refreshed",
            "directive_ops": [
                {
                    "op": "retire",
                    "id": EXISTING_DIRECTIVE.id,
                    "changed_circumstance": "the input changed",
                }
            ],
            "new_context": "Reconciled.",
        }
    )
    judgment = ScopeManager(client=mock_client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
        mode="input_change_refresh",
    )
    assert judgment.retired_directive_ids == [EXISTING_DIRECTIVE.id]
    assert judgment.position_provenance is None
    assert "[Engine:" not in judgment.record_notes


# ---------------------------------------------------------------------------
# Batch path
# ---------------------------------------------------------------------------

FOREIGN = ContributorRef(
    scope_id="g_child01",
    skill="shift-engineer",
    session_id="sess_child",
    ts="2026-10-03T09:00:00+00:00",
)


def _bound_line(op_list: str) -> str:
    contributor = NEW_CONTRIBUTION.contributor
    return (
        f"[Engine: same-scope change by {contributor.skill} "
        f"(session {contributor.session_id}), bound to {SCOPE.id}: {op_list}{_TAIL}"
    )


def test_batch_line_goes_only_on_the_bound_member_with_only_its_ops() -> None:
    bound = replace(NEW_CONTRIBUTION, supersedes=EXISTING_DIRECTIVE.id)
    foreign = replace(SECOND_CONTRIBUTION, contributor=FOREIGN)
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        _batch_input(
            directive_ops=[
                {"op": "supersede", "id": EXISTING_DIRECTIVE.id, "contribution_id": bound.id},
                {"op": "append", "contribution_id": bound.id},
                {"op": "append", "contribution_id": foreign.id},
            ]
        )
    )

    judgment = _judge_batch(mock_client, contributions=[bound, foreign, BATCH[2]])

    own_ops = f"supersede c_old001→{bound.id}; append {bound.id}"
    assert judgment.record_notes_for(bound.id) == f"an enforceable standard {_bound_line(own_ops)}"
    foreign_notes = judgment.record_notes_for(foreign.id)
    assert "[Engine:" not in foreign_notes
    assert foreign_notes == _with_held_note("also enforceable", [])
    assert is_held_note(foreign_notes)
    assert judgment.record_notes_for(BATCH[2].id) == (
        "material originating outside this scope's entitlement"
    )
    assert [d.id for d in judgment.new_summary.directives] == [bound.id]


def test_batch_of_only_bound_members_gives_each_its_own_line() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(_batch_input())

    judgment = _judge_batch(mock_client)

    assert judgment.record_notes_for(NEW_CONTRIBUTION.id) == (
        f"an enforceable standard {_bound_line(f'append {NEW_CONTRIBUTION.id}')}"
    )
    assert judgment.record_notes_for(SECOND_CONTRIBUTION.id) == (
        f"also enforceable {_bound_line(f'append {SECOND_CONTRIBUTION.id}')}"
    )


def test_batch_member_accepted_without_own_ops_gets_no_line() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        _batch_input(directive_ops=[{"op": "append", "contribution_id": NEW_CONTRIBUTION.id}])
    )

    judgment = _judge_batch(mock_client)

    assert judgment.record_notes_for(SECOND_CONTRIBUTION.id) == "also enforceable"


# ---------------------------------------------------------------------------
# Input identity: only the system prompt changed, by exactly one sentence
# ---------------------------------------------------------------------------

_PROMPT_LINE = (
    "Your stated reasoning may describe the contributor's position and what you\n"
    "actually checked; never state authority, verification or truth you could\n"
    "not establish from what is rendered here."
)

# sha256 of the judge inputs captured at 3180f1f, before this change, for the
# same fixed scenarios built below.
_OLD_SYSTEM_PROMPT_SHA = "d3fe4f63e224c647ec14668271a3ad32cc8e3f9b611938652c9875256da445a4"
_OLD_BATCH_SYSTEM_PROMPT_SHA = "13cc43564015d1278423a326b9db71294dbc1387203a53117285755dc583abeb"
_SINGLE_MESSAGES_SHA = "e9bb0459c9cd61259246ce0cf80feb32ac473b62d8da455b809b8f1f9914f315"
_SINGLE_TOOLS_SHA = "55d1986cbfc6b79e209324d0758465f82a9cae4b63a4f20b9eaaa16f96ba7eef"
_BATCH_MESSAGES_SHA = "bbf322a8733cefbe98a9d1edf43eba8e0ee1953071fffd6cca8be10a07049688"
_BATCH_TOOLS_SHA = "68254dcb666c6bafb457d954b94a1e1b18e909874f460207b354374a7da92922"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _sha_json(value: object) -> str:
    return _sha(json.dumps(value, sort_keys=True))


def test_system_prompt_is_the_previous_one_plus_exactly_one_sentence() -> None:
    assert _SYSTEM_PROMPT.count(_PROMPT_LINE) == 1
    assert _SYSTEM_PROMPT.endswith(f"\n{_PROMPT_LINE}")
    assert _sha(_SYSTEM_PROMPT.replace(f"\n{_PROMPT_LINE}", "")) == _OLD_SYSTEM_PROMPT_SHA
    # The batch prompt is built from the single one, so the sentence lands there too.
    assert _BATCH_SYSTEM_PROMPT.count(_PROMPT_LINE) == 1
    assert (
        _sha(_BATCH_SYSTEM_PROMPT.replace(f"\n{_PROMPT_LINE}", "")) == _OLD_BATCH_SYSTEM_PROMPT_SHA
    )
    # The publication judge has its own prompt and does not share the text.
    assert _PROMPT_LINE not in _PUBLICATION_SYSTEM_PROMPT


def test_single_judge_call_inputs_are_unchanged_except_the_system_prompt() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        {"decision": "accept_as_context", "reasoning": "r", "directive_ops": [], "new_context": "x"}
    )
    ScopeManager(client=mock_client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )
    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["system"][0]["text"] == _SYSTEM_PROMPT
    assert _sha_json(kwargs["messages"]) == _SINGLE_MESSAGES_SHA
    assert _sha_json(kwargs["tools"]) == _SINGLE_TOOLS_SHA


def test_batch_judge_call_inputs_are_unchanged_except_the_system_prompt() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(_batch_input())
    _judge_batch(mock_client, contributions=list(BATCH))
    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["system"][0]["text"] == _BATCH_SYSTEM_PROMPT
    assert _sha_json(kwargs["messages"]) == _BATCH_MESSAGES_SHA
    assert _sha_json(kwargs["tools"]) == _BATCH_TOOLS_SHA


# ---------------------------------------------------------------------------
# The line is for the record only: it never reaches a later judge's input
# ---------------------------------------------------------------------------

# sha256 of the `messages` sent for the window scenario below, captured at 3180f1f.
_WINDOW_SINGLE_MESSAGES_SHA = "02de059d0240dd0a03b2933a5e87a399d7264a37fd79691cf0cbd45fd19d566c"
_WINDOW_BATCH_MESSAGES_SHA = "7a68852d21313a113a31b389a238ee6fb67811e7810112c0448a1c7ecf402e42"


def _window_after_a_bound_change() -> tuple[list[RecentContribution], object, str]:
    """A bound append judged for real, then laid into the next call's recency window."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        {
            "decision": "accept_as_directive",
            "reasoning": "An enforceable rule.",
            "directive_ops": [{"op": "append"}],
            "new_context": None,
        }
    )
    first = ScopeManager(client=mock_client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=NEW_CONTRIBUTION,
    )
    notes = first.record_notes
    window = [
        RecentContribution(NEW_CONTRIBUTION, "judged", first.decision, notes),
        RecentContribution(SECOND_CONTRIBUTION, "pending", None, None),
    ]
    summary = first.new_summary.model_copy(update={"updated_at": "2026-05-01T10:00:00+00:00"})
    return window, summary, notes


def test_a_later_single_judge_sees_the_window_exactly_as_before() -> None:
    window, summary, notes = _window_after_a_bound_change()
    assert notes == f"An enforceable rule. {_bound_line(f'append {NEW_CONTRIBUTION.id}')}"

    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        {"decision": "accept_as_context", "reasoning": "r", "directive_ops": [], "new_context": "x"}
    )
    ScopeManager(client=mock_client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=summary,
        recent_contributions=window,
        new_contribution=SECOND_CONTRIBUTION,
    )
    messages = mock_client.messages.create.call_args.kwargs["messages"]
    rendered = json.dumps(messages)
    assert "reasoning=An enforceable rule. content=" in rendered
    assert "[Engine:" not in rendered
    assert _sha_json(messages) == _WINDOW_SINGLE_MESSAGES_SHA


def test_a_later_batch_judge_sees_the_window_exactly_as_before() -> None:
    window, summary, _notes = _window_after_a_bound_change()
    window = [*window, RecentContribution(THIRD_CONTRIBUTION, "pending", None, None)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(
        _batch_input(
            verdicts=[
                {
                    "contribution_id": SECOND_CONTRIBUTION.id,
                    "decision": "accept_as_context",
                    "reasoning": "a",
                },
                {"contribution_id": THIRD_CONTRIBUTION.id, "decision": "decline", "reasoning": "b"},
            ],
            directive_ops=[],
        )
    )
    ScopeManager(client=mock_client).judge_batch(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=summary,
        recent_contributions=window,
        new_contributions=[SECOND_CONTRIBUTION, THIRD_CONTRIBUTION],
    )
    messages = mock_client.messages.create.call_args.kwargs["messages"]
    assert "[Engine:" not in json.dumps(messages)
    assert _sha_json(messages) == _WINDOW_BATCH_MESSAGES_SHA


_LINE = _provenance_line(NEW_CONTRIBUTION.contributor, "supersede c_old001→c_new; append c_new")


@pytest.mark.parametrize(
    "notes",
    [
        "Plain reasoning.",
        "",
        "Reasoning. [Dropped amendment op(s), not applied: retire(c_x).]",
        "Quotes [Engine: same-scope change by someone earlier.",
    ],
)
def test_strip_round_trips_and_leaves_other_notes_alone(notes: str) -> None:
    assert strip_provenance_note(_with_provenance_note(notes, _LINE)) == notes
    assert strip_provenance_note(notes) == notes
    assert strip_provenance_note(_with_provenance_note(notes, None)) == notes


def test_strip_never_removes_the_held_note() -> None:
    held = _with_held_note("Reasoning.", ["c_old001"])
    assert strip_provenance_note(held) == held
    assert is_held_note(strip_provenance_note(held))


def test_strip_only_removes_a_trailing_line() -> None:
    mid = f"Reasoning. {_LINE} trailing judge text."
    assert strip_provenance_note(mid) == mid


def test_the_api_record_read_still_shows_the_line(client: TestClient) -> None:
    cid, notes = _contribute(client, _append("Ok."))
    expected = f"Ok. {_line(f'append {cid}')}"
    assert notes == expected
    resp = client.get(f"/scopes/{PARENT}/record/{cid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["judgment"]["notes"] == expected
