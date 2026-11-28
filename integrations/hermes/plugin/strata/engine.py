"""Where the Strata engine (``strata-mem``) comes from.

The provider never imports Strata: the engine runs as the ``strata-mcp`` subprocess. Hermes's own
package manager (PM) provisions it into an isolated side environment, so nothing is pip-installed
into Hermes and the engine's own ``mcp`` pin never meets Hermes's. ``pm.ensure_python_tool`` is the
public operation for this (``pm/client.py``): it returns the validated ``strata-mcp`` path and runs
no shell child of ours. Non-explicit calls honour ``security.allow_lazy_installs``.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ENGINE_NAME = "strata-mem"
EXECUTABLE = "strata-mcp"
#: Exactly the Strata release this plugin was tested with; a test pins it to the repo version.
ENGINE_REQUIREMENT = "strata-mem==1.22.0"
SETUP_HINT = "run `hermes memory setup strata`"
_TIMEOUT = 900


class EngineUnavailable(Exception):
    """The engine could not be found or provisioned; the message says why."""


def installed_engine() -> Path | None:
    """The ``strata-mcp`` of the engine PM already selected, or None. Never installs."""
    try:
        import pm  # noqa: PLC0415

        found = pm.python_tool(ENGINE_NAME, EXECUTABLE)
    except Exception:  # noqa: BLE001 - no PM (a bare checkout) or no selection yet
        return None
    return Path(found) if found else None


def lazy_install_allowed() -> bool:
    try:
        import pm  # noqa: PLC0415

        return bool(pm.lazy_installs_allowed())
    except Exception:  # noqa: BLE001
        return False


def ensure_engine(requirement: str, *, explicit: bool) -> Path:
    """Install (or find) the pinned engine through PM and return its ``strata-mcp`` path.

    ``explicit=False`` is the lazy path (PM refuses when lazy installs are off); ``explicit=True``
    is the user's own ``hermes memory setup``. Raises ``EngineUnavailable``; never prompts.
    """
    try:
        import pm  # noqa: PLC0415
    except ImportError as exc:
        raise EngineUnavailable("Hermes's package manager is not available.") from exc
    try:
        return Path(
            pm.ensure_python_tool(
                ENGINE_NAME, [requirement], EXECUTABLE, explicit=explicit, timeout=_TIMEOUT
            )
        )
    except Exception as exc:  # noqa: BLE001 - PM's refusals and install errors both end here
        logger.debug("engine bootstrap failed: %s", exc)
        raise EngineUnavailable(str(exc) or exc.__class__.__name__) from exc
