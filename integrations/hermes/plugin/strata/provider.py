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

Profile settings are read the way Hermes 0.23.3 reads a profile's secrets, NOT from
``os.environ``: for a named profile the profile ``.env`` lives in the profile's SECRET SCOPE
(``agent.secret_scope.get_secret``, which ``plugins/memory/retaindb`` and ``byterover`` use for the
same purpose), and ``os.environ`` is the launch profile's. The values are resolved once, from
``initialize(hermes_home=...)`` (the multiplexing contract in ``agent/memory_provider.py``: bind
from ``hermes_home`` and the scoped secrets read during ``initialize``, keep the binding on the
instance), and passed to the ``strata-mcp`` subprocess explicitly.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import threading
from pathlib import Path
from typing import Any

from agent.memory_provider import MemoryProvider, RecallStatus, spawn_context_thread

from . import engine, tools
from .client import UNBOUND, UNREACHABLE, StrataClient, StrataError

logger = logging.getLogger(__name__)

COMMAND_ENV = "STRATA_MCP_COMMAND"
PROJECT_ENV = "STRATA_PROJECT_DIR"
# advanced: a different exact pin (a mirror, a local build)
ENGINE_ENV = "STRATA_ENGINE_REQUIREMENT"
JUDGE_KEY_ENV = "JUDGE_API_KEY"
_UNTOUCHED = "Hermes built-in memory is untouched."


SCOPE_ENV = "STRATA_AGENT_SCOPE"
SKILL_ENV = "STRATA_AGENT_SKILL"


def _read(name: str, hermes_home: str | None = None) -> str:
    """One profile setting. Order: the profile secret scope (``get_secret``: the profile ``.env``
    for a named profile, ``os.environ`` for the default one), then the profile's own ``.env``
    under ``hermes_home`` (bound by ``initialize``), then ``os.environ`` ONLY where Hermes' secret
    scope is not importable (the provider's own test run). A routed profile never reads another
    profile's ``os.environ`` value."""
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret  # noqa: PLC0415
    except ImportError:
        return os.environ.get(name, "").strip()
    try:
        value = get_secret(name, "")
    except UnscopedSecretError:
        value = ""  # multiplexing with no scope bound: never fall back to the launch environ
    except Exception:  # noqa: BLE001 - a settings read must never raise into Hermes
        value = ""
    if (value or "").strip():
        return str(value).strip()
    if hermes_home:
        try:
            from agent.secret_scope import load_env_file  # noqa: PLC0415

            return (load_env_file(Path(hermes_home) / ".env").get(name) or "").strip()
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _split_command(text: str) -> list[str]:
    return shlex.split(text.strip()) if text.strip() else []


