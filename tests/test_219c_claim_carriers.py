"""Issue #219 C — the owner-judge's own paraphrase check on a corrected claim.

Two layers:

1. :meth:`ScopeManager.check_claim_carriers` — the standalone model call,
   offline with the underlying Anthropic client mocked (never a scripted
   judgment object — the live-shape discipline #225 established): decisions
   applied per item id, a missing/malformed decision or id defaults to
   ``unresolved_unreadable``, and the whole call raising does too, for every
   candidate.
2. :func:`strata.publication.check_claim_carriers` — the engine-level helper,
   real ``RecordStore``/publication artifact, a small fake standing in for
   the judge call (no LLM involved) so the ranking/cap/row-writing/cascade
   logic is tested directly: a paraphrase carrier withdrawn with its relay
   cascading, a subject-swap near-miss left untouched but still rowed, the
   20-item cap with overflow, an unreadable decision, and a tokenless claim
   still reaching the judge call.

Plus the drain-path site (:func:`strata.app.drain_scope`, #221's sweep) and
the gating that keeps an ordinary (non-correction) amendment from ever
calling either helper.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.publication import (
    PublishedItem,
    _write_publication,
    check_claim_carriers,
    observed_value_veto,
    propose_publish,
    read_publication,
)
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import PublicationJudgment, ScopeManager

from .test_scope_manager import _fake_response

# ---------------------------------------------------------------------------
# 1. ScopeManager.check_claim_carriers — the standalone model call.
# ---------------------------------------------------------------------------


def _decisions_response(entries: list[dict]) -> MagicMock:
    return _fake_response({"decisions": entries})


def test_decisions_applied_per_item_id() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _decisions_response(
        [
            {"item_id": "pub_a", "decision": "carries"},
            {"item_id": "pub_b", "decision": "does_not_carry"},
        ]
    )
    manager = ScopeManager(client=mock_client, model="test-model")
    result = manager.check_claim_carriers(
        refuted_claim_content="The service listens on port 8443.",
        correcting_content="Used port 8443, the service refused.",
        candidates=[("pub_a", "Port: 8443"), ("pub_b", "The service is healthy.")],
    )
    assert result == {"pub_a": "carries", "pub_b": "does_not_carry"}


def test_a_missing_id_defaults_to_unresolved_unreadable() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _decisions_response(
        [{"item_id": "pub_a", "decision": "carries"}]
    )
    manager = ScopeManager(client=mock_client, model="test-model")
    result = manager.check_claim_carriers(
        refuted_claim_content="claim",
        correcting_content="correction",
        candidates=[("pub_a", "x"), ("pub_b", "y")],
    )
    assert result == {"pub_a": "carries", "pub_b": "unresolved_unreadable"}


def test_a_malformed_decision_value_defaults_to_unresolved_unreadable() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _decisions_response(
        [{"item_id": "pub_a", "decision": "maybe"}]
    )
    manager = ScopeManager(client=mock_client, model="test-model")
    result = manager.check_claim_carriers(
        refuted_claim_content="claim",
        correcting_content="correction",
        candidates=[("pub_a", "x")],
    )
    assert result == {"pub_a": "unresolved_unreadable"}


def test_an_id_naming_no_candidate_is_ignored() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _decisions_response(
        [{"item_id": "pub_ghost", "decision": "carries"}]
    )
    manager = ScopeManager(client=mock_client, model="test-model")
    result = manager.check_claim_carriers(
        refuted_claim_content="claim",
        correcting_content="correction",
        candidates=[("pub_a", "x")],
    )
    assert result == {"pub_a": "unresolved_unreadable"}


def test_the_whole_call_raising_fails_closed_for_every_candidate() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("network error")
    manager = ScopeManager(client=mock_client, model="test-model")
    result = manager.check_claim_carriers(
        refuted_claim_content="claim",
        correcting_content="correction",
        candidates=[("pub_a", "x"), ("pub_b", "y")],
    )
    assert result == {"pub_a": "unresolved_unreadable", "pub_b": "unresolved_unreadable"}


def test_no_candidates_makes_no_call_at_all() -> None:
    mock_client = MagicMock()
    manager = ScopeManager(client=mock_client, model="test-model")
    result = manager.check_claim_carriers(
        refuted_claim_content="claim", correcting_content="correction", candidates=[]
    )
    assert result == {}
    mock_client.messages.create.assert_not_called()


def test_the_ordinary_judge_tool_and_system_prompt_are_untouched() -> None:
    """Input identity (#225's own discipline): CLAIM_CARRIER_TOOL is a
    standalone tool the ordinary `judge()` call never sees, never nested
    into or merged with JUDGE_TOOL/`_SYSTEM_PROMPT` — this method makes its
    OWN separate `messages.create` call, with its own tool and system text,
    and is never invoked from inside `judge()`/`judge_batch()` at all."""
    from strata.scope_manager import _SYSTEM_PROMPT, CLAIM_CARRIER_TOOL, JUDGE_TOOL

    assert CLAIM_CARRIER_TOOL["name"] != JUDGE_TOOL["name"]
    assert CLAIM_CARRIER_TOOL is not JUDGE_TOOL

    mock_client = MagicMock()
    mock_client.messages.create.return_value = _decisions_response(
        [{"item_id": "pub_a", "decision": "carries"}]
    )
    manager = ScopeManager(client=mock_client, model="test-model")
    manager.check_claim_carriers(
        refuted_claim_content="claim",
        correcting_content="correction",
        candidates=[("pub_a", "x")],
    )
    assert mock_client.messages.create.call_count == 1
    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["tools"] == [CLAIM_CARRIER_TOOL]
    assert kwargs["system"][0]["text"] != _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 2. publication.check_claim_carriers — the engine-level helper.
# ---------------------------------------------------------------------------


def _make_fleet(tmp_path: Path) -> FleetConfig:
    import yaml

    fleet = {
        "strata": [
            {"id": "L0", "name": "executive", "ordinal": 0},
            {"id": "L1", "name": "function", "ordinal": 1},
        ],
        "scopes": [
            {"id": "g_exec", "name": "Executive", "stratum_id": "L0"},
            {"id": "g_func", "name": "Function", "stratum_id": "L1"},
        ],
        "edges": [{"from": "g_func", "to": "g_exec"}],
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
def fleet(tmp_path: Path) -> FleetConfig:
    return _make_fleet(tmp_path)


def _proposer(scope_id: str = "g_exec") -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id,
        skill="strata-developer",
        session_id="sess_test",
        ts="2026-10-02T00:00:00+00:00",
    )


def _seed_published_item(
    record_store: RecordStore,
    summaries_dir: str,
    scope_id: str,
    *,
    content: str,
    subject: str | None = None,
) -> PublishedItem:
    act = record_store.append_publication_act(
        scope_id=scope_id,
        act="publish",
        kind="context",
        content=content,
        subject=subject,
        anchors=[],
        withdraws=None,
        trigger=None,
        proposer=_proposer(scope_id),
    )
    record_store.record_publication_judgment(
        act_id=act.id, decision="accept", judged_by="scope-manager", reasoning="seeded for test"
    )
    item = PublishedItem(
        id=act.id,
        kind="context",
        content=content,
        subject=subject,
        anchors=[],
        published_at=act.created_at,
    )
    existing = read_publication(scope_id, summaries_dir=summaries_dir)
    _write_publication(scope_id, [*existing, item], summaries_dir=summaries_dir)
    return item


class _FakeCarrierManager:
    """Stands in for the judge call — no LLM, a canned mapping by item id.

    An id this map omits falls back to the SAME default
    :meth:`ScopeManager.check_claim_carriers` uses — ``unresolved_unreadable``
    — so a test can exercise that default without mocking an Anthropic
    response at all.
    """

    def __init__(self, decisions: dict[str, str]) -> None:
        self._decisions = decisions
        self.calls: list[list[tuple[str, str]]] = []

    def check_claim_carriers(self, *, candidates, **_kwargs) -> dict[str, str]:  # noqa: ANN001
        self.calls.append(list(candidates))
        return {
            item_id: self._decisions.get(item_id, "unresolved_unreadable")
            for item_id, _ in candidates
        }


def test_a_paraphrase_carrier_is_withdrawn_and_notified_and_relays_cascade(
    fleet, record_store, summaries_dir
) -> None:
    from strata.summary_store import SummaryStore

    summary_store = SummaryStore(summaries_dir)
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="API port: 8443",
        subject="service-port",
    )
    relay_manager = MagicMock()
    relay_manager.judge_publication.return_value = PublicationJudgment(
        decision="accept", reasoning="Worth relaying."
    )
    relay_outcome = propose_publish(
        "g_func",
        "API port: 8443",
        "context",
        "service-port",
        ["subject:service-port"],
        _proposer("g_func"),
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        scope_manager=relay_manager,
        relay_source_scope_id="g_exec",
        relay_source_item_id=item.id,
    )
    assert relay_outcome.decision == "accept"

    carrier_manager = _FakeCarrierManager({item.id: "carries"})
    withdrawn = check_claim_carriers(
        "g_exec",
        claim_id="c_target01",
        corrected_claim_content="The internal API service listens on port 8443.",
        correcting_content="The port is actually unassigned; port 8443 was never correct.",
        trigger_id="c_outcome01",
        already_withdrawn=[],
        scope_manager=carrier_manager,
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
        change_ids=["chg_1"],
    )
    assert [i.id for i in withdrawn] == [item.id]
    assert read_publication("g_exec", summaries_dir=summaries_dir) == []
    assert read_publication("g_func", summaries_dir=summaries_dir) == []

    rows = record_store.list_claim_carrier_checks(scope_id="g_exec")
    assert len(rows) == 1
    assert rows[0].item_id == item.id
    assert rows[0].outcome == "carries"
    assert rows[0].corrected_claim_id == "c_target01"


def test_a_subject_swap_near_miss_is_judged_does_not_carry_rowed_and_left_untouched(
    fleet, record_store, summaries_dir
) -> None:
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Billing retries run nightly at 2am.",
        subject="billing",
    )
    carrier_manager = _FakeCarrierManager({item.id: "does_not_carry"})
    withdrawn = check_claim_carriers(
        "g_exec",
        claim_id="c_target02",
        corrected_claim_content="The internal API service listens on port 8443.",
        correcting_content="The port is unassigned.",
        trigger_id="c_outcome02",
        already_withdrawn=[],
        scope_manager=carrier_manager,
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert withdrawn == []
    assert [i.id for i in read_publication("g_exec", summaries_dir=summaries_dir)] == [item.id]

    rows = record_store.list_claim_carrier_checks(scope_id="g_exec")
    assert len(rows) == 1
    assert rows[0].outcome == "does_not_carry"


def test_25_face_items_cap_at_20_judged_5_unresolved_overflow(
    fleet, record_store, summaries_dir
) -> None:
    items = [
        _seed_published_item(
            record_store,
            summaries_dir,
            "g_exec",
            content=f"Unrelated published fact number {n}.",
            subject=f"fact-{n}",
        )
        for n in range(25)
    ]
    carrier_manager = _FakeCarrierManager({})  # every classified item -> unresolved_unreadable
    check_claim_carriers(
        "g_exec",
        claim_id="c_target03",
        corrected_claim_content="The internal API service listens on port 8443.",
        correcting_content="The port is unassigned.",
        trigger_id="c_outcome03",
        already_withdrawn=[],
        scope_manager=carrier_manager,
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert len(carrier_manager.calls[0]) == 20

    rows = record_store.list_claim_carrier_checks(scope_id="g_exec")
    assert len(rows) == 25
    by_outcome: dict[str, int] = {}
    for row in rows:
        by_outcome[row.outcome] = by_outcome.get(row.outcome, 0) + 1
    assert by_outcome == {"unresolved_unreadable": 20, "unresolved_overflow": 5}
    all_item_ids = {row.item_id for row in rows}
    assert all_item_ids == {i.id for i in items}


def test_an_unreadable_item_gets_unresolved_unreadable_nothing_withdrawn(
    fleet, record_store, summaries_dir
) -> None:
    item = _seed_published_item(
        record_store, summaries_dir, "g_exec", content="Some published text.", subject="x"
    )
    carrier_manager = _FakeCarrierManager({})  # omits item.id entirely
    withdrawn = check_claim_carriers(
        "g_exec",
        claim_id="c_target04",
        corrected_claim_content="The internal API service listens on port 8443.",
        correcting_content="The port is unassigned.",
        trigger_id="c_outcome04",
        already_withdrawn=[],
        scope_manager=carrier_manager,
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert withdrawn == []
    assert [i.id for i in read_publication("g_exec", summaries_dir=summaries_dir)] == [item.id]
    rows = record_store.list_claim_carrier_checks(scope_id="g_exec")
    assert len(rows) == 1
    assert rows[0].outcome == "unresolved_unreadable"


def test_a_tokenless_claim_still_sends_candidates_to_the_judge(
    fleet, record_store, summaries_dir
) -> None:
    """The claim's own content is nothing but stopwords — the ranking score is
    0.0 for every candidate, but the whole face is still the candidate set
    (no threshold anywhere), so the judge call still happens."""
    item = _seed_published_item(
        record_store, summaries_dir, "g_exec", content="Some published text.", subject="x"
    )
    carrier_manager = _FakeCarrierManager({item.id: "does_not_carry"})
    check_claim_carriers(
        "g_exec",
        claim_id="c_target05",
        corrected_claim_content="is a the of",
        correcting_content="correction",
        trigger_id="c_outcome05",
        already_withdrawn=[],
        scope_manager=carrier_manager,
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert len(carrier_manager.calls) == 1
    assert [cid for cid, _ in carrier_manager.calls[0]] == [item.id]


def test_a_scope_manager_without_the_method_degrades_to_unresolved_unreadable(
    fleet, record_store, summaries_dir
) -> None:
    """A lighter test double elsewhere in the fleet (predating this method)
    must not crash the write it runs inside — it degrades exactly like an
    unreadable response would."""
    item = _seed_published_item(
        record_store, summaries_dir, "g_exec", content="API port: 8443", subject="x"
    )

    class _LegacyScopeManager:
        def judge(self, **_kwargs):  # pragma: no cover - not exercised here
            raise NotImplementedError

    withdrawn = check_claim_carriers(
        "g_exec",
        claim_id="c_target07",
        corrected_claim_content="The API listens on port 8443.",
        correcting_content="correction",
        trigger_id="c_outcome07",
        already_withdrawn=[],
        scope_manager=_LegacyScopeManager(),
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert withdrawn == []
    assert [i.id for i in read_publication("g_exec", summaries_dir=summaries_dir)] == [item.id]
    rows = record_store.list_claim_carrier_checks(scope_id="g_exec")
    assert len(rows) == 1
    assert rows[0].outcome == "unresolved_unreadable"


def test_already_withdrawn_items_never_become_candidates(
    fleet, record_store, summaries_dir
) -> None:
    item = _seed_published_item(
        record_store, summaries_dir, "g_exec", content="API port: 8443", subject="x"
    )
    carrier_manager = _FakeCarrierManager({item.id: "carries"})
    withdrawn = check_claim_carriers(
        "g_exec",
        claim_id="c_target06",
        corrected_claim_content="The API listens on port 8443.",
        correcting_content="correction",
        trigger_id="c_outcome06",
        already_withdrawn=[item.id],
        scope_manager=carrier_manager,
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert withdrawn == []
    assert carrier_manager.calls == []
    assert record_store.list_claim_carrier_checks(scope_id="g_exec") == []


# ---------------------------------------------------------------------------
# 3. observed_value_veto — the mechanical guard (CEO, standing rule 1): can
# only PREVENT a withdrawal the judge's own "carries" answer would otherwise
# cause, never force one. VALUE tokens only (numbers, identifiers, quoted
# spans, and the closed POL word list) — measured against 1,881 real judge
# answers from the re-gate after two content-word attempts both over-fired
# on realistic corrections sharing subject/action vocabulary with the
# refuted claim (one caught the canonical inversion 20/21 but vetoed 51 TRUE
# carriers). The VALUE-only version measured 14/21 inversions vetoed, 0/198
# true carriers vetoed.
# ---------------------------------------------------------------------------


def test_the_inversion_pair_from_the_gate_is_kept_by_the_guard(
    fleet, record_store, summaries_dir
) -> None:
    """The live-gate inversion: the judge reads "carries the corrected claim"
    as "carries the correction" and marks the item stating the NEW value
    carries. The item's own value tokens ("on") match the observed value,
    not the refuted claim ("off"), so the guard keeps it published."""
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Feature flags default to on in production.",
        subject="flags",
    )
    carrier_manager = _FakeCarrierManager({item.id: "carries"})
    withdrawn = check_claim_carriers(
        "g_exec",
        claim_id="c_target08",
        corrected_claim_content="Feature flags default to off in production.",
        correcting_content="Feature flags default to on in production.",
        trigger_id="c_outcome08",
        already_withdrawn=[],
        scope_manager=carrier_manager,
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert withdrawn == []
    assert [i.id for i in read_publication("g_exec", summaries_dir=summaries_dir)] == [item.id]
    rows = record_store.list_claim_carrier_checks(scope_id="g_exec")
    assert len(rows) == 1
    assert rows[0].outcome == "kept_by_guard"


def test_an_item_carrying_both_values_is_not_vetoed(fleet, record_store, summaries_dir) -> None:
    """The item's own value tokens include the REFUTED claim's own value too
    ("off"), so the guard does not fire — the judge's carries stands."""
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Feature flags default to on, previously off, in production.",
        subject="flags",
    )
    carrier_manager = _FakeCarrierManager({item.id: "carries"})
    withdrawn = check_claim_carriers(
        "g_exec",
        claim_id="c_target09",
        corrected_claim_content="Feature flags default to off in production.",
        correcting_content="Feature flags default to on in production.",
        trigger_id="c_outcome09",
        already_withdrawn=[],
        scope_manager=carrier_manager,
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert [i.id for i in withdrawn] == [item.id]
    assert read_publication("g_exec", summaries_dir=summaries_dir) == []
    rows = record_store.list_claim_carrier_checks(scope_id="g_exec")
    assert rows[0].outcome == "carries"


def test_a_generic_correction_never_vetoes(fleet, record_store, summaries_dir) -> None:
    """A correction with no VALUE tokens of its own — no numbers,
    identifiers, quoted spans, or POL words — gives the guard nothing to
    check against; it never fires, whatever the judge said."""
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Feature flags default to off in production.",
        subject="flags",
    )
    carrier_manager = _FakeCarrierManager({item.id: "carries"})
    withdrawn = check_claim_carriers(
        "g_exec",
        claim_id="c_target10",
        corrected_claim_content="Feature flags default to off in production.",
        correcting_content="This is not that.",
        trigger_id="c_outcome10",
        already_withdrawn=[],
        scope_manager=carrier_manager,
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert [i.id for i in withdrawn] == [item.id]
    rows = record_store.list_claim_carrier_checks(scope_id="g_exec")
    assert rows[0].outcome == "carries"


def test_the_guard_never_turns_does_not_carry_into_carries(
    fleet, record_store, summaries_dir
) -> None:
    item = _seed_published_item(
        record_store,
        summaries_dir,
        "g_exec",
        content="Feature flags default to on in production.",
        subject="flags",
    )
    carrier_manager = _FakeCarrierManager({item.id: "does_not_carry"})
    withdrawn = check_claim_carriers(
        "g_exec",
        claim_id="c_target11",
        corrected_claim_content="Feature flags default to off in production.",
        correcting_content="Feature flags default to on in production.",
        trigger_id="c_outcome11",
        already_withdrawn=[],
        scope_manager=carrier_manager,
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
    )
    assert withdrawn == []
    rows = record_store.list_claim_carrier_checks(scope_id="g_exec")
    assert rows[0].outcome == "does_not_carry"


def test_observed_value_veto_unit() -> None:
    assert observed_value_veto(
        "Feature flags default to off in production.",
        "Feature flags default to on in production.",
        "Feature flags default to on in production.",
    )
    assert not observed_value_veto(
        "Feature flags default to off in production.",
        "Feature flags default to on in production.",
        "Feature flags default to on, previously off, in production.",
    )
    # A generic correction with no value tokens of its own never vetoes via
    # the value-token clause specifically (the antonym-flip/added-exception
    # clauses are independent of the correction text — see re-gate 2 below).
    assert not observed_value_veto(
        "The limit is set to 15 items.",
        "This is not that.",
        "The limit is set to 99 items.",
    )


def test_the_number_inversion_is_vetoed() -> None:
    """Tokens 15 -> 60: "expire after 60 minutes"."""
    assert observed_value_veto(
        "Access tokens expire after 15 minutes.",
        "Access tokens expire after 60 minutes.",
        "Access tokens expire after 60 minutes.",
    )


def test_an_identifier_swap_is_vetoed_even_when_the_correction_mentions_both() -> None:
    """pyproject.toml vs VERSION.txt, where the correction mentions both —
    the "block = (VR - VC) or VR" fallback: since the correction repeats
    the refuted claim's own identifier, the block falls back to the WHOLE
    refuted value set, and the item carries none of it."""
    assert observed_value_veto(
        "The version lives only in pyproject.toml.",
        "Ignoring pyproject.toml, the version lives in VERSION.txt.",
        "The version lives only in VERSION.txt.",
    )


def test_a_flag_near_miss_stating_the_refuted_value_is_not_vetoed() -> None:
    """ "In production, feature flags are off by default" — a real carrier of
    the refuted claim, not the correction; must not be kept."""
    assert not observed_value_veto(
        "Feature flags default to off in production.",
        "Feature flags default to on in production.",
        "In production, feature flags are off by default.",
    )


def test_an_unrelated_number_near_miss_is_not_vetoed() -> None:
    """ "Hygiene visits use 45-minute booking slots" — a near-miss on a
    DIFFERENT number than what the correction actually states; shares no
    value with the correction, so the guard gives the judge's carries no
    reason to be overridden."""
    assert not observed_value_veto(
        "Appointment slots are capped at 30 minutes.",
        "Appointment slots are capped at 20 minutes.",
        "Hygiene visits use 45-minute booking slots.",
    )


def test_a_paraphrase_with_no_extractable_value_is_not_vetoed() -> None:
    """ "Access tokens last a quarter of an hour" — a true paraphrase of the
    refuted claim stated in words the value tokeniser cannot parse
    numerically; shares no detectable value with the correction, so the
    judge's carries stands — the veto is a narrow backstop, not the primary
    paraphrase defence."""
    assert not observed_value_veto(
        "Access tokens expire after 15 minutes.",
        "Access tokens expire after 60 minutes.",
        "Access tokens last a quarter of an hour.",
    )


def test_a_tokenless_refuted_claim_never_vetoes() -> None:
    assert not observed_value_veto(
        "The team prefers minimal abstractions.",
        "The team actually prefers maximal abstractions.",
        "The team prefers maximal abstractions.",
    )


# ---------------------------------------------------------------------------
# Re-gate 2: an antonym-flip clause was tried and dropped — 0 overrides in
# the held-out run, no evidence it helps (the errors there were subject
# swaps, which no antonym pair can see). An added-exception clause was also
# tried and dropped — the philosopher's ruling: it would keep refuted values
# published, the CEO's own named failure — so an exception-only item must
# NOT be vetoed, left to the judge in both directions. The veto is
# value-token only again.
# ---------------------------------------------------------------------------


def test_an_added_exception_item_is_not_vetoed() -> None:
    """The philosopher's ruling: "PII is redacted in logs except in debug
    builds" carries the refuted claim's VALUE but not its SCOPE — whether
    it should be withdrawn depends on what the correction actually hit
    (changed value vs. IS the exception), which this function cannot tell.
    No veto clause fires on an added exception alone; the judge's own
    carries answer stands, win or lose."""
    assert not observed_value_veto(
        "PII is always redacted in logs.",
        "PII was found unredacted in debug build logs.",
        "PII is redacted in logs except in debug builds.",
    )


def test_a_generic_correction_with_no_value_tokens_never_vetoes() -> None:
    assert not observed_value_veto(
        "The limit is set to 15 items.",
        "This is not accurate.",
        "The limit is set to 99 items.",
    )


# ---------------------------------------------------------------------------
# 4. The drain-path site (strata.app.drain_scope, #221's centralised sweep).
# ---------------------------------------------------------------------------

_DRAIN_FLEET_YAML = """
strata:
  - id: L0
    name: Root
    ordinal: 0
scopes:
  - id: g_owner
    name: Owner
    stratum_id: L0
edges: []
"""


def _stratum_for(fleet, scope_id: str):
    scope = fleet.get_scope(scope_id)
    return next(s for s in fleet.strata if s.id == scope.stratum_id)


def test_the_drain_path_site_runs_the_judge_call_too(tmp_path: Path) -> None:
    """A cross-scope-notified refresh's own centralised sweep (#221) now runs
    the paraphrase check unconditionally too, whatever the refresh judgment
    did (here: decline)."""
    from strata import app
    from strata.summary_store import ScopeSummary, SummaryStore

    db_path = str(tmp_path / "test.db")
    fleet_yaml = tmp_path / "fleet.yaml"
    fleet_yaml.write_text(textwrap.dedent(_DRAIN_FLEET_YAML), encoding="utf-8")
    summaries_dir = tmp_path / "summaries"
    run_migrations(db_path)
    summary_store = SummaryStore(str(summaries_dir))

    with RecordStore(db_path) as store:
        item = _seed_published_item(
            store, str(summaries_dir), "g_owner", content="API port: 8443", subject="x"
        )
        summary_store.write(
            "g_owner",
            ScopeSummary(
                scope_id="g_owner",
                directives=[],
                context="",
                updated_at="2026-10-02T00:00:00Z",
            ),
        )
        notice = store.append_contribution(
            scope_id="g_owner",
            content="[Input change chg_1: claim c_claim01 was corrected.]",
            proposed_classification="context",
            subject="manager-refresh",
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_owner",
                skill="scope-manager",
                session_id="refresh",
                ts="2026-10-02T00:01:00Z",
            ),
        )
        store.append_change_event(
            change_id="chg_1",
            contribution_id=notice.id,
            scope_id="g_owner",
            item_id="c_claim01",
            kind="claim_corrected",
            before="The internal API service listens on port 8443.",
            after="The port is actually unassigned.",
        )

    def fake_judge(**kwargs: Any) -> Any:
        from strata.scope_manager import ScopeManagerJudgment

        assert kwargs["mode"] == "input_change_refresh"
        return ScopeManagerJudgment(
            decision="decline", reasoning="Nothing to admit.", new_summary=None
        )

    manager = MagicMock()
    manager.judge.side_effect = fake_judge
    manager.check_claim_carriers.return_value = {item.id: "carries"}

    with RecordStore(db_path) as store:
        fleet_ = FleetConfig.load(fleet_yaml)
        summary_store_ = SummaryStore(str(summaries_dir))
        result = app.drain_scope(
            "g_owner",
            fleet=fleet_,
            record_store=store,
            summary_store=summary_store_,
            scope_manager=manager,
            summary_max_words=500,
        )
        assert result.judged is True

    assert read_publication("g_owner", summaries_dir=str(summaries_dir)) == []
    with RecordStore(db_path) as store:
        rows = store.list_claim_carrier_checks(scope_id="g_owner")
        assert len(rows) == 1
        assert rows[0].outcome == "carries"
        assert rows[0].item_id == item.id


