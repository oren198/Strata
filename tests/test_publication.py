"""Tests for src/strata/publication.py — the publication channel (ADR 0007, issue #90).

Covers:

1. The publication artifact: render/parse round-trip (byte-identical),
   atomic write, honestly-empty read for a scope that has published nothing.
2. Judged publish/withdraw acts (propose_publish / propose_withdraw): accept
   records the act + judgment + rewrites the artifact; decline records the
   act + judgment but leaves the artifact untouched; structural anchor
   errors (zero anchors; an explicit ``directive:`` anchor naming an id not
   in the current summary) raise BEFORE any act row is appended.
3. Mechanical propagation (propagate_directive_removals): a directive-only-
   anchored item is withdrawn (with ``trigger`` set, no judgment row) when
   its directive vanishes; a subject-anchored item survives.
4. Judged propagation (apply_judged_withdrawals): withdrawal acts carry a
   judgment row using the SAME judged_by/reasoning as the triggering
   contribution judgment; unknown ids are ignored, not errors.
5. Bootstrap (bootstrap_publication): accepted candidates become ordinary
   accepted publish acts; a decline (or an empty item list) records nothing.

Vocabulary follows CONTEXT.md verbatim: publication, withdrawal, scope,
scope summary, directive, context, record, provenance, supersession,
retirement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.publication import (
    BootstrapOutcome,
    PublishedItem,
    _parse_publication,
    _render_publication,
    _write_publication,
    apply_judged_withdrawals,
    bootstrap_publication,
    list_scopes_with_publications,
    propagate_directive_removals,
    propose_publish,
    propose_withdraw,
    read_publication,
    read_publication_text,
)
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import (
    BootstrapJudgment,
    BootstrapPublishedItemInput,
    PublicationJudgment,
)
from strata.summary_store import Directive, ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Fixture fleet — g_exec (L0) <- g_func (L1) <- g_team (L2)
# ---------------------------------------------------------------------------


def _make_fleet(tmp_path: Path) -> FleetConfig:
    import yaml

    fleet = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "function", "ordinal": 1},
            {"id": "L2", "name": "team", "ordinal": 2},
        ],
        "scopes": [
            {"id": "g_exec", "name": "Executive", "stratum_id": "L0"},
            {"id": "g_func", "name": "Function", "stratum_id": "L1"},
            {"id": "g_team", "name": "Team", "stratum_id": "L2"},
        ],
        "edges": [
            {"from": "g_func", "to": "g_exec"},
            {"from": "g_team", "to": "g_func"},
        ],
    }
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(yaml.dump(fleet, default_flow_style=False), encoding="utf-8")
    return FleetConfig.load(fleet_path)


@pytest.fixture()
def record_store(tmp_path: Path):
    db_path = str(tmp_path / "strata.db")
    run_migrations(db_path)
    store = RecordStore(db_path)
    yield store
    store.close()


@pytest.fixture()
def summaries_dir(tmp_path: Path) -> str:
    return str(tmp_path / "summaries")


@pytest.fixture()
def summary_store(summaries_dir: str) -> SummaryStore:
    return SummaryStore(summaries_dir)


@pytest.fixture()
def fleet(tmp_path: Path) -> FleetConfig:
    return _make_fleet(tmp_path)


def _proposer(scope_id: str = "g_team") -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id,
        skill="strata-developer",
        session_id="sess_test",
        ts="2026-07-12T00:00:00+00:00",
    )


def _seed_summary_with_directive(
    summary_store: SummaryStore, scope_id: str, directive_id: str = "c_dir1"
) -> ScopeSummary:
    summary = ScopeSummary(
        scope_id=scope_id,
        directives=[
            Directive(
                id=directive_id,
                content="Use protobuf for all RPC.",
                subject="rpc",
                source_scope_id=scope_id,
                source_skill="strata-developer",
                created_at="2026-07-12T00:00:00+00:00",
            )
        ],
        context="Deploys happen at 3pm UTC.",
        updated_at="2026-07-12T00:00:00+00:00",
    )
    return summary_store.write(scope_id, summary)


def _seed_published_item(
    record_store: RecordStore,
    summaries_dir: str,
    scope_id: str,
    *,
    kind: str = "directive",
    content: str = "content",
    subject: str | None = None,
    anchors: list[str] | None = None,
) -> PublishedItem:
    """Append a real ``publish`` act (so a later ``withdraws`` FK reference resolves) and write it.

    Mirrors what :func:`strata.publication.propose_publish` does, minus the
    judging — used by propagation tests that need a published item whose id
    genuinely exists in ``publication_acts`` (the ``withdraws`` column is a
    foreign key).
    """
    act = record_store.append_publication_act(
        scope_id=scope_id,
        act="publish",
        kind=kind,
        content=content,
        subject=subject,
        anchors=anchors or [],
        withdraws=None,
        trigger=None,
        proposer=_proposer(scope_id),
    )
    record_store.record_publication_judgment(
        act_id=act.id, decision="accept", judged_by="scope-manager", reasoning="seeded for test"
    )
    item = PublishedItem(
        id=act.id,
        kind=kind,
        content=content,
        subject=subject,
        anchors=anchors or [],
        published_at=act.created_at,
    )
    existing = read_publication(scope_id, summaries_dir=summaries_dir)
    _write_publication(scope_id, [*existing, item], summaries_dir=summaries_dir)
    return item


class _FakeScopeManager:
    """A scope-manager fake for judge_publication / judge_bootstrap_publication."""

    def __init__(
        self,
        publication_judgment: PublicationJudgment | None = None,
        bootstrap_judgment: BootstrapJudgment | None = None,
    ) -> None:
        self._publication_judgment = publication_judgment
        self._bootstrap_judgment = bootstrap_judgment
        self.publication_calls: list[dict] = []
        self.bootstrap_calls: list[dict] = []

    def judge_publication(self, **kwargs) -> PublicationJudgment:
        self.publication_calls.append(kwargs)
        return self._publication_judgment

    def judge_bootstrap_publication(self, **kwargs) -> BootstrapJudgment:
        self.bootstrap_calls.append(kwargs)
        return self._bootstrap_judgment


# ---------------------------------------------------------------------------
# 1. The publication artifact — render/parse round-trip, atomic write,
#    honestly-empty read.
# ---------------------------------------------------------------------------


def test_artifact_round_trip_byte_identical_multiline_content(summaries_dir: str) -> None:
    """Multi-line, markdown-ish content survives write -> read -> re-render byte-for-byte."""
    items = [
        PublishedItem(
            id="pub_aaa111",
            kind="directive",
            content="Use protobuf for all RPC.\n\n- No exceptions.\n- See ADR-12.",
            subject="rpc-protocol",
            anchors=["directive:c_dir1"],
            published_at="2026-07-12T10:00:00+00:00",
        ),
        PublishedItem(
            id="pub_bbb222",
            kind="context",
            content="Deploys happen at 3pm UTC.\n> nested quote\n## fake heading",
            subject=None,
            anchors=["subject:deploy-notes"],
            published_at="2026-07-12T10:05:00+00:00",
        ),
    ]
    _write_publication("g_team", items, summaries_dir=summaries_dir)

    read_back = read_publication("g_team", summaries_dir=summaries_dir)
    assert read_back == items

    # Re-rendering the parsed items reproduces the exact same file content.
    original_text = Path(summaries_dir, "g_team.pub.md").read_text(encoding="utf-8")
    assert _render_publication("g_team", read_back) == original_text
    assert _parse_publication(_render_publication("g_team", items)) == items


def test_artifact_round_trip_preserves_relay_fields(summaries_dir: str) -> None:
    """A relayed item's origin/relay pointer survives write -> read -> re-render (ADR 0013 D4)."""
    items = [
        PublishedItem(
            id="pub_relay1",
            kind="context",
            content="Deploys happen at 3pm UTC.",
            subject="deploy-notes",
            anchors=["subject:deploy-notes"],
            published_at="2026-08-31T10:00:00+00:00",
            origin_scope_id="g_exec",
            relay_scope_id="g_func",
            relay_item_id="pub_orig1",
        ),
    ]
    _write_publication("g_team", items, summaries_dir=summaries_dir)

    read_back = read_publication("g_team", summaries_dir=summaries_dir)
    assert read_back == items
    assert read_back[0].origin_scope_id == "g_exec"
    assert read_back[0].relay_scope_id == "g_func"
    assert read_back[0].relay_item_id == "pub_orig1"

    original_text = Path(summaries_dir, "g_team.pub.md").read_text(encoding="utf-8")
    assert _render_publication("g_team", read_back) == original_text


