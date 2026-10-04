"""#236 — the batch-path twin of #235: a second protocol slip on a BATCH
retry must fail closed too — every member declined together, with the fixed
judge-failure reasoning and `judge_failure=True` — never an unhandled
exception reaching `run_contribution`'s caller.

Reproduced through the REAL drain/batch path: real concurrent
`run_contribution` calls force a genuine multi-member batch (the exact
concurrency harness `test_v16_229_batch_acted_on.py` already established),
with a REAL `ScopeManager` — only the underlying Anthropic client is mocked,
replaying a malformed `submit_batch_judgment` payload on both the batch's
first attempt and its one corrective retry.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from strata.migrator import run_migrations
from strata.record_store import RecordStore
from strata.scope_manager import ScopeManager
from strata.summary_store import SummaryStore

from .test_v16_229_batch_acted_on import _CHILD, _contributor, _fleet, _spawn, _wait_for_pending


def _tool_use_response(**payload) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.id = "toolu_fake"
    block.input = payload
    resp = MagicMock()
    resp.content = [block]
    return resp


def _malformed_batch_response() -> MagicMock:
    """A `submit_batch_judgment` payload whose `verdicts` is neither a list
    nor a string — a structural failure `_parse_batch_verdicts` rejects
    regardless of which real contribution ids are in this batch, so the
    test never needs to know them in advance."""
    return _tool_use_response(verdicts=None, directive_ops=[], new_context=None)


def test_a_double_slip_on_a_real_batch_retry_declines_every_member(tmp_path: Path) -> None:
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    fleet = _fleet(tmp_path)
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    scope = fleet.get_scope(_CHILD)
    stratum = next(s for s in fleet.strata if s.id == "L1")

    mock_client = MagicMock()
    gate = threading.Event()
    call_log: list[int] = []

    def create_side_effect(**kwargs):  # noqa: ANN003
        call_log.append(1)
        if len(call_log) == 1:
            # The "in-flight" contribution's own single-path judgment —
            # blocks so the next two contributions queue behind it, forcing
            # a real multi-member batch.
            assert gate.wait(timeout=10.0), "test never released the gate"
            return _tool_use_response(
                decision="accept_as_context",
                reasoning="in flight, ok",
                directive_ops=[],
                new_context="in flight content",
            )
        # Calls 2 and 3: the real batch's own first attempt AND its one
        # corrective retry — BOTH malformed, so the terminal fail-closed
        # path is exercised deterministically.
        return _malformed_batch_response()

    mock_client.messages.create.side_effect = create_side_effect
    manager = ScopeManager(client=mock_client)

    in_flight_thread, in_flight_errors = _spawn(
        content="in flight",
        scope=scope,
        stratum=stratum,
        fleet=fleet,
        db_path=db_path,
        summary_store=summary_store,
        manager=manager,
        contributor=_contributor(_CHILD),
    )
    while not call_log:
        time.sleep(0.005)

    queued: list[threading.Thread] = []
    queued_errors: list[list] = []
    for n, content in enumerate(["queued one", "queued two"], start=1):
        thread, errors = _spawn(
            content=content,
            scope=scope,
            stratum=stratum,
            fleet=fleet,
            db_path=db_path,
            summary_store=summary_store,
            manager=manager,
            contributor=_contributor(_CHILD),
        )
        queued.append(thread)
        queued_errors.append(errors)
        _wait_for_pending(_CHILD, n)

    gate.set()
    for thread in [in_flight_thread, *queued]:
        thread.join(timeout=15.0)
    assert in_flight_errors == []
    assert [e for errors in queued_errors for e in errors] == []

    assert len(call_log) == 3  # in-flight, batch first attempt, batch retry — never a third

    with RecordStore(db_path) as rs:
        by_content = {c.content: c for c in rs.list_contributions(scope_id=_CHILD)}
        judgment_one = rs.get_judgment(by_content["queued one"].id)
        judgment_two = rs.get_judgment(by_content["queued two"].id)
        judgment_in_flight = rs.get_judgment(by_content["in flight"].id)

    assert judgment_in_flight is not None
    assert judgment_in_flight.decision == "accept_as_context"  # unaffected by the batch's own slip

    assert judgment_one is not None
    assert judgment_two is not None
    assert judgment_one.decision == "decline"
    assert judgment_two.decision == "decline"
    assert "judge failure: the response was still malformed" in (judgment_one.notes or "")
    assert "judge failure: the response was still malformed" in (judgment_two.notes or "")
