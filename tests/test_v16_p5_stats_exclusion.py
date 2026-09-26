"""v1.16 P5 — CEO add: a raised contribution counts in write-back/session stats as
system-raised, never as the reporter's own second contribution.

Pins the exact scenario Aron asked for: a reporter session submits ONE outcome
that raises to its issuer — the reporter's own session stats show exactly 1
contribution, not 2, even though the engine minted a second contribution row
(at the issuing scope) carrying that same reporter's provenance.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from strata.fleet_config import FleetConfig
from strata.scope_manager import ScopeManagerJudgment
from tests.test_mcp_server import (
    _load_mcp_module,
    _make_db,
    _make_fleet_yaml,
    _make_summary,
    _patch_agent_binding,
)


def _judgment(decision: str, *, summary=None, outcome_disposition=None) -> ScopeManagerJudgment:
    return ScopeManagerJudgment(
        decision=decision,
        reasoning="test reasoning",
        new_summary=summary,
        outcome_disposition=outcome_disposition,
    )


async def test_a_raised_contribution_counts_once_for_the_reporters_session_only(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)  # g_backend (L1) -> g_arch (L0)
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    directive_judgment = _judgment("accept_as_directive", summary=_make_summary("g_arch", "TLS"))
    scope_p, skill_p, session_p = _patch_agent_binding(
        mod, scope="g_arch", session_id="sess_issuer"
    )
    with (
        scope_p,
        skill_p,
        session_p,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=directive_judgment),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        directive_result = await mod.strata_contribute(
            scope_id="g_arch",
            content="All services must use TLS 1.3 or later.",
            proposed_classification="directive",
            subject="tls",
            supersedes=None,
        )
    directive_id = directive_result["contribution_id"]

    failed_judgment = _judgment(
        "accept_as_context",
        summary=_make_summary("g_backend", "TLS 1.3 unsupported by peer"),
        outcome_disposition="failed",
    )
    raise_judgment = _judgment("accept_as_context", summary=_make_summary("g_arch", "revised"))

    scope_p2, skill_p2, session_p2 = _patch_agent_binding(
        mod, scope="g_backend", session_id="sess_reporter"
    )
    with (
        scope_p2,
        skill_p2,
        session_p2,
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch(
            "strata.scope_manager.ScopeManager.judge",
            side_effect=[failed_judgment, raise_judgment],
        ),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        await mod.strata_contribute(
            scope_id="g_backend",
            content="Tried TLS 1.3; the peer only supports 1.2.",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            acted_on=directive_id,
        )

    reporter_state = mod._session_store.read("sess_reporter")
    assert reporter_state is not None
    assert reporter_state.contributions == 1

    # No OTHER session's counter moved either — the raise is not attributed as a
    # second act by any session, issuer's or reporter's; it is a mechanical,
    # unattributed engine act (see the `Contribution.raised_from`/
    # `record_judgment_and_raise` docstrings).
    issuer_state = mod._session_store.read("sess_issuer")
    assert issuer_state is not None
    assert issuer_state.contributions == 1  # only its own directive, from the first call
