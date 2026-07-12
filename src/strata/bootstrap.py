"""Bootstrap configuration for the Strata fleet.

Validates ``fleet.yaml`` and prepares the in-memory :class:`FleetConfig`
mirror — no DB writes.  The command name (``strata bootstrap``) is preserved
for backward compatibility; its semantics changed under ADR 0002.

Vocabulary follows CONTEXT.md exactly: stratum, scope, edge — never level,
group, or relation.
"""

from __future__ import annotations

from pathlib import Path

from strata.fleet_config import FleetConfig

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_fleet_config(yaml_path: str | Path) -> FleetConfig:
    """Parse *yaml_path*, validate all load-time invariants, and return a
    :class:`FleetConfig`.

    This is the primary entry point for ``strata bootstrap``.  It does not
    write to any database.

    Args:
        yaml_path: Path to the fleet YAML file.

    Returns:
        Validated and loaded :class:`FleetConfig`.

    Raises:
        FileNotFoundError:    If *yaml_path* does not exist.
        FleetConfigError:     On any load-time invariant violation — the
                              original 8 (see ADR 0002 § "Validation
                              invariants") plus ADR 0004's and ADR 0008's
                              reserved ``"operator"`` stratum label.
    """
    return FleetConfig.load(Path(yaml_path))
