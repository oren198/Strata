"""The drain splices the parent's directives before it judges (ADR 0011 D4).

Inherited directives reach a child's summary MECHANICALLY — byte-exact, ids
and provenance preserved — because a judge asked to quote them paraphrases
them sooner or later. That splice used to live only in ``strata launch``'s
refresh, which an MCP-only user never runs (ADR 0014 D6's whole point), so
an agent working entirely through the MCP surface never inherited a
directive at all.

It lives in :func:`strata.app.drain_scope` now — the ONE refresh mechanism
(ADR 0014 implementation pin 6), which both the launch path and the read
path go through. Unconditional and idempotent
(:func:`~strata.summary_store.splice_parent_directives` returns the same
object when there is nothing to splice), so nothing here is a staleness
test.

Root-first is NOT required: the parent's own drain happens on the parent's
own read, which is ADR 0014's documented known gap, not something this
splice works around.

Vocabulary follows CONTEXT.md: scope, directive, scope summary, record,
change event, refresh.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from strata.app import drain_scope  # noqa: E402
from strata.fleet_config import FleetConfig  # noqa: E402
from strata.migrator import run_migrations  # noqa: E402
from strata.record_store import ContributorRef, RecordStore  # noqa: E402
from strata.scope_manager import ScopeManagerJudgment  # noqa: E402
from strata.summary_store import Directive, ScopeSummary, SummaryStore  # noqa: E402

_FLEET_YAML = """
strata:
  - id: L0
    name: executive
    ordinal: 0
  - id: L1
    name: team
    ordinal: 1
scopes:
  - id: g_root
    name: Root
    stratum_id: L0
  - id: g_team
    name: Team
    stratum_id: L1
edges:
  - from: g_team
    to: g_root
"""

PARENT_DIRECTIVE = Directive(
    id="c_parent_rule",
    content="All services must ship behind a flag.\nNo exceptions.",
    subject="rollout",
    source_scope_id="g_root",
    source_skill="scope-manager",
    created_at="2026-09-05T09:00:00+00:00",
)


def _fleet(tmp_path: Path) -> FleetConfig:
    path = tmp_path / "fleet.yaml"
    path.write_text(textwrap.dedent(_FLEET_YAML), encoding="utf-8")
    return FleetConfig.load(path)


def _seed(tmp_path: Path, *, child_summary: ScopeSummary | None) -> tuple[str, SummaryStore]:
    """A parent holding one directive, and whatever summary the child has."""
    db_path = str(tmp_path / "test.db")
    run_migrations(db_path)
    store = SummaryStore(str(tmp_path / "summaries"))
    store.write(
        "g_root",
        ScopeSummary(
            scope_id="g_root",
            directives=[PARENT_DIRECTIVE],
            context="Root context.",
            updated_at="2026-09-05T10:00:00+00:00",
        ),
    )
    if child_summary is not None:
        store.write("g_team", child_summary)
    return db_path, store


def _stale_child() -> ScopeSummary:
    return ScopeSummary(
        scope_id="g_team",
        directives=[
            Directive(
                id="c_local",
                content="Local rule.",
                subject="local",
                source_scope_id="g_team",
                source_skill="strata-developer",
                created_at="2026-09-05T09:30:00+00:00",
            )
        ],
        context="What the team believed before.",
        updated_at="2026-09-05T10:00:00+00:00",
    )


def _notice(record_store: RecordStore, *, change_id: str = "chg_dir") -> None:
    """What an emitter writes when the parent's directive set moved."""
    record_store.append_change_notice(
        scope_id="g_team",
        content=f"[Input change {change_id}: c_parent_rule was appended by g_root.]",
        contributor=ContributorRef(
            scope_id="g_team",
            skill="scope-manager",
            session_id="change-event",
            ts="2026-09-05T10:30:00+00:00",
        ),
        change_id=change_id,
        source_scope_id="g_root",
        item_id="c_parent_rule",
        kind="directive_appended",
        before=None,
        after="c_parent_rule",
    )


