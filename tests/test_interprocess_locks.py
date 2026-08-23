"""Cross-process per-scope locking (issue #19, ADR 0012).

``test_contribute_choke_point.py`` already proves the choke point's
``threading`` locks serialise two contributions racing inside ONE process
(issue #38). ``strata-mcp`` runs one process per Claude Code session, so in
embedded mode two agents contributing to the same scope are two OS
processes, and a ``threading.Lock`` cannot see across a fork boundary at
all — each process gets its own uncontended lock and both judge against the
same stale summary. This test proves that race with real OS processes, then
(once ``strata.locks`` grows a cross-process flock, Task 1.2) proves it is
fixed.

Two OS processes each contribute N_PER_PROCESS times to the SAME scope,
using a stub scope-manager that appends one marker directive per accepted
contribution with a deliberate delay between reading the current summary and
writing the rewrite — the same race-widening shape as
``_AccumulatingManager`` in ``test_contribute_choke_point.py``, just run
from two processes instead of two threads. Without a cross-process lock, two
judgments launched in different processes can both read the summary before
either writes it back, so the second write clobbers the first accepted
directive — a lost update: fewer markers in the final summary than accepted
judgments in the record.

N_PER_PROCESS=20 (40 contributions total) was picked because it fails
reliably (lost updates observed on every run during development) without
making the test slow.

Vocabulary follows CONTEXT.md: scope, contribution, record, scope summary.
"""

from __future__ import annotations

import multiprocessing
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.fleet_config import FleetConfig  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.record_store import RecordStore  # noqa: E402
from strata.scope_manager import ScopeManagerJudgment  # noqa: E402
from strata.summary_store import Directive, ScopeSummary, SummaryStore  # noqa: E402

N_PER_PROCESS = 20
SCOPE_ID = "g_root"


def _fleet(root: Path) -> FleetConfig:
    """A single-scope fleet (``g_root``, L0) written under *root*."""
    fleet = {
        "strata": [{"id": "L0", "name": "executive", "ordinal": 0}],
        "scopes": [{"id": SCOPE_ID, "name": "Root", "stratum_id": "L0"}],
        "edges": [],
    }
    root.mkdir(parents=True, exist_ok=True)
    path = root / "fleet.yaml"
    path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return FleetConfig.load(path)


class _MarkerManager:
    """Scope-manager stub that accepts every contribution as a directive.

    Appends exactly one marker directive per accepted contribution, built
    from ``current_summary`` — a faithful read-modify-write, exactly like
    ``_AccumulatingManager`` in ``test_contribute_choke_point.py``. The sleep
    between reading and returning the rewrite widens the race window: without
    a lock spanning both processes, a second concurrent judge (in the OTHER
    process) reads the same stale summary and its write clobbers this one's.
    """

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay

    def judge(
        self,
        *,
        scope,
        stratum,
        parent_summary,
        current_summary,
        recent_contributions,
        new_contribution,
        summary_max_words,
        entitlement,
        operator_memory=None,
        current_publication=None,
        peer_publications=None,
        window_verbatim_tail=None,
    ):  # noqa: ANN001, ANN201, E501
        existing = list(current_summary.directives) if current_summary is not None else []
        time.sleep(self.delay)
        new_directive = Directive(
            id=new_contribution.id,
            content=new_contribution.content,
            subject=new_contribution.subject,
            source_scope_id=scope.id,
            source_skill="strata-developer",
            created_at="2026-08-23T00:00:00+00:00",
        )
        summary = ScopeSummary(
            scope_id=scope.id,
            directives=[*existing, new_directive],
            context="",
            updated_at="2026-08-23T00:00:00+00:00",
        )
        return ScopeManagerJudgment(
            decision="accept_as_directive",
            reasoning="accepted",
            new_summary=summary,
        )