# ---------------------------------------------------------------------------
# 5. Gating — an ordinary (non-correction) amendment never calls either site.
# ---------------------------------------------------------------------------


def test_an_ordinary_accept_never_calls_check_claim_carriers(tmp_path: Path, monkeypatch) -> None:
    from strata import app
    from strata.record_store import Contribution
    from strata.scope_manager import ScopeManagerJudgment
    from strata.summary_store import ScopeSummary, SummaryStore

    db_path = str(tmp_path / "test.db")
    fleet_yaml = tmp_path / "fleet.yaml"
    fleet_yaml.write_text(textwrap.dedent(_DRAIN_FLEET_YAML), encoding="utf-8")
    summaries_dir = tmp_path / "summaries"
    run_migrations(db_path)

    calls: list[dict] = []
    monkeypatch.setattr(
        app,
        "check_claim_carriers",
        lambda *a, **k: calls.append(k) or [],  # noqa: ARG005
    )

    with RecordStore(db_path) as store:
        contribution = store.append_contribution(
            scope_id="g_owner",
            content="An ordinary observation.",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_owner", skill="engineer", session_id="s1", ts="2026-10-02T00:00:00Z"
            ),
        )

    outcome_judgment = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="Noted.",
        new_summary=ScopeSummary(
            scope_id="g_owner",
            directives=[],
            context="An ordinary observation.",
            updated_at="2026-10-02T00:00:01Z",
        ),
    )
    mock_manager = MagicMock()
    mock_manager.judge.return_value = outcome_judgment

    with RecordStore(db_path) as store:
        fleet_ = FleetConfig.load(fleet_yaml)
        summary_store_ = SummaryStore(str(summaries_dir))

        def _fetch(cid: str) -> Contribution:
            return store.get_contribution(cid)

        app._judge_and_record(
            contribution=_fetch(contribution.id),
            scope=fleet_.get_scope("g_owner"),
            stratum=_stratum_for(fleet_, "g_owner"),
            fleet=fleet_,
            record_store=store,
            summary_store=summary_store_,
            scope_manager=mock_manager,
            summary_max_words=500,
        )

    assert calls == []
