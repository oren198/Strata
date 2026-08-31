"""Suite-wide test isolation guards.

`strata register` / `strata unregister` fall back to
:func:`strata.install.detect_harnesses` when no explicit ``--harness`` flag
is given, and Codex wiring reads/writes ``$CODEX_HOME/config.toml``
(default ``~/.codex/config.toml`` when ``$CODEX_HOME`` is unset). Before this
fixture existed, a test that forgot to pin harness selection or
``$CODEX_HOME`` could fall through to real-machine detection and land a
write in the developer's actual ``~/.codex/config.toml`` — the suite once
did exactly that.

This autouse fixture is a structural backstop, not a substitute for tests
that care about the real values: it

  (a) monkeypatches ``strata.install.detect_harnesses`` to return ``[]``, so
      any code path that resolves harnesses via detection (and not an
      explicit ``--harness``/``harness=[...]`` pin) falls back to
      claude-code-only instead of fanning out onto whatever happens to be
      installed on the machine running the suite, and
  (b) points ``$CODEX_HOME`` at a private location under this test's own
      ``tmp_path``, so any code path that still reaches for the Codex
      config default never finds — or writes to — the real one.

Both are plain defaults, applied via ``monkeypatch``: a test that requests
its own ``codex_home`` fixture (tests/test_register_codex.py,
tests/test_register_multi_harness.py) or otherwise calls
``monkeypatch.setenv("CODEX_HOME", ...)`` / ``monkeypatch.setattr(install,
"detect_harnesses", ...)`` itself simply overrides these defaults from
within the test body, which always runs after fixture setup — no marker
needed. A test that must exercise the real, unpatched machine (there are
none today) can opt out with ``@pytest.mark.real_machine``.
"""

from __future__ import annotations

import pytest

from strata import install


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_machine: opt this test out of the autouse harness/CODEX_HOME isolation guard "
        "(tests/conftest.py) so it can exercise real-machine detection/env.",
    )


@pytest.fixture(autouse=True)
def _isolate_harness_detection_and_codex_home(
    request: pytest.FixtureRequest,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural guard: never detect or write against the real machine.

    See module docstring. Skipped only for tests marked ``real_machine``.
    """
    if request.node.get_closest_marker("real_machine") is not None:
        return

    monkeypatch.setattr(install, "detect_harnesses", lambda *a, **k: [])
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "_autouse_codex_home_guard"))

    # (c) Run each test from a private directory (issue #181).
    #
    # ``project_config.load_project_config`` walks UP from the current
    # working directory looking for ``.strata/config.toml``, and
    # ``resolve_storage_paths`` gives whatever it finds precedence over env
    # settings. With the suite's cwd inside a checkout that has been
    # registered against itself — which the README encourages for
    # dogfooding — every test that resolves storage without an explicit
    # override finds the developer's real config and reads their live store.
    # On 2026-08-31 that produced 23 failures in tests/test_app.py alone,
    # asserting against the developer's own scope ids.
    #
    # tmp_path is empty and its ancestors are pytest's own tmp root, so the
    # walk-up terminates having found nothing — the same state a bare
    # machine is in. Tests that want a project config still build one and
    # pass its path explicitly; only the ambient default is pinned.
    guard_cwd = tmp_path / "_autouse_cwd_guard"
    guard_cwd.mkdir(exist_ok=True)
    monkeypatch.chdir(guard_cwd)
