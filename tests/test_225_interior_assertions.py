"""#225 — interior assertions about another scope (ADR 0016).

Offline: a REAL `ScopeManager`, only the underlying Anthropic client mocked —
the FIRST response is an ordinary accept naming another fleet scope this
scope is not entitled to; the SECOND (when reached) is the one targeted
re-ask's own response.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from strata.fleet_config import EntitlementView, Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import ScopeManager
from strata.summary_store import Directive, ScopeSummary

from .test_scope_manager import _fake_response

STRATUM = Stratum(id="L1", name="function", ordinal=1)
PARENT = Scope(id="g_225parent", name="225-parent", stratum_id="L0")
SCOPE = Scope(id="g_225self", name="225-self", stratum_id="L1")
OTHER = Scope(id="g_225other", name="225-other-scope", stratum_id="L1")
PEER = Scope(id="g_225peer", name="225-peer", stratum_id="L1")

CONTRIBUTOR = ContributorRef(
    scope_id="g_225reporter",
    skill="on-call-engineer",
    session_id="sess_225",
    ts="2026-10-02T10:00:00+00:00",
)

EXISTING_DIRECTIVE = Directive(
    id="c_225old",
    content="Use the shared logging format.",
    subject="logging",
    source_scope_id=SCOPE.id,
    source_skill="architect",
    created_at="2026-09-01T09:00:00+00:00",
)

CURRENT_SUMMARY = ScopeSummary(
    scope_id=SCOPE.id,
    directives=[EXISTING_DIRECTIVE],
    context="The team favours minimal abstractions.",
    updated_at="2026-09-01T09:00:00+00:00",
)

ENTITLEMENT = EntitlementView(
    chain=[PARENT, SCOPE], descendants=[], referenced_peers=[PEER], others=[OTHER]
)


def _contribution(content: str, *, cid: str = "c_225new") -> Contribution:
    return Contribution(
        id=cid,
        scope_id=SCOPE.id,
        content=content,
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=CONTRIBUTOR,
        created_at="2026-10-02T09:00:00+00:00",
    )


def _ordinary_accept(content: str, **extra) -> MagicMock:
    payload = {
        "decision": "accept_as_context",
        "reasoning": "Accepting this observation.",
        "directive_ops": [],
        "new_context": f"{CURRENT_SUMMARY.context} {content}",
    }
    payload.update(extra)
    return _fake_response(payload)


def _judge(mock_client: MagicMock, content: str, *, entitlement=ENTITLEMENT) -> tuple:
    manager = ScopeManager(client=mock_client)
    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=_contribution(content),
        entitlement=entitlement,
    )
    return judgment, manager


def _reask_response(**payload) -> MagicMock:
    payload.setdefault("reasoning", "Classified.")
    return _fake_response(payload)


# ---------------------------------------------------------------------------
# Non-triggers: self, ancestor, entitled peer, alias text
# ---------------------------------------------------------------------------


def test_mentioning_this_scope_itself_does_not_trigger() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _ordinary_accept(
        "225-self decided to keep the current approach."
    )
    judgment, _ = _judge(mock_client, "225-self decided to keep the current approach.")
    assert mock_client.messages.create.call_count == 1
    assert not any("interior assertion" in n for n in judgment.protocol_notes)


def test_mentioning_an_ancestor_does_not_trigger() -> None:
    mock_client = MagicMock()
    content = "225-parent only approves changes under budget."
    mock_client.messages.create.return_value = _ordinary_accept(content)
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 1
    assert not any("interior assertion" in n for n in judgment.protocol_notes)


def test_mentioning_an_entitled_referenced_peer_does_not_trigger() -> None:
    mock_client = MagicMock()
    content = "225-peer only approves changes under budget."
    mock_client.messages.create.return_value = _ordinary_accept(content)
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 1
    assert not any("interior assertion" in n for n in judgment.protocol_notes)


def test_alias_text_does_not_trigger() -> None:
    """No fuzzy or alias matching (CEO condition 3) — only a literal
    occurrence of the id or name itself triggers."""
    mock_client = MagicMock()
    content = "the other team only approves changes under budget."
    mock_client.messages.create.return_value = _ordinary_accept(content)
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 1
    assert not any("interior assertion" in n for n in judgment.protocol_notes)


def test_a_decline_never_triggers() -> None:
    """Only an ACCEPT triggers #225's own interior-assertion re-ask — a
    decline has nothing to verify there. Reasoning deliberately avoids v1.17
    item 1's own trigger phrases ("manufactured attribution", "no one
    spoke", ...), which legitimately fire a DIFFERENT re-ask on a decline —
    that the two never confuse each other is covered by that item's own
    tests, not here."""
    mock_client = MagicMock()
    content = "225-other-scope only approves changes under budget."
    mock_client.messages.create.return_value = _fake_response(
        {
            "decision": "decline",
            "reasoning": "Declined: contradicts a binding directive.",
            "directive_ops": [],
            "new_context": None,
        }
    )
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 1
    assert judgment.decision == "decline"


# ---------------------------------------------------------------------------
# Triggers: each classification path
# ---------------------------------------------------------------------------


def test_conduct_is_admitted_as_judged_unchanged() -> None:
    content = "225-other-scope's agent rejected our request twice this week."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="conduct", act_span=content),
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_context"
    assert judgment.new_context == f"{CURRENT_SUMMARY.context} {content}"
    assert any("interior assertion" in n and "conduct" in n for n in judgment.protocol_notes)
    assert judgment.interior_assertion == {
        "scopes": [OTHER.id],
        "class": "conduct",
        "result": "admitted as judged",
        "act_span": content,
    }


def test_none_classification_is_declined_as_manufactured_attribution() -> None:
    content = "225-other-scope has decided to freeze the budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="none"),
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "decline"
    assert judgment.new_summary is None
    assert "Manufactured attribution" in judgment.reasoning
    assert any("none" in n for n in judgment.protocol_notes)


def test_publication_with_a_visible_ref_id_is_admitted() -> None:
    content = "225-other-scope published a note about its new policy."
    mock_client = MagicMock()
    published = [_published_item("pub_225")]
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="publication", ref_id="pub_225"),
    ]
    manager = ScopeManager(client=mock_client)
    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=_contribution(content),
        entitlement=ENTITLEMENT,
        current_publication=published,
    )
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_context"
    assert any("publication" in n for n in judgment.protocol_notes)


def test_publication_with_an_invisible_ref_id_is_declined() -> None:
    content = "225-other-scope published a note about its new policy."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="publication", ref_id="pub_nonexistent"),
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "decline"
    assert "not visible" in judgment.reasoning
    assert any("ref not visible" in n for n in judgment.protocol_notes)


def test_directive_with_a_visible_ancestor_directive_id_is_admitted() -> None:
    content = "225-parent's policy is that 225-other-scope owns the budget decision."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="directive", ref_id="c_225ancestor_dir"),
    ]
    manager = ScopeManager(client=mock_client)
    judgment = manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[],
        new_contribution=_contribution(content),
        entitlement=ENTITLEMENT,
        ancestor_directives=[
            (
                PARENT.id,
                [
                    Directive(
                        id="c_225ancestor_dir",
                        content="225-other-scope owns the budget decision.",
                        subject="budget",
                        source_scope_id=PARENT.id,
                        source_skill="architect",
                        created_at="2026-01-01T00:00:00+00:00",
                    )
                ],
            )
        ],
    )
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_context"
    assert any("directive" in n for n in judgment.protocol_notes)


def test_invented_informant_span_is_declined() -> None:
    content = "225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant", informant_span="Nobody Real", telling_span="told me"
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "decline"
    assert "invented informant" in judgment.reasoning
    assert any("invented informant" in n for n in judgment.protocol_notes)


def test_verified_informant_replaces_context_and_strips_directive_ops() -> None:
    content = "Jordan Lee from 225-other-scope told me they only approve changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(
            {
                "decision": "accept_as_directive",
                "reasoning": "Treating this as binding.",
                "directive_ops": [{"op": "append"}],
                "new_context": None,
            }
        ),
        _reask_response(
            classification="informant",
            informant_span="Jordan Lee from 225-other-scope",
            telling_span="told me",
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    # Forced to context — an informant's word is never a directive.
    assert judgment.decision == "accept_as_context"
    assert judgment.directive_ops == []
    assert judgment.replaced_context is None  # first judgment's new_context was None
    assert "on-call-engineer (g_225reporter) reports that" in judgment.new_context
    assert "Jordan Lee from 225-other-scope" in judgment.new_context
    assert content in judgment.new_context
    assert judgment.new_summary is not None
    assert judgment.new_summary.context == judgment.new_context
    # The existing directive survives untouched — stripping only drops the
    # NEW append op, never carries-across directives.
    assert judgment.new_summary.directives == [EXISTING_DIRECTIVE]
    assert any("informant" in n and "context replaced" in n for n in judgment.protocol_notes)


def test_verified_informant_never_supersedes_or_retires_an_existing_directive() -> None:
    """ADR 0016 D1: a directive changes only by its issuer's own act. If the
    FIRST judgment emitted a supersede+append against the existing
    directive, the informant path must not let it through, even converted
    to a retire — the directive must stay byte-identical, and the claim
    enters only as the attributed context line."""
    content = "Jordan Lee from 225-other-scope told me they only approve changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _fake_response(
            {
                "decision": "accept_as_directive",
                "reasoning": "Superseding the old rule.",
                "directive_ops": [
                    {"op": "supersede", "id": EXISTING_DIRECTIVE.id},
                    {"op": "append"},
                ],
                "new_context": None,
            }
        ),
        _reask_response(
            classification="informant",
            informant_span="Jordan Lee from 225-other-scope",
            telling_span="told me",
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "accept_as_context"
    assert judgment.directive_ops == []
    # The directive is untouched — not removed, not replaced.
    assert judgment.new_summary.directives == [EXISTING_DIRECTIVE]
    assert content in judgment.new_context
    assert any(
        "Dropped directive op" in n and "informant's word never binds" in n
        for n in judgment.protocol_notes
    )


def test_unreadable_reask_response_fails_closed_as_judge_failure() -> None:
    content = "225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="not-a-real-value"),
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "decline"
    assert judgment.judge_failure is True
    assert "judge failure" in judgment.reasoning
    assert any("judge failure" in n for n in judgment.protocol_notes)


def test_informant_with_no_span_at_all_fails_closed_as_judge_failure() -> None:
    content = "225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="informant"),  # no informant_span
    ]
    judgment, _ = _judge(mock_client, content)
    assert mock_client.messages.create.call_count == 2
    assert judgment.decision == "decline"
    assert judgment.judge_failure is True


def _published_item(item_id: str):
    item = MagicMock()
    item.id = item_id
    return item


# ---------------------------------------------------------------------------
# Review fix 1 — the own-role hole: an informant span that is really the
# contributor's own voice is not an informant at all.
# ---------------------------------------------------------------------------


def test_informant_own_role_is_declined_as_invented() -> None:
    """ "As on-call-engineer I can tell you X" has the contributor's own
    skill verbatim in the text, so the plain occurrence check alone would
    pass it — this is the hole the architect's live-gate review found."""
    content = (
        "As on-call-engineer I can tell you 225-other-scope only approves changes under budget."
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant", informant_span="on-call-engineer", telling_span="told me"
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "own role is not an informant" in judgment.reasoning
    assert judgment.interior_assertion["result"] == "declined (invented informant)"


def test_informant_own_scope_is_declined_as_invented() -> None:
    content = "g_225reporter here -- 225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant", informant_span="g_225reporter", telling_span="told me"
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "own role is not an informant" in judgment.reasoning


def test_informant_bare_first_person_is_declined_as_invented() -> None:
    content = "I can tell you 225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="informant", informant_span="I", telling_span="told me"),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "own role is not an informant" in judgment.reasoning


def test_informant_span_with_connector_and_role_is_declined_as_invented() -> None:
    """The architect's narrowing example: "me, as on-call-engineer" is still
    entirely the contributor's own voice once "as" is dropped as a
    connector, so it is rejected even though it is not a bare pronoun or a
    bare skill alone."""
    content = "me, as on-call-engineer -- 225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant",
            informant_span="me, as on-call-engineer",
            telling_span="told me",
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "own role is not an informant" in judgment.reasoning


def test_informant_span_with_possessive_role_is_declined_as_invented() -> None:
    """ "our on-call-engineer" is still entirely the contributor's own voice
    once the possessive determiner "our" is dropped."""
    content = "our on-call-engineer says 225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant",
            informant_span="our on-call-engineer",
            telling_span="told me",
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "own role is not an informant" in judgment.reasoning


def test_informant_span_naming_a_genuine_third_party_with_a_possessive_passes() -> None:
    """The narrowing's other direction: a span that merely MENTIONS a
    first-person possessive but NAMES a genuine third party is not the
    contributor's own voice, and must be admitted as an informant."""
    content = (
        "my colleague Lena Fischer in procurement told me 225-other-scope only "
        "approves changes under budget."
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant",
            informant_span="my colleague Lena Fischer",
            telling_span="told me",
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "accept_as_context"
    assert judgment.interior_assertion["result"] == "admitted (context replaced)"
    assert judgment.interior_assertion["telling_span"] == "told me"


def test_joint_event_verb_informant_is_attributed_in_discussion_with_form() -> None:
    """Philis's ruling (architect review round 2, widening #225): a
    JOINT-EVENT verb with a party ("discussed with X") is a telling event
    too, and records at ITS OWN strength — "in discussion with X: ...",
    never "X says"."""
    content = "As discussed with 225-other-scope, they only approve changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant",
            informant_span="225-other-scope",
            telling_span="discussed with 225-other-scope",
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "accept_as_context"
    assert judgment.new_context is not None
    assert "In discussion with 225-other-scope:" in judgment.new_context
    assert "says" not in judgment.new_context


def test_informant_span_naming_a_genuine_third_party_with_our_passes() -> None:
    content = (
        "our contact Ravi in treasury told us 225-other-scope only approves changes under budget."
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant",
            informant_span="our contact Ravi in treasury",
            telling_span="told us",
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "accept_as_context"
    assert judgment.interior_assertion["result"] == "admitted (context replaced)"


# ---------------------------------------------------------------------------
# Re-gate fix 4 (Philis's ruling): the informant verifies through a TELLING
# EVENT (`telling_span`), not by rejecting a bare scope name/collective
# outright — ADR 0016's informant is "a person OR PARTY who told the agent",
# and a party can tell. A scope's own name or a collective passes when backed
# by a genuine telling event naming the contributor as addressee/audience.
# ---------------------------------------------------------------------------

_J4_INTERIOR_INFORMANT_TWINS = [
    "225-other-scope told me on Tuesday that the arm-controller order needs a second quote",
    "225-other-scope's maintenance crew told us the server room HVAC would be off "
    "on the last two first Sundays",
    "225-other-scope's review bot messaged me that the diagram PRs need a palette fix",
    "225-other-scope informed us it would decline the FX hedge request for the launch on Tuesday",
    "225-other-scope wrote to me that the firmware builds would ship on the following Wednesday",
    "225-other-scope's scanner team shared with us that the click-log export job "
    "was quarantined yesterday",
]


def test_all_six_j4_interior_informant_twins_pass() -> None:
    for content in _J4_INTERIOR_INFORMANT_TWINS:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _ordinary_accept(content),
            _reask_response(
                classification="informant",
                informant_span="225-other-scope",
                telling_span=content,
            ),
        ]
        judgment, _ = _judge(mock_client, content)
        assert judgment.decision == "accept_as_context", content
        assert judgment.interior_assertion["result"] == "admitted (context replaced)", content


def test_scope_name_informant_backed_by_a_telling_event_passes() -> None:
    """ "procurement told us on Tuesday that…" — the architect's own example:
    a bare scope name/id is no longer rejected outright now that a genuine
    telling event backs it."""
    content = "225-other-scope told us on Tuesday that budgets are frozen for this quarter."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant",
            informant_span="225-other-scope",
            telling_span="told us on Tuesday",
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "accept_as_context"
    assert judgment.interior_assertion["result"] == "admitted (context replaced)"
    assert judgment.interior_assertion["telling_span"] == "told us on Tuesday"


def test_collective_informant_backed_by_a_telling_event_passes() -> None:
    """ "the procurement team told me…" — a collective is a PARTY, and a party
    can tell (ADR 0016)."""
    content = "the 225-other-scope team told me that budgets are frozen for this quarter."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant",
            informant_span="the 225-other-scope team",
            telling_span="told me",
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "accept_as_context"
    assert judgment.interior_assertion["result"] == "admitted (context replaced)"


def test_flat_rule_with_the_scope_name_as_span_fails_with_no_telling_event() -> None:
    content = "225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant", informant_span="225-other-scope", telling_span=content
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "no telling event is reported: state who told you" in judgment.reasoning
    assert judgment.interior_assertion["result"] == "declined (no telling event)"


def test_unaddressed_telling_verb_fails() -> None:
    """ "procurement says orders under 5k…" — "says" is not a listed telling
    verb form (only "say", never stemmed), so this fails the same way the
    flat rule does, with no addressee ever reached."""
    content = "225-other-scope says orders under 5k are auto-approved."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant", informant_span="225-other-scope", telling_span=content
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "no telling event is reported: state who told you" in judgment.reasoning


def test_stated_limit_a_real_telling_with_no_contributor_named_is_declined() -> None:
    """KNOWN LIMIT, pinned per Philis's ruling: "the procurement lead said X"
    is a REAL telling event — "said" is a listed verb — but names no one as
    the one told, so it is declined rather than guessed as addressed to the
    contributor."""
    content = "the 225-other-scope lead said budgets are frozen for this quarter."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant",
            informant_span="the 225-other-scope lead",
            telling_span="said budgets are frozen for this quarter",
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "the telling names no one it was told to" in judgment.reasoning
    assert judgment.interior_assertion["result"] == "declined (no telling event)"


def test_telling_span_not_occurring_verbatim_is_declined_as_invented() -> None:
    content = "225-other-scope told me on Tuesday that budgets are frozen for this quarter."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant",
            informant_span="225-other-scope",
            telling_span="mentioned to me last week",
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "invented informant" in judgment.reasoning
    assert judgment.interior_assertion["result"] == "declined (invented informant)"


def test_informant_with_no_telling_span_at_all_fails_closed_as_judge_failure() -> None:
    content = "225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="informant", informant_span="225-other-scope"
        ),  # no telling_span
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert judgment.judge_failure is True


# ---------------------------------------------------------------------------
# Review fix 2 — the structured `interior_assertion` field.
# ---------------------------------------------------------------------------


def test_interior_assertion_field_is_none_when_the_re_ask_never_fires() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _ordinary_accept("Nothing here names another scope.")
    judgment, _ = _judge(mock_client, "Nothing here names another scope.")
    assert judgment.interior_assertion is None


def test_interior_assertion_field_on_judge_failure_has_a_none_class() -> None:
    content = "225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="not-a-real-value"),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.interior_assertion == {
        "scopes": [OTHER.id],
        "class": None,
        "result": "judge failure",
    }


