"""Strata — shared memory for agent fleets."""

__version__ = "1.5.0"

#: PyPI distribution name. The import name (``strata``) and console scripts
#: (``strata``, ``strata-mcp``) are unchanged — only the package name pip
#: resolves is different, because ``strata`` on PyPI belongs to an unrelated,
#: dormant package (issue #49). Anything that looks up *this* project's
#: installed distribution metadata (e.g. ``importlib.metadata``) must use
#: this constant, not the string ``"strata"``.
DISTRIBUTION_NAME = "mem-strata"
