"""Issue #145: one public definition of the accepted verdicts.

The constant is the set of non-decline values on the judgment ``decision``
Literal. Adding a verdict to that Literal without adding it to the constant
fails here, instead of quietly dropping the new verdict out of every count.
"""

from __future__ import annotations

from typing import get_args, get_type_hints

from strata import ACCEPTED_DECISIONS
from strata.record_store import Judgment
from strata.scope_manager import BatchVerdict, ScopeManagerJudgment


def test_accepted_decisions_are_the_non_decline_judgment_verdicts() -> None:
    assert isinstance(ACCEPTED_DECISIONS, frozenset)
    for judgment_cls in (ScopeManagerJudgment, BatchVerdict, Judgment):
        values = get_args(get_type_hints(judgment_cls)["decision"])
        assert "decline" in values
        non_decline = frozenset(value for value in values if value != "decline")
        assert non_decline == ACCEPTED_DECISIONS
