"""v1.16 P6 part 2 — the EXAMINED CONTEXT block (ADR 0017, issue #202/D6).

No judge INPUT change for a scope with nothing examined: the existing golden
corpus modules (`_judge_prompt_corpus.py`, `_v1160`, `_v1165_p5`) already prove
byte-identity for the with-no-examined-items case just by being re-run against
this tree unmodified — this file adds the one NEW shape, the with-block
render, plus the derivation/resolution logic in app.py and scope_manager.py.
"""

from __future__ import annotations

from pathlib import Path

from strata.app import _EXAMINED_CONTEXT_CAP, _resolve_examined_context
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import (
    _build_user_message,
    _render_examined_context,
)
from strata.summary_store import ScopeSummary
from tests._judge_prompt_corpus_p6_examined_context import (
    EXAMINED_ITEMS,
    capture_p6_examined_context_corpus,
)

_GOLDEN_DIR = Path(__file__).parent / "fixtures" / "judge_prompts_p6"


def _golden(name: str) -> str:
    return (_GOLDEN_DIR / f"{name}.txt").read_text(encoding="utf-8")


# --- rendering ------------------------------------------------------------------------


def test_no_examined_items_renders_nothing() -> None:
    assert _render_examined_context([]) == ""


def test_the_with_block_render_matches_the_captured_fixture() -> None:
    after = capture_p6_examined_context_corpus()["examined_context_block"]
    assert after == _golden("examined_context_block")


def test_the_block_names_each_kind_in_the_briefs_own_format() -> None:
    block = _render_examined_context(EXAMINED_ITEMS)
    assert block.startswith(
        "EXAMINED CONTEXT: items an outcome has tested. Under budget pressure, "
        "drop unexamined context before examined context:\n"
    )
    assert "- The service listens on port 8443. (corroborated: 3 held outcomes)" in block
    assert "- It corrects an earlier claim. (correcting: corrects c_p6_target)" in block
    assert "- It was raised. (raised: evidence from g_child)" in block
    assert "unexamined" in block.lower()
    assert "poorly standing" not in block.lower()


def test_omitted_from_a_message_with_no_examined_context_kwarg() -> None:
    from strata.fleet_config import EntitlementView, Scope, Stratum
    from strata.record_store import Contribution

    scope = Scope(id="g_x", name="x", stratum_id="L1")
    stratum = Stratum(id="L1", name="team", ordinal=1)
    contributor = ContributorRef(
        scope_id="g_x", skill="e", session_id="s1", ts="2026-09-26T00:00:00Z"
    )
    contribution = Contribution(
        id="c_x",
        scope_id="g_x",
        content="A plain observation.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=contributor,
        created_at="2026-09-26T00:00:00Z",
    )
    rendered_without_kwarg = _build_user_message(
        scope=scope,
        stratum=stratum,
        ancestor_directives=None,
        current_summary=None,
        recent_contributions=[],
        new_contribution=contribution,
        entitlement=EntitlementView(chain=[scope], descendants=[], referenced_peers=[], others=[]),
    )
    rendered_with_empty_list = _build_user_message(
        scope=scope,
        stratum=stratum,
        ancestor_directives=None,
        current_summary=None,
        recent_contributions=[],
        new_contribution=contribution,
        entitlement=EntitlementView(chain=[scope], descendants=[], referenced_peers=[], others=[]),
        examined_context=[],
    )
    assert rendered_without_kwarg == rendered_with_empty_list
    assert "EXAMINED CONTEXT" not in rendered_without_kwarg


# --- resolution (app.py) ---------------------------------------------------------------


def _seed(rs: RecordStore, scope_id: str, content: str, *, created_at: str) -> str:
    cid = rs.append_contribution(
        scope_id=scope_id,
        content=content,
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(scope_id=scope_id, skill="e", session_id="s1", ts=created_at),
    ).id
    rs.record_judgment(contribution_id=cid, decision="accept_as_context", judged_by="scope-manager")
    return cid


def test_resolve_examined_context_returns_empty_with_no_summary(tmp_path) -> None:
    from strata.fleet_config import FleetConfig
    from strata.migrator import run_migrations
    from strata.summary_store import SummaryStore

    db_path = str(tmp_path / "test.db")
    run_migrations(db_path)
    store = RecordStore(db_path)
    fleet = FleetConfig.model_validate(
        {
            "strata": [{"id": "L0", "name": "Executive", "ordinal": 0}],
            "scopes": [{"id": "g_x", "name": "X", "stratum_id": "L0"}],
            "edges": [],
        }
    )
    summary_store = SummaryStore(str(tmp_path / "summaries"))
    assert (
        _resolve_examined_context(
            scope_id="g_x",
            current_summary=None,
            fleet=fleet,
            record_store=store,
            summary_store=summary_store,
        )
        == []
    )


