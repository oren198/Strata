"""#206 — the private MCP SDK seam Strata wraps must exist, and its loss must not be fatal.

The connect hook and the tool-call counter wrap ``FastMCP._mcp_server._handle_message``
(private; pyproject pins ``mcp <1.30``). If a newer SDK moves it, the server must still
start — a memory-blind session is worse than an unmeasured denominator — and the
write-back report must say its denominator may undercount.
"""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from strata.mcp import server as srv
from strata.session_state import (
    SessionStateStore,
    compute_writeback_report,
)


# --- CI tripwire: the seam is there on the pinned SDK -------------------------------------


def test_the_sdk_seam_strata_wraps_still_exists() -> None:
    """Fails loudly when a new mcp release moves the private seam, before a user meets it."""
    from mcp.server.fastmcp import FastMCP

    lowlevel = FastMCP("probe")._mcp_server
    handler = getattr(lowlevel, "_handle_message", None)
    assert handler is not None, (
        "mcp's lowlevel Server no longer has `_handle_message`: Strata's connect hook and "
        "tool-call counter (strata/mcp/server.py _install_connect_hook) need a new seam. "
        "Keep the `mcp <1.30` pin until it is re-verified (#206)."
    )
    assert inspect.iscoroutinefunction(handler)
    params = list(inspect.signature(handler).parameters)
    assert params[:2] == ["message", "session"], params


def test_the_real_server_installed_the_hook() -> None:
    assert srv._CONNECT_HOOK_INSTALLED is True
    assert srv._connect_seam_problem is None


# --- graceful degradation -----------------------------------------------------------------


class _NoSeam:
    """A FastMCP stand-in whose lowlevel server has no `_handle_message`."""

    class _mcp_server:  # noqa: N801
        pass


class _NoLowlevel:
    pass


class _NotAsync:
    class _mcp_server:  # noqa: N801
        @staticmethod
        def _handle_message(message, session):
            return None


class _RenamedArgs:
    class _mcp_server:  # noqa: N801
        @staticmethod
        async def _handle_message(a, b, c, d):
            return None


@pytest.mark.parametrize("fake", [_NoSeam, _NoLowlevel, _NotAsync, _RenamedArgs])
def test_a_missing_or_changed_seam_warns_and_does_not_raise(
    fake, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "_connect_seam_problem", None)
    with caplog.at_level(logging.WARNING, logger="strata.mcp"):
        installed = srv._install_connect_hook(fake())
    assert installed is False
    assert srv._connect_seam_problem  # the reason is remembered for the report
    assert any("connect" in r.getMessage().lower() for r in caplog.records)
    assert any("without" in r.getMessage().lower() for r in caplog.records)


def test_a_present_seam_is_wrapped_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Low:
        async def _handle_message(self, message, session, *args, **kwargs):
            calls.append(message)
            return "ok"

    class Fake:
        _mcp_server = Low()

    monkeypatch.setattr(srv, "_connect_seam_problem", None)
    monkeypatch.setattr(srv, "_record_connect", lambda session: calls.append(("connect", session)))
    assert srv._install_connect_hook(Fake()) is True

    import asyncio

    result = asyncio.run(Fake._mcp_server._handle_message("m", "s"))
    assert result == "ok"
    assert ("connect", "s") in calls and "m" in calls
    assert srv._connect_seam_problem is None


def test_the_seam_problem_is_written_for_the_report_once_stores_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SessionStateStore(tmp_path / "sessions")
    monkeypatch.setattr(srv, "_session_store", store)
    monkeypatch.setattr(srv, "_sessions_dir", str(tmp_path / "sessions"))
    monkeypatch.setattr(srv, "_connect_seam_problem", "mcp seam moved")
    srv._publish_connect_seam_problem()
    marker = store.connect_seam_unavailable()
    assert marker is not None
    assert marker["reason"] == "mcp seam moved"


def test_no_problem_writes_no_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStateStore(tmp_path / "sessions")
    monkeypatch.setattr(srv, "_session_store", store)
    monkeypatch.setattr(srv, "_sessions_dir", str(tmp_path / "sessions"))
    monkeypatch.setattr(srv, "_connect_seam_problem", None)
    srv._publish_connect_seam_problem()
    assert store.connect_seam_unavailable() is None


# --- the report says so -------------------------------------------------------------------


def test_report_flags_a_possibly_undercounted_denominator(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path / "sessions")
    store.record_connect_seam_unavailable("mcp seam moved", now=datetime(2026, 9, 20, tzinfo=UTC))
    report = compute_writeback_report(store)
    assert report.denominator_may_undercount is True
    assert report.denominator_note and "mcp seam moved" in report.denominator_note
    # The marker is not a session and never counts as one, nor as an unreadable file.
    assert report.overall.n == 0
    assert report.unreadable_files == 0


def test_report_is_clean_without_the_marker(tmp_path: Path) -> None:
    report = compute_writeback_report(SessionStateStore(tmp_path / "sessions"))
    assert report.denominator_may_undercount is False
    assert report.denominator_note is None


def test_marker_keeps_the_first_time_and_updates_the_last(tmp_path: Path) -> None:
    store = SessionStateStore(tmp_path / "sessions")
    store.record_connect_seam_unavailable("r1", now=datetime(2026, 9, 20, tzinfo=UTC))
    store.record_connect_seam_unavailable("r1", now=datetime(2026, 9, 21, tzinfo=UTC))
    marker = store.connect_seam_unavailable()
    assert marker["first_at"].startswith("2026-09-20")
    assert marker["last_at"].startswith("2026-09-21")
