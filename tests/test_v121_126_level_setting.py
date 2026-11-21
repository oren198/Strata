"""#126 / ADR 0018 D1: ``human_review`` is an integer level on a scope in fleet.yaml.

Absent means 0. 0 and 1 load. Anything else is refused at load, naming the level, with no
fallback to 0 or 1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strata.fleet_config import FleetConfig, FleetConfigError
from tests.v121_126_support import write_fleet


def _load(tmp_path: Path, value) -> FleetConfig:  # noqa: ANN001
    return FleetConfig.load(write_fleet(tmp_path, {"g_org": value}))


def test_an_absent_level_is_zero(tmp_path: Path) -> None:
    fleet = FleetConfig.load(write_fleet(tmp_path))
    assert fleet.get_scope("g_org").human_review == 0
    assert fleet.get_scope("g_web").human_review == 0


@pytest.mark.parametrize("level", [0, 1])
def test_levels_zero_and_one_load(tmp_path: Path, level: int) -> None:
    fleet = _load(tmp_path, level)
    assert fleet.get_scope("g_org").human_review == level
    assert fleet.get_scope("g_web").human_review == 0  # per scope, never inherited


@pytest.mark.parametrize("value", [2, -1, "yes", True, 1.5])
def test_every_other_value_is_refused_at_load_with_no_fallback(tmp_path: Path, value) -> None:  # noqa: ANN001
    with pytest.raises(FleetConfigError, match=r"level .* is not defined"):
        _load(tmp_path, value)