def test_resolve_examined_context_priority_corroborated_over_correcting_over_raised(
    tmp_path,
) -> None:
    from strata.fleet_config import FleetConfig
    from strata.migrator import run_migrations
    from strata.summary_store import SummaryStore

    db_path = str(tmp_path / "test.db")
    summaries_dir = str(tmp_path / "summaries")
    run_migrations(db_path)
    store = RecordStore(db_path)
    fleet = FleetConfig.model_validate(
        {
            "strata": [{"id": "L0", "name": "Executive", "ordinal": 0}],
            "scopes": [{"id": "g_x", "name": "X", "stratum_id": "L0"}],
            "edges": [],
        }
    )
    summary_store = SummaryStore(summaries_dir)

    corroborated_id = _seed(
        store, "g_x", "Corroborated item text.", created_at="2026-09-01T00:00:00Z"
    )
    held_outcome_id = store.append_contribution(
        scope_id="g_x",
        content="Confirmed by direct action.",
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_x", skill="e", session_id="s1", ts="2026-09-02T00:00:00Z"
        ),
        acted_on=corroborated_id,
    ).id
    store.record_judgment(
        contribution_id=held_outcome_id, decision="accept_as_context", judged_by="scope-manager"
    )

    raised_id = _seed(store, "g_x", "Raised item text.", created_at="2026-09-03T00:00:00Z")
    # Mark it raised without going through the full P5 mechanism — the FK just
    # needs a real contribution row to point at, its own content is irrelevant.
    fake_outcome_id = _seed(store, "g_x", "A fake outcome.", created_at="2026-09-02T12:00:00Z")
    store._conn.execute(
        "UPDATE contributions SET raised_from = ? WHERE id = ?", (fake_outcome_id, raised_id)
    )
    store._conn.commit()

    current_summary = ScopeSummary(
        scope_id="g_x",
        directives=[],
        context="Corroborated item text. Raised item text.",
        updated_at="2026-09-04T00:00:00+00:00",
    )
    items = _resolve_examined_context(
        scope_id="g_x",
        current_summary=current_summary,
        fleet=fleet,
        record_store=store,
        summary_store=summary_store,
    )
    by_id = {i.contribution_id: i for i in items}
    assert by_id[corroborated_id].kind == "corroborated"
    assert by_id[corroborated_id].detail == "1 held outcome"
    assert by_id[raised_id].kind == "raised"
    assert by_id[raised_id].detail == "evidence from g_x"


def test_resolve_examined_context_caps_and_orders_newest_first(tmp_path) -> None:
    from strata.fleet_config import FleetConfig
    from strata.migrator import run_migrations
    from strata.summary_store import SummaryStore

    db_path = str(tmp_path / "test.db")
    summaries_dir = str(tmp_path / "summaries")
    run_migrations(db_path)
    store = RecordStore(db_path)
    fleet = FleetConfig.model_validate(
        {
            "strata": [{"id": "L0", "name": "Executive", "ordinal": 0}],
            "scopes": [{"id": "g_x", "name": "X", "stratum_id": "L0"}],
            "edges": [],
        }
    )
    summary_store = SummaryStore(summaries_dir)

    fake_outcome_id = _seed(store, "g_x", "A fake outcome.", created_at="2026-08-31T00:00:00Z")
    ids = []
    for i in range(12):
        cid = _seed(store, "g_x", f"Item number {i}.", created_at=f"2026-09-{i + 1:02d}T00:00:00Z")
        # `append_contribution` stamps its own `created_at` via SQLite
        # `datetime('now')` — force distinct, ordered values here so the
        # newest-first contract is actually exercised, not tie-broken.
        store._conn.execute(
            "UPDATE contributions SET raised_from = ?, created_at = ? WHERE id = ?",
            (fake_outcome_id, f"2026-09-{i + 1:02d}T00:00:00", cid),
        )
        store._conn.commit()
        ids.append(cid)

    context = " ".join(f"Item number {i}." for i in range(12))
    current_summary = ScopeSummary(
        scope_id="g_x", directives=[], context=context, updated_at="2026-09-13T00:00:00+00:00"
    )
    items = _resolve_examined_context(
        scope_id="g_x",
        current_summary=current_summary,
        fleet=fleet,
        record_store=store,
        summary_store=summary_store,
    )
    assert len(items) == _EXAMINED_CONTEXT_CAP
    # Newest first: item 11 (created last) through item 2.
    assert [i.contribution_id for i in items] == list(reversed(ids))[:_EXAMINED_CONTEXT_CAP]
