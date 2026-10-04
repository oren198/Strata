"""#234, read signaling — surface (a): `perspective_stale` on every tool result.

`_with_read_signal` wraps every `@mcp.tool()` function (applied UNDER it, so
FastMCP's schema introspection sees the ORIGINAL function through
`functools.wraps`). These tests check the wrapping is COMPLETE (every
registered tool carries it — a new tool without the decorator fails here,
not silently), that it does not change a tool's registered schema, and the
end-to-end behavior: a stale scope's id appears, a non-stale read carries no
key at all, and a WRITE tool gets it too.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

from strata.fleet_config import FleetConfig
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import ScopeManagerJudgment
from strata.summary_store import SummaryStore

from .test_mcp_server import _load_mcp_module, _make_db, _make_fleet_yaml, _make_summary


def _fake_judgment(context: str) -> ScopeManagerJudgment:
    return ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="Valid observation.",
        new_summary=_make_summary("g_arch", context),
    )


async def test_every_registered_tool_is_read_signal_wrapped(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    mod = _load_mcp_module(db_path, str(tmp_path / "summaries"), str(_make_fleet_yaml(tmp_path)))

    tools = await mod.mcp.list_tools()
    assert tools, "expected at least one registered tool"
    unwrapped = [
        t.name
        for t in tools
        if not getattr(mod.mcp._tool_manager._tools[t.name].fn, "_read_signal_wrapped", False)
    ]
    assert not unwrapped, f"tool(s) missing @_with_read_signal: {unwrapped}"


async def test_wrapping_does_not_change_the_registered_schema(tmp_path: Path) -> None:
    """The wrapper's `functools.wraps` must leave FastMCP's own schema
    derivation untouched — a tool's registered `inputSchema` reflects the
    UNDECORATED function's real signature, never `(*args, **kwargs)`."""
    db_path = _make_db(tmp_path)
    mod = _load_mcp_module(db_path, str(tmp_path / "summaries"), str(_make_fleet_yaml(tmp_path)))

    tools = {t.name: t for t in await mod.mcp.list_tools()}
    assert "strata_contribute" in tools
    contribute_props = set(tools["strata_contribute"].inputSchema["properties"])
    # The real strata_contribute signature, read directly off the wrapped
    # function (inspect.signature follows functools.wraps' __wrapped__).
    fn = mod.mcp._tool_manager._tools["strata_contribute"].fn
    real_params = set(inspect.signature(fn).parameters) - {"self"}
    assert contribute_props == real_params

    list_scopes_params = set(
        inspect.signature(mod.mcp._tool_manager._tools["strata_list_scopes"].fn).parameters
    )
    assert list_scopes_params == set(tools["strata_list_scopes"].inputSchema["properties"])


async def test_a_stale_read_scope_is_surfaced_on_the_next_tool_result(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write("g_arch", _make_summary("g_arch", "arch context"))
    ss.write("g_backend", _make_summary("g_backend", "backend context"))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
    ):
        mod._summary_store = ss
        first = await mod.strata_read_perspective("g_backend")
        assert "perspective_stale" not in first

        # ANOTHER session writes to g_backend, moving its watermark out from
        # under the receipt the read above just stamped.
        with RecordStore(db_path) as rs:
            cid = rs.append_contribution(
                scope_id="g_backend",
                content="Some other session's new context.",
                proposed_classification="context",
                subject=None,
                supersedes=None,
                contributor=ContributorRef(
                    scope_id="g_backend",
                    skill="other",
                    session_id="sess-other",
                    ts="2026-10-02T00:01:00Z",
                ),
            ).id
            rs.record_judgment(
                contribution_id=cid, decision="accept_as_context", judged_by="scope-manager"
            )
        ss.write("g_backend", ss.read("g_backend").model_copy(update={"context": "moved"}))

        second = mod.strata_list_scopes()
        assert second.get("perspective_stale") == ["g_backend"]


async def test_a_non_stale_read_carries_no_key_at_all(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write("g_arch", _make_summary("g_arch", "arch context"))
    ss.write("g_backend", _make_summary("g_backend", "backend context"))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
    ):
        mod._summary_store = ss
        await mod.strata_read_perspective("g_backend")
        result = mod.strata_list_scopes()
        assert "perspective_stale" not in result


