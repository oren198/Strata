"""v1.20 item 7(d): condensation lineage and the order-inversion check.

The judge reports, per line of its new context, the notes the line rests on; the engine checks
those ids, compares old and new lineage, and records whether the drops followed the order the
judge was told. Measure only: a write it cannot compare is `unmeasured` with a reason, counted
apart from "measured, no inversion"."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from strata.app import run_contribution
from strata.fleet_config import FleetConfig
from strata.lineage import (
    LINEAGE_FROM_FRACTION,
    LineageEntry,
    LineageLineView,
    context_hash,
    context_lines,
    lineage_offered,
    order_check,
    record_context_write,
    render_lineage_block,
    validate_lineage,
)
from strata.migrator import run_migrations
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import ScopeManager
from strata.summary_store import ScopeSummary, SummaryStore
from tests._judge_prompt_corpus import capture_corpus
from tests.test_scope_manager import _fake_response

# -- pure pieces ------------------------------------------------------------------------


def test_lines_are_the_non_blank_lines_and_the_hash_ignores_blank_ones() -> None:
    assert context_lines("a\n\n b \n") == ["a", " b "]
    assert context_hash("a\n\nb") == context_hash("a\nb\n") != context_hash("a\nc")


@pytest.mark.parametrize(
    ("context", "budget", "mode", "acted_on", "expected"),
    [
        ("w " * 50, 100, "ordinary", False, True),  # exactly the threshold
        ("w " * 49, 100, "ordinary", False, False),
        ("w " * 80, 100, "input_change_refresh", False, True),
        ("w " * 80, 100, "ordinary", True, False),  # never on an acted_on call
        ("w " * 80, 100, "publication", False, False),
        ("", 100, "ordinary", False, False),
    ],
)
def test_the_lineage_is_offered_only_near_the_budget(
    context, budget, mode, acted_on, expected
) -> None:  # noqa: ANN001
    assert LINEAGE_FROM_FRACTION == 0.5
    assert lineage_offered(context, budget, mode=mode, acted_on=acted_on) is expected


def test_the_first_offered_call_shows_the_rule_without_per_line_metadata() -> None:
    block = render_lineage_block(None, "one\ntwo")
    assert "unexamined context, oldest first" in block and "context_lineage" in block
    assert "[1]" not in block  # no stored lineage: the per-line metadata is omitted, not "?"


def test_with_stored_lineage_each_line_names_its_notes_dates_and_states() -> None:
    view = [
        LineageLineView(
            1, (("c_a", "2026-10-01", "unexamined"), ("c_b", "2026-10-02", "examined"))
        ),
        LineageLineView(2, ()),
    ]
    block = render_lineage_block(view, "x\ny")
    assert "[1] rests on c_a (2026-10-01, unexamined), c_b (2026-10-02, examined)" in block
    assert "[2] rests on no admitted note" in block


ALLOWED = {"c_a", "c_b", "c_c"}


def _ok(raw, ctx="l1\nl2\nl3"):  # noqa: ANN001, ANN202
    return validate_lineage(raw, ctx, allowed_ids=ALLOWED, foreign=lambda i: i == "c_other")


def test_a_valid_lineage_with_a_merge_and_an_unattributed_line() -> None:
    entries, reason = _ok([{"line": 1, "sources": ["c_a", "c_b"]}, {"line": 3, "sources": []}])
    assert reason is None
    assert entries == [
        LineageEntry(1, ("c_a", "c_b")),  # a merge keeps both ids
        LineageEntry(2, ()),  # a line the judge did not mention is unattributed
        LineageEntry(3, ()),  # a line resting on no admitted note
    ]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (None, "absent"),
        ("lines", "malformed"),
        ([1], "malformed"),
        ([{"line": "1", "sources": []}], "malformed"),
        ([{"line": True, "sources": []}], "malformed"),
        ([{"line": 1, "sources": "c_a"}], "malformed"),
        ([{"line": 1, "sources": [3]}], "malformed"),
        ([{"line": 1, "sources": []}, {"line": 1, "sources": []}], "malformed"),
        ([{"line": 0, "sources": []}], "bad_line"),
        ([{"line": 4, "sources": []}], "bad_line"),
        ([{"line": 1, "sources": ["c_nope"]}], "invalid_id"),
        ([{"line": 1, "sources": ["c_a", "c_other"]}], "foreign_id"),
    ],
)
def test_an_unusable_lineage_makes_the_write_unmeasured_with_a_reason(raw, reason) -> None:  # noqa: ANN001
    entries, got = _ok(raw)
    assert entries is None and got == reason


def _info(table):  # noqa: ANN001, ANN202
    return lambda note_id: table[note_id]


def test_dropping_a_newer_unexamined_note_over_an_older_kept_one_is_an_inversion() -> None:
    old = [LineageEntry(1, ("c_old",)), LineageEntry(2, ("c_new",))]
    new = [LineageEntry(1, ("c_old",))]
    info = _info({"c_old": (1, "2026-10-01"), "c_new": (1, "2026-10-09")})
    check = order_check(old, new, info)
    assert check["dropped"] == ["c_new"] and check["kept"] == ["c_old"]
    assert check["inversions_recency"] == [["c_new", "c_old"]] and check["inversions_n"] == 1


def test_dropping_the_oldest_unexamined_note_is_not_an_inversion() -> None:
    old = [LineageEntry(1, ("c_old",)), LineageEntry(2, ("c_new",))]
    new = [LineageEntry(1, ("c_new",))]
    info = _info({"c_old": (1, "2026-10-01"), "c_new": (1, "2026-10-09")})
    assert order_check(old, new, info)["inversions_n"] == 0


def test_dropping_an_examined_or_unexamined_note_while_keeping_a_superseded_one() -> None:
    old = [LineageEntry(1, ("c_sup",)), LineageEntry(2, ("c_ex",)), LineageEntry(3, ("c_un",))]
    new = [LineageEntry(1, ("c_sup",))]
    info = _info({"c_sup": (0, "2026-10-01"), "c_ex": (2, "2026-10-02"), "c_un": (1, "2026-10-03")})
    check = order_check(old, new, info)
    assert sorted(p[0] for p in check["inversions_tier"]) == ["c_ex", "c_un"]


def test_a_merged_line_keeps_every_one_of_its_notes_alive() -> None:
    old = [LineageEntry(1, ("c_a", "c_b"))]
    new = [LineageEntry(1, ("c_a", "c_b"))]
    check = order_check(old, new, _info({"c_a": (1, "1"), "c_b": (1, "2")}))
    assert check["dropped"] == [] and check["inversions_n"] == 0


def test_an_unattributed_line_takes_part_in_no_comparison() -> None:
    check = order_check(
        [LineageEntry(1, ())], [LineageEntry(1, ()), LineageEntry(2, ())], _info({})
    )
    assert check["unattributed"] == 2 and check["inversions_n"] == 0


# -- the store and the chain ------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path):  # noqa: ANN201
    db = str(tmp_path / "record.db")
    run_migrations(db)
    with RecordStore(db) as record_store:
        yield record_store


def _write(store, prev, new, raw=None, *, offered=True, judged=("c_j",), allowed=None, info=None):  # noqa: ANN001, ANN202
    return record_context_write(
        scope_id="g_a",
        previous_context=prev,
        new_context=new,
        summary_version=1,
        raw_lineage=raw,
        offered=offered,
        judged_ids=list(judged),
        allowed_ids=allowed if allowed is not None else {"c_j", "c_1", "c_2", "c_3"},
        foreign=lambda _i: False,
        info=info or (lambda i: (1, i)),
        record_store=store,
    )


def test_a_write_that_leaves_the_context_alone_records_nothing_and_keeps_the_chain(store) -> None:  # noqa: ANN001
    assert _write(store, "same", "same") is None
    assert store.condensation_order_metrics()["writes"] == 0


def test_the_first_offered_write_is_no_previous_and_stored_the_second_is_measured(store) -> None:  # noqa: ANN001
    # a scope that already has context but no lineage: the first offered write bootstraps
    first = _write(
        store,
        "old one\nold two",
        "kept\nmerged",
        [{"line": 1, "sources": ["c_1"]}, {"line": 2, "sources": ["c_2", "c_3"]}],
    )
    assert first["measured"] == 0 and first["reason"] == "no_previous"
    assert store.context_lineage_for("g_a", context_hash("kept\nmerged")) is not None
    # the next write compares against that base: c_1 and c_3 and c_2 drop, c_new arrives
    second = _write(
        store,
        "kept\nmerged",
        "fresh",
        [{"line": 1, "sources": ["c_j"]}],
        info=lambda i: (1, {"c_1": "1", "c_2": "2", "c_3": "3", "c_j": "9"}[i]),
    )
    assert second["measured"] == 1 and second["reason"] is None
    assert second["dropped_n"] == 3 and second["kept_n"] == 0 and second["inversions_n"] == 0
    metrics = store.condensation_order_metrics()
    assert metrics["writes"] == 2 and metrics["measured"] == 1 and metrics["coverage"] == 0.5
    assert metrics["unmeasured_by_reason"] == {"no_previous": 1}


def test_a_measured_write_counts_its_inversions_apart_from_unmeasured_ones(store) -> None:  # noqa: ANN001
    _write(store, "x", "a\nb", [{"line": 1, "sources": ["c_1"]}, {"line": 2, "sources": ["c_2"]}])
    row = _write(
        store,
        "a\nb",
        "a",
        [{"line": 1, "sources": ["c_1"]}],
        info=lambda i: (1, {"c_1": "2026-10-01", "c_2": "2026-10-09"}[i]),
    )
    assert row["measured"] == 1 and row["inversions_n"] == 1
    _write(store, "a", "z", None, offered=False)  # not offered: unmeasured, never "no inversion"
    metrics = store.condensation_order_metrics()
    assert metrics["measured_with_inversion"] == 1 and metrics["inversions"] == 1
    assert metrics["unmeasured_by_reason"]["not_offered"] == 1
    assert metrics["coverage"] == pytest.approx(1 / 3)


def test_a_write_without_lineage_breaks_the_chain(store) -> None:  # noqa: ANN001
    _write(store, "x", "a\nb", [{"line": 1, "sources": ["c_1"]}, {"line": 2, "sources": []}])
    broken = _write(store, "a\nb", "c\nd", None, offered=False)
    assert broken["reason"] == "not_offered"
    after = _write(store, "c\nd", "e", [{"line": 1, "sources": ["c_1"]}])
    assert after["reason"] == "no_previous"  # nothing is stored for "c\nd"


def test_an_invalid_lineage_stores_nothing_and_says_why(store) -> None:  # noqa: ANN001
    row = _write(store, "x", "a", [{"line": 1, "sources": ["c_ghost"]}])
    assert row["measured"] == 0 and row["reason"] == "invalid_id"
    assert store.context_lineage_for("g_a", context_hash("a")) is None


def test_an_engine_appended_line_carries_the_old_lineage(store) -> None:  # noqa: ANN001
    _write(store, "x", "a\nb", [{"line": 1, "sources": ["c_1"]}, {"line": 2, "sources": ["c_2"]}])
    row = _write(store, "a\nb", "a\nb\nheld note", None, offered=False, judged=("c_3",))
    assert row["measured"] == 1 and row["dropped_n"] == 0
    carried = store.context_lineage_for("g_a", context_hash("a\nb\nheld note"))
    assert [e.sources for e in carried] == [("c_1",), ("c_2",), ("c_3",)]


def test_an_engine_append_that_does_not_fit_is_counted_as_a_line_mismatch(store) -> None:  # noqa: ANN001
    _write(store, "x", "a\nb", [{"line": 1, "sources": ["c_1"]}])  # stored for "a\nb"
    row = _write(store, "a\nb", "a\nb\nm\nn", None, offered=False)  # two lines added
    assert row["measured"] == 0 and row["reason"] == "engine_line_mismatch"


def test_an_empty_context_is_a_valid_chain_base(store) -> None:  # noqa: ANN001
    row = _write(store, None, "first note", None, offered=False, judged=("c_1",))
    assert row["measured"] == 1 and row["dropped_n"] == 0


# -- the judge call and the write boundary ----------------------------------------------

_FLEET = {
    "strata": [{"id": "L0", "name": "executive", "ordinal": 0}],
    "scopes": [{"id": "g_sv", "name": "SV", "stratum_id": "L0"}],
    "edges": [],
}


@pytest.fixture
def world(tmp_path: Path, store):  # noqa: ANN001, ANN201
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.dump(_FLEET), encoding="utf-8")
    fleet = FleetConfig.load(path)
    summaries = SummaryStore(str(tmp_path / "summaries"))
    return fleet, store, summaries


def _who() -> ContributorRef:
    return ContributorRef(scope_id="g_sv", skill=None, session_id="s1", ts="2026-10-01T00:00:00Z")


def _contribute(world, client, content, *, budget=10):  # noqa: ANN001, ANN202
    fleet, store, summaries = world
    return run_contribution(
        scope=fleet.get_scope("g_sv"),
        stratum=fleet.strata[0],
        content=content,
        proposed_classification="context",
        subject=None,
        supersedes=None,
        contributor=_who(),
        fleet=fleet,
        record_store=store,
        summary_store=summaries,
        scope_manager=ScopeManager(client=client),
        summary_max_words=budget,
    )


def _answer(new_context, lineage=None):  # noqa: ANN001, ANN202
    body = {
        "decision": "accept_as_context",
        "reasoning": "A note.",
        "directive_ops": [],
        "new_context": new_context,
    }
    if lineage is not None:
        body["context_lineage"] = lineage
    return _fake_response(body)


def test_two_successive_over_threshold_writes_on_a_fresh_scope_bootstrap_then_measure(
    world,
) -> None:  # noqa: ANN001
    fleet, store, summaries = world
    summaries.write(
        "g_sv",
        ScopeSummary(
            scope_id="g_sv",
            directives=[],
            context="old a b c d e f",
            updated_at="2026-10-01T00:00:00Z",
        ),
    )
    # write 1: over half of the 10-word budget, so the field is offered; the model omits it
    first = _contribute(world, client_with(MagicMock(), _answer("one two three four five")), "n1")
    assert store.condensation_order_metrics()["unmeasured_by_reason"] == {"absent": 1}
    # write 2: lineage that validates but nothing stored to compare with: no_previous, stored
    second = _contribute(
        world,
        client_with(
            MagicMock(),
            _answer(
                "kept line here\nsecond line c d e",
                [{"line": 1, "sources": [first.contribution_id]}, {"line": 2, "sources": []}],
            ),
        ),
        "n2",
    )
    metrics = store.condensation_order_metrics()
    assert metrics["unmeasured_by_reason"] == {"absent": 1, "no_previous": 1}
    assert store.context_lineage_for("g_sv", context_hash(summaries.read("g_sv").context))
    # write 3: the base exists now, so this one is measured (first's note dropped, nothing newer)
    _contribute(
        world,
        client_with(
            MagicMock(),
            _answer("kept line here", [{"line": 1, "sources": [second.contribution_id]}]),
        ),
        "n3",
    )
    metrics = store.condensation_order_metrics()
    assert metrics["writes"] == 3 and metrics["measured"] == 1
    assert metrics["unmeasured_by_reason"] == {"absent": 1, "no_previous": 1}
    assert metrics["coverage"] == pytest.approx(1 / 3)


def client_with(client, *responses):  # noqa: ANN001, ANN201
    client.messages.create.side_effect = list(responses)
    return client


def test_the_offered_call_carries_the_block_and_the_tool_property_before_the_summary(world) -> None:  # noqa: ANN001
    world[2].write(
        "g_sv",
        ScopeSummary(scope_id="g_sv", directives=[], context="w1 w2 w3 w4 w5 w6", updated_at="t"),
    )
    client = client_with(MagicMock(), _answer("w1 w2"))
    _contribute(world, client, "another note")
    kwargs = client.messages.create.call_args_list[0].kwargs
    message = kwargs["messages"][0]["content"]
    assert "CONTEXT LINES" in message
    assert message.index("CONTEXT LINES") < message.index("CURRENT SUMMARY\n---")
    assert "context_lineage" in kwargs["tools"][0]["input_schema"]["properties"]


def test_a_small_context_gets_exactly_the_old_message_and_the_old_tool(world) -> None:  # noqa: ANN001
    client = client_with(MagicMock(), _answer("tiny"))
    _contribute(world, client, "a note", budget=500)
    kwargs = client.messages.create.call_args_list[0].kwargs
    assert "CONTEXT LINES" not in kwargs["messages"][0]["content"]
    assert "context_lineage" not in kwargs["tools"][0]["input_schema"]["properties"]


def test_no_corpus_prompt_carries_the_block() -> None:
    for name, text in capture_corpus(acted_on=None).items():
        assert "CONTEXT LINES" not in text, name
