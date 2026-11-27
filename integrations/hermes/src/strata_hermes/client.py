"""A stdio MCP client for ``strata-mcp``, run on its own thread.

Hermes calls a provider from ordinary threads, so the asyncio MCP session lives on a dedicated
loop thread started with Hermes's own ``spawn_context_thread`` (which carries the profile's
contextvars). Nothing here imports Strata: the engine runs in the ``strata-mcp`` subprocess.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from agent.memory_provider import spawn_context_thread

logger = logging.getLogger(__name__)

UNREACHABLE = "strata_unreachable"
UNBOUND = "strata_unbound"
FAILED = "strata_error"

#: How the engine's own refusal reads when the session has no scope (README: "not bound").
_UNBOUND_MARKERS = ("not bound", "strata_agent_scope")


class StrataError(Exception):
    """A failed Strata call, with a stable ``code`` the provider reports to the agent."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def classify(message: str) -> str:
    lowered = message.lower()
    return UNBOUND if any(marker in lowered for marker in _UNBOUND_MARKERS) else FAILED


class StrataClient:
    def __init__(
        self,
        command: list[str],
        env: dict[str, str],
        *,
        cwd: str | None = None,
        startup_timeout: float = 30.0,
        call_timeout: float = 120.0,
    ) -> None:
        self._command = command
        self._env = env
        self._cwd = cwd
        self._startup_timeout = startup_timeout
        self._call_timeout = call_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: Any = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_error: str | None = None

    # -- lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        """Launch the server and complete the MCP handshake, or raise ``StrataError``."""
        self._thread = spawn_context_thread(self._run, name="strata-mcp-client")
        self._thread.start()
        if not self._ready.wait(self._startup_timeout):
            self.close()
            raise StrataError(UNREACHABLE, "strata-mcp did not answer the MCP handshake in time.")
        if self._startup_error is not None:
            raise StrataError(UNREACHABLE, self._startup_error)

    def is_up(self) -> bool:
        return self._session is not None and self._startup_error is None

    def close(self) -> None:
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(stop.set)
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._session = None

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # noqa: BLE001 — reported as an unreachable server
            if self._startup_error is None and not self._ready.is_set():
                self._startup_error = f"strata-mcp could not be started: {exc}"
            logger.debug("strata-mcp client loop ended: %s", exc)
        finally:
            self._ready.set()

    async def _main(self) -> None:
        from mcp import ClientSession, StdioServerParameters  # noqa: PLC0415
        from mcp.client.stdio import stdio_client  # noqa: PLC0415

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        params = StdioServerParameters(
            command=self._command[0], args=self._command[1:], env=self._env, cwd=self._cwd
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            self._session = session
            self._ready.set()
            await self._stop.wait()

    # -- calls --------------------------------------------------------------------------------

    def call_tool(
        self, name: str, args: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Call one Strata tool and return its payload as a dict; raise ``StrataError``."""
        if not self.is_up() or self._loop is None:
            raise StrataError(UNREACHABLE, "strata-mcp is not running.")
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, args or {}), self._loop
        )
        try:
            result = future.result(timeout or self._call_timeout)
        except TimeoutError as exc:
            future.cancel()
            raise StrataError(FAILED, f"{name} did not answer in time.") from exc
        except Exception as exc:  # noqa: BLE001
            raise StrataError(UNREACHABLE, f"strata-mcp failed: {exc}") from exc
        text = "".join(getattr(block, "text", "") for block in result.content)
        if result.isError:
            raise StrataError(classify(text), text or f"{name} failed.")
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return (
                structured.get("result", structured)
                if set(structured) == {"result"}
                else structured
            )
        try:
            data = json.loads(text)
        except ValueError:
            return {"text": text}
        return data if isinstance(data, dict) else {"result": data}
