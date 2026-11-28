"""#138 (v1.22): the extra-call counter, and the shipped single behaviour (arm C).

``ScopeManager.attribution_reask_counts`` counts the accepts the attribution check looked at (in
a scope with operator directives) and the extra judge calls it made. The A/B arm switch is gone:
the engine re-asks only when it can match an admitted text to an operator directive's subject (two
shared stems) or the judge cites the id; an unrelated accept gets no re-ask.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

from strata import scope_manager as sm
from strata.scope_manager import ScopeManager
from tests.test_scope_manager import NEW_CONTRIBUTION, SCOPE, STRATUM
from tests.test_scope_manager import _fake_response as fake_response
from tests.test_v19_138_attribution import ECHO, EMPTY, MEMORY, UNRELATED, _accept

DECLINE = {"decision": "decline", "reasoning": "No.", "directive_ops": [], "new_context": None}


def _text(call) -> str:  # noqa: ANN001
    content = call.kwargs["messages"][-1]["content"]
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))


def _run(manager: ScopeManager, content: str, memory=MEMORY) -> None:  # noqa: ANN001
    contribution = replace(NEW_CONTRIBUTION, content=content, proposed_classification="context")
    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=EMPTY,
        recent_contributions=[],
        new_contribution=contribution,
        operator_memory=memory,
    )


def _script(*answers: dict) -> MagicMock:
    """A client that answers the judge's calls in order (repeating the last one when they run out)
    and the #242 context re-ask, which operator memory also triggers, with 'unrelated'."""
    pending = list(answers)

    def create(**kwargs):  # noqa: ANN003, ANN202
        if kwargs["tool_choice"]["name"] == "classify_inherited_relation":
            return fake_response({"kind": "unrelated", "inherited_id": "op", "reasoning": "r"})
        return fake_response(pending.pop(0) if len(pending) > 1 else pending[0])

    client = MagicMock()
    client.messages.create.side_effect = create
    return client


def test_there_is_no_arm_switch_and_an_unrelated_accept_gets_no_reask() -> None:
    assert not hasattr(sm, "_ATTRIBUTION_REASK_ARM")
    assert not hasattr(sm, "_attribution_reask_arm")
    manager = ScopeManager(client=_script(_accept(UNRELATED)))
    _run(manager, UNRELATED)  # operator memory in scope, the text echoes none of it
    assert manager.attribution_reask_counts == {"accepts_checked": 1, "reasks_fired": 0}


def test_the_counter_counts_checked_accepts_and_fired_reasks() -> None:
    manager = ScopeManager(client=_script(_accept(UNRELATED)))
    assert manager.attribution_reask_counts == {"accepts_checked": 0, "reasks_fired": 0}
    _run(manager, UNRELATED)  # an accept that echoes nothing: checked, no re-ask
    assert manager.attribution_reask_counts == {"accepts_checked": 1, "reasks_fired": 0}
    manager2 = ScopeManager(client=_script(_accept(ECHO), _accept(ECHO)))
    _run(manager2, ECHO)  # an echo of the operator directive: checked and one re-ask
    assert manager2.attribution_reask_counts == {"accepts_checked": 1, "reasks_fired": 1}


def test_the_counter_ignores_declines_and_scopes_without_operator_memory() -> None:
    manager = ScopeManager(client=_script(DECLINE, _accept(UNRELATED)))
    _run(manager, UNRELATED)  # a decline
    _run(manager, UNRELATED, memory=[])  # an accept, but no operator directive in scope
    assert manager.attribution_reask_counts == {"accepts_checked": 0, "reasks_fired": 0}
