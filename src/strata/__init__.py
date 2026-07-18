"""Strata — shared memory for agent fleets."""

__version__ = "1.6.0"

#: PyPI distribution name. The import name (``strata``) and console scripts
#: (``strata``, ``strata-mcp``) are unchanged — only the package name pip
#: resolves is different, because ``strata`` on PyPI belongs to an unrelated,
#: dormant package (issue #49). The engine distribution is ``strata-mem``
#: (ADR 0009 D1); PyPI ``memfleet`` was repurposed for the cloud client.
#: Anything that looks up *this* project's installed distribution metadata
#: (e.g. ``importlib.metadata``) must use this constant, not the string
#: ``"strata"``.
DISTRIBUTION_NAME = "strata-mem"
