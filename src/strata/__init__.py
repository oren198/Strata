"""Strata — shared memory for agent fleets."""

# Re-exported so hosts can ask "was this accepted?" without restating the
# verdict pair (issue #145). Defined once, beside the record's Judgment.
from strata.record_store import ACCEPTED_DECISIONS as ACCEPTED_DECISIONS

__version__ = "1.18.0"

#: PyPI distribution name. The import name (``strata``) and console scripts
#: (``strata``, ``strata-mcp``) are unchanged — only the package name pip
#: resolves is different, because ``strata`` on PyPI belongs to an unrelated,
#: dormant package (issue #49). The engine distribution is ``strata-mem``
#: (ADR 0009 D1).
#: Anything that looks up *this* project's installed distribution metadata
#: (e.g. ``importlib.metadata``) must use this constant, not the string
#: ``"strata"``.
DISTRIBUTION_NAME = "strata-mem"