def _hammer(
    db_path: str, fleet_path: str, worker_id: int, n: int, out_q: multiprocessing.Queue
) -> None:
    """Run in a fresh OS process: contribute *n* times to the shared scope.

    Everything is re-opened from scratch here (fresh imports, fresh store
    connections) — exactly what happens when two ``strata-mcp`` processes
    each serve one Claude Code session against the same project.
    """
    import sys as _sys  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "src"))

    from strata.app import run_contribution  # noqa: PLC0415
    from strata.fleet_config import FleetConfig as _FleetConfig  # noqa: PLC0415
    from strata.locks import configure_lock_dir  # noqa: PLC0415
    from strata.record_store import ContributorRef as _ContributorRef  # noqa: PLC0415
    from strata.record_store import RecordStore as _RecordStore  # noqa: PLC0415
    from strata.summary_store import SummaryStore as _SummaryStore  # noqa: PLC0415

    try:
        # Mirrors the real wiring (strata.mcp.server._init_stores /
        # strata.app.create_app's lifespan): every process that touches this
        # DB configures the same ``<db_dir>/.locks`` directory before doing
        # any contribute work, so the flock actually spans the two processes.
        configure_lock_dir(_Path(db_path).parent / ".locks")
        fleet = _FleetConfig.load(_Path(fleet_path))
        summary_store = _SummaryStore(str(_Path(db_path).parent / "summaries"))
        scope = fleet.get_scope(SCOPE_ID)
        stratum = fleet.strata[0]
        manager = _MarkerManager()
        contributor = _ContributorRef(
            scope_id=SCOPE_ID,
            skill="strata-developer",
            session_id=f"sess_{worker_id}",
            ts="2026-08-23T00:00:00+00:00",
        )
        with _RecordStore(db_path) as rs:
            for i in range(n):
                run_contribution(
                    scope=scope,
                    stratum=stratum,
                    content=f"marker {worker_id}-{i}",
                    proposed_classification="directive",
                    subject=f"subject-{worker_id}-{i}",
                    supersedes=None,
                    contributor=contributor,
                    fleet=fleet,
                    record_store=rs,
                    summary_store=summary_store,
                    scope_manager=manager,
                    summary_max_words=5000,
                )
        out_q.put(("ok", worker_id))
    except Exception as exc:  # noqa: BLE001 — surfaced to the parent, not swallowed
        out_q.put(("error", worker_id, repr(exc)))


def test_two_processes_one_scope_no_lost_updates(tmp_path: Path) -> None:
    """Two OS processes contribute to one scope; every accepted contribution
    must be reflected exactly once in the final summary (markers == accepted
    judgments in the record) — the summary stays explainable by the record
    across process boundaries, not just across threads.
    """
    db_path = str(tmp_path / "strata.db")
    fleet_path = str(tmp_path / "fleet.yaml")
    run_migrations(db_path)
    _fleet(tmp_path)
    # Ensure the summaries dir exists before the children race to create it.
    SummaryStore(str(tmp_path / "summaries"))

    ctx = multiprocessing.get_context("spawn")
    out_q = ctx.Queue()
    procs = [
        ctx.Process(target=_hammer, args=(db_path, fleet_path, worker_id, N_PER_PROCESS, out_q))
        for worker_id in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=180)

    for p in procs:
        assert p.exitcode == 0, f"child process {p.pid} exited with {p.exitcode}"

    results = [out_q.get(timeout=5) for _ in procs]
    for result in results:
        assert result[0] == "ok", f"child process failed: {result}"

    with RecordStore(db_path) as rs:
        judgments = rs.list_judgments(scope_id=SCOPE_ID)
    accepted = [j for j in judgments if j.decision == "accept_as_directive"]

    final = SummaryStore(str(tmp_path / "summaries")).read(SCOPE_ID)
    assert final is not None

    assert len(accepted) == 2 * N_PER_PROCESS, (
        f"expected {2 * N_PER_PROCESS} accepted judgments in the record, got {len(accepted)}"
    )
    assert len(final.directives) == len(accepted), (
        "lost update: the summary has fewer marker directives than accepted "
        f"judgments in the record ({len(final.directives)} != {len(accepted)}) — "
        "two processes judged against the same stale summary"
    )
