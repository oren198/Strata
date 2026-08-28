"""Lazy fleet reload — stat-before-read freshness for ``fleet.yaml``.

ADR 0002 loaded ``fleet.yaml`` once, at process startup, and served every
fleet-reading request from that in-memory mirror for the life of the
process. That is fine until something else — an agent, an operator, another
process — edits ``fleet.yaml`` out of band: the running Console backend or
MCP server keeps answering from the stale snapshot until it is restarted
(the incident this module fixes — see the dated addendum in
``docs/adr/0002-fleet-config-source-of-truth.md``).

:class:`FleetReloader` is the ONE place that decides "is fleet.yaml still
what I last loaded?" — both :mod:`strata.app` (the FastAPI backend, via
``app.state.fleet_reloader``) and :mod:`strata.mcp.server` (the MCP server,
via its module-level singleton) wrap their :class:`~strata.fleet_config.FleetConfig`
in one of these rather than reimplementing the check twice.

Strategy: stat the file (mtime + size) before serving any fleet-reading call.
Unchanged → return the cached :class:`FleetConfig` with no re-parse. Changed →
reload through :meth:`FleetConfig.load`, so the full set of load-time
invariants runs again. A reload that fails validation (or fails to parse at
all) does NOT propagate to the caller once a good fleet has been served
before: the reloader keeps serving the last good :class:`FleetConfig` and
records a plain-language warning on :attr:`FleetReloader.warning`, so a typo
mid-edit degrades to "stale but working" rather than "every fleet-reading
call now throws." The first load (no prior good config) still raises —
there is nothing valid to fall back to.

No watcher, no polling thread: the check is a single ``stat()`` call, paid
once per read, deliberately cheap enough that ADR 0004's "re-read on every
call" MCP design and a request-scoped FastAPI read both afford it.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from strata.fleet_config import FleetConfig

logger = logging.getLogger(__name__)

#: Empty fleet used when fleet.yaml does not exist yet (fresh install, or a
#: test harness that never wrote one). Matches the fallback both call sites
#: used before this module existed.
_EMPTY_FLEET = FleetConfig(strata=[], scopes=[], edges=[])


class FleetReloader:
    """Serves a :class:`FleetConfig`, reloading from *path* only when it changed.

    Thread-safe (a single lock around the stat-check-reload sequence) so the
    FastAPI backend, which may serve requests from a thread pool, can share
    one reloader across requests without a torn read.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._config: FleetConfig | None = None
        self._stat_key: tuple[int, int] | None = None
        self._warning: str | None = None

    @property
    def path(self) -> Path:
        """The ``fleet.yaml`` path this reloader watches."""
        return self._path

    @property
    def warning(self) -> str | None:
        """Plain-language warning from the most recent reload attempt, or ``None``.

        Set when a reload was attempted (the file's mtime/size changed) but
        the new content failed to load — invalid YAML, or a fleet.yaml
        invariant violation. Cleared the next time a reload succeeds.
        """
        return self._warning

    def get(self) -> FleetConfig:
        """Return the current :class:`FleetConfig`, reloading if *path* changed.

        - File unchanged since the last read (same mtime + size) → returns
          the cached config, no re-parse.
        - File changed and loads cleanly → returns the freshly loaded
          config; :attr:`warning` is cleared.
        - File changed but fails to load, AND a good config was already
          cached → returns the STALE cached config and sets :attr:`warning`.
        - File changed but fails to load, and nothing has ever loaded
          successfully → raises (nothing valid to fall back to).
        - File does not exist → returns an empty :class:`FleetConfig` (or the
          last good one, if the file existed and then disappeared).
        """
        with self._lock:
            try:
                st = self._path.stat()
            except OSError:
                if self._config is not None:
                    return self._config
                self._config = _EMPTY_FLEET
                self._stat_key = None
                return self._config

            stat_key = (st.st_mtime_ns, st.st_size)
            if self._config is not None and stat_key == self._stat_key:
                return self._config

            try:
                loaded = FleetConfig.load(self._path)
            except Exception as exc:  # noqa: BLE001 - any load failure keeps serving stale
                self._stat_key = stat_key
                message = (
                    f"fleet.yaml changed but failed to reload "
                    f"({getattr(exc, 'kind', exc.__class__.__name__)}): {exc}. "
                    "Serving the last known-good fleet."
                )
                if self._config is not None:
                    self._warning = message
                    logger.warning("fleet reload failed, serving stale fleet: %s", exc)
                    return self._config
                # Nothing to fall back to — the caller (startup path) needs
                # the real exception, not a swallowed one.
                logger.warning("fleet reload failed, no prior fleet to fall back to: %s", exc)
                raise
            else:
                self._config = loaded
                self._stat_key = stat_key
                self._warning = None
                return self._config


__all__ = ["FleetReloader"]