# ---------------------------------------------------------------------------
# Review fix 3 — Philis's ruling: conduct needs a witness-or-party act_span,
# verbatim, with a first-person marker surviving a leading attestation or
# perception frame strip.
# ---------------------------------------------------------------------------

# The six j4_interior conduct twins' own sentences (datasets/j4_interior/
# generate.py's DOMAINS), with the real scope name swapped for this test
# file's OTHER so the trigger fires on these fixtures.
_J4_INTERIOR_CONDUCT_TWINS = [
    "225-other-scope's agent sent back our arm-controller order twice this week "
    "asking for a second quote",
    "225-other-scope's maintenance crew had the server room HVAC switched off "
    "when we arrived on the last two first Sundays",
    "225-other-scope's review bot rejected our last three diagram PRs with a palette warning",
    "225-other-scope's agent declined our request to open an FX hedge for the launch on Tuesday",
    "the last two firmware builds we handed to 225-other-scope were installed "
    "at the depots on the following Wednesday",
    "225-other-scope's scanner flagged and quarantined our click-log export job yesterday",
]


def test_all_six_j4_interior_conduct_twins_pass() -> None:
    for content in _J4_INTERIOR_CONDUCT_TWINS:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _ordinary_accept(content),
            _reask_response(classification="conduct", act_span=content),
        ]
        judgment, _ = _judge(mock_client, content)
        assert judgment.decision == "accept_as_context", content
        assert judgment.interior_assertion["result"] == "admitted as judged", content


