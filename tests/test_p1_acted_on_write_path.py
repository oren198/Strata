"""v1.15 P1 (ADR 0017) — the `acted_on` write path: `strata_contribute`'s validator.

Each rule is rejected at the tool boundary with a message naming which one failed:
1. acted_on + supersedes together.
2. the referenced contribution must exist.
3. its scope must be within this agent's entitled READ surface.
4. it must have been ADMITTED (an accepting judgment) — never merely undeclined.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from strata.fleet_config import FleetConfig
from strata.record_store import ContributorRef
from strata.scope_manager import ScopeManagerJudgment
from strata.summary_store import ScopeSummary
from tests.test_mcp_server import (  # noqa: F401 — reuse the module's own fixtures
    _load_mcp_module,
    _make_db,
    _make_fleet_yaml,
)


def _make_summary(scope_id: str, context: str = "some context") -> ScopeSummary:
    return ScopeSummary(
        scope_id=scope_id, directives=[], context=context, updated_at="2026-01-01T00:00:00Z"
    )


def _judgment(decision: str = "accept_as_context") -> ScopeManagerJudgment:
    return ScopeManagerJudgment(
        decision=decision,
        reasoning="A plain observation.",
        new_summary=_make_summary("g_arch") if decision != "decline" else None,
    )


@pytest.fixture
def mod(tmp_path: Path):
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)
    module = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)
    with (
        patch.object(module, "_AGENT_SCOPE", "g_backend"),
        patch.object(module, "_AGENT_SKILL", "strata-developer"),
        patch.object(module, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(module, "_load_fleet", return_value=fleet),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        yield module


async def _contribute(mod, *, decision: str = "accept_as_context", **kwargs) -> dict:
    with patch("strata.scope_manager.ScopeManager.judge", return_value=_judgment(decision)):
        return await mod.strata_contribute(
            scope_id="g_backend",
            content=kwargs.pop("content", "An observation."),
            proposed_classification="context",
            **kwargs,
        )


async def test_acted_on_together_with_supersedes_is_rejected(mod) -> None:
    with pytest.raises(RuntimeError, match="acted_on and supersedes cannot both be set"):
        await _contribute(mod, supersedes="c_whatever", acted_on="c_whatever_else")


async def test_acted_on_referencing_nothing_is_rejected(mod) -> None:
    with pytest.raises(RuntimeError, match="does not reference an existing contribution"):
        await _contribute(mod, acted_on="c_does_not_exist")


async def test_acted_on_referencing_an_unentitled_scope_is_rejected(mod) -> None:
    # A contribution in g_arch's own record, never entitled from a peer scope not in
    # the chain: seed a target in a scope this agent (bound to g_backend) has no read
    # entitlement to at all (a fresh unrelated scope not in fleet.yaml's edges).
    target = await _contribute(mod, content="Root-only note.")  # in g_backend, entitled
    # g_backend IS entitled to its own scope, so instead simulate a target OUTSIDE the
    # chain by writing directly to the record store under an unrelated scope id.
    mod._record_store.append_contribution(
        scope_id="g_unrelated",
        content="Something in an unrelated scope.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_unrelated", skill=None, session_id="s1", ts="2026-01-01T00:00:00Z"
        ),
    )
    other_scope_target_id = mod._record_store.list_contributions(scope_id="g_unrelated")[-1].id
    mod._record_store.record_judgment(
        contribution_id=other_scope_target_id,
        decision="accept_as_context",
        judged_by="scope-manager",
    )
    with pytest.raises(RuntimeError, match="outside your entitled surface"):
        await _contribute(mod, acted_on=other_scope_target_id)
    assert target["contribution_id"]  # sanity: the entitled contribution above worked


async def test_acted_on_referencing_a_declined_contribution_is_rejected(mod) -> None:
    declined = await _contribute(mod, content="Trivia.", decision="decline")
    with pytest.raises(RuntimeError, match="never admitted into memory"):
        await _contribute(mod, acted_on=declined["contribution_id"])


async def test_acted_on_referencing_a_pending_contribution_is_rejected(mod) -> None:
    pending_id = mod._record_store.append_contribution(
        scope_id="g_backend",
        content="Unjudged so far.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_backend", skill=None, session_id="s1", ts="2026-01-01T00:00:00Z"
        ),
    ).id
    with pytest.raises(RuntimeError, match="never admitted into memory"):
        await _contribute(mod, acted_on=pending_id)


async def test_acted_on_referencing_an_admitted_contribution_succeeds(mod) -> None:
    target = await _contribute(mod, content="Release tags use rel-, never v.")
    result = await _contribute(
        mod,
        content="Tagged rel-2.0.0 following the convention; it worked.",
        acted_on=target["contribution_id"],
    )
    fetched = mod._record_store.get_contribution(result["contribution_id"])
    assert fetched.acted_on == target["contribution_id"]


async def test_acted_on_referencing_an_accepted_directive_succeeds(mod) -> None:
    target = await _contribute(mod, content="A rule.", decision="accept_as_directive")
    result = await _contribute(
        mod, content="Followed it; held.", acted_on=target["contribution_id"]
    )
    assert result["contribution_id"]


async def test_acted_on_is_readable_via_strata_read_contribution(mod) -> None:
    """Criterion 6 / gate readability: the eval run must be able to read acted_on
    off a recorded contribution through the ordinary read surface."""
    target = await _contribute(mod, content="A note worth acting on.")
    outcome = await _contribute(
        mod, content="Acted on it; it held.", acted_on=target["contribution_id"]
    )
    result = await mod.strata_read_contribution(contribution_id=outcome["contribution_id"])
    assert result["contribution"]["acted_on"] == target["contribution_id"]


async def test_acted_on_is_readable_via_strata_read_scope_record(mod) -> None:
    target = await _contribute(mod, content="A note worth acting on.")
    outcome = await _contribute(
        mod, content="Acted on it; it held.", acted_on=target["contribution_id"]
    )
    result = await mod.strata_read_scope_record(scope_id="g_backend")
    by_id = {c["id"]: c for c in result["contributions"]}
    assert by_id[outcome["contribution_id"]]["acted_on"] == target["contribution_id"]