def _resolvable(command: list[str]) -> bool:
    return bool(command) and (os.access(command[0], os.X_OK) or Path(command[0]).is_file())


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
        # the profile binding: resolved by initialize() (or lazily for is_available) and kept here
        self._hermes_home: str | None = None
        self._bound: dict[str, str] | None = None

    # -- identity and availability ----------------------

    @property
    def name(self) -> str:
        return "strata"

    def _settings(self) -> dict[str, str]:
        """The profile's Strata settings: the binding ``initialize`` made, else a fresh read."""
        if self._bound is not None:
            return self._bound
        return self._resolve(self._hermes_home)

    @staticmethod
    def _resolve(hermes_home: str | None) -> dict[str, str]:
        return {
            "scope": _read(SCOPE_ENV, hermes_home),
            "skill": _read(SKILL_ENV, hermes_home),
            "project": _read(PROJECT_ENV, hermes_home),
            "command": _read(COMMAND_ENV, hermes_home),
            "engine": _read(ENGINE_ENV, hermes_home) or engine.ENGINE_REQUIREMENT,
            "judge_key": _read(JUDGE_KEY_ENV, hermes_home),
        }

    def _scope(self) -> str:
        return self._settings()["scope"]

    def _skill(self) -> str:
        return self._settings()["skill"]

    def _resolve_command(self, settings: dict[str, str]) -> list[str]:
        """``STRATA_MCP_COMMAND`` if set, else the ``strata-mcp`` of the engine PM selected."""
        explicit = _split_command(settings.get("command", ""))
        if explicit:
            return explicit
        found = engine.installed_engine()
        return [str(found)] if found else []

    def is_available(self) -> bool:
        """Config only, no network, no install: a scope is set and the engine is there or can be
        provisioned lazily."""
        if not self._scope():
            return False
        return _resolvable(self._resolve_command(self._settings())) or engine.lazy_install_allowed()

    def unavailable_reason(self) -> str:
        if not self._scope():
            return (
                "STRATA_AGENT_SCOPE is not set for this profile; set it (and STRATA_AGENT_SKILL) "
                "in the profile's env to bind it to a Strata scope."
            )
        explicit = self._settings()["command"].strip()
        if explicit:
            return f"the strata-mcp command {explicit!r} was not found; fix {COMMAND_ENV}."
        return f"the Strata engine ({engine.ENGINE_NAME}) is not installed; {engine.SETUP_HINT}."

    def identity_signature(self) -> dict[str, Any]:
        return {"strata": {"scope": self._scope(), "skill": self._skill()}}

    # -- lifecycle ----------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
        self._init_error = None
        home = kwargs.get("hermes_home")
        self._hermes_home = str(home) if home else self._hermes_home
        self._bound = self._resolve(self._hermes_home)  # bound once for this profile
        settings = self._bound
        if not settings["scope"]:
            self._init_error = StrataError(UNBOUND, self.unavailable_reason())
            return
        command = self._resolve_command(settings)
        if not command:
            try:  # the lazy path: PM refuses when security.allow_lazy_installs is off
                command = [str(engine.ensure_engine(settings["engine"], explicit=False))]
            except engine.EngineUnavailable as exc:
                self._init_error = StrataError(UNREACHABLE, f"{self.unavailable_reason()} ({exc})")
                return
        if not _resolvable(command):
            self._init_error = StrataError(UNREACHABLE, self.unavailable_reason())
            return
        # the scoped values go to strata-mcp EXPLICITLY: the launch environ is not this profile's
        env = {**os.environ, SCOPE_ENV: settings["scope"]}
        if settings["skill"]:
            env[SKILL_ENV] = settings["skill"]
        if settings["judge_key"]:  # a Hermes-route key reaches only the engine subprocess
            env[JUDGE_KEY_ENV] = settings["judge_key"]
        env.pop("STRATA_AGENT_SESSION_ID", None)  # the server derives it; setting one splits it
        client = StrataClient(command, env, cwd=settings["project"] or None)
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
        return tools.SYSTEM_BLOCK.format(scope=self._scope() or "(unbound)")

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
            sent = tools.server_arguments(tool_name, args or {}, self._scope())
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
            "scope": self._scope() or None,
            "skill": self._skill() or None,
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
                "env_var": SCOPE_ENV,
            },
            {
                "key": "skill",
                "description": "The Strata skill this profile acts as (STRATA_AGENT_SKILL)",
                "required": False,
                "env_var": SKILL_ENV,
            },
            {
                "key": "project_dir",
                "description": (
                    "The registered Strata project (the folder holding .strata/config.toml) "
                    "the engine runs in"
                ),
                "required": False,
                "env_var": PROJECT_ENV,
            },
        ]

    def get_status_config(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """What ``hermes memory status`` shows (Hermes passes the ``memory.strata`` block)."""
        settings = self._settings()
        return {
            "scope": settings["scope"] or None,
            "skill": settings["skill"] or None,
            "project_dir": settings["project"] or None,
            "engine": self._engine_line(settings),
        }

    def _engine_line(self, settings: dict[str, str]) -> str:
        explicit = _split_command(settings["command"])
        if explicit:
            return f"custom command {' '.join(explicit)}"
        found = engine.installed_engine()
        if found:
            return f"installed at {found}"
        if engine.lazy_install_allowed():
            return f"{settings['engine']} will be installed on the first chat"
        return f"not installed, and lazy installs are off; {engine.SETUP_HINT}"

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        """The wizard has written the env fields; this is the explicit engine bootstrap.

        Where lazy installs are off this is the step that installs the engine. It never raises
        into the wizard: a failure is printed with what to do next.
        """
        try:
            path = engine.ensure_engine(self._settings()["engine"], explicit=True)
        except engine.EngineUnavailable as exc:
            print(f"  Strata engine not installed: {exc}\n  Retry: {engine.SETUP_HINT}.")
            return
        print(f"  Strata engine ready: {path}")
