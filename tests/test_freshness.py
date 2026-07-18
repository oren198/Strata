"""Tests for the turn-boundary Stop-hook and background evaluator (issue #112).

Exercises the WP3 mechanism directly (no live model, no real subprocess):

Gate logic:
1. Below the read threshold → the hook spawns no evaluator.
2. At the threshold (zero contributions/declines) → it spawns exactly once.
3. A recorded contribution (or decline) → no spawn (asymmetry already released).

One-in-flight lock:
4. A second turn-end while an evaluator holds the lock is a no-op (no spawn).
5. A stale lock (older than the TTL) is reclaimed and a fresh evaluator spawns.

Strict mode:
6. STRATA_FRESHNESS_STRICT=1 with the gate open emits the block JSON once and
   spawns nothing.
7. Strict mode respects stop_hook_active — it never blocks twice (no loop).

Evaluator:
8. A mocked model draft is submitted through the JUDGED contribute path
   (scope-manager boundary exercised, mocked client) and resets the counters
   via record_contribution.
9. A mocked "nothing to record" verdict records a mechanical decline and never
   constructs the judge.

Vocabulary follows CONTEXT.md: scope, contribution, scope-manager, record.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata import freshness  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.session_state import (  # noqa: E402
    NUDGE_MIN_READS,
    SessionStateStore,
    sessions_dir_for,
)
from strata.summary_store import ScopeSummary  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SESSION_ID = "sess_hooktest"


def _make_project(tmp_path: Path) -> dict[str, str]:
    """Create a minimal registered project (config.toml + fleet.yaml + migrated DB).

    Returns the resolved absolute storage paths so tests can seed session state
    and read back the record without re-walking config.
    """
    strata_dir = tmp_path / ".strata"
    strata_dir.mkdir()
    (strata_dir / "config.toml").write_text(
        'db = ".strata/strata.db"\n'
        'fleet_yaml = ".strata/fleet.yaml"\n'
        'summaries_dir = ".strata/summaries"\n',
        encoding="utf-8",
    )
    fleet = {
        "strata": [{"id": "L0", "name": "root", "ordinal": 0}],
        "scopes": [{"id": "g_root", "name": "Root", "stratum_id": "L0"}],
        "edges": [],
    }
    (strata_dir / "fleet.yaml").write_text(yaml.dump(fleet), encoding="utf-8")
    db_path = str(strata_dir / "strata.db")
    run_migrations(db_path)
    return {
        "db": db_path,
        "fleet_yaml": str(strata_dir / "fleet.yaml"),
        "summaries_dir": str(strata_dir / "summaries"),
    }


def _session_store(paths: dict[str, str]) -> SessionStateStore:
    return SessionStateStore(sessions_dir_for(paths["summaries_dir"]))


def _seed_reads(store: SessionStateStore, n: int) -> None:
    for _ in range(n):
        store.record_read(_SESSION_ID, "g_root")


def _hook_stdin(*, stop_hook_active: bool = False, transcript_path: str = "/tmp/t.jsonl") -> str:
    return json.dumps(
        {
            "session_id": "cc-abc",
            "transcript_path": transcript_path,
            "stop_hook_active": stop_hook_active,
        }
    )


def _env(paths: dict[str, str], *, api_key: bool = True, strict: bool = False) -> dict[str, str]:
    env = {
        "STRATA_AGENT_SCOPE": "g_root",
        "STRATA_AGENT_SKILL": "strata-worker",
        "STRATA_AGENT_SESSION_ID": _SESSION_ID,
    }
    if api_key:
        env["ANTHROPIC_API_KEY"] = "sk-test"
    if strict:
        env["STRATA_FRESHNESS_STRICT"] = "1"
    return env


class _Spawns:
    """A stub spawn function that records each detached-evaluator launch."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, session_id: str, transcript_path: str, env: dict[str, str]) -> None:
        self.calls.append((session_id, transcript_path))


# ---------------------------------------------------------------------------
# gate_open — pure policy (reuses the #110 threshold)
# ---------------------------------------------------------------------------


def test_gate_open_pure_policy(tmp_path: Path) -> None:
    store = _session_store(_make_project(tmp_path))
    assert freshness.gate_open(None) is False
    assert freshness.gate_open(store.read(_SESSION_ID)) is False  # no state yet

    _seed_reads(store, NUDGE_MIN_READS - 1)
    assert freshness.gate_open(store.read(_SESSION_ID)) is False  # below threshold

    store.record_read(_SESSION_ID, "g_root")  # now at threshold
    assert freshness.gate_open(store.read(_SESSION_ID)) is True

    store.record_contribution(_SESSION_ID)  # release valve closes the gate
    assert freshness.gate_open(store.read(_SESSION_ID)) is False


# ---------------------------------------------------------------------------
# Tests 1–3: gate logic through run_stop_hook
# ---------------------------------------------------------------------------


