"""#138 (v1.22): the internal attribution re-ask switch and the extra-call counter.

``_ATTRIBUTION_REASK_ARM`` ("C" default, "A" the comparison arm) is a module constant for the live
A/B, never a setting. ``ScopeManager.attribution_reask_counts`` counts the accepts the attribution
check looked at (in a scope with operator directives) and the extra judge calls it made, so the A/B
can report cost per arm. This file holds only what is true under EVERY arm and stem floor; the arm
A and arm C behaviours have their own files, so reverting either commit leaves this suite green.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from strata import scope_manager as sm
from strata.scope_manager import ScopeManager
from tests.test_scope_manager import NEW_CONTRIBUTION, SCOPE, STRATUM
from tests.test_scope_manager import _fake_response as fake_response
from tests.test_v19_138_attribution import ECHO, EMPTY, MEMORY, UNRELATED, _accept, _judge

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


def test_the_default_arm_is_c() -> None:
    assert sm._ATTRIBUTION_REASK_ARM == "C"


def test_an_unknown_arm_is_refused_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "_ATTRIBUTION_REASK_ARM", "B")
    with pytest.raises(ValueError, match="attribution re-ask arm"):
        _judge(_accept(UNRELATED), content=UNRELATED)


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
