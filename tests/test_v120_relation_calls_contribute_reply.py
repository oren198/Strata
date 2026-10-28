"""v1.20 (#242 b): strata_contribute's reply carries the relation answer, filtered for the
contributor (spans verbatim in its own content, a failed re-ask cut to the exception class)."""

from __future__ import annotations

from unittest.mock import patch

from strata.scope_manager import ScopeManager, ScopeManagerJudgment
from tests.test_p1_acted_on_write_path import (  # noqa: F401 — reuse the module's own fixtures
    _contribute,
    mod,
)

CONTENT = "The pumps may stay running."


def _decline(cid: str, **over) -> ScopeManagerJudgment:  # noqa: ANN003
    return ScopeManagerJudgment(
        decision="decline",
        reasoning="declined",
        new_summary=None,
        inherited_relation={
            "contribution_id": cid,
            "kind": "exception",
            "verdict": "decline",
            "fallback": False,
            "inherited_id": "c_x",
            "origin": "g_arch",
            "reason": "answered exception; covers a subject",
            **over,
        },
    )


async def _contribute_declined(mod, extras: dict, **over):  # noqa: ANN001, ANN003, ANN202, F811
    def judge(self, **kw):  # noqa: ANN001, ANN003, ANN202
        cid = kw["new_contribution"].id
        self._relation_extras[(cid, "context")] = extras
        return _decline(cid, **over)

    with patch.object(ScopeManager, "judge", judge):
        return await mod.strata_contribute(
            scope_id="g_backend", content=CONTENT, proposed_classification="context"
        )


async def test_a_decline_reply_carries_the_relation_answer(mod) -> None:  # noqa: ANN001, F811
    out = await _contribute_declined(mod, {"marker": "may"})
    (relation,) = out["relation"]
    assert relation["call"] == "context" and relation["verdict"] == "decline"
    assert relation["marker"] == "may"
    assert relation["reason"] == "answered exception; covers a subject"


async def test_an_admit_with_no_re_ask_keeps_the_old_reply_shape(mod) -> None:  # noqa: ANN001, F811
    out = await _contribute(mod, content="A plain note.")
    assert "relation" not in out
    assert set(out) <= {"contribution_id", "judgment", "fleet_notice"}
    assert set(out["judgment"]) == {"decision", "reasoning", "summary_updated"}


async def test_a_span_not_in_the_content_is_null_in_the_reply_but_kept_in_the_store(
    mod,  # noqa: ANN001, F811
) -> None:
    out = await _contribute_declined(
        mod, {"instead_span": "may stay until 22:00", "occurrence_span": "may stay running"}
    )
    (relation,) = out["relation"]
    assert relation["instead_span"] is None
    assert relation["occurrence_span"] == "may stay running"  # verbatim in CONTENT's own words
    (stored,) = mod._record_store.get_relation_calls(out["contribution_id"])
    assert stored["instead_span"] == "may stay until 22:00"


async def test_a_failed_re_ask_reason_shows_only_the_exception_class(mod) -> None:  # noqa: ANN001, F811
    long = "ValueError: " + "raw judge output (with parens); " * 20
    out = await _contribute_declined(mod, {}, reason=f"re-ask failed ({long}); declined")
    (relation,) = out["relation"]
    assert relation["reason"] == "re-ask failed (ValueError)"


async def test_read_contribution_nulls_a_span_not_in_the_contribution(mod) -> None:  # noqa: ANN001, F811
    out = await _contribute_declined(mod, {"instead_span": "may stay until 22:00"})
    read = await mod.strata_read_contribution(contribution_id=out["contribution_id"])
    assert read["relation"][0]["instead_span"] is None
    (stored,) = mod._record_store.get_relation_calls(out["contribution_id"])
    assert stored["instead_span"] == "may stay until 22:00"


async def test_publish_nulls_a_span_not_in_the_publication_text(mod) -> None:  # noqa: ANN001, F811
    from tests.test_v120_relation_calls_publish_reply import (  # noqa: PLC0415
        OUTCOME,
        _act,
        _outcome,
    )

    act_id = _act(mod, relations=[{**OUTCOME, "instead_span": "the judge's invented span"}])
    with patch.object(mod, "propose_publish", return_value=_outcome(act_id)):
        result = await mod.strata_publish(
            content="Hotfix builds don't need the full suite.", kind="context", anchors=["x"]
        )
    assert result["relation"][0]["instead_span"] is None
    (stored,) = mod._record_store.get_publication_relation_calls(act_id)
    assert stored["instead_span"] == "the judge's invented span"