def _echoing_judge() -> MagicMock:
    """A judge that rewrites the context and leaves the directives alone.

    Stands in for the engine's mechanical apply: whatever directives the
    judge was HANDED come back, so anything in the written summary that the
    judge was not handed did not come from the judge.
    """

    def fake(**kwargs: Any) -> ScopeManagerJudgment:
        received = kwargs["current_summary"]
        return ScopeManagerJudgment(
            decision="accept_as_context",
            reasoning="Reconciled the digest with the refreshed inputs.",
            new_summary=received.model_copy(update={"context": "Reconciled context."}),
            new_context="Reconciled context.",
        )

    manager = MagicMock()
    manager.judge.side_effect = fake
    manager.judge_batch.side_effect = AssertionError("one notice judges on the single path")
    return manager


def test_a_drained_directive_change_leaves_the_parent_row_byte_for_byte(
    tmp_path: Path,
) -> None:
    """The MCP-only path inherits, and inherits VERBATIM (ADR 0011 D4).

    The judge is never asked to copy the directive across: it is already in
    the summary the judge is handed, ids and provenance and bytes intact.
    """
    fleet = _fleet(tmp_path)
    db_path, store = _seed(tmp_path, child_summary=_stale_child())

    with RecordStore(db_path) as record_store:
        _notice(record_store)
        manager = _echoing_judge()
        outcome = drain_scope(
            "g_team",
            fleet=fleet,
            record_store=record_store,
            summary_store=store,
            scope_manager=manager,
            summary_max_words=500,
        )

        # Vacuous-pass guard (implementation pin 10): the refresh really ran
        # and left its verdict in the record, so "the row survived" is not
        # just "nothing happened".
        assert outcome.events_processed == 1
        assert len(record_store.list_judgments(scope_id="g_team")) == 1

    written = store.read("g_team")
    assert written is not None
    spliced = next(d for d in written.directives if d.id == "c_parent_rule")
    assert spliced == PARENT_DIRECTIVE
    # The child's own directive is untouched, and the judge did see the
    # already-spliced summary rather than being asked to quote it.
    assert any(d.id == "c_local" for d in written.directives)
    judge_kwargs = manager.judge.call_args_list[0].kwargs
    assert any(d.id == "c_parent_rule" for d in judge_kwargs["current_summary"].directives)


def test_a_first_ever_bind_splices_even_with_nothing_pending(tmp_path: Path) -> None:
    """A scope with no summary and no pending events is a drain that still splices.

    A child registered after its parent's directive was already standing
    never receives a change event for it — nothing changed after the child
    existed. The splice is what makes it inherit anyway, so it runs whether
    or not there is a queue (ADR 0011 D4).
    """
    fleet = _fleet(tmp_path)
    db_path, store = _seed(tmp_path, child_summary=None)

    with RecordStore(db_path) as record_store:
        manager = _echoing_judge()
        outcome = drain_scope(
            "g_team",
            fleet=fleet,
            record_store=record_store,
            summary_store=store,
            scope_manager=manager,
            summary_max_words=500,
        )

        assert outcome.events_processed == 0
        assert outcome.spliced is True
        # The splice's own reconciliation is context-only: the directives are
        # already in place mechanically, so an admitting op would have
        # nothing honest to mint from (ADR 0011 D4, pin 6's modes).
        assert manager.judge.call_args_list[0].kwargs["mode"] == "splice_refresh"
        # Vacuous-pass guard: a judgment row against the refresh notice.
        assert len(record_store.list_judgments(scope_id="g_team")) == 1

    written = store.read("g_team")
    assert written is not None
    assert written.directives == [PARENT_DIRECTIVE]


