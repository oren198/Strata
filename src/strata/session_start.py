"""SessionStart hook — the READ-side trigger for shared memory.

Agents do not read their memory at session start, even when instructions
tell them to. Static instructions compete with everything else in context;
nothing fires at the moment a session begins. The WRITE side already learned
this lesson — a ``Stop`` hook exists (:mod:`strata.freshness`) precisely
because "contribute before ending" as prose was not enough. This module is
the READ-side equivalent trigger.

``strata register`` (the claude-code path) wires a Claude Code
``SessionStart`` hook running ``strata session-start-hook``
(:func:`run_session_start_hook`), symmetric with the existing ``Stop`` hook
in every install respect — see :mod:`strata.install`'s
``HOOK_SCRIPT_NAME_SESSION_START`` / ``HOOK_SESSION_START_ENTRY`` and their
merge/remove/self-update machinery.

ABSOLUTE CONSTRAINT — this hook is a TRIGGER, not a delivery channel. It
prints a short imperative to stdout, which Claude Code injects as session
context: read your perspective (``strata_read_perspective``) before your
first substantive answer; if that read reports the session is not bound, ask
the user which scope this session should act as, then call ``strata_bind``.
It never prints memory content itself — doing so would bypass scope binding,
entitlement, and judgment, the three things the whole engine exists to
enforce.

It may cheaply name the fleet's available scope ids (:func:`_available_scope_ids`)
so that ask is concrete — reading fleet.yaml is fleet *topology*, not scoped
memory (the same distinction ``strata_list_scopes`` draws when it answers an
unbound session). No database access, no LLM call, no judging.

Vocabulary follows CONTEXT.md: scope, fleet, perspective, bound.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

#: The imperative printed to stdout. ``{scopes}`` is filled in by
#: :func:`render_session_start_message` with a parenthetical naming the
#: fleet's available scope ids when cheaply known, or left empty otherwise.
#: Never carries memory content — only the instruction to go read it.
SESSION_START_MESSAGE = (
    "This project keeps shared memory across sessions. Before your first "
    "substantive answer, call strata_read_perspective to read your "
    "perspective. If it reports this session is not bound, ask the user "
    "which scope this session should act as{scopes}, then call strata_bind "
    "with their answer."
)


def _available_scope_ids() -> list[str] | None:
    """Cheaply read fleet.yaml's active scope ids, or ``None`` if unavailable.

    A plain file read — no database access, no LLM call, no judgment — so
    the hook's ask can name concrete scope ids instead of asking blind.
    Every failure (no registered project, unreadable or invalid fleet.yaml)
    degrades to ``None``: the hook still prints its instruction, just
    without names — mirroring the Stop hook's "a broken hook must never
    break the session" rule.
    """
    try:
        from strata.fleet_config import FleetConfig  # noqa: PLC0415
        from strata.project_config import resolve_storage_paths  # noqa: PLC0415

        paths = resolve_storage_paths()
        fleet = FleetConfig.load(Path(paths.fleet_yaml_path))
    except Exception:  # noqa: BLE001 — the hook must never raise
        return None
    return [s.id for s in fleet.active_scopes()]


def render_session_start_message(scope_ids: list[str] | None) -> str:
    """Render :data:`SESSION_START_MESSAGE`, naming *scope_ids* when given.

    ``None`` or an empty list both render the same plain instruction, with
    no parenthetical — either "no fleet was found" or "the fleet has no
    active scopes" reads identically to an agent deciding what to do next.
    """
    scopes = f" (available scopes: {', '.join(scope_ids)})" if scope_ids else ""
    return SESSION_START_MESSAGE.format(scopes=scopes)


def run_session_start_hook(*, out: TextIO | None = None) -> int:
    """Print the READ-side trigger instruction. Always returns ``0``.

    Every failure path — no project registered, unreadable fleet.yaml, any
    other error while naming scopes — degrades to printing the instruction
    without scope names; a broken hook must never break the session (mirrors
    :func:`strata.freshness.run_stop_hook`'s degrade-silently rule).
    """
    out = sys.stdout if out is None else out
    out.write(render_session_start_message(_available_scope_ids()))
    return 0