async def test_a_write_tool_also_carries_the_signal(tmp_path: Path) -> None:
    """#234 §8: a contribute to S does not make some OTHER scope this session
    read current — write tools get the signal too."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))

    ss = SummaryStore(summaries_dir)
    ss.write("g_arch", _make_summary("g_arch", "arch context"))
    ss.write("g_backend", _make_summary("g_backend", "backend context"))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
    ):
        mod._summary_store = ss
        # Read g_arch (an ancestor) — a write to g_backend below never
        # catches up g_arch's own receipt.
        await mod.strata_read_perspective("g_arch")

        with RecordStore(db_path) as rs:
            cid = rs.append_contribution(
                scope_id="g_arch",
                content="Some other session's new context, again.",
                proposed_classification="context",
                subject=None,
                supersedes=None,
                contributor=ContributorRef(
                    scope_id="g_arch",
                    skill="other",
                    session_id="sess-other",
                    ts="2026-10-02T00:01:00Z",
                ),
            ).id
            rs.record_judgment(
                contribution_id=cid, decision="accept_as_context", judged_by="scope-manager"
            )
        ss.write("g_arch", ss.read("g_arch").model_copy(update={"context": "moved again"}))

        result = await mod.strata_session_stats()
        assert result.get("perspective_stale") == ["g_arch"]


# ---------------------------------------------------------------------------
# §8 — the self-trigger: a session's own accepted contribution to S may
# catch ITS OWN receipt up, but only if nothing ELSE moved S first.
# ---------------------------------------------------------------------------


async def test_self_trigger_contribute_then_next_result_is_not_stale(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", "strata-developer"),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_self"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=_fake_judgment("v1")),
        patch("anthropic.Anthropic", return_value=object()),
    ):
        await mod.strata_read_perspective("g_arch")
        await mod.strata_contribute(
            scope_id="g_arch",
            content="All services should use structured logging.",
            proposed_classification="context",
            subject="logging-standard",
            supersedes=None,
        )
        result = mod.strata_list_scopes()
        assert "perspective_stale" not in result


async def test_self_trigger_another_sessions_write_is_still_stale(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)
    ss = SummaryStore(summaries_dir)
    ss.write("g_arch", _make_summary("g_arch", "initial context"))

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", "strata-developer"),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_self"),
        patch.object(mod, "_load_fleet", return_value=fleet),
    ):
        await mod.strata_read_perspective("g_arch")

        with RecordStore(db_path) as rs:
            cid = rs.append_contribution(
                scope_id="g_arch",
                content="Another session's own new context.",
                proposed_classification="context",
                subject=None,
                supersedes=None,
                contributor=ContributorRef(
                    scope_id="g_arch",
                    skill="other",
                    session_id="sess-other",
                    ts="2026-10-02T00:01:00Z",
                ),
            ).id
            rs.record_judgment(
                contribution_id=cid, decision="accept_as_context", judged_by="scope-manager"
            )
        ss.write("g_arch", ss.read("g_arch").model_copy(update={"context": "moved by other"}))

        result = mod.strata_list_scopes()
        assert result.get("perspective_stale") == ["g_arch"]


async def test_self_trigger_another_writes_then_self_contributes_is_still_stale(
    tmp_path: Path,
) -> None:
    """Another session's write lands BEFORE this session's own contribute —
    the self-trigger's compare-and-set must not paper over that foreign
    change just because this session also wrote afterward."""
    db_path = _make_db(tmp_path)
    summaries_dir = str(tmp_path / "summaries")
    fleet_path = _make_fleet_yaml(tmp_path)
    mod = _load_mcp_module(db_path, summaries_dir, str(fleet_path))
    fleet = FleetConfig.load(fleet_path)
    ss = SummaryStore(summaries_dir)
    ss.write("g_arch", _make_summary("g_arch", "initial context"))

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", "strata-developer"),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_self"),
        patch.object(mod, "_load_fleet", return_value=fleet),
        patch("strata.scope_manager.ScopeManager.judge", return_value=_fake_judgment("v2")),
        patch("anthropic.Anthropic", return_value=object()),
    ):
        await mod.strata_read_perspective("g_arch")

        with RecordStore(db_path) as rs:
            cid = rs.append_contribution(
                scope_id="g_arch",
                content="Another session's own new context, first.",
                proposed_classification="context",
                subject=None,
                supersedes=None,
                contributor=ContributorRef(
                    scope_id="g_arch",
                    skill="other",
                    session_id="sess-other",
                    ts="2026-10-02T00:01:00Z",
                ),
            ).id
            rs.record_judgment(
                contribution_id=cid, decision="accept_as_context", judged_by="scope-manager"
            )
        ss.write("g_arch", ss.read("g_arch").model_copy(update={"context": "moved by other first"}))

        # This session now ALSO writes — its own receipt predates the other
        # session's write above, so the self-trigger's compare-and-set must
        # find a mismatch and leave the receipt stale.
        await mod.strata_contribute(
            scope_id="g_arch",
            content="This session's own new context, second.",
            proposed_classification="context",
            subject="logging-standard",
            supersedes=None,
        )

        result = mod.strata_list_scopes()
        assert result.get("perspective_stale") == ["g_arch"]