def test_artifact_render_omits_relay_lines_for_non_relay_item(summaries_dir: str) -> None:
    """A non-relay item's rendered section carries no origin/relay lines at all.

    Load-bearing for ADR 0013 D7 (no migration, no back-filling): an
    old-format artifact predating relay fields must re-render byte-identical
    once parsed, which only holds if an absent origin/relay emits nothing.
    """
    item = PublishedItem(
        id="pub_plain1",
        kind="directive",
        content="Use protobuf for all RPC.",
        subject="rpc-protocol",
        anchors=["directive:c_dir1"],
        published_at="2026-07-12T10:00:00+00:00",
    )
    rendered = _render_publication("g_team", [item])
    assert "origin" not in rendered
    assert "relay" not in rendered

    parsed = _parse_publication(rendered)
    assert parsed == [item]
    assert parsed[0].origin_scope_id is None
    assert parsed[0].relay_scope_id is None
    assert parsed[0].relay_item_id is None


def test_old_format_artifact_without_relay_fields_parses_and_rerenders_byte_identical(
    summaries_dir: str,
) -> None:
    """A pre-ADR-0013 artifact on disk (no origin/relay lines) is untouched by the new parser.

    Simulates a store written before this release: existing items keep
    exactly what they have, nothing is back-filled (ADR 0013 D7).
    """
    old_text = (
        "---\n"
        "scope_id: g_team\n"
        "---\n"
        "\n"
        "# Publication: g_team\n"
        "\n"
        "## [pub_old1] directive\n"
        "- subject: rpc-protocol\n"
        '- anchors: ["directive:c_dir1"]\n'
        "- published_at: 2026-07-01T00:00:00+00:00\n"
        "\n"
        "> Use protobuf for all RPC.\n"
    )
    parsed = _parse_publication(old_text)
    assert len(parsed) == 1
    assert parsed[0].origin_scope_id is None
    assert parsed[0].relay_scope_id is None
    assert parsed[0].relay_item_id is None
    assert _render_publication("g_team", parsed) == old_text


def test_read_publication_empty_for_scope_with_no_artifact(summaries_dir: str) -> None:
    """A scope that has published nothing yet returns an empty list — the honestly empty face."""
    assert read_publication("g_never_published", summaries_dir=summaries_dir) == []


def test_read_publication_text_none_for_missing_artifact(summaries_dir: str) -> None:
    assert read_publication_text("g_never_published", summaries_dir=summaries_dir) is None


def test_list_scopes_with_publications(summaries_dir: str) -> None:
    _write_publication("g_team", [], summaries_dir=summaries_dir)
    _write_publication("g_func", [], summaries_dir=summaries_dir)
    assert list_scopes_with_publications(summaries_dir) == ["g_func", "g_team"]


def test_publication_artifact_write_is_atomic_no_tmp_left_behind(summaries_dir: str) -> None:
    _write_publication("g_team", [], summaries_dir=summaries_dir)
    tmp_path = Path(summaries_dir, "g_team.pub.md.tmp")
    assert not tmp_path.exists()
    assert Path(summaries_dir, "g_team.pub.md").exists()


# ---------------------------------------------------------------------------
# 2. propose_publish / propose_withdraw
# ---------------------------------------------------------------------------


