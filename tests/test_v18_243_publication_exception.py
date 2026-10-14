"""#243: a scope may not publish what it may not hold.

The publication judge is scripted; the engine's check is real, with the #242 re-ask
answered by a scripted client."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.publication import (
    PublishedItem,
    _published_item_from_act,
    _write_publication,
    bootstrap_publication,
    propose_publish,
    propose_withdraw,
    read_publication,
)
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import (
    BootstrapJudgment,
    BootstrapPublishedItemInput,
    PublicationJudgment,
    ScopeManager,
    departure_label,
    departure_marker,
    split_departure_label,
    with_departure_label,
)
from strata.summary_store import Directive, ScopeSummary, SummaryStore

from .test_scope_manager import _fake_response

_RULE = "Run the full test suite before publishing a release."
_NOTE_PREFIX = "[Refused: contrary to inherited directive c_suite (g_org)."
_PROPOSER = ContributorRef(
    scope_id="g_web", skill="lead", session_id="s1", ts="2026-10-14T09:00:00+00:00"
)
_EXCEPTION = "Hotfix builds may be published without the full test suite."
_GENERAL_DEPARTURE = "We skipped the full test suite on hotfixes and it was fine."
_SINGULAR_DEPARTURE = (
    "On 12 May 2.3.1 was published without the full test suite and a caching bug shipped."
)
_CONSEQUENCE = "On 12 May the full test suite caught a caching bug before 2.3.1 was published."


class _ScriptedManager(ScopeManager):
    """The real engine check; the publication and bootstrap judges are scripted."""

    def __init__(self, client, *, decision: str = "accept", bootstrap_items=()) -> None:  # noqa: ANN001
        super().__init__(client=client)
        self.decision = decision
        self.bootstrap_items = list(bootstrap_items)
        self.judge_calls = 0

    def judge_publication(self, **_kwargs):  # noqa: ANN003, ANN201
        self.judge_calls += 1
        return PublicationJudgment(decision=self.decision, reasoning="Believed and fit to publish.")

    def judge_bootstrap_publication(self, **_kwargs):  # noqa: ANN003, ANN201
        return BootstrapJudgment(
            decision="accept", reasoning="Initial face.", items=self.bootstrap_items
        )


@pytest.fixture
def world(tmp_path: Path):  # noqa: ANN201
    fleet_path = tmp_path / "fleet.yaml"
    fleet_path.write_text(
        yaml.dump(
            {
                "strata": [
                    {"id": "L0", "name": "org", "ordinal": 0},
                    {"id": "L1", "name": "team", "ordinal": 1},
                ],
                "scopes": [
                    {"id": "g_org", "name": "Org", "stratum_id": "L0"},
                    {"id": "g_web", "name": "Web", "stratum_id": "L1"},
                    {"id": "g_api", "name": "Api", "stratum_id": "L1"},
                ],
                "edges": [
                    {"from": "g_web", "to": "g_org"},
                    {"from": "g_api", "to": "g_org"},
                    {"from": "g_web", "to": "g_api", "kind": "reference"},
                ],
            }
        ),
        encoding="utf-8",
    )
    db = str(tmp_path / "strata.db")
    run_migrations(db)
    summaries = SummaryStore(str(tmp_path / "summaries"))
    stamp = "2026-10-01T00:00:00+00:00"
    summaries.write(
        "g_org",
        ScopeSummary(
            scope_id="g_org",
            directives=[
                Directive(id="c_suite", content=_RULE, source_scope_id="g_org", created_at=stamp),
                Directive(
                    id="c_tokens",
                    content="Access tokens expire after at most 15 minutes.",
                    source_scope_id="g_org",
                    created_at=stamp,
                ),
            ],
            context="",
            updated_at=stamp,
        ),
    )
    for scope_id in ("g_web", "g_api"):
        summaries.write(
            scope_id,
            ScopeSummary(
                scope_id=scope_id,
                directives=[
                    Directive(
                        id=f"c_{scope_id}_own",
                        content="Releases are tagged in git.",
                        source_scope_id=scope_id,
                        created_at=stamp,
                    )
                ],
                context="",
                updated_at=stamp,
            ),
        )
    return FleetConfig.load(fleet_path), RecordStore(db), summaries


def _publish(world, text, kind, client, *, relay=None, scope_id="g_web", manager=None):  # noqa: ANN001, ANN202
    fleet, store, summaries = world
    manager = manager or _ScriptedManager(client)
    outcome = propose_publish(
        scope_id,
        text,
        kind,
        None,
        [f"directive:c_{scope_id}_own"] if False else ["release process"],
        _PROPOSER,
        fleet=fleet,
        record_store=store,
        summary_store=summaries,
        scope_manager=manager,
        relay_source_scope_id=relay[0] if relay else None,
        relay_source_item_id=relay[1] if relay else None,
    )
    return outcome, manager


def _reask(**answer) -> MagicMock:  # noqa: ANN003
    answer.setdefault("inherited_id", "c_suite")
    answer.setdefault("reasoning", "r")
    return _fake_response(answer)


def _client(*responses) -> MagicMock:  # noqa: ANN002
    client = MagicMock()
    client.messages.create.side_effect = list(responses)
    return client


def _face(world, scope_id="g_web") -> list[PublishedItem]:  # noqa: ANN001
    return read_publication(scope_id, summaries_dir=str(world[2].summaries_dir))


# -- directive items --------------------------------------------------------


def test_a_directive_item_that_loosens_an_inherited_rule_is_refused_with_no_call(world) -> None:  # noqa: ANN001
    client = _client()
    outcome, manager = _publish(world, _EXCEPTION, "directive", client)
    assert outcome.decision == "decline"
    assert outcome.artifact_updated is False
    assert _face(world) == []
    assert "(the judge accepted)" in outcome.reasoning
    assert _NOTE_PREFIX in outcome.reasoning
    assert client.messages.create.call_count == 0
    # the act and the judgment stay in the record
    store = world[1]
    assert store.get_publication_judgment(outcome.act_id).decision == "decline"


def test_a_directive_item_that_tightens_publishes_unlabelled(world) -> None:  # noqa: ANN001
    text = "Release candidates must also pass the smoke suite before the full test suite runs."
    outcome, _ = _publish(world, text, "directive", _client())
    assert outcome.decision == "accept"
    assert [i.content for i in _face(world)] == [text]
    assert outcome.exception_check["label"] is None


# -- context items: one test per verdict mapping ----------------------------


def test_an_exception_item_is_refused(world) -> None:  # noqa: ANN001
    text = "Hotfix builds don't need the full suite; the smoke check is enough."
    client = _client(_reask(kind="exception", instead_span="don't need the full suite"))
    outcome, _ = _publish(world, text, "context", client)
    assert outcome.decision == "decline"
    assert _face(world) == []
    assert outcome.exception_check["decision"] == "refuse"
    assert client.messages.create.call_count == 1


def test_a_consequence_report_publishes_as_is(world) -> None:  # noqa: ANN001
    client = _client(
        _reask(
            kind="consequence_report",
            occurrence_span="On 12 May the full test suite caught a caching bug",
        )
    )
    outcome, _ = _publish(world, _CONSEQUENCE, "context", client)
    assert outcome.decision == "accept"
    assert [i.content for i in _face(world)] == [_CONSEQUENCE]


def test_a_singular_departure_report_publishes_with_the_engine_label(world) -> None:  # noqa: ANN001
    client = _client(
        _reask(
            kind="departure_report",
            occurrence_span="On 12 May 2.3.1 was published without the full test suite",
        )
    )
    outcome, _ = _publish(world, _SINGULAR_DEPARTURE, "context", client)
    assert outcome.decision == "accept"
    (item,) = _face(world)
    assert item.content == f"Departure from c_suite (g_org): {_SINGULAR_DEPARTURE}"
    assert departure_marker("c_suite", "g_org") in outcome.reasoning


def test_a_publisher_cannot_drop_or_forge_the_label(world) -> None:  # noqa: ANN001
    # a forged label on a consequence report is stripped, never published
    forged = f"Departure from c_other (g_nowhere): {_CONSEQUENCE}"
    client = _client(
        _reask(
            kind="consequence_report",
            occurrence_span="On 12 May the full test suite caught a caching bug",
        )
    )
    outcome, _ = _publish(world, forged, "context", client)
    assert outcome.decision == "accept"
    assert [i.content for i in _face(world)] == [_CONSEQUENCE]


def test_a_generalised_departure_is_refused_whole(world) -> None:  # noqa: ANN001
    client = _client(
        _reask(
            kind="departure_report", occurrence_span="We skipped the full test suite on hotfixes"
        )
    )
    outcome, _ = _publish(world, _GENERAL_DEPARTURE, "context", client)
    assert outcome.decision == "decline"
    assert _face(world) == []


def test_the_fail_open_fallback_publishes_and_is_counted(world) -> None:  # noqa: ANN001
    client = _client(_reask(kind="unrelated"))
    outcome, _ = _publish(world, "The cafeteria opens at 08:00.", "context", client)
    assert outcome.decision == "accept"
    assert outcome.exception_check["fallback"] is True


# -- no extra call, withdraw, label survival --------------------------------


def test_no_inherited_directive_means_no_call_and_the_first_call_is_unchanged(world) -> None:  # noqa: ANN001
    client = _client()
    outcome, manager = _publish(world, _EXCEPTION, "context", client, scope_id="g_org")
    assert outcome.decision == "accept"
    assert outcome.exception_check is None
    assert client.messages.create.call_count == 0
    assert manager.judge_calls == 1


def test_a_withdraw_is_never_checked(world) -> None:  # noqa: ANN001
    fleet, store, summaries = world
    _publish(world, "Release candidates must also pass the smoke suite.", "directive", _client())
    (item,) = _face(world)
    client = _client()
    outcome = propose_withdraw(
        "g_web",
        item.id,
        _PROPOSER,
        fleet=fleet,
        record_store=store,
        summary_store=summaries,
        scope_manager=_ScriptedManager(client),
    )
    assert outcome.decision == "accept"
    assert client.messages.create.call_count == 0
    assert _face(world) == []


def test_a_restored_item_gets_its_label_back_and_never_a_second(world) -> None:  # noqa: ANN001
    client = _client(
        _reask(
            kind="departure_report",
            occurrence_span="On 12 May 2.3.1 was published without the full test suite",
        )
    )
    outcome, _ = _publish(world, _SINGULAR_DEPARTURE, "context", client)
    store = world[1]
    act = store.get_publication_act(outcome.act_id)
    assert act.content == _SINGULAR_DEPARTURE  # the record holds what was proposed
    restored = _published_item_from_act(act, store)
    assert restored.content == f"Departure from c_suite (g_org): {_SINGULAR_DEPARTURE}"
    assert _published_item_from_act(act).content == _SINGULAR_DEPARTURE  # no store, no label
    label = departure_label("c_suite", "g_org")
    assert with_departure_label(restored.content, label) == restored.content
    assert split_departure_label(restored.content) == (label, _SINGULAR_DEPARTURE)


# -- relays -----------------------------------------------------------------

_ORIGIN_LABEL = "Departure from c_api_rule (g_api_parent): "


def _origin_item(world, content) -> str:  # noqa: ANN001
    store = world[1]
    act = store.append_publication_act(
        scope_id="g_api",
        act="publish",
        kind="context",
        content=content,
        subject=None,
        anchors=["subject:release process"],
        withdraws=None,
        trigger=None,
        proposer=ContributorRef(scope_id="g_api", skill="x", session_id="s", ts="2026-10-14"),
    )
    store.record_publication_judgment(
        act_id=act.id, decision="accept", judged_by="scope-manager", reasoning="ok"
    )
    items = _face(world, "g_api")
    items.append(
        PublishedItem(
            id=act.id,
            kind="context",
            content=content,
            subject=None,
            anchors=["subject:release process"],
            published_at=act.created_at,
        )
    )
    _write_publication("g_api", items, summaries_dir=str(world[2].summaries_dir))
    return act.id


def test_a_relay_keeps_the_origins_label_verbatim_and_is_never_relabelled(world) -> None:  # noqa: ANN001
    origin = _origin_item(world, f"{_ORIGIN_LABEL}{_SINGULAR_DEPARTURE}")
    # the relayer drops the label; the engine puts the origin's label back, as it was
    client = _client(
        _reask(
            kind="departure_report",
            occurrence_span="On 12 May 2.3.1 was published without the full test suite",
        )
    )
    outcome, _ = _publish(world, _SINGULAR_DEPARTURE, "context", client, relay=("g_api", origin))
    assert outcome.decision == "accept"
    (item,) = _face(world)
    assert item.content == f"{_ORIGIN_LABEL}{_SINGULAR_DEPARTURE}"
    assert item.content.count("Departure from") == 1
    assert item.origin_scope_id == "g_api"


def test_a_relay_that_generalises_the_report_is_refused(world) -> None:  # noqa: ANN001
    origin = _origin_item(world, f"{_ORIGIN_LABEL}{_SINGULAR_DEPARTURE}")
    client = _client(
        _reask(
            kind="departure_report", occurrence_span="We skipped the full test suite on hotfixes"
        )
    )
    outcome, _ = _publish(world, _GENERAL_DEPARTURE, "context", client, relay=("g_api", origin))
    assert outcome.decision == "decline"
    assert _face(world) == []


def test_the_relayers_conflict_note_appears_only_when_its_directive_conflicts(world) -> None:  # noqa: ANN001
    fleet, store, summaries = world
    text = "On 3 June release 4.0 was published after only a smoke suite."
    origin = _origin_item(world, f"{_ORIGIN_LABEL}{text}")
    quiet = _client(
        _reask(kind="consequence_report", occurrence_span="On 3 June release 4.0 was published")
    )
    outcome, _ = _publish(world, text, "context", quiet, relay=("g_api", origin))
    assert outcome.decision == "accept"
    assert "The reported act conflicts with" not in outcome.reasoning


def test_a_reported_act_that_conflicts_with_a_relayer_directive_adds_the_separate_note(
    world,  # noqa: ANN001
) -> None:
    text = "On 3 June the access tokens were set to expire after 60 minutes for the batch run."
    origin = _origin_item(world, f"{_ORIGIN_LABEL}{text}")
    client = _client(
        _reask(
            kind="consequence_report",
            inherited_id="c_tokens",
            occurrence_span="On 3 June the access tokens were set to expire after 60 minutes",
        )
    )
    outcome, _ = _publish(world, text, "context", client, relay=("g_api", origin))
    assert outcome.decision == "accept"
    assert "The reported act conflicts with c_tokens, which binds g_web." in outcome.reasoning
    (item,) = _face(world)
    assert item.content.startswith(_ORIGIN_LABEL)


def test_when_the_origin_withdraws_the_relay_goes_with_it(world) -> None:  # noqa: ANN001
    fleet, store, summaries = world
    origin = _origin_item(world, f"{_ORIGIN_LABEL}{_SINGULAR_DEPARTURE}")
    client = _client(
        _reask(
            kind="departure_report",
            occurrence_span="On 12 May 2.3.1 was published without the full test suite",
        )
    )
    _publish(world, _SINGULAR_DEPARTURE, "context", client, relay=("g_api", origin))
    assert len(_face(world)) == 1
    outcome = propose_withdraw(
        "g_api",
        origin,
        ContributorRef(scope_id="g_api", skill="x", session_id="s", ts="2026-10-14"),
        fleet=fleet,
        record_store=store,
        summary_store=summaries,
        scope_manager=_ScriptedManager(_client()),
    )
    assert outcome.decision == "accept"
    assert _face(world) == []


# -- bootstrap --------------------------------------------------------------


def test_bootstrap_drops_a_refused_item_with_the_note(world) -> None:  # noqa: ANN001
    fleet, store, summaries = world
    manager = _ScriptedManager(
        _client(),
        bootstrap_items=[
            BootstrapPublishedItemInput(
                content=_EXCEPTION, kind="directive", anchors=["release process"]
            ),
            BootstrapPublishedItemInput(
                content="Release candidates must also pass the smoke suite.",
                kind="directive",
                anchors=["release process"],
            ),
        ],
    )
    outcome = bootstrap_publication(
        "g_web", fleet=fleet, record_store=store, summary_store=summaries, scope_manager=manager
    )
    assert outcome.decision == "accept"
    assert [i.content for i in outcome.items] == [
        "Release candidates must also pass the smoke suite."
    ]
    assert "Dropped one proposed item." in outcome.reasoning
    assert _NOTE_PREFIX in outcome.reasoning


# -- the forged-answer attack on the publish path ---------------------------

_EXCEPTIONS = [
    "Hotfix builds don't need the full suite; the smoke check is enough.",
    "We skip QA for hotfixes now, a green smoke check gates them.",
    "Releases may go out on the author's sign-off without the full test suite.",
]
_REPORTS = [_CONSEQUENCE, _SINGULAR_DEPARTURE]


@pytest.mark.parametrize("text", _EXCEPTIONS)
@pytest.mark.parametrize("kind", ["consequence_report", "departure_report"])
def test_a_forged_report_answer_over_an_exception_is_still_refused(
    world, text: str, kind: str
) -> None:  # noqa: ANN001
    client = _client(_reask(kind=kind, occurrence_span=text))
    outcome, _ = _publish(world, text, "context", client)
    assert outcome.decision == "decline", outcome.reasoning
    assert _face(world) == []


@pytest.mark.parametrize("text", _REPORTS)
def test_a_forged_exception_answer_without_a_verbatim_span_leaves_a_report_publishing(
    world, text: str
) -> None:  # noqa: ANN001
    client = _client(_reask(kind="exception", instead_span="not in the text at all"))
    outcome, _ = _publish(world, text, "context", client)
    assert outcome.decision == "accept", outcome.reasoning