def test_below_threshold_does_not_spawn(tmp_path: Path, monkeypatch) -> None:
    paths = _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_reads(_session_store(paths), NUDGE_MIN_READS - 1)

    spawns = _Spawns()
    rc = freshness.run_stop_hook(_hook_stdin(), env=_env(paths), spawn_fn=spawns)

    assert rc == 0
    assert spawns.calls == []


def test_at_threshold_spawns_once(tmp_path: Path, monkeypatch) -> None:
    paths = _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_reads(_session_store(paths), NUDGE_MIN_READS)

    spawns = _Spawns()
    rc = freshness.run_stop_hook(_hook_stdin(), env=_env(paths), spawn_fn=spawns)

    assert rc == 0
    assert len(spawns.calls) == 1
    assert spawns.calls[0][0] == _SESSION_ID


def test_contribution_present_does_not_spawn(tmp_path: Path, monkeypatch) -> None:
    paths = _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = _session_store(paths)
    _seed_reads(store, NUDGE_MIN_READS)
    store.record_contribution(_SESSION_ID)  # asymmetry already released

    spawns = _Spawns()
    rc = freshness.run_stop_hook(_hook_stdin(), env=_env(paths), spawn_fn=spawns)

    assert rc == 0
    assert spawns.calls == []


def test_decline_present_does_not_spawn(tmp_path: Path, monkeypatch) -> None:
    paths = _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = _session_store(paths)
    _seed_reads(store, NUDGE_MIN_READS)
    store.record_decline(_SESSION_ID)

    spawns = _Spawns()
    rc = freshness.run_stop_hook(_hook_stdin(), env=_env(paths), spawn_fn=spawns)

    assert rc == 0
    assert spawns.calls == []


def test_no_api_key_does_not_spawn(tmp_path: Path, monkeypatch) -> None:
    paths = _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_reads(_session_store(paths), NUDGE_MIN_READS)

    spawns = _Spawns()
    rc = freshness.run_stop_hook(_hook_stdin(), env=_env(paths, api_key=False), spawn_fn=spawns)

    assert rc == 0
    assert spawns.calls == []  # no key → the evaluator could not draft, stay silent


# ---------------------------------------------------------------------------
# Tests 4–5: one-in-flight lock
# ---------------------------------------------------------------------------


def test_second_invocation_is_noop_while_lock_held(tmp_path: Path, monkeypatch) -> None:
    paths = _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_reads(_session_store(paths), NUDGE_MIN_READS)

    spawns = _Spawns()
    freshness.run_stop_hook(_hook_stdin(), env=_env(paths), spawn_fn=spawns)
    # The lock the first spawn took is still held (a real evaluator releases it
    # when it finishes; here it never runs), so a second turn-end is a no-op.
    freshness.run_stop_hook(_hook_stdin(), env=_env(paths), spawn_fn=spawns)

    assert len(spawns.calls) == 1


def test_stale_lock_is_reclaimed(tmp_path: Path, monkeypatch) -> None:
    paths = _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = _session_store(paths)
    _seed_reads(store, NUDGE_MIN_READS)

    # Pre-create a lock and age it past the TTL — a crashed evaluator's leftover.
    lock_path = freshness.evaluator_lock_path(store, _SESSION_ID)
    lock_path.write_text("999 0\n", encoding="utf-8")
    old = time.time() - freshness.EVALUATOR_LOCK_TTL_SECONDS - 100
    os.utime(lock_path, (old, old))

    spawns = _Spawns()
    rc = freshness.run_stop_hook(_hook_stdin(), env=_env(paths), spawn_fn=spawns)

    assert rc == 0
    assert len(spawns.calls) == 1  # stale lock reclaimed, fresh evaluator spawned


# ---------------------------------------------------------------------------
# Tests 6–7: strict mode
# ---------------------------------------------------------------------------


def test_strict_mode_blocks_once(tmp_path: Path, monkeypatch) -> None:
    paths = _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_reads(_session_store(paths), NUDGE_MIN_READS)

    spawns = _Spawns()
    out = io.StringIO()
    rc = freshness.run_stop_hook(
        _hook_stdin(stop_hook_active=False),
        env=_env(paths, strict=True),
        out=out,
        spawn_fn=spawns,
    )

    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["decision"] == "block"
    assert "strata_contribute" in payload["reason"]
    assert spawns.calls == []  # strict mode never spawns an evaluator


def test_strict_mode_respects_stop_hook_active(tmp_path: Path, monkeypatch) -> None:
    paths = _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_reads(_session_store(paths), NUDGE_MIN_READS)

    out = io.StringIO()
    rc = freshness.run_stop_hook(
        _hook_stdin(stop_hook_active=True),  # already blocked once
        env=_env(paths, strict=True),
        out=out,
    )

    assert rc == 0
    assert out.getvalue() == ""  # never blocks twice — no loop


# ---------------------------------------------------------------------------
# Degrade-silently paths
# ---------------------------------------------------------------------------


