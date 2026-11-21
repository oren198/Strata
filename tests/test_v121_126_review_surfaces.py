"""#126 / ADR 0018 D6: what the judge, the agent and the operator see of held items.

The judge's recency digest renders a held item AS held (never as precedent); a level-0 digest is
byte-identical. The Console's HTTP perspective shows the same ``review`` list as the MCP read.
An approval depends on the scope's position gate, not on the requesting session still being live.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from strata import review
from strata.app import create_app
from strata.scope_manager import _render_digest_row
from strata.settings import Settings
from tests.test_mcp_server import _load_mcp_module
from tests.v121_126_support import NEW_RULE, World, directive_answer


@pytest.fixture
def world0(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path)
    yield w
    w.close()


@pytest.fixture
def world1(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path, {"g_org": 1})
    yield w
    w.close()


def _old_format(row) -> str:  # noqa: ANN001
    """The digest row exactly as it was rendered before review levels existed."""
    from strata.scope_manager import _content_excerpt, strip_notes_for_judge  # noqa: PLC0415

    c = row.contribution
    return (
        f"[{c.id}] at={c.created_at} subject={c.subject or '(none)'} "
        f"state={row.state} decision={row.decision or '(none)'} "
        f"reasoning={strip_notes_for_judge(row.judgment_notes or '') or '(none)'} "
        f"content={_content_excerpt(c.content)!r}"
    )


def _row(w: World):  # noqa: ANN202
    (row,) = w.store.list_recent_contributions(scope_id="g_org", limit=20)
    return row


def test_a_level_zero_digest_row_is_byte_identical(world0: World) -> None:
    world0.propose(directive_answer([{"op": "append"}]))
    row = _row(world0)
    assert row.disposition is None
    assert _render_digest_row(row, verbatim=False) == _old_format(row)
    assert "state=judged decision=accept_as_directive" in _render_digest_row(row, verbatim=False)


def test_a_held_item_is_rendered_as_held_never_as_precedent(world1: World) -> None:
    world1.propose(directive_answer([{"op": "append"}]))
    text = _render_digest_row(_row(world1), verbatim=True)
    assert "held for the operator (judge said accept_as_directive)" in text
    assert "state=judged" not in text and "decision=accept_as_directive" not in text


def test_an_approved_item_is_rendered_as_the_admitted_directive_it_became(world1: World) -> None:
    outcome = world1.propose(directive_answer([{"op": "append"}]))
    review.approve(outcome.contribution_id, operator="oren", **world1.kw())
    text = _render_digest_row(_row(world1), verbatim=True)
    assert "state=judged decision=accept_as_directive" in text and "held" not in text


# -- the HTTP perspective shows what the MCP perspective shows ----------------------------


def _client(w: World) -> TestClient:
    settings = Settings(
        db_path=str(w.tmp_path / "strata.db"),
        summaries_dir=str(w.tmp_path / "summaries"),
        fleet_yaml_path=str(w.tmp_path / "fleet.yaml"),
        manager_model="claude-haiku-4-5",
        anthropic_api_key="k",
    )
    return TestClient(create_app(settings=settings))


async def test_the_http_perspective_shows_the_same_review_list_as_mcp(world1: World) -> None:
    from unittest.mock import patch  # noqa: PLC0415

    world1.propose(directive_answer([{"op": "append"}]))
    with _client(world1) as client:
        http = client.get("/scopes/g_org/perspective").json()
    http_review = next(layer for layer in http["layers"] if layer["relation"] == "self")["review"]

    mod = _load_mcp_module(
        str(world1.tmp_path / "strata.db"),
        str(world1.tmp_path / "summaries"),
        str(world1.tmp_path / "fleet.yaml"),
    )
    with patch.multiple(mod, _AGENT_SCOPE="g_org", _AGENT_SKILL=None, _AGENT_SESSION_ID="s_g_org"):
        mcp = await mod.strata_read_perspective()
    mcp_review = next(layer for layer in mcp["layers"] if layer["relation"] == "self")["review"]

    assert http_review == mcp_review
    assert "held for the operator" in http_review[0]["text"]
    assert NEW_RULE in http_review[0]["text"]


def test_a_scope_with_nothing_to_review_has_no_review_key(world0: World) -> None:
    with _client(world0) as client:
        http = client.get("/scopes/g_org/perspective").json()
    assert all("review" not in layer for layer in http["layers"])


# -- an approval needs the position gate, not a live session ------------------------------


def test_approve_still_works_after_the_requesting_session_is_gone(world1: World) -> None:
    outcome = world1.propose(directive_answer([{"op": "append"}]), session="s_long_gone")
    # no session store, no heartbeat, nothing: the contributor's session simply no longer exists
    review.approve(outcome.contribution_id, operator="oren", **world1.kw())
    assert NEW_RULE in [d.content for d in world1.summaries.read("g_org").directives]


def test_a_held_retire_request_is_approvable_after_its_session_is_gone(world1: World) -> None:
    request = review.request_retirement(
        "g_org",
        "c_tokens",
        "currency",
        "Not needed.",
        actor_session_id="s_long_gone",
        **world1.kw(),
    )
    review.approve(request.id, operator="oren", **world1.kw())
    assert "c_tokens" not in world1.org_ids()
