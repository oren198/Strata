"""v1.20 (#242 b): strata_publish / strata_restore carry the relation answer when the #243
re-ask fired for the act, and are byte-identical otherwise."""

from __future__ import annotations

from unittest.mock import patch

from strata.publication import PublicationOutcome
from strata.record_store import ContributorRef
from tests.test_p1_acted_on_write_path import mod  # noqa: F401 — the module's fixture

OUTCOME = {
    "contribution_id": "publication-item-ab",
    "kind": "exception",
    "verdict": "decline",
    "fallback": False,
    "inherited_id": "c_suite",
    "origin": "g_org",
    "reason": "an exception: states what happens instead of the rule",
    "call": "context",
    "instead_span": "don't need the full suite",
    "marker": "normative marker 'don't need'",
}


def _act(mod, *, relations) -> str:  # noqa: ANN001, F811
    store = mod._record_store
    act = store.append_publication_act(
        scope_id="g_backend",
        act="publish",
        kind="context",
        content="Hotfix builds don't need the full suite.",
        subject=None,
        anchors=["subject:release"],
        withdraws=None,
        trigger=None,
        proposer=ContributorRef(
            scope_id="g_backend", skill="s", session_id="sess_test", ts="2026-10-26T00:00:00Z"
        ),
    )
    store.record_publication_judgment(
        act_id=act.id, decision="decline", judged_by="scope-manager", relations=relations
    )
    return act.id


def _outcome(act_id: str, act: str = "publish") -> PublicationOutcome:
    return PublicationOutcome(
        act_id=act_id, act=act, decision="decline", reasoning="no", artifact_updated=False
    )


async def test_strata_publish_carries_relation_when_the_re_ask_fired(mod) -> None:  # noqa: ANN001, F811
    act_id = _act(mod, relations=[OUTCOME])
    with patch.object(mod, "propose_publish", return_value=_outcome(act_id)):
        result = await mod.strata_publish(
            content="Hotfix builds don't need the full suite.", kind="context", anchors=["x"]
        )
    (row,) = result["relation"]
    assert row["call"] == "context" and row["answer_kind"] == "exception"
    assert row["instead_span"] == "don't need the full suite" and row["verdict"] == "decline"
    # the same shape strata_read_contribution uses
    assert set(row) == {
        "call",
        "answer_kind",
        "inherited_id",
        "origin",
        "occurrence_span",
        "instead_span",
        "requirement_span",
        "verdict",
        "fallback",
        "reason",
        "marker",
    }


async def test_strata_publish_is_unchanged_when_no_re_ask_fired(mod) -> None:  # noqa: ANN001, F811
    act_id = _act(mod, relations=[])
    with patch.object(mod, "propose_publish", return_value=_outcome(act_id)):
        result = await mod.strata_publish(content="A note.", kind="context", anchors=["x"])
    assert set(result) == {"act_id", "judgment"}
    assert set(result["judgment"]) == {"decision", "reasoning", "artifact_updated"}


async def test_strata_restore_carries_relation_and_is_otherwise_unchanged(mod) -> None:  # noqa: ANN001, F811
    with_row = _act(mod, relations=[OUTCOME])
    without = _act(mod, relations=[])
    with patch.object(mod, "propose_restore", return_value=_outcome(with_row, "restore")):
        has = await mod.strata_restore(item_id="pub_x")
    with patch.object(mod, "propose_restore", return_value=_outcome(without, "restore")):
        none = await mod.strata_restore(item_id="pub_x")
    assert has["relation"][0]["verdict"] == "decline"
    assert set(none) == {"act_id", "judgment"}


async def test_strata_withdraw_never_carries_relation(mod) -> None:  # noqa: ANN001, F811
    act_id = _act(mod, relations=[OUTCOME])
    with patch.object(mod, "propose_withdraw", return_value=_outcome(act_id, "withdraw")):
        result = await mod.strata_withdraw(item_id="pub_x")
    assert "relation" not in result