def test_propose_publish_accept_records_act_and_judgment_and_updates_artifact(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Fit for export.")
    )

    outcome = propose_publish(
        "g_team",
        "Use protobuf for all RPC.",
        "directive",
        "rpc-protocol",
        ["c_dir1"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "accept"
    assert outcome.artifact_updated is True

    acts = record_store.list_publication_acts(scope_id="g_team")
    assert len(acts) == 1
    act = acts[0]
    assert act.act == "publish"
    assert act.anchors == ["directive:c_dir1"]
    assert act.trigger is None

    judgments = record_store.list_publication_judgments(scope_id="g_team")
    assert len(judgments) == 1
    assert judgments[0].decision == "accept"
    assert judgments[0].judged_by == "scope-manager"

    items = read_publication("g_team", summaries_dir=summaries_dir)
    assert len(items) == 1
    assert items[0].id == act.id
    assert items[0].content == "Use protobuf for all RPC."
    assert items[0].anchors == ["directive:c_dir1"]


def test_propose_publish_decline_records_rows_only_artifact_untouched(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="decline", reasoning="Reads as scratch.")
    )

    outcome = propose_publish(
        "g_team",
        "half-formed idea",
        "context",
        None,
        ["c_dir1"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "decline"
    assert outcome.artifact_updated is False

    acts = record_store.list_publication_acts(scope_id="g_team")
    assert len(acts) == 1
    judgments = record_store.list_publication_judgments(scope_id="g_team")
    assert len(judgments) == 1
    assert judgments[0].decision == "decline"

    # Artifact was never even created.
    assert read_publication("g_team", summaries_dir=summaries_dir) == []
    assert read_publication_text("g_team", summaries_dir=summaries_dir) is None


def test_propose_publish_threads_operator_memory_binding_into_judge_publication(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """The operator memory binding g_team reaches judge_publication, per ADR 0008 D3.

    Mirrors the contribution path (strata.app._read_judge_inputs): operator
    memory attached at an inter-stratum ancestor (g_exec here) binds every
    descendant scope, publication judging included.
    """
    from strata.operator import operator_publish

    _seed_summary_with_directive(summary_store, "g_team")
    item = operator_publish(
        "g_exec",
        "All services must use TLS 1.3 or later.",
        "tls",
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Fit for export.")
    )

    propose_publish(
        "g_team",
        "Use protobuf for all RPC.",
        "directive",
        "rpc-protocol",
        ["c_dir1"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert len(manager.publication_calls) == 1
    operator_memory = manager.publication_calls[0]["operator_memory"]
    assert operator_memory == [("g_exec", [item])]


def test_propose_publish_operator_memory_empty_when_none_attached(
    fleet, record_store, summary_store
) -> None:
    """No operator memory anywhere on the chain threads through as an empty binding."""
    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Fit for export.")
    )

    propose_publish(
        "g_team",
        "Use protobuf for all RPC.",
        "directive",
        "rpc-protocol",
        ["c_dir1"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert manager.publication_calls[0]["operator_memory"] == []


def test_propose_publish_over_budget_declines_via_real_scope_manager_no_artifact(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """End-to-end: a real ScopeManager mechanically declines an over-budget publish.

    No Anthropic call is made and the artifact is left untouched — same
    outcome shape as an ordinary judged decline (act + judgment recorded,
    artifact_updated is False), but without ever reaching the judge prompt.
    """
    from unittest.mock import MagicMock

    from strata.scope_manager import ScopeManager

    _seed_summary_with_directive(summary_store, "g_team")
    mock_client = MagicMock()
    manager = ScopeManager(client=mock_client)

    outcome = propose_publish(
        "g_team",
        "one two three four five six seven eight nine ten",
        "context",
        None,
        ["deploy-notes"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
        publication_max_words=3,
    )

    assert outcome.decision == "decline"
    assert outcome.artifact_updated is False
    assert mock_client.messages.create.call_count == 0
    assert read_publication("g_team", summaries_dir=summaries_dir) == []


def test_propose_withdraw_unblocked_even_on_already_over_budget_face(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """A withdrawal proceeds even when the face it shrinks is already over any budget.

    Existing over-budget faces are never retroactively trimmed (ADR 0013
    D7 — append-only, stored state is never rewritten); withdrawal must
    stay the only shrink path and must never itself be blocked by a budget.
    """
    _seed_summary_with_directive(summary_store, "g_team")
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_team",
        content="one two three four five six seven eight nine ten",
        anchors=["subject:notes"],
    )
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Fine to withdraw.")
    )

    outcome = propose_withdraw(
        "g_team",
        item.id,
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "accept"
    assert outcome.artifact_updated is True
    assert read_publication("g_team", summaries_dir=summaries_dir) == []


def test_propose_publish_threads_publication_max_words_into_judge_publication(
    fleet, record_store, summary_store
) -> None:
    """propose_publish threads publication_max_words through, default PUBLICATION_MAX_WORDS."""
    from strata.scope_manager import PUBLICATION_MAX_WORDS

    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Fit for export.")
    )

    propose_publish(
        "g_team",
        "Use protobuf for all RPC.",
        "directive",
        "rpc-protocol",
        ["c_dir1"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert manager.publication_calls[0]["publication_max_words"] == PUBLICATION_MAX_WORDS


def test_propose_publish_threads_explicit_publication_max_words(
    fleet, record_store, summary_store
) -> None:
    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Fit for export.")
    )

    propose_publish(
        "g_team",
        "Use protobuf for all RPC.",
        "directive",
        "rpc-protocol",
        ["c_dir1"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
        publication_max_words=42,
    )

    assert manager.publication_calls[0]["publication_max_words"] == 42


def test_propose_withdraw_threads_operator_memory_binding_into_judge_publication(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    from strata.operator import operator_publish

    _seed_summary_with_directive(summary_store, "g_team")
    item = operator_publish(
        "g_exec",
        "All services must use TLS 1.3 or later.",
        "tls",
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    publish_manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Fit for export.")
    )
    published = propose_publish(
        "g_team",
        "Use protobuf for all RPC.",
        "directive",
        "rpc-protocol",
        ["c_dir1"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=publish_manager,
    )

    withdraw_manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="No longer relevant.")
    )
    propose_withdraw(
        "g_team",
        published.act_id,
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=withdraw_manager,
    )

    assert len(withdraw_manager.publication_calls) == 1
    assert withdraw_manager.publication_calls[0]["operator_memory"] == [("g_exec", [item])]


def test_propose_publish_zero_anchors_raises_and_appends_no_act_row(
    fleet, record_store, summary_store
) -> None:
    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager()

    with pytest.raises(ValueError, match="at least one anchor"):
        propose_publish(
            "g_team",
            "content",
            "context",
            None,
            [],
            _proposer(),
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=manager,
        )

    assert record_store.list_publication_acts(scope_id="g_team") == []
    assert manager.publication_calls == []


def test_propose_publish_unknown_directive_anchor_raises_and_appends_no_act_row(
    fleet, record_store, summary_store
) -> None:
    _seed_summary_with_directive(summary_store, "g_team", directive_id="c_dir1")
    manager = _FakeScopeManager()

    with pytest.raises(ValueError, match="not in this"):
        propose_publish(
            "g_team",
            "content",
            "context",
            None,
            ["directive:c_does_not_exist"],
            _proposer(),
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=manager,
        )

    assert record_store.list_publication_acts(scope_id="g_team") == []
    assert manager.publication_calls == []


def test_propose_publish_unknown_scope_raises_valueerror(record_store, summary_store) -> None:
    empty_fleet = FleetConfig(strata=[], scopes=[], edges=[])
    manager = _FakeScopeManager()
    with pytest.raises(ValueError, match="Scope not found"):
        propose_publish(
            "g_nonexistent",
            "content",
            "context",
            None,
            ["subject:x"],
            _proposer(),
            fleet=empty_fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=manager,
        )


def test_propose_withdraw_accept_removes_item_and_records_rows(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    _seed_summary_with_directive(summary_store, "g_team")
    publish_manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Fit for export.")
    )
    published = propose_publish(
        "g_team",
        "Use protobuf for all RPC.",
        "directive",
        "rpc-protocol",
        ["c_dir1"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=publish_manager,
    )

    withdraw_manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="No longer relevant.")
    )
    outcome = propose_withdraw(
        "g_team",
        published.act_id,
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=withdraw_manager,
    )

    assert outcome.decision == "accept"
    assert outcome.artifact_updated is True
    assert read_publication("g_team", summaries_dir=summaries_dir) == []

    acts = record_store.list_publication_acts(scope_id="g_team")
    assert len(acts) == 2
    withdraw_act = next(a for a in acts if a.act == "withdraw")
    assert withdraw_act.withdraws == published.act_id
    assert withdraw_act.trigger is None

    judgments = record_store.list_publication_judgments(scope_id="g_team")
    assert len(judgments) == 2


def test_propose_withdraw_unknown_item_raises_keyerror_no_act_row(
    fleet, record_store, summary_store
) -> None:
    manager = _FakeScopeManager()
    with pytest.raises(KeyError):
        propose_withdraw(
            "g_team",
            "pub_does_not_exist",
            _proposer(),
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=manager,
        )
    assert record_store.list_publication_acts(scope_id="g_team") == []
    assert manager.publication_calls == []


def test_propose_publish_judge_failure_leaves_act_row_unjudged(
    fleet, record_store, summary_store
) -> None:
    """A judge_publication failure propagates AS-IS, after the act row already exists —
    but, unlike before this fix, the failure is now recorded as a marked attempt
    event, so the act is visibly stranded rather than indistinguishable from one
    nobody has gotten around to judging yet.
    """
    _seed_summary_with_directive(summary_store, "g_team")

    class _RaisingManager:
        def judge_publication(self, **_kwargs):
            raise RuntimeError("scope-manager unavailable")

    with pytest.raises(RuntimeError, match="scope-manager unavailable"):
        propose_publish(
            "g_team",
            "content",
            "context",
            None,
            ["c_dir1"],
            _proposer(),
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=_RaisingManager(),
        )

    # The act row exists — the record never lies — but carries no judgment.
    acts = record_store.list_publication_acts(scope_id="g_team")
    assert len(acts) == 1
    assert record_store.list_publication_judgments(scope_id="g_team") == []

    # The failure is now legible on the record: one attempt, marked terminal,
    # never a fabricated verdict.
    attempts = record_store.list_publication_judgment_attempts(scope_id="g_team")
    assert len(attempts) == 1
    assert attempts[0].act_id == acts[0].id
    assert attempts[0].error_class == "RuntimeError"
    assert attempts[0].message == "scope-manager unavailable"
    assert attempts[0].outcome == "judge_failed"

    (state,) = record_store.list_publication_act_states(scope_id="g_team")
    assert state.state == "judge_failed"
    assert state.error_class == "RuntimeError"


def test_propose_withdraw_judge_failure_records_marked_attempt(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """The same reliability treatment applies to a withdraw act's judge failure."""
    _seed_summary_with_directive(summary_store, "g_team")
    published = propose_publish(
        "g_team",
        "Use protobuf for all RPC.",
        "directive",
        "rpc-protocol",
        ["c_dir1"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=_FakeScopeManager(
            publication_judgment=PublicationJudgment(decision="accept", reasoning="Fit.")
        ),
    )

    class _RaisingManager:
        def judge_publication(self, **_kwargs):
            raise ValueError("malformed judge output")

    with pytest.raises(ValueError, match="malformed judge output"):
        propose_withdraw(
            "g_team",
            published.act_id,
            _proposer(),
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=_RaisingManager(),
        )

    acts = record_store.list_publication_acts(scope_id="g_team")
    withdraw_act = next(a for a in acts if a.act == "withdraw")
    attempts = record_store.list_publication_judgment_attempts(scope_id="g_team")
    assert len(attempts) == 1
    assert attempts[0].act_id == withdraw_act.id
    assert attempts[0].error_class == "ValueError"
    assert attempts[0].outcome == "judge_failed"


# ---------------------------------------------------------------------------
# 2b. Republication (ADR 0013 D4) — propose_publish relaying material
#     received in another scope's publication.
# ---------------------------------------------------------------------------


def test_propose_publish_relay_records_origin_and_relay_and_marks_second_hand(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """Relaying a scope's own original item: origin and relay both name the source scope."""
    origin_item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Deploys happen at 3pm UTC.",
        subject="deploy-notes",
        anchors=["subject:deploy-notes"],
    )
    _seed_summary_with_directive(summary_store, "g_func", directive_id="c_dir2")
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Worth relaying.")
    )

    outcome = propose_publish(
        "g_func",
        "Deploys happen at 3pm UTC.",
        "context",
        "deploy-notes",
        ["deploy-notes"],
        _proposer("g_func"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
        relay_source_scope_id="g_exec",
        relay_source_item_id=origin_item.id,
    )

    assert outcome.decision == "accept"
    item = read_publication("g_func", summaries_dir=summaries_dir)[0]
    assert item.origin_scope_id == "g_exec"
    assert item.relay_scope_id == "g_exec"
    assert item.relay_item_id == origin_item.id

    act = record_store.get_publication_act(outcome.act_id)
    assert act is not None
    assert act.origin_scope_id == "g_exec"
    assert act.relay_scope_id == "g_exec"
    assert act.relay_item_id == origin_item.id

    # The judge was told this is second-hand, with the origin — D4c.
    assert len(manager.publication_calls) == 1
    call = manager.publication_calls[0]
    assert call["relay_origin_scope_id"] == "g_exec"
    assert call["relay_via_scope_id"] == "g_exec"


def test_propose_publish_relay_transitive_origin_across_two_hops(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """C relaying B's copy of A's item: origin stays A (the ultimate origin), relay is B."""
    origin_item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Deploys happen at 3pm UTC.",
        subject="deploy-notes",
        anchors=["subject:deploy-notes"],
    )
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Worth relaying.")
    )
    first_hop = propose_publish(
        "g_func",
        "Deploys happen at 3pm UTC.",
        "context",
        "deploy-notes",
        ["deploy-notes"],
        _proposer("g_func"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
        relay_source_scope_id="g_exec",
        relay_source_item_id=origin_item.id,
    )
    relayed_by_func = read_publication("g_func", summaries_dir=summaries_dir)[0]
    assert relayed_by_func.id == first_hop.act_id

    second_hop = propose_publish(
        "g_team",
        "Deploys happen at 3pm UTC.",
        "context",
        "deploy-notes",
        ["deploy-notes"],
        _proposer("g_team"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
        relay_source_scope_id="g_func",
        relay_source_item_id=relayed_by_func.id,
    )

    item = read_publication("g_team", summaries_dir=summaries_dir)[0]
    assert second_hop.decision == "accept"
    assert item.origin_scope_id == "g_exec"  # ultimate origin, not g_func
    assert item.relay_scope_id == "g_func"  # immediate predecessor
    assert item.relay_item_id == relayed_by_func.id


def test_propose_publish_relay_missing_source_item_raises_no_act_row(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    manager = _FakeScopeManager()
    with pytest.raises(ValueError):  # noqa: PT011 — structural error, message not asserted
        propose_publish(
            "g_func",
            "Deploys happen at 3pm UTC.",
            "context",
            "deploy-notes",
            ["deploy-notes"],
            _proposer("g_func"),
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=manager,
            relay_source_scope_id="g_exec",
            relay_source_item_id="pub_does_not_exist",
        )
    assert record_store.list_publication_acts(scope_id="g_func") == []
    assert manager.publication_calls == []


def test_propose_publish_without_relay_source_leaves_relay_fields_none(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """An ordinary (non-relay) publish gets no relay origin in the judge's inputs."""
    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Fit for export.")
    )
    propose_publish(
        "g_team",
        "Use protobuf for all RPC.",
        "directive",
        "rpc-protocol",
        ["c_dir1"],
        _proposer(),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )
    item = read_publication("g_team", summaries_dir=summaries_dir)[0]
    assert item.origin_scope_id is None
    assert item.relay_scope_id is None
    assert item.relay_item_id is None
    assert manager.publication_calls[0]["relay_origin_scope_id"] is None
    assert manager.publication_calls[0]["relay_via_scope_id"] is None


# ---------------------------------------------------------------------------
# 2c. Withdrawal cascade to relayed copies (ADR 0013 D4b) — mechanical, no
#     LLM in the loop, a fourth choke point of ADR 0007 D3's class.
# ---------------------------------------------------------------------------


def _relay_via_publish(
    fleet,
    record_store,
    summary_store,
    summaries_dir,
    *,
    into_scope: str,
    from_scope: str,
    from_item_id: str,
    content: str = "Deploys happen at 3pm UTC.",
    subject: str = "deploy-notes",
) -> PublishedItem:
    """Relay *from_scope*'s published item into *into_scope*'s publication (accepted)."""
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Worth relaying.")
    )
    outcome = propose_publish(
        into_scope,
        content,
        "context",
        subject,
        [subject],
        _proposer(into_scope),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
        relay_source_scope_id=from_scope,
        relay_source_item_id=from_item_id,
    )
    assert outcome.decision == "accept"
    published = read_publication(into_scope, summaries_dir=summaries_dir)
    return next(i for i in published if i.id == outcome.act_id)


def test_propose_withdraw_cascades_to_relayed_copy_mechanically_no_judge_call(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    origin_item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Deploys happen at 3pm UTC.",
        subject="deploy-notes",
        anchors=["subject:deploy-notes"],
    )
    relayed = _relay_via_publish(
        fleet,
        record_store,
        summary_store,
        summaries_dir,
        into_scope="g_func",
        from_scope="g_exec",
        from_item_id=origin_item.id,
    )
    assert read_publication("g_func", summaries_dir=summaries_dir) != []

    withdraw_manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Retracted.")
    )
    outcome = propose_withdraw(
        "g_exec",
        origin_item.id,
        _proposer("g_exec"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=withdraw_manager,
    )
    assert outcome.decision == "accept"

    # The relayed copy is gone from g_func's publication.
    assert read_publication("g_func", summaries_dir=summaries_dir) == []

    # The cascade withdrawal is mechanical: no judgment row, a trigger
    # naming the origin's withdraw act, and NO extra judge_publication call
    # (the only call the fake manager saw is for the g_exec withdraw itself).
    assert len(withdraw_manager.publication_calls) == 1
    func_acts = record_store.list_publication_acts(scope_id="g_func")
    cascade_withdraw = next(a for a in func_acts if a.act == "withdraw")
    assert cascade_withdraw.withdraws == relayed.id
    assert cascade_withdraw.trigger is not None
    assert record_store.get_publication_judgment(cascade_withdraw.id) is None
    state = next(
        s
        for s in record_store.list_publication_act_states(scope_id="g_func")
        if s.act_id == cascade_withdraw.id
    )
    assert state.state == "mechanical"


def test_propose_withdraw_cascade_is_transitive_across_two_hops(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    origin_item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Deploys happen at 3pm UTC.",
        subject="deploy-notes",
        anchors=["subject:deploy-notes"],
    )
    hop1 = _relay_via_publish(
        fleet,
        record_store,
        summary_store,
        summaries_dir,
        into_scope="g_func",
        from_scope="g_exec",
        from_item_id=origin_item.id,
    )
    _relay_via_publish(
        fleet,
        record_store,
        summary_store,
        summaries_dir,
        into_scope="g_team",
        from_scope="g_func",
        from_item_id=hop1.id,
    )
    assert read_publication("g_team", summaries_dir=summaries_dir) != []

    withdraw_manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Retracted.")
    )
    propose_withdraw(
        "g_exec",
        origin_item.id,
        _proposer("g_exec"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=withdraw_manager,
    )

    assert read_publication("g_func", summaries_dir=summaries_dir) == []
    assert read_publication("g_team", summaries_dir=summaries_dir) == []
    # Still only the one judge call — for the origin withdraw. Both cascaded
    # withdrawals (g_func, then g_team) are mechanical.
    assert len(withdraw_manager.publication_calls) == 1


def test_propose_withdraw_cascade_self_relay_does_not_deadlock(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """A scope relaying its own former (already-relayed) item back into a new item of
    its own must not re-lock itself mid-cascade (scope_lock is not reentrant)."""
    origin_item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Deploys happen at 3pm UTC.",
        subject="deploy-notes",
        anchors=["subject:deploy-notes"],
    )
    hop1 = _relay_via_publish(
        fleet,
        record_store,
        summary_store,
        summaries_dir,
        into_scope="g_func",
        from_scope="g_exec",
        from_item_id=origin_item.id,
    )
    # g_exec relays g_func's copy into a SECOND item of its own.
    _relay_via_publish(
        fleet,
        record_store,
        summary_store,
        summaries_dir,
        into_scope="g_exec",
        from_scope="g_func",
        from_item_id=hop1.id,
    )

    withdraw_manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Retracted.")
    )
    outcome = propose_withdraw(
        "g_func",
        hop1.id,
        _proposer("g_func"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=withdraw_manager,
    )
    assert outcome.decision == "accept"
    # No hang, and g_exec's second item (relayed from g_func's now-withdrawn
    # copy) is cascaded away too.
    assert read_publication("g_exec", summaries_dir=summaries_dir) == [origin_item]


def test_propose_withdraw_cascade_reaches_a_relayed_copy_in_the_withdrawing_scope_itself(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """A relayed copy that loops back into the SAME scope being withdrawn from (whose
    lock the outer call already holds) is still withdrawn — not skipped for lack of a
    fresh lock to take."""
    origin_item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Deploys happen at 3pm UTC.",
        subject="deploy-notes",
        anchors=["subject:deploy-notes"],
    )
    # g_exec relays its OWN item into a second item of its own.
    _relay_via_publish(
        fleet,
        record_store,
        summary_store,
        summaries_dir,
        into_scope="g_exec",
        from_scope="g_exec",
        from_item_id=origin_item.id,
    )
    assert len(read_publication("g_exec", summaries_dir=summaries_dir)) == 2

    withdraw_manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Retracted.")
    )
    propose_withdraw(
        "g_exec",
        origin_item.id,
        _proposer("g_exec"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=withdraw_manager,
    )

    assert read_publication("g_exec", summaries_dir=summaries_dir) == []


def test_propose_withdraw_cascade_reaches_a_scope_revisited_via_a_different_relay_chain(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """A -> B -> C -> (back into) B: withdrawing A's item must still reach B's SECOND
    item, even though B was already visited earlier in the same cascade for its FIRST
    item. Regression guard for an over-coarse per-scope visited-set that would have
    left the second B item stranded."""
    origin_item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Deploys happen at 3pm UTC.",
        subject="deploy-notes",
        anchors=["subject:deploy-notes"],
    )
    hop_b1 = _relay_via_publish(
        fleet,
        record_store,
        summary_store,
        summaries_dir,
        into_scope="g_func",
        from_scope="g_exec",
        from_item_id=origin_item.id,
    )
    hop_c = _relay_via_publish(
        fleet,
        record_store,
        summary_store,
        summaries_dir,
        into_scope="g_team",
        from_scope="g_func",
        from_item_id=hop_b1.id,
    )
    # g_func relays g_team's copy into a SECOND item of its own.
    _relay_via_publish(
        fleet,
        record_store,
        summary_store,
        summaries_dir,
        into_scope="g_func",
        from_scope="g_team",
        from_item_id=hop_c.id,
    )
    assert len(read_publication("g_func", summaries_dir=summaries_dir)) == 2

    withdraw_manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="Retracted.")
    )
    propose_withdraw(
        "g_exec",
        origin_item.id,
        _proposer("g_exec"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=withdraw_manager,
    )

    assert read_publication("g_func", summaries_dir=summaries_dir) == []
    assert read_publication("g_team", summaries_dir=summaries_dir) == []


def test_relay_origin_and_relay_survive_a_summary_rewrite(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """A relayed item's origin/relay pointer is untouched by an unrelated summary rewrite
    and mechanical propagation event in the SAME scope (D4: survives a summary rewrite)."""
    origin_item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Deploys happen at 3pm UTC.",
        subject="deploy-notes",
        anchors=["subject:deploy-notes"],
    )
    relayed = _relay_via_publish(
        fleet,
        record_store,
        summary_store,
        summaries_dir,
        into_scope="g_func",
        from_scope="g_exec",
        from_item_id=origin_item.id,
    )

    # An unrelated directive is seeded and then removed via the scope's
    # mechanical propagation path — the closest analogue, in this module, to
    # a scope-manager rewriting g_func's summary and dropping a directive.
    _seed_summary_with_directive(summary_store, "g_func", directive_id="c_unrelated")
    unrelated = _seed_published_item(
        record_store,
        summaries_dir,
        "g_func",
        content="Use protobuf.",
        anchors=["directive:c_unrelated"],
    )
    withdrawn = propagate_directive_removals(
        "g_func",
        {"c_unrelated"},
        "c_trigger_unrelated",
        surviving_directive_ids=set(),
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert [i.id for i in withdrawn] == [unrelated.id]

    # The relayed item — untouched by that event — still carries its origin
    # and relay pointer exactly as it did before the rewrite.
    survivor = next(
        i for i in read_publication("g_func", summaries_dir=summaries_dir) if i.id == relayed.id
    )
    assert survivor == relayed
    assert survivor.origin_scope_id == "g_exec"
    assert survivor.relay_scope_id == "g_exec"
    assert survivor.relay_item_id == origin_item.id


def test_mechanical_directive_removal_cascades_to_relayed_copy(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    origin_item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Use protobuf.",
        anchors=["directive:c_dir1"],
    )
    _relay_via_publish(
        fleet,
        record_store,
        summary_store,
        summaries_dir,
        into_scope="g_func",
        from_scope="g_exec",
        from_item_id=origin_item.id,
        content="Use protobuf.",
        subject="rpc",
    )
    assert read_publication("g_func", summaries_dir=summaries_dir) != []

    withdrawn = propagate_directive_removals(
        "g_exec",
        {"c_dir1"},
        "c_trigger1",
        surviving_directive_ids=set(),
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert [i.id for i in withdrawn] == [origin_item.id]
    assert read_publication("g_func", summaries_dir=summaries_dir) == []


def test_judged_propagation_withdrawal_cascades_to_relayed_copy(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    origin_item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="stale belief",
        anchors=["subject:x"],
    )
    _relay_via_publish(
        fleet,
        record_store,
        summary_store,
        summaries_dir,
        into_scope="g_func",
        from_scope="g_exec",
        from_item_id=origin_item.id,
        content="stale belief",
        subject="x",
    )
    assert read_publication("g_func", summaries_dir=summaries_dir) != []

    withdrawn = apply_judged_withdrawals(
        "g_exec",
        [origin_item.id],
        judged_by="scope-manager",
        reasoning="No longer believed.",
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert [i.id for i in withdrawn] == [origin_item.id]
    assert read_publication("g_func", summaries_dir=summaries_dir) == []


# ---------------------------------------------------------------------------
# 3. Mechanical propagation (propagate_directive_removals)
# ---------------------------------------------------------------------------


def test_mechanical_propagation_withdraws_directive_only_anchored_item(
    fleet, record_store, summaries_dir
) -> None:
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_team",
        content="Use protobuf.",
        anchors=["directive:c_dir1"],
    )

    withdrawn = propagate_directive_removals(
        "g_team",
        {"c_dir1"},
        "c_trigger1",
        surviving_directive_ids=set(),
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert [i.id for i in withdrawn] == [item.id]
    assert read_publication("g_team", summaries_dir=summaries_dir) == []

    acts = record_store.list_publication_acts(scope_id="g_team")
    withdraw_act = next(a for a in acts if a.act == "withdraw")
    assert withdraw_act.withdraws == item.id
    assert withdraw_act.trigger == "c_trigger1"
    # Mechanical propagation gets NO judgment row for the withdraw act (the
    # seeded publish act above has its own judgment row, from the fixture).
    withdraw_judgment = record_store.get_publication_judgment(withdraw_act.id)
    assert withdraw_judgment is None


def test_mechanical_propagation_spares_item_with_surviving_subject_anchor(
    record_store, summaries_dir
) -> None:
    item = PublishedItem(
        id="pub_x2",
        kind="directive",
        content="Use protobuf, per our conventions doc.",
        subject="conventions",
        anchors=["directive:c_dir1", "subject:conventions"],
        published_at="2026-07-12T00:00:00+00:00",
    )
    _write_publication("g_team", [item], summaries_dir=summaries_dir)

    withdrawn = propagate_directive_removals(
        "g_team",
        {"c_dir1"},
        "c_trigger1",
        surviving_directive_ids=set(),
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert withdrawn == []
    assert read_publication("g_team", summaries_dir=summaries_dir) == [item]
    assert record_store.list_publication_acts(scope_id="g_team") == []


def test_mechanical_propagation_spares_item_anchored_to_a_different_surviving_directive(
    record_store, summaries_dir
) -> None:
    item = PublishedItem(
        id="pub_x3",
        kind="directive",
        content="Two-anchor item.",
        subject=None,
        anchors=["directive:c_dir1", "directive:c_dir2"],
        published_at="2026-07-12T00:00:00+00:00",
    )
    _write_publication("g_team", [item], summaries_dir=summaries_dir)

    withdrawn = propagate_directive_removals(
        "g_team",
        {"c_dir1"},  # c_dir2 survives
        "c_trigger1",
        surviving_directive_ids={"c_dir2"},
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert withdrawn == []
    assert read_publication("g_team", summaries_dir=summaries_dir) == [item]


def test_mechanical_propagation_fires_when_the_last_anchor_vanishes_in_a_later_event(
    fleet, record_store, summaries_dir
) -> None:
    # Review fix (PR #97): anchor vanishing is a property of the summary's
    # CURRENT state, not of one removal batch. A two-anchor item loses
    # c_dir1 in one event (it survives — c_dir2 still stands), then loses
    # c_dir2 in a LATER event: that second event removes only c_dir2, but
    # the item's anchors have now ALL vanished and it must be withdrawn.
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_team",
        content="Two-anchor item, anchors vanish across separate events.",
        anchors=["directive:c_dir1", "directive:c_dir2"],
    )

    first = propagate_directive_removals(
        "g_team",
        {"c_dir1"},
        "c_trigger1",
        surviving_directive_ids={"c_dir2"},
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert first == []

    second = propagate_directive_removals(
        "g_team",
        {"c_dir2"},
        "c_trigger2",
        surviving_directive_ids=set(),
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert [i.id for i in second] == [item.id]
    assert read_publication("g_team", summaries_dir=summaries_dir) == []
    withdraw_act = next(
        a for a in record_store.list_publication_acts(scope_id="g_team") if a.act == "withdraw"
    )
    assert withdraw_act.trigger == "c_trigger2"


def test_mechanical_propagation_attributes_each_item_to_its_own_trigger(
    fleet, record_store, summaries_dir
) -> None:
    # Issue #137: ONE amendment can remove directives on behalf of several
    # contributions (a batch — ADR 0011 D3), and it writes ONE summary, so
    # every per-trigger call below sees the SAME surviving set. Only the
    # removed ids tell the calls apart: each item must be withdrawn by the
    # trigger that removed ITS anchor, not by whichever call ran first.
    px = _seed_published_item(
        record_store,
        summaries_dir,
        "g_team",
        content="Anchored to member A's directive.",
        anchors=["directive:c_dirA"],
    )
    py = _seed_published_item(
        record_store,
        summaries_dir,
        "g_team",
        content="Anchored to member B's directive.",
        anchors=["directive:c_dirB"],
    )
    pz = _seed_published_item(
        record_store,
        summaries_dir,
        "g_team",
        content="Anchored to a directive the amendment left alone.",
        anchors=["directive:c_dirC"],
    )

    # The post-write summary of the whole amendment: A's and B's directives
    # are both gone, c_dirC still stands.
    surviving = {"c_dirC"}

    first = propagate_directive_removals(
        "g_team",
        {"c_dirA"},
        "c_memberA",
        surviving_directive_ids=surviving,
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    second = propagate_directive_removals(
        "g_team",
        {"c_dirB"},
        "c_memberB",
        surviving_directive_ids=surviving,
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    # A's call takes only A's item, even though B's is already un-anchored.
    assert [i.id for i in first] == [px.id]
    assert [i.id for i in second] == [py.id]

    # The item with a surviving anchor is untouched by both calls.
    assert [i.id for i in read_publication("g_team", summaries_dir=summaries_dir)] == [pz.id]

    triggers = {
        a.withdraws: a.trigger
        for a in record_store.list_publication_acts(scope_id="g_team")
        if a.act == "withdraw"
    }
    assert triggers == {px.id: "c_memberA", py.id: "c_memberB"}


def test_mechanical_propagation_noop_for_empty_publication(record_store, summaries_dir) -> None:
    assert (
        propagate_directive_removals(
            "g_never_published",
            {"c_dir1"},
            "c_trigger1",
            surviving_directive_ids=set(),
            fleet=fleet,
            record_store=record_store,
            summaries_dir=summaries_dir,
        )
        == []
    )


# ---------------------------------------------------------------------------
# 4. Judged propagation (apply_judged_withdrawals)
# ---------------------------------------------------------------------------


def test_judged_propagation_withdraws_named_item_with_judgment_row(
    fleet, record_store, summaries_dir
) -> None:
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_team",
        kind="context",
        content="Stale belief.",
        subject="status",
        anchors=["subject:status"],
    )

    withdrawn = apply_judged_withdrawals(
        "g_team",
        [item.id],
        judged_by="scope-manager",
        reasoning="Rewrite dropped this belief.",
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert [i.id for i in withdrawn] == [item.id]
    assert read_publication("g_team", summaries_dir=summaries_dir) == []

    acts = record_store.list_publication_acts(scope_id="g_team")
    withdraw_act = next(a for a in acts if a.act == "withdraw")
    assert withdraw_act.trigger is None

    withdraw_judgment = record_store.get_publication_judgment(withdraw_act.id)
    assert withdraw_judgment is not None
    assert withdraw_judgment.decision == "accept"
    assert withdraw_judgment.judged_by == "scope-manager"
    assert withdraw_judgment.reasoning == "Rewrite dropped this belief."


def test_judged_propagation_ignores_unknown_item_id(record_store, summaries_dir) -> None:
    _write_publication("g_team", [], summaries_dir=summaries_dir)

    withdrawn = apply_judged_withdrawals(
        "g_team",
        ["pub_does_not_exist"],
        judged_by="scope-manager",
        reasoning="whatever",
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert withdrawn == []
    assert record_store.list_publication_acts(scope_id="g_team") == []


# ---------------------------------------------------------------------------
# 5. Bootstrap (bootstrap_publication)
# ---------------------------------------------------------------------------


def test_bootstrap_accept_records_items_as_ordinary_accepted_publish_acts(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager(
        bootstrap_judgment=BootstrapJudgment(
            decision="accept",
            reasoning="Two items are fit for export.",
            items=[
                BootstrapPublishedItemInput(
                    content="Use protobuf for all RPC.",
                    kind="directive",
                    subject="rpc",
                    anchors=["c_dir1"],
                ),
                BootstrapPublishedItemInput(
                    content="Deploys happen at 3pm UTC.",
                    kind="context",
                    subject=None,
                    anchors=["deploy-notes"],
                ),
            ],
        )
    )

    outcome: BootstrapOutcome = bootstrap_publication(
        "g_team",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "accept"
    assert len(outcome.items) == 2

    acts = record_store.list_publication_acts(scope_id="g_team")
    assert len(acts) == 2
    assert all(a.act == "publish" for a in acts)
    judgments = record_store.list_publication_judgments(scope_id="g_team")
    assert len(judgments) == 2
    assert all(j.decision == "accept" and j.judged_by == "scope-manager" for j in judgments)

    items = read_publication("g_team", summaries_dir=summaries_dir)
    assert len(items) == 2
    anchor_sets = {tuple(i.anchors) for i in items}
    assert ("directive:c_dir1",) in anchor_sets
    assert ("subject:deploy-notes",) in anchor_sets


def test_bootstrap_decline_records_nothing(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    manager = _FakeScopeManager(
        bootstrap_judgment=BootstrapJudgment(
            decision="decline", reasoning="Nothing fit to publish yet.", items=[]
        )
    )

    outcome = bootstrap_publication(
        "g_team",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "decline"
    assert outcome.items == []
    assert record_store.list_publication_acts(scope_id="g_team") == []
    assert read_publication("g_team", summaries_dir=summaries_dir) == []


def test_bootstrap_drops_candidate_with_invalid_anchors_keeps_the_rest(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    _seed_summary_with_directive(summary_store, "g_team", directive_id="c_dir1")
    manager = _FakeScopeManager(
        bootstrap_judgment=BootstrapJudgment(
            decision="accept",
            reasoning="Proposed two, one is invalid.",
            items=[
                BootstrapPublishedItemInput(
                    content="Good item.",
                    kind="context",
                    subject=None,
                    anchors=["deploy-notes"],
                ),
                BootstrapPublishedItemInput(
                    content="Bad item — anchors a directive that doesn't exist.",
                    kind="directive",
                    subject=None,
                    anchors=["directive:c_does_not_exist"],
                ),
            ],
        )
    )

    outcome = bootstrap_publication(
        "g_team",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "accept"
    assert len(outcome.items) == 1
    assert outcome.items[0].content == "Good item."
    assert len(record_store.list_publication_acts(scope_id="g_team")) == 1


def test_bootstrap_outcome_threads_trimmed_flag_from_judgment(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """Issue #185: a caller of bootstrap_publication must be able to tell a mechanical

    backstop trim happened without parsing the reasoning prose — the judgment's
    ``trimmed`` flag must reach the outcome the caller actually sees.
    """
    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager(
        bootstrap_judgment=BootstrapJudgment(
            decision="accept",
            reasoning="One item is fit for export.",
            items=[
                BootstrapPublishedItemInput(
                    content="Use protobuf for all RPC.",
                    kind="directive",
                    subject="rpc",
                    anchors=["c_dir1"],
                ),
            ],
            trimmed=True,
        )
    )

    outcome: BootstrapOutcome = bootstrap_publication(
        "g_team",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.trimmed is True


def test_bootstrap_outcome_trimmed_false_when_judgment_not_trimmed(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    _seed_summary_with_directive(summary_store, "g_team")
    manager = _FakeScopeManager(
        bootstrap_judgment=BootstrapJudgment(
            decision="accept",
            reasoning="One item is fit for export.",
            items=[
                BootstrapPublishedItemInput(
                    content="Use protobuf for all RPC.",
                    kind="directive",
                    subject="rpc",
                    anchors=["c_dir1"],
                ),
            ],
            trimmed=False,
        )
    )

    outcome: BootstrapOutcome = bootstrap_publication(
        "g_team",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.trimmed is False


def test_bootstrap_unknown_scope_raises_valueerror(record_store, summary_store) -> None:
    empty_fleet = FleetConfig(strata=[], scopes=[], edges=[])
    manager = _FakeScopeManager()
    with pytest.raises(ValueError, match="Scope not found"):
        bootstrap_publication(
            "g_nonexistent",
            fleet=empty_fleet,
            record_store=record_store,
            summary_store=summary_store,
            scope_manager=manager,
        )


# ---------------------------------------------------------------------------
# 6. Change-event emission (ADR 0014 D1/D4/D5)
#
# Every writer of a shared input emits: a scope's face changing is exactly
# the kind of change a downstream scope cannot see for itself.
# ---------------------------------------------------------------------------


def _events_for(record_store: RecordStore, scope_id: str) -> list:
    return record_store.list_change_events(scope_id=scope_id)


def test_accepted_publish_emits_a_published_event_to_readers(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """g_func publishes; its chain child g_team is told (ADR 0014 D1 — an
    addition triggers exactly as a removal does)."""
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="fine")
    )
    _seed_summary_with_directive(summary_store, "g_func")

    outcome = propose_publish(
        "g_func",
        "Use protobuf for all RPC.",
        "directive",
        None,
        ["directive:c_dir1"],
        _proposer("g_func"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "accept"
    (event,) = _events_for(record_store, "g_team")
    assert event.kind == "published"
    assert event.item_id == outcome.act_id
    assert event.before is None
    assert "protobuf" in (event.after or "")


def test_declined_publish_emits_nothing(fleet, record_store, summary_store, summaries_dir) -> None:
    """A decline changed no input — nothing composed differently, nobody is told."""
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="decline", reasoning="no")
    )
    _seed_summary_with_directive(summary_store, "g_func")

    outcome = propose_publish(
        "g_func",
        "Use protobuf for all RPC.",
        "directive",
        None,
        ["directive:c_dir1"],
        _proposer("g_func"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    # Precondition: the act really was recorded and judged — the decline is
    # real, not a call that never happened.
    assert outcome.decision == "decline"
    assert record_store.get_publication_judgment(outcome.act_id) is not None

    assert _events_for(record_store, "g_team") == []


def test_accepted_withdraw_emits_a_withdrawn_event_carrying_what_was_lost(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="fine")
    )
    item = _seed_published_item(
        record_store, summaries_dir, "g_func", content="Use protobuf.", subject="rpc"
    )

    outcome = propose_withdraw(
        "g_func",
        item.id,
        _proposer("g_func"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.artifact_updated is True
    (event,) = _events_for(record_store, "g_team")
    assert event.kind == "withdrawn"
    assert event.item_id == item.id
    assert "protobuf" in (event.before or "")
    assert event.after is None


def test_cascaded_relay_withdrawal_emits_a_derived_event_inheriting_the_change_id(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """ADR 0014 D4 — a relayed withdrawal is DERIVED, so it inherits the id
    rather than minting a fresh one that would bound nothing."""
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="fine")
    )
    origin_item = _seed_published_item(
        record_store, summaries_dir, "g_exec", content="Use protobuf.", subject="rpc"
    )
    _relay_via_publish(
        fleet,
        record_store,
        summary_store,
        summaries_dir,
        into_scope="g_func",
        from_scope="g_exec",
        from_item_id=origin_item.id,
        content="Use protobuf.",
        subject="rpc",
    )

    propose_withdraw(
        "g_exec",
        origin_item.id,
        _proposer("g_exec"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    # Precondition: the cascade really did remove the relayed copy.
    assert read_publication("g_func", summaries_dir=summaries_dir) == []

    # g_func was told as a reader of g_exec (hop 0); g_team was told because
    # the copy IT read left g_func (hop 1) — same change id throughout.
    (origin_event,) = [e for e in _events_for(record_store, "g_func") if e.kind == "withdrawn"]
    (derived_event,) = [e for e in _events_for(record_store, "g_team") if e.kind == "withdrawn"]
    assert derived_event.change_id == origin_event.change_id
    assert derived_event.hop == origin_event.hop + 1
    assert derived_event.kind == "withdrawn"


def test_mechanical_directive_propagation_emits_for_each_withdrawn_item(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_func",
        content="Use protobuf.",
        anchors=["directive:c_dir1"],
    )

    withdrawn = propagate_directive_removals(
        "g_func",
        {"c_dir1"},
        "c_trigger1",
        surviving_directive_ids=set(),
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert [i.id for i in withdrawn] == [item.id]
    (event,) = _events_for(record_store, "g_team")
    assert event.kind == "withdrawn"
    assert event.item_id == item.id


def test_mechanical_propagation_inherits_every_change_id_it_is_given(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    _seed_published_item(
        record_store,
        summaries_dir,
        "g_func",
        content="Use protobuf.",
        anchors=["directive:c_dir1"],
    )

    propagate_directive_removals(
        "g_func",
        {"c_dir1"},
        "c_trigger1",
        surviving_directive_ids=set(),
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
        change_ids=["chg_upstream", "chg_also_upstream"],
    )

    # One row per wave the refresh drained (ADR 0014 D4, Phase A finding 2):
    # the reader refreshes if EITHER id is still unseen.
    events = _events_for(record_store, "g_team")
    assert {e.change_id for e in events} == {"chg_upstream", "chg_also_upstream"}


def test_judged_withdrawal_emits_and_inherits_the_judgment_s_change_ids(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """ADR 0014 D8 — the change ids are threaded into withdraw_published,
    never looked up."""
    item = _seed_published_item(record_store, summaries_dir, "g_func", content="Use protobuf.")

    withdrawn = apply_judged_withdrawals(
        "g_func",
        [item.id],
        judged_by="scope-manager",
        reasoning="No longer believed.",
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
        change_ids=["chg_from_judgment"],
    )

    assert [i.id for i in withdrawn] == [item.id]
    (event,) = _events_for(record_store, "g_team")
    assert event.change_id == "chg_from_judgment"
    assert event.kind == "withdrawn"


def test_a_withdrawal_that_removes_nothing_emits_nothing(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """Precondition first: a withdrawal that DOES remove something emits."""
    item = _seed_published_item(record_store, summaries_dir, "g_func", content="Use protobuf.")
    apply_judged_withdrawals(
        "g_func",
        [item.id],
        judged_by="scope-manager",
        reasoning="No longer believed.",
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert len(_events_for(record_store, "g_team")) == 1

    apply_judged_withdrawals(
        "g_func",
        ["pub_never_existed"],
        judged_by="scope-manager",
        reasoning="Nothing to do.",
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )

    assert len(_events_for(record_store, "g_team")) == 1


def test_bootstrap_emits_one_published_event_per_item(
    fleet, record_store, summary_store, summaries_dir
) -> None:
    """A first face is still an addition to everyone downstream (ADR 0014 D1)."""
    _seed_summary_with_directive(summary_store, "g_func")
    manager = _FakeScopeManager(
        bootstrap_judgment=BootstrapJudgment(
            decision="accept",
            reasoning="a reasonable first face",
            items=[
                BootstrapPublishedItemInput(
                    kind="directive",
                    content="Use protobuf for all RPC.",
                    subject="rpc",
                    anchors=["directive:c_dir1"],
                )
            ],
        )
    )

    outcome = bootstrap_publication(
        "g_func",
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "accept"
    events = _events_for(record_store, "g_team")
    assert [e.kind for e in events] == ["published"]


def test_emission_failure_never_fails_the_publish(
    fleet, record_store, summary_store, summaries_dir, monkeypatch
) -> None:
    """ADR 0014 D6: the writer writes its event and returns. A notice that
    cannot be written must not undo an act that already succeeded."""
    manager = _FakeScopeManager(
        publication_judgment=PublicationJudgment(decision="accept", reasoning="fine")
    )
    _seed_summary_with_directive(summary_store, "g_func")

    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("the record is unreachable")

    monkeypatch.setattr(record_store, "append_change_event", _boom)

    outcome = propose_publish(
        "g_func",
        "Use protobuf for all RPC.",
        "directive",
        None,
        ["directive:c_dir1"],
        _proposer("g_func"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=manager,
    )

    assert outcome.decision == "accept"
    assert outcome.artifact_updated is True
    assert [i.content for i in read_publication("g_func", summaries_dir=summaries_dir)] == [
        "Use protobuf for all RPC."
    ]