def test_a_drain_with_nothing_to_splice_and_nothing_pending_judges_nothing(
    tmp_path: Path,
) -> None:
    """Idempotent: the splice is unconditional, the judge call is not.

    ``splice_parent_directives`` returns the summary unchanged when there is
    nothing to splice, so a second drain over settled state costs no judge
    call — fixpoint damping (pin 7), not a staleness detector.
    """
    fleet = _fleet(tmp_path)
    db_path, store = _seed(tmp_path, child_summary=None)

    with RecordStore(db_path) as record_store:
        first = _echoing_judge()
        drain_scope(
            "g_team",
            fleet=fleet,
            record_store=record_store,
            summary_store=store,
            scope_manager=first,
            summary_max_words=500,
        )
        # Precondition: the first drain DID splice and judge.
        assert first.judge.call_count == 1
        settled = store.read("g_team")

        second = _echoing_judge()
        outcome = drain_scope(
            "g_team",
            fleet=fleet,
            record_store=record_store,
            summary_store=store,
            scope_manager=second,
            summary_max_words=500,
        )

    assert outcome.spliced is False
    assert outcome.judged is False
    assert second.judge.call_count == 0
    assert store.read("g_team") == settled


# ---------------------------------------------------------------------------
# One mechanism (ADR 0014 implementation pin 6)
# ---------------------------------------------------------------------------


async def test_the_launch_path_and_the_read_path_produce_the_same_summary(
    tmp_path: Path,
) -> None:
    """``strata launch``'s refresh is a thin call to the drain the MCP read makes.

    Two mechanisms for one question is what pin 6 removes, and the only way
    to hold them to it is to run the same state through both surfaces and
    compare what lands on disk.
    """
    from test_mcp_server import _load_mcp_module  # noqa: PLC0415

    from strata.__main__ import _refresh_scope  # noqa: PLC0415

    # --- the launch path -----------------------------------------------
    launch_root = tmp_path / "launch"
    launch_root.mkdir()
    launch_fleet = _fleet(launch_root)
    launch_db, launch_store = _seed(launch_root, child_summary=_stale_child())
    with RecordStore(launch_db) as record_store:
        _notice(record_store)
        _refresh_scope(
            "g_team",
            fleet_config=launch_fleet,
            record_store=record_store,
            summary_store=launch_store,
            manager=_echoing_judge(),
            summary_max_words=500,
        )

    # --- the MCP read path ---------------------------------------------
    read_root = tmp_path / "read"
    read_root.mkdir()
    read_fleet = _fleet(read_root)
    read_db, read_store = _seed(read_root, child_summary=_stale_child())
    with RecordStore(read_db) as record_store:
        _notice(record_store)

    mod = _load_mcp_module(read_db, str(read_store.summaries_dir), str(read_root / "fleet.yaml"))
    mod._settings = mod._settings.model_copy(update={"judge_api_key": "test-key"})
    judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="Reconciled the digest with the refreshed inputs.",
        new_summary=None,
        new_context="Reconciled context.",
    )

    def _judge(**kwargs: Any) -> ScopeManagerJudgment:
        received = kwargs["current_summary"]
        return judgment.model_copy(
            update={"new_summary": received.model_copy(update={"context": "Reconciled context."})}
        )

    with (
        patch.object(mod, "_AGENT_SCOPE", "g_team"),
        patch.object(mod, "_AGENT_SKILL", None),
        patch.object(mod, "_AGENT_SESSION_ID", "sess_test"),
        patch.object(mod, "_load_fleet", return_value=read_fleet),
        patch("strata.scope_manager.ScopeManager.judge", side_effect=_judge),
        patch("anthropic.Anthropic", return_value=MagicMock()),
    ):
        result = await mod.strata_read_perspective()

    assert result.get("refresh_pending", 0) == 0

    from_launch = launch_store.read("g_team")
    from_read = read_store.read("g_team")
    assert from_launch is not None
    assert from_read is not None
    # Same directives (the parent's row included, byte for byte) and the
    # same context: one mechanism, two entry points.
    assert from_launch.directives == from_read.directives
    assert PARENT_DIRECTIVE in from_read.directives
    assert from_launch.context == from_read.context
