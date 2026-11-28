"""v1.20 (#242 b): strata_read_contribution carries the relation answer behind a verdict."""

from __future__ import annotations

from unittest.mock import patch

from strata.scope_manager import ScopeManagerJudgment
from tests.test_p1_acted_on_write_path import (  # noqa: F401 — reuse the module's own fixtures
    _contribute,
    mod,
)


def _declined_with_relation() -> ScopeManagerJudgment:
    return ScopeManagerJudgment(
        decision="decline",
        reasoning="declined",
        new_summary=None,
        inherited_relation={
            "contribution_id": "",
            "kind": "unrelated",
            "verdict": "decline",
            "fallback": False,
            "inherited_id": "c_x",
            "origin": "g_arch",
            "reason": "answered unrelated; covers a subject and (normative marker 'may')",
        },
    )


async def test_a_declined_contribution_reads_back_its_relation_answer(mod) -> None:  # noqa: ANN001, F811
    judgment = _declined_with_relation()

    def judge(self, **kw):  # noqa: ANN001, ANN003, ANN202
        cid = kw["new_contribution"].id
        return judgment.model_copy(
            update={"inherited_relation": {**judgment.inherited_relation, "contribution_id": cid}}
        )

    with patch("strata.scope_manager.ScopeManager.judge", judge):
        out = await mod.strata_contribute(
            scope_id="g_backend",
            content="The pumps may stay running.",
            proposed_classification="context",
        )
    read = await mod.strata_read_contribution(contribution_id=out["contribution_id"])
    (relation,) = read["relation"]
    assert relation["call"] == "context" and relation["answer_kind"] == "unrelated"
    assert relation["verdict"] == "decline" and relation["inherited_id"] == "c_x"
    assert "normative marker 'may'" in relation["reason"]


async def test_a_contribution_with_no_re_ask_has_no_relation_key(mod) -> None:  # noqa: ANN001, F811
    out = await _contribute(mod, content="A plain note.")
    read = await mod.strata_read_contribution(contribution_id=out["contribution_id"])
    assert "relation" not in read
