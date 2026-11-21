"""The Strata memory provider for Hermes Agent.

It reads a perspective and writes ONLY through the tools the agent calls (``tools.py``): the
judged contribute tool among them. Every lifecycle hook that would copy a transcript into
memory (``sync_turn``, ``on_session_end``, ``on_memory_write`` and the rest) is inherited from the
ABC as a no-op on purpose: automatic transcript sync would bypass the judge. The perspective is
delivered through ``queue_prefetch`` / ``prefetch`` from a cache, so a turn never waits on the
server, which may be waiting on the scope lock.

Scope and skill come from the profile's environment (``STRATA_AGENT_SCOPE``,
``STRATA_AGENT_SKILL``); ``STRATA_AGENT_SESSION_ID`` is never set, so the server derives the
session from this process. Nothing here imports Strata: the engine runs in ``strata-mcp``.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import threading
from typing import Any

from agent.memory_provider import MemoryProvider, RecallStatus, spawn_context_thread

from strata_hermes import tools
from strata_hermes.client import UNBOUND, UNREACHABLE, StrataClient, StrataError

logger = logging.getLogger(__name__)

COMMAND_ENV = "STRATA_MCP_COMMAND"
PROJECT_ENV = "STRATA_PROJECT_DIR"
DEFAULT_COMMAND = "strata-mcp"
_UNTOUCHED = "Hermes built-in memory is untouched."


def _command() -> list[str]:
    return shlex.split(os.environ.get(COMMAND_ENV, "").strip() or DEFAULT_COMMAND)


def _resolvable(command: list[str]) -> bool:
    return bool(command) and (
        shutil.which(command[0]) is not None or os.access(command[0], os.X_OK)
    )


def _scope() -> str:
    return os.environ.get("STRATA_AGENT_SCOPE", "").strip()


def _skill() -> str:
    return os.environ.get("STRATA_AGENT_SKILL", "").strip()


def render_perspective(data: dict[str, Any]) -> str:
    """The perspective as plain text for the model: what binds, then the scope's own context."""
    lines = [f"Strata memory for scope {data.get('scope_id', '?')}:"]
    for layer in data.get("layers", []):
        scope_id = layer.get("scope_id", "?")
        directives = list(layer.get("directives") or [])
        summary = layer.get("summary") or {}
        directives += list(summary.get("directives") or [])
        for directive in directives:
            lines.append(f"- [{scope_id}] binding: {directive.get('content', '')}")
        context = (summary.get("context") or "").strip()
        if context:
            lines.append(f"- [{scope_id}] context: {context}")
    return "\n".join(lines)


class StrataMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._client: StrataClient | None = None
        self._session_id = ""
        self._init_error: StrataError | None = None
        self._lock = threading.Lock()
        self._cache = ""
        self._directives = 0
        self._refreshing = False
        self._again = False

    # -- identity and availability ----------------------

    @property
    def name(self) -> str:
        return "strata"

    def is_available(self) -> bool:
        """Config only, no network: a scope is set and the strata-mcp command resolves."""
        return bool(_scope()) and _resolvable(_command())

    def unavailable_reason(self) -> str:
        if not _scope():
            return (
                "STRATA_AGENT_SCOPE is not set for this profile; set it (and STRATA_AGENT_SKILL) "
                "in the profile's env to bind it to a Strata scope."
            )
        return (
            f"the strata-mcp command {' '.join(_command())!r} was not found; install strata-mem "
            f"or set {COMMAND_ENV}."
        )

    def identity_signature(self) -> dict[str, Any]:
        return {"strata": {"scope": _scope(), "skill": _skill()}}

    # -- lifecycle ----------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
        self._init_error = None
        if not _scope():
            self._init_error = StrataError(UNBOUND, self.unavailable_reason())
            return
        command = _command()
        if not _resolvable(command):
            self._init_error = StrataError(UNREACHABLE, self.unavailable_reason())
            return
        env = {**os.environ, "STRATA_AGENT_SCOPE": _scope()}
        if _skill():
            env["STRATA_AGENT_SKILL"] = _skill()
        env.pop("STRATA_AGENT_SESSION_ID", None)  # the server derives it; setting one splits it
        client = StrataClient(command, env, cwd=os.environ.get(PROJECT_ENV, "").strip() or None)
        try:
            client.start()
        except StrataError as exc:
            self._init_error = exc
            return
        self._client = client
        self.queue_prefetch("", session_id=session_id)

    def shutdown(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()

    # -- recall: always from the cache ----------------------

    def system_prompt_block(self) -> str:
        return tools.SYSTEM_BLOCK.format(scope=_scope() or "(unbound)")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        with self._lock:
            return self._cache

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        client = self._client
        with self._lock:
            if client is None:
                return
            if self._refreshing:
                self._again = True  # one more refresh follows the one in flight
                return
            self._refreshing = True
        spawn_context_thread(self._refresh, name="strata-prefetch", args=(client,)).start()

    def _refresh(self, client: StrataClient) -> None:
        try:
            data = client.call_tool(tools.READ_PERSPECTIVE, {})
            text = render_perspective(data)
            count = sum(
                len(layer.get("directives") or [])
                + len((layer.get("summary") or {}).get("directives") or [])
                for layer in data.get("layers", [])
            )
            with self._lock:
                self._cache, self._directives = text, count
        except StrataError as exc:
            logger.debug("strata prefetch failed: %s", exc.message)
        finally:
            with self._lock:
                again, self._again = self._again, False
                self._refreshing = again
            if again:
                spawn_context_thread(self._refresh, name="strata-prefetch", args=(client,)).start()

    def recall_status(self) -> RecallStatus | None:
        with self._lock:
            return RecallStatus("Strata", self._directives) if self._cache else None

    # -- tools ----------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [dict(schema) for schema in tools.SCHEMAS]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        """Run one tool; always return a JSON string, errors included (never raise into Hermes)."""
        try:
            if tool_name == tools.STATUS:
                return json.dumps(self._status())
            if tool_name == tools.READ_PERSPECTIVE:
                return json.dumps(self._require_client().call_tool(tool_name, {}))
            sent = tools.server_arguments(tool_name, args or {}, _scope())
            return json.dumps(self._require_client().call_tool(tool_name, sent))
        except KeyError:
            return json.dumps(
                {"error": f"{self.name} has no tool {tool_name!r}.", "code": "unknown"}
            )
        except StrataError as exc:
            return json.dumps({"error": f"{exc.message} {_UNTOUCHED}", "code": exc.code})

    def _require_client(self) -> StrataClient:
        if self._client is None:
            raise self._init_error or StrataError(UNREACHABLE, "Strata is not running.")
        return self._client

    def _status(self) -> dict[str, Any]:
        up = self._client is not None and self._client.is_up()
        return {
            "scope": _scope() or None,
            "skill": _skill() or None,
            "server": "up" if up else "down",
            "problem": None if up or self._init_error is None else self._init_error.message,
        }

    # -- setup ----------------------

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "scope",
                "description": "The Strata scope this profile is bound to (STRATA_AGENT_SCOPE)",
                "required": True,
                "env_var": "STRATA_AGENT_SCOPE",
            },
            {
                "key": "skill",
                "description": "The Strata skill this profile acts as (STRATA_AGENT_SKILL)",
                "required": False,
                "env_var": "STRATA_AGENT_SKILL",
            },
            {
                "key": "project_dir",
                "description": (
                    "The registered Strata project (the folder holding .strata/config.toml) "
                    "the server runs in"
                ),
                "required": False,
                "env_var": PROJECT_ENV,
            },
            {
                "key": "command",
                "description": "How to launch the Strata MCP server",
                "required": False,
                "default": DEFAULT_COMMAND,
                "env_var": COMMAND_ENV,
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        """Every field carries an ``env_var``, so there is no native config to write."""
