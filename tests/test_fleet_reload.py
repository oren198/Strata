"""Tests for :mod:`strata.fleet_reload` — the shared lazy-reload wrapper.

Feature A (live fleet reload): before serving any fleet-reading request or
tool call, the reloader stats fleet.yaml's mtime + size; unchanged, it serves
the cached FleetConfig without re-parsing; changed, it reloads through the
normal FleetConfig validation. An invalid file at reload time keeps serving
the last good fleet and records a plain warning rather than raising.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from strata.fleet_config import FleetConfig
from strata.fleet_reload import FleetReloader

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_fleet(path: Path, scope_ids: list[str]) -> None:
    raw = {
        "strata": [{"id": "L0", "name": "top", "ordinal": 0}],
        "scopes": [{"id": sid, "name": sid, "stratum_id": "L0"} for sid in scope_ids],
        "edges": [],
    }
    path.write_text(yaml.dump(raw, default_flow_style=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Basic reload-on-change behavior
# ---------------------------------------------------------------------------


def test_unchanged_file_serves_cached_config_object(tmp_path: Path) -> None:
    fleet_path = tmp_path / "fleet.yaml"
    _write_fleet(fleet_path, ["g_a"])

    reloader = FleetReloader(fleet_path)
    first = reloader.get()
    second = reloader.get()
    third = reloader.get()
    assert second is first
    assert third is first


def test_changed_file_reloads_and_reflects_new_content(tmp_path: Path) -> None:
    fleet_path = tmp_path / "fleet.yaml"
    _write_fleet(fleet_path, ["g_a"])

    reloader = FleetReloader(fleet_path)
    first = reloader.get()
    assert {s.id for s in first.scopes} == {"g_a"}

    _write_fleet(fleet_path, ["g_a", "g_b"])

    second = reloader.get()
    assert {s.id for s in second.scopes} == {"g_a", "g_b"}


def test_reload_invocation_count_tracks_only_actual_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A monkeypatched FleetConfig.load must fire once per real content change."""
    fleet_path = tmp_path / "fleet.yaml"
    _write_fleet(fleet_path, ["g_a"])

    calls = 0
    real_load = FleetConfig.load.__func__

    def patched(cls, path):
        nonlocal calls
        calls += 1
        return real_load(cls, path)

    monkeypatch.setattr(FleetConfig, "load", classmethod(patched))

    reloader = FleetReloader(fleet_path)
    reloader.get()
    assert calls == 1

    # No change — no reparse.
    reloader.get()
    reloader.get()
    assert calls == 1

    # Real change — exactly one more reparse.
    _write_fleet(fleet_path, ["g_a", "g_b"])
    reloader.get()
    assert calls == 2


# ---------------------------------------------------------------------------
# Invalid file at reload time
# ---------------------------------------------------------------------------


def test_invalid_reload_keeps_serving_last_good_and_sets_warning(tmp_path: Path) -> None:
    fleet_path = tmp_path / "fleet.yaml"
    _write_fleet(fleet_path, ["g_a"])

    reloader = FleetReloader(fleet_path)
    good = reloader.get()
    assert reloader.warning is None

    # Corrupt the file: a scope referencing an undefined stratum.
    raw = {
        "strata": [{"id": "L0", "name": "top", "ordinal": 0}],
        "scopes": [{"id": "g_a", "name": "g_a", "stratum_id": "NOPE"}],
        "edges": [],
    }
    fleet_path.write_text(yaml.dump(raw, default_flow_style=False), encoding="utf-8")

    served = reloader.get()
    assert served is good
    assert {s.id for s in served.scopes} == {"g_a"}
    assert reloader.warning is not None
    assert "fleet.yaml" in reloader.warning


def test_invalid_yaml_at_reload_time_also_keeps_last_good(tmp_path: Path) -> None:
    fleet_path = tmp_path / "fleet.yaml"
    _write_fleet(fleet_path, ["g_a"])

    reloader = FleetReloader(fleet_path)
    good = reloader.get()

    fleet_path.write_text(": : : not valid yaml : : :\n[[[", encoding="utf-8")

    served = reloader.get()
    assert served is good
    assert reloader.warning is not None


def test_recovery_after_invalid_edit_clears_warning(tmp_path: Path) -> None:
    fleet_path = tmp_path / "fleet.yaml"
    _write_fleet(fleet_path, ["g_a"])

    reloader = FleetReloader(fleet_path)
    reloader.get()

    raw = {
        "strata": [{"id": "L0", "name": "top", "ordinal": 0}],
        "scopes": [{"id": "g_a", "name": "g_a", "stratum_id": "NOPE"}],
        "edges": [],
    }
    fleet_path.write_text(yaml.dump(raw, default_flow_style=False), encoding="utf-8")
    reloader.get()
    assert reloader.warning is not None

    _write_fleet(fleet_path, ["g_a", "g_b"])
    fixed = reloader.get()
    assert {s.id for s in fixed.scopes} == {"g_a", "g_b"}
    assert reloader.warning is None


def test_first_load_invalid_raises_when_no_prior_good_config(tmp_path: Path) -> None:
    fleet_path = tmp_path / "fleet.yaml"
    raw = {
        "strata": [{"id": "L0", "name": "top", "ordinal": 0}],
        "scopes": [{"id": "g_a", "name": "g_a", "stratum_id": "NOPE"}],
        "edges": [],
    }
    fleet_path.write_text(yaml.dump(raw, default_flow_style=False), encoding="utf-8")

    reloader = FleetReloader(fleet_path)
    with pytest.raises(Exception):  # noqa: B017,PT011 - FleetConfigError, deliberately broad
        reloader.get()


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------


def test_missing_file_serves_empty_fleet(tmp_path: Path) -> None:
    fleet_path = tmp_path / "does_not_exist.yaml"
    reloader = FleetReloader(fleet_path)
    fleet = reloader.get()
    assert fleet.scopes == []
    assert fleet.strata == []


def test_file_appearing_after_missing_is_picked_up(tmp_path: Path) -> None:
    fleet_path = tmp_path / "fleet.yaml"
    reloader = FleetReloader(fleet_path)
    empty = reloader.get()
    assert empty.scopes == []

    _write_fleet(fleet_path, ["g_a"])
    loaded = reloader.get()
    assert {s.id for s in loaded.scopes} == {"g_a"}