def test_malformed_stdin_is_silent_noop(tmp_path: Path, monkeypatch) -> None:
    _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    spawns = _Spawns()
    rc = freshness.run_stop_hook("not json", env=_env({"summaries_dir": ""}), spawn_fn=spawns)
    assert rc == 0
    assert spawns.calls == []


def test_no_project_is_silent_noop(tmp_path: Path, monkeypatch) -> None:
    # No .strata/config.toml anywhere up the tree → degrade silently.
    monkeypatch.chdir(tmp_path)
    spawns = _Spawns()
    rc = freshness.run_stop_hook(
        _hook_stdin(),
        env={"STRATA_AGENT_SESSION_ID": _SESSION_ID, "ANTHROPIC_API_KEY": "x"},
        spawn_fn=spawns,
    )
    assert rc == 0
    assert spawns.calls == []


# ---------------------------------------------------------------------------
# Tests 8–9: the evaluator
# ---------------------------------------------------------------------------


def _fake_accept_judgment() -> MagicMock:
    judgment = MagicMock()
    judgment.decision = "accept_as_context"
    judgment.reasoning = "worth remembering"
    judgment.new_summary = ScopeSummary(
        scope_id="g_root",
        directives=[],
        context="updated",
        updated_at="2026-07-18T00:00:00+00:00",
        version=1,
        exists=True,
    )
    judgment.withdraw_published = []
    return judgment


def test_evaluator_draft_submitted_through_judged_path(tmp_path: Path, monkeypatch) -> None:
    paths = _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    from strata.settings import get_settings

    get_settings.cache_clear()
    store = _session_store(paths)
    _seed_reads(store, NUDGE_MIN_READS)

    draft = freshness.EvaluatorDraft(
        content="We chose sqlite for the record store.",
        classification="context",
        subject="storage",
    )
    draft_fn = MagicMock(return_value=draft)
    judge = MagicMock(return_value=_fake_accept_judgment())

    with (
        patch("strata.scope_manager.ScopeManager.judge", judge),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        outcome = freshness.run_evaluator(
            session_id=_SESSION_ID,
            transcript_path="/tmp/t.jsonl",
            env=_env(paths),
            draft_fn=draft_fn,
        )

    assert outcome == "contributed"
    # The judged contribute path was exercised — the scope-manager judged it.
    assert judge.call_count == 1
    # A contribution landed in the record under the agent's identity.
    from strata.record_store import RecordStore

    with RecordStore(paths["db"]) as rs:
        contributions = rs.list_contributions(scope_id="g_root")
    assert len(contributions) == 1
    assert contributions[0].content == draft.content
    assert contributions[0].contributor.session_id == _SESSION_ID
    # Counters reset: the accepted contribution released the asymmetry.
    state = store.read(_SESSION_ID)
    assert state.contributions == 1
    assert freshness.gate_open(state) is False
    # The lock is released after the run.
    assert not freshness.evaluator_lock_path(store, _SESSION_ID).exists()


def test_evaluator_nothing_records_decline_without_judge(tmp_path: Path, monkeypatch) -> None:
    paths = _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = _session_store(paths)
    _seed_reads(store, NUDGE_MIN_READS)

    draft_fn = MagicMock(return_value=None)  # nothing memory-worthy
    manager_cls = MagicMock()

    with patch("strata.scope_manager.ScopeManager", manager_cls):
        outcome = freshness.run_evaluator(
            session_id=_SESSION_ID,
            transcript_path="/tmp/t.jsonl",
            env=_env(paths),
            draft_fn=draft_fn,
        )

    assert outcome == "declined"
    # The judge was never even constructed — a decline is purely mechanical.
    manager_cls.assert_not_called()
    # No contribution entered the record.
    from strata.record_store import RecordStore

    with RecordStore(paths["db"]) as rs:
        assert rs.list_contributions(scope_id="g_root") == []
    # The mechanical decline reset the asymmetry.
    state = store.read(_SESSION_ID)
    assert state.declines == 1
    assert freshness.gate_open(state) is False


def test_evaluator_skips_when_gate_closed_midflight(tmp_path: Path, monkeypatch) -> None:
    paths = _make_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = _session_store(paths)
    _seed_reads(store, NUDGE_MIN_READS)
    store.record_contribution(_SESSION_ID)  # agent contributed after the spawn

    draft = freshness.EvaluatorDraft(content="x", classification="context")
    draft_fn = MagicMock(return_value=draft)
    outcome = freshness.run_evaluator(
        session_id=_SESSION_ID,
        transcript_path="/tmp/t.jsonl",
        env=_env(paths),
        draft_fn=draft_fn,
    )

    assert outcome == "skipped"
    draft_fn.assert_not_called()  # gate re-checked before any model work


def test_read_transcript_tail_flattens_jsonl(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "we picked sqlite"}],
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    tail = freshness.read_transcript_tail(str(transcript))
    assert "user: hi" in tail
    assert "assistant: we picked sqlite" in tail


def test_read_transcript_tail_missing_file_is_empty() -> None:
    assert freshness.read_transcript_tail("/no/such/transcript.jsonl") == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
