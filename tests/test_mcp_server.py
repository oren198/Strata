"""Tests for the Strata MCP server tool functions.

All backend HTTP calls are mocked via unittest.mock — no real Strata backend
is required.  Tests cover the seven scenarios specified in the task brief:

1. strata_list_scopes() calls GET /scopes with the configured URL.
2. strata_read_scope_summary("g_arch") calls the right endpoint.
3. strata_read_perspective("g_arch") returns the same shape as summary (V1 stub).
4. strata_contribute(...) sends POST /contribute with the correct body.
5. Non-2xx response surfaces a clear error (status + body in message).
6. Backend unreachable (ConnectError) surfaces a clear error.
7. Env var defaults work when not set.

The MCP protocol layer (FastMCP, stdio transport) is not tested here — that is
the SDK's responsibility.  Only the thin Python tool wrappers are exercised.
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int, json_data: dict) -> MagicMock:
    """Build a mock :class:`httpx.Response`."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_error = status_code >= 400
    resp.json.return_value = json_data
    resp.text = str(json_data)
    return resp


def _make_client_ctx(response: MagicMock) -> MagicMock:
    """Return a mock context-manager that yields a client returning *response*."""
    mock_client = MagicMock()
    mock_client.get.return_value = response
    mock_client.post.return_value = response
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_client)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx, mock_client


# ---------------------------------------------------------------------------
# Module reload helper — reloads strata_mcp with fresh env vars each time
# ---------------------------------------------------------------------------


def _reload_module(env_overrides: dict | None = None):
    """Reload ``mcp_server.strata_mcp`` with patched env vars and return it."""
    env = {
        "STRATA_BACKEND_URL": "http://127.0.0.1:8000",
        "STRATA_AGENT_SCOPE": "g_backend",
        "STRATA_AGENT_SKILL": "strata-developer",
        "STRATA_AGENT_SESSION_ID": "sess_test",
        **(env_overrides or {}),
    }
    # Remove so the module reads from our patched env
    for key in list(sys.modules.keys()):
        if "strata_mcp" in key:
            del sys.modules[key]

    with patch.dict("os.environ", env, clear=False):
        import mcp_server.strata_mcp as mod

        importlib.reload(mod)
    return mod


# ---------------------------------------------------------------------------
# Test 1: strata_list_scopes calls GET /scopes
# ---------------------------------------------------------------------------


def test_list_scopes_calls_get_scopes():
    """strata_list_scopes() must call GET /scopes and return parsed JSON."""
    fleet_data = {
        "strata": [{"id": "s1", "name": "executive", "rank": 0}],
        "scopes": [{"id": "g_ceo", "name": "CEO", "stratum_id": "s1"}],
        "edges": [],
    }
    response = _make_response(200, fleet_data)
    ctx, mock_client = _make_client_ctx(response)

    import mcp_server.strata_mcp as mod

    with patch.object(mod, "_client", return_value=ctx):
        result = mod.strata_list_scopes()

    mock_client.get.assert_called_once_with("/scopes")
    assert result == fleet_data


# ---------------------------------------------------------------------------
# Test 2: strata_read_scope_summary calls the right endpoint
# ---------------------------------------------------------------------------


def test_read_scope_summary_calls_correct_endpoint():
    """strata_read_scope_summary("g_arch") must call GET /scopes/g_arch/summary."""
    summary_data = {
        "scope_id": "g_arch",
        "directives": [],
        "context": "some context",
        "updated_at": "2026-05-24T00:00:00Z",
    }
    response = _make_response(200, summary_data)
    ctx, mock_client = _make_client_ctx(response)

    import mcp_server.strata_mcp as mod

    with patch.object(mod, "_client", return_value=ctx):
        result = mod.strata_read_scope_summary("g_arch")

    mock_client.get.assert_called_once_with("/scopes/g_arch/summary")
    assert result == summary_data


# ---------------------------------------------------------------------------
# Test 3: strata_read_perspective returns the same shape + _v1_limitation
# ---------------------------------------------------------------------------


def test_read_perspective_returns_summary_plus_limitation_note():
    """strata_read_perspective returns the scope summary with a _v1_limitation key."""
    summary_data = {
        "scope_id": "g_arch",
        "directives": [],
        "context": "arch context",
        "updated_at": "2026-05-24T00:00:00Z",
    }
    response = _make_response(200, summary_data)
    ctx, mock_client = _make_client_ctx(response)

    import mcp_server.strata_mcp as mod

    with patch.object(mod, "_client", return_value=ctx):
        result = mod.strata_read_perspective("g_arch")

    # Same core shape as strata_read_scope_summary
    assert result["scope_id"] == "g_arch"
    assert result["directives"] == []
    assert result["context"] == "arch context"
    # Plus the V1 stub note
    assert "_v1_limitation" in result
    assert "post-V1" in result["_v1_limitation"]


