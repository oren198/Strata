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
    """Only an ACCEPT triggers — a decline has nothing to verify."""
    mock_client = MagicMock()
    content = "225-other-scope only approves changes under budget."
    mock_client.messages.create.return_value = _fake_response(
        {
            "decision": "decline",
            "reasoning": "Manufactured attribution.",
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
        _reask_response(classification="informant", informant_span="Nobody Real"),
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
            classification="informant", informant_span="Jordan Lee from 225-other-scope"
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
            classification="informant", informant_span="Jordan Lee from 225-other-scope"
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
        _reask_response(classification="informant", informant_span="on-call-engineer"),
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
        _reask_response(classification="informant", informant_span="g_225reporter"),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "own scope is not an informant" in judgment.reasoning


def test_informant_bare_first_person_is_declined_as_invented() -> None:
    content = "I can tell you 225-other-scope only approves changes under budget."
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _ordinary_accept(content),
        _reask_response(classification="informant", informant_span="I"),
    ]
    judgment, _ = _judge(mock_client, content)
    assert judgment.decision == "decline"
    assert "first-person reference is not an informant" in judgment.reasoning


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