def test_joint_event_verb_with_party_satisfies_the_conduct_burden() -> None:
    """Addendum (bridge "after" run on j4_joint_verb, architect review
    round 3): a joint-event verb WITH a party ("As discussed with X") is
    conduct the contributor took part in too — the contributor is the
    implied counterpart, same ruling as the telling-event check. No
    separate first-person word is required; a bare "as discussed" with no
    party still fails."""
    content = (
        "As discussed with 225-other-scope in Tuesday's sync, we'll get two quotes "
        "before approving the purchase."
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(
            classification="conduct",
            act_span="As discussed with 225-other-scope in Tuesday's sync",
        ),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "accept_as_context"
    assert judgment.interior_assertion["result"] == "admitted as judged"

    content_bad = "As discussed, 225-other-scope only approves changes under budget."
    mock_client_bad = MagicMock()
    mock_client_bad.messages.create.side_effect = [
        _ordinary_accept(content_bad),
        _reask_response(classification="conduct", act_span="As discussed"),
    ]
    judgment_bad, _ = _judge(mock_client_bad, content_bad)
    assert judgment_bad.decision == "decline"
    assert "no observed act in the contributor's own dealings is stated" in judgment_bad.reasoning


def test_a_flat_rule_fails_the_conduct_burden() -> None:
    content = (
        "225-other-scope only approves hardware orders under 5,000 EUR without a second quote."
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="conduct", act_span=content),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "no observed act in the contributor's own dealings is stated" in judgment.reasoning


def test_the_own_role_frame_fails_the_conduct_burden() -> None:
    """The architect's own illustrative example — no comma after the role —
    must strip exactly as the comma'd form does."""
    content = "As eng-lead I can tell you 225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="conduct", act_span=content),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "no observed act in the contributor's own dealings is stated" in judgment.reasoning


def test_the_own_role_frame_with_a_comma_also_fails_the_conduct_burden() -> None:
    content = "As eng-lead, I can tell you 225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="conduct", act_span=content),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "no observed act in the contributor's own dealings is stated" in judgment.reasoning


def test_the_perception_frame_fails_the_conduct_burden() -> None:
    """ "I observed that X" is exactly the judge's own failure: an
    observation claimed over a policy, never a dealing."""
    content = "I observed that 225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="conduct", act_span=content),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"


def test_conduct_act_span_missing_fails_closed_as_judge_failure() -> None:
    content = "225-other-scope's agent rejected our request twice this week."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="conduct"),  # no act_span
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert judgment.judge_failure is True


# --- the three stated limits of the mechanical heuristic -------------------


def test_stated_limit_a_flat_rule_with_an_incidental_possessive_slips_through() -> None:
    """Known limit: "our orders" supplies a first-person marker even though
    the sentence states a flat RULE, not an observed act — left to the
    judge's own first-call verdict, per the architect."""
    content = "225-other-scope only approves our orders under 5k without a second quote."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="conduct", act_span=content),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "accept_as_context"


def test_stated_limit_b_real_conduct_with_no_first_person_marker_is_declined() -> None:
    """Known limit: genuine conduct with no explicit first-person tie to the
    contributor's own dealings is declined — the conservative, safe-side
    miss, per the architect."""
    content = "225-other-scope's agent sent the order back twice."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="conduct", act_span=content),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"


def test_stated_limit_c_real_conduct_under_a_perception_frame_is_declined() -> None:
    """Known limit, Philis: "I saw X" is real conduct, but the frame strip
    removes the only first-person marker — the reason tells the contributor
    to state the dealing instead ("our order")."""
    content = "I saw 225-other-scope's agent send the order back."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="conduct", act_span=content),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "no observed act in the contributor's own dealings is stated" in judgment.reasoning