# ---------------------------------------------------------------------------
# Test 4: strata_contribute sends POST /contribute with correct body
# ---------------------------------------------------------------------------


def test_contribute_sends_correct_post_body():
    """strata_contribute must POST to /contribute with the expected JSON body."""
    contribute_response = {
        "contribution_id": "c_001",
        "judgment": {
            "decision": "accept_as_context",
            "reasoning": "Valid observation.",
            "summary_updated": True,
        },
    }
    response = _make_response(200, contribute_response)
    ctx, mock_client = _make_client_ctx(response)

    import mcp_server.strata_mcp as mod

    # Patch env vars on the module
    with (
        patch.object(mod, "_AGENT_SCOPE", "g_backend"),
        patch.object(mod, "_AGENT_SKILL", "strata-developer"),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_client", return_value=ctx),
    ):
        result = mod.strata_contribute(
            scope_id="g_arch",
            content="All services should use structured logging.",
            proposed_classification="context",
            subject="logging-standard",
            supersedes=None,
        )

    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    # First positional arg is the path
    assert call_kwargs[0][0] == "/contribute"
    body = call_kwargs[1]["json"]
    assert body["scope_id"] == "g_arch"
    assert body["content"] == "All services should use structured logging."
    assert body["proposed_classification"] == "context"
    assert body["subject"] == "logging-standard"
    assert body["supersedes"] is None
    # Provenance block
    assert body["contributor"]["scope_id"] == "g_backend"
    assert body["contributor"]["skill"] == "strata-developer"
    assert body["contributor"]["session_id"] == "sess_test"
    assert "ts" in body["contributor"]
    assert result == contribute_response


# ---------------------------------------------------------------------------
# Test 5: Non-2xx response surfaces a clear error
# ---------------------------------------------------------------------------


def test_non_2xx_response_raises_runtime_error_with_status_and_body():
    """A non-2xx backend response must raise RuntimeError with status + body."""
    error_response = _make_response(404, {"detail": "Scope not found: 'g_missing'"})
    error_response.is_error = True
    error_response.status_code = 404
    error_response.text = '{"detail": "Scope not found: \'g_missing\'"}'
    ctx, _ = _make_client_ctx(error_response)

    import mcp_server.strata_mcp as mod

    with patch.object(mod, "_client", return_value=ctx), pytest.raises(RuntimeError) as exc_info:
        mod.strata_read_scope_summary("g_missing")

    msg = str(exc_info.value)
    assert "404" in msg
    assert "g_missing" in msg


# ---------------------------------------------------------------------------
# Test 6: Backend unreachable raises RuntimeError
# ---------------------------------------------------------------------------


def test_backend_unreachable_raises_runtime_error():
    """When the backend is unreachable a clear RuntimeError must be raised."""
    import httpx

    import mcp_server.strata_mcp as mod

    mock_client = MagicMock()
    mock_client.get.side_effect = httpx.ConnectError("Connection refused")
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_client)
    ctx.__exit__ = MagicMock(return_value=False)

    with patch.object(mod, "_client", return_value=ctx), pytest.raises(RuntimeError) as exc_info:
        mod.strata_list_scopes()

    msg = str(exc_info.value)
    assert "Cannot reach Strata backend" in msg


# ---------------------------------------------------------------------------
# Test 7: Env var defaults work when vars are not set
# ---------------------------------------------------------------------------


def test_env_var_defaults_when_not_set(monkeypatch):
    """Module-level defaults apply when STRATA_* env vars are absent."""
    # Remove the env vars entirely
    monkeypatch.delenv("STRATA_BACKEND_URL", raising=False)
    monkeypatch.delenv("STRATA_AGENT_SCOPE", raising=False)
    monkeypatch.delenv("STRATA_AGENT_SKILL", raising=False)
    monkeypatch.delenv("STRATA_AGENT_SESSION_ID", raising=False)
    monkeypatch.delenv("STRATA_BACKEND_TIMEOUT", raising=False)

    # Force module reload so it picks up the patched env
    for key in list(sys.modules.keys()):
        if "strata_mcp" in key:
            del sys.modules[key]

    import mcp_server.strata_mcp as mod

    importlib.reload(mod)

    assert mod._BACKEND_URL == "http://127.0.0.1:8000"
    assert mod._AGENT_SCOPE == "unknown"
    assert mod._AGENT_SKILL == "unknown"
    assert mod._AGENT_SESSION_ID == "sess_local"
    assert mod._TIMEOUT == 30.0
