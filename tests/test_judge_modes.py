"""The two judge modes (ADR 0014 D2, ADR 0015 D6, implementation pin 6).

``amendment_context_only`` was a bool: refresh or not. ADR 0014 split
"refresh" in two; ADR 0015 D1 deleted the splice, and with it the third mode,
leaving the pair that genuinely differ in what the judge is allowed to do:

- ``ordinary`` — a contribution arrived; every op is available.
- ``input_change_refresh`` — ADR 0014 D2's reactive re-judgement. It admits
  nothing (amended at the 1.11.0 gate, #198): the changed input is already
  composed for every reader, so neither the notice's bytes (``append``) nor
  the judge's own words about it (``publish``) may enter under this scope's
  name. On a refresh whose events are all additions the amendment's
  ``new_context`` is dropped too (#198 third form).

Plus ``context_sources`` (ADR 0014 D3): the judge declares which published
item ids its ``new_context`` rests on. Record, never trigger — but it has to
be ASKED for, in the tool schema and in the prompt, or no judge ever declares
anything.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from strata.fleet_config import Scope, Stratum
from strata.record_store import Contribution, ContributorRef
from strata.scope_manager import (
    JUDGE_BATCH_TOOL,
    JUDGE_TOOL,
    ScopeManager,
    ScopeManagerBatchJudgment,
    ScopeManagerJudgment,
    _build_judge_preamble,
)
from strata.summary_store import ScopeSummary

SCOPE = Scope(id="g_child", name="Child", stratum_id="L1")
STRATUM = Stratum(id="L1", name="Team", ordinal=1)


def _summary() -> ScopeSummary:
    return ScopeSummary(
        scope_id="g_child",
        directives=[],
        context="the child's own working note",
        updated_at="2026-09-01T10:00:00+00:00",
        version=1,
    )


def _contribution(content: str = "[input change]") -> Contribution:
    return Contribution(
        id="c_refresh",
        scope_id="g_child",
        content=content,
        proposed_classification="context",
        subject="manager-refresh",
        supersedes=None,
        contributor=ContributorRef(
            scope_id="g_child",
            skill="scope-manager",
            session_id="refresh",
            ts="2026-09-05T00:00:00+00:00",
        ),
        created_at="2026-09-05T00:00:00+00:00",
    )


def _change_event(item_id: str = "p_1", kind: str = "withdrawn") -> SimpleNamespace:
    return SimpleNamespace(
        id="ce_1",
        change_id="chg_1",
        scope_id="g_child",
        item_id=item_id,
        kind=kind,
        before="Ship behind a flag.",
        after=None,
        hop=0,
    )


def _preamble(**kwargs) -> str:  # noqa: ANN003
    return _build_judge_preamble(
        scope=SCOPE,
        stratum=STRATUM,
        ancestor_directives=None,
        current_summary=_summary(),
        recent_contributions=[],
        judged_contribution_ids=[],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The rendered prompt
# ---------------------------------------------------------------------------


def test_ordinary_mode_renders_no_refresh_block():
    text = _preamble(mode="ordinary")
    assert "INPUT-CHANGE REFRESH" not in text


def test_the_manager_refresh_block_is_gone_from_every_mode():
    """ADR 0015 D1: the splice's instruction went with the splice."""
    assert "MANAGER REFRESH" not in _preamble(mode="ordinary")
    assert "MANAGER REFRESH" not in _preamble(
        mode="input_change_refresh", input_changes=[_change_event()]
    )


def test_input_change_refresh_renders_its_own_block():
    text = _preamble(mode="input_change_refresh", input_changes=[_change_event()])
    assert "INPUT-CHANGE REFRESH" in text
    assert "MANAGER REFRESH:" not in text


def test_input_change_refresh_block_states_no_admitting_op_and_no_restatement():
    """#198: a refresh reconciles the scope's OWN memory. It admits nothing —
    the changed input is already composed for every reader — and it never
    writes the changed input into ``new_context``."""
    text = _preamble(mode="input_change_refresh", input_changes=[_change_event()])
    for op in ("retire", "supersede", "withdraw_published"):
        assert op in text
    assert "`append` and `publish` are dropped" in text
    assert "restate" in text.lower()


def test_input_change_refresh_never_restates_a_parents_context():
    """ADR 0013 D1 holds on this path too — say so in the block itself."""
    text = _preamble(mode="input_change_refresh", input_changes=[_change_event()])
    assert "parent" in text.lower()


def test_pending_change_events_render_as_an_input_changes_block():
    text = _preamble(mode="input_change_refresh", input_changes=[_change_event()])
    assert "INPUT CHANGES" in text
    assert "p_1" in text
    assert "withdrawn" in text
    assert "Ship behind a flag." in text


def test_input_changes_block_is_omitted_when_there_are_none():
    assert "INPUT CHANGES" not in _preamble(mode="ordinary")


# ---------------------------------------------------------------------------
# context_sources — asked for, not merely accepted (ADR 0014 D3)
# ---------------------------------------------------------------------------


def test_judge_tool_schema_asks_for_context_sources():
    field = JUDGE_TOOL["input_schema"]["properties"]["context_sources"]
    assert "published item ids" in field["description"]
    assert "new_context" in field["description"]


def test_batch_judge_tool_inherits_context_sources():
    assert "context_sources" in JUDGE_BATCH_TOOL["input_schema"]["properties"]


def test_system_prompt_explains_context_sources():
    from strata.scope_manager import _SYSTEM_PROMPT

    assert "context_sources" in _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Op handling per mode
# ---------------------------------------------------------------------------


def _tool_block(**payload) -> SimpleNamespace:  # noqa: ANN003
    return SimpleNamespace(input=payload)


def _parse(mode: str, ops=({"op": "append"},)):  # noqa: ANN001, ANN201
    return ScopeManager._parse_judgment(
        scope=SCOPE,
        tool_use_block=_tool_block(
            decision="accept_as_directive",
            reasoning="The withdrawn input no longer supports this claim.",
            directive_ops=list(ops),
            new_context="Reconciled.",
            withdraw_published=["p_1"],
        ),
        current_summary=_summary(),
        new_contribution=_contribution(),
        mode=mode,
    )


def test_input_change_refresh_drops_every_admitting_op():
    """#198: on a refresh the only contribution in the batch is the change
    notice, and the material it names is already composed for the reader
    (ADR 0013/0015). Admitting it again — as the notice's bytes (``append``)
    or as the judge's own words (``publish``) — manufactures a second copy
    under the hearer's name, escalates a note into a rule, and outlives a
    withdrawal at the source. Both are dropped; lifecycle ops stand."""
    judgment = _parse(
        "input_change_refresh",
        ops=[
            {"op": "append"},
            {"op": "publish", "content": "Judge's own words.", "subject": "x"},
            {"op": "retire", "id": "c_gone"},
        ],
    )
    assert [op.op for op in judgment.directive_ops] == ["retire"]
    assert judgment.dropped_ops == [
        "append",
        "publish(content='Judge's own words.', subject='x')",
    ] or (
        len(judgment.dropped_ops) == 2
        and all(k in " ".join(judgment.dropped_ops) for k in ("append", "publish"))
    )
    assert "publish" in judgment.record_notes


def test_input_change_refresh_keeps_withdraw_published():
    assert _parse("input_change_refresh").withdraw_published == ["p_1"]


def test_ordinary_mode_keeps_admitting_ops():
    assert [op.op for op in _parse("ordinary").directive_ops] == ["append"]


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="mode"):
        _parse("refresh")


def test_the_splice_refresh_mode_no_longer_exists():
    """ADR 0015 D6: two modes, and a stale caller must fail loudly, not degrade."""
    from strata.scope_manager import _DROPPED_ADMITTING_OPS, _JUDGE_MODES

    assert _JUDGE_MODES == ("ordinary", "input_change_refresh")
    assert set(_DROPPED_ADMITTING_OPS) == {"ordinary", "input_change_refresh"}
    with pytest.raises(ValueError, match="mode"):
        _parse("splice_refresh")


def test_the_batch_parser_refuses_an_unknown_mode_too():
    """The mode table governs the batch parser as well as the single one.

    The two parsers must agree about what a mode MEANS — a mode that quietly
    changed meaning between them, or silently degraded to ``ordinary`` on one
    side only, would be the drift the derived schema exists to prevent.
    """
    contribution = _contribution()
    block = _tool_block(
        verdicts=[
            {
                "contribution_id": contribution.id,
                "decision": "accept_as_context",
                "reasoning": "reconciled",
            }
        ],
        directive_ops=[
            {"op": "append", "contribution_id": contribution.id},
            {"op": "retire", "id": "c_gone", "contribution_id": contribution.id},
        ],
        new_context="Reconciled.",
    )

    def _batch(*, mode):
        return ScopeManager._parse_batch_judgment(
            scope=SCOPE,
            tool_use_block=block,
            current_summary=_summary(),
            contributions={contribution.id: contribution},
            mode=mode,
        )

    with pytest.raises(ValueError, match="mode"):
        _batch(mode="splice_refresh")

    judgment = ScopeManager._parse_batch_judgment(
        scope=SCOPE,
        tool_use_block=block,
        current_summary=_summary(),
        contributions={contribution.id: contribution},
        mode="input_change_refresh",
    )

    assert [op.op for op in judgment.directive_ops] == ["retire"]
    assert judgment.dropped_ops == ["append(contribution=c_refresh)"]
    assert "append" in judgment.record_notes_for(contribution.id)


def test_an_input_change_refresh_batch_drops_every_admitting_op():
    contribution = _contribution()
    judgment = ScopeManager._parse_batch_judgment(
        scope=SCOPE,
        tool_use_block=_tool_block(
            verdicts=[
                {
                    "contribution_id": contribution.id,
                    "decision": "accept_as_directive",
                    "reasoning": "reconciled",
                }
            ],
            directive_ops=[
                {"op": "append", "contribution_id": contribution.id},
                {
                    "op": "publish",
                    "content": "Judge's own words.",
                    "subject": "x",
                    "contribution_id": contribution.id,
                },
            ],
            new_context="Reconciled.",
        ),
        current_summary=_summary(),
        contributions={contribution.id: contribution},
        mode="input_change_refresh",
    )

    assert judgment.directive_ops == []
    assert len(judgment.dropped_ops) == 2
    assert any("publish" in d for d in judgment.dropped_ops)


def test_wave_ids_reads_the_same_on_both_judgment_shapes():
    """One field for an emitter to read, whichever shape it was handed.

    A drain always produces the BATCH shape, whose scalar `change_id` is
    always None — so an emitter reading `change_id` off a refresh judgment
    would inherit nothing and ADR 0014 D4's once-per-id rule, the whole
    termination guarantee, would quietly stop bounding anything.
    """
    single = ScopeManagerJudgment(
        decision="accept_as_context",
        reasoning="judged",
        new_summary=None,
        change_id="chg_a",
    )
    batch = ScopeManagerBatchJudgment(new_summary=None, change_ids=["chg_a", "chg_b"])
    ordinary = ScopeManagerJudgment(
        decision="accept_as_context", reasoning="judged", new_summary=None
    )

    assert single.wave_ids == ["chg_a"]
    assert batch.wave_ids == ["chg_a", "chg_b"]
    assert ordinary.wave_ids == []


# ---------------------------------------------------------------------------
# A refresh on additions alone reconciles nothing of its own (#198, third
# form; ADR 0014 D2 as amended 2026-09-08)
# ---------------------------------------------------------------------------


def _locked(events) -> bool:  # noqa: ANN001
    from strata.scope_manager import _refresh_events_are_all_additions

    return _refresh_events_are_all_additions(events)


def test_addition_only_events_lock_the_context():
    """`published`, `amended` and `directive_appended` add an input; there is
    nothing of the scope's OWN for a refresh to reconcile against them."""
    assert _locked([_change_event(kind="published")])
    assert _locked([_change_event(kind="amended")])
    assert _locked([_change_event(kind="directive_appended")])
    assert _locked([_change_event(kind="published"), _change_event(kind="directive_appended")])


def test_one_removal_among_additions_unlocks_the_context():
    """A removal may leave the scope asserting what its inputs no longer
    support, so its own context must stay writable."""
    for kind in (
        "withdrawn",
        "directive_retired",
        "directive_superseded",
        "operator_directive_changed",
    ):
        assert not _locked([_change_event(kind=kind)])
        assert not _locked([_change_event(kind="published"), _change_event(kind=kind)])


def test_an_unspliced_event_counts_as_neither():
    """ADR 0015 D5's unsplice says a row stopped pretending; it is not an
    addition, and on its own it leaves the old behaviour in place."""
    assert not _locked([_change_event(kind="directive_unspliced")])
    assert _locked([_change_event(kind="published"), _change_event(kind="directive_unspliced")])


def test_no_events_and_an_unknown_kind_keep_the_old_behaviour():
    """The rule locks on a positive classification, never on the absence of a
    removal — a splice-only drain and a kind this module has never heard of
    both fall through unlocked."""
    assert not _locked([])
    assert not _locked(None)
    assert not _locked([_change_event(kind="something_new")])


def test_the_two_kind_sets_partition_the_settled_vocabulary():
    """ADR 0014 D1's vocabulary is spelled in three places now — the CHECK in
    migration 0011, :mod:`strata.change_events`, and the refresh's two sets.
    A kind added upstream and classified nowhere would fall through unlocked
    and silently; here it fails loudly instead."""
    from strata.change_events import (
        DIRECTIVE_KINDS,
        DIRECTIVE_UNSPLICED,
        OPERATOR_DIRECTIVE_CHANGED,
        PUBLICATION_KINDS,
    )
    from strata.scope_manager import (
        _REFRESH_ADDITION_KINDS,
        _REFRESH_NEUTRAL_KIND,
        _REFRESH_REMOVAL_KINDS,
    )

    assert not _REFRESH_ADDITION_KINDS & _REFRESH_REMOVAL_KINDS
    assert _REFRESH_NEUTRAL_KIND == DIRECTIVE_UNSPLICED
    assert _REFRESH_ADDITION_KINDS | _REFRESH_REMOVAL_KINDS | {_REFRESH_NEUTRAL_KIND} == (
        PUBLICATION_KINDS | DIRECTIVE_KINDS | {OPERATOR_DIRECTIVE_CHANGED, DIRECTIVE_UNSPLICED}
    )


def test_a_locked_refresh_drops_new_context_and_notes_it():
    judgment = ScopeManager._parse_judgment(
        scope=SCOPE,
        tool_use_block=_tool_block(
            decision="accept_as_context",
            reasoning="The publication from billing must be acknowledged here.",
            new_context="The invoice run must not start before ledger close — per billing.",
        ),
        current_summary=_summary(),
        new_contribution=_contribution(),
        mode="input_change_refresh",
        context_locked=True,
    )

    assert judgment.new_context is None
    assert judgment.new_summary is not None
    assert judgment.new_summary.context == _summary().context
    assert "Dropped new_context" in judgment.record_notes


def test_an_unlocked_refresh_keeps_new_context():
    judgment = _parse("input_change_refresh", ops=[])
    assert judgment.new_context == "Reconciled."
    assert "Dropped new_context" not in judgment.record_notes


def test_ordinary_mode_is_never_context_locked():
    judgment = _parse("ordinary", ops=[])
    assert judgment.new_context == "Reconciled."


def test_a_locked_refresh_batch_drops_new_context_and_notes_it():
    contribution = _contribution()
    judgment = ScopeManager._parse_batch_judgment(
        scope=SCOPE,
        tool_use_block=_tool_block(
            verdicts=[
                {
                    "contribution_id": contribution.id,
                    "decision": "accept_as_context",
                    "reasoning": "acknowledging the new publication",
                }
            ],
            directive_ops=[{"op": "retire", "id": "c_gone", "contribution_id": contribution.id}],
            new_context="Inherited directive c_1 now applies: write incidents up in a day.",
        ),
        current_summary=_summary(),
        contributions={contribution.id: contribution},
        mode="input_change_refresh",
        context_locked=True,
    )

    assert judgment.new_context is None
    assert [op.op for op in judgment.directive_ops] == ["retire"]
    assert judgment.new_summary is not None
    assert judgment.new_summary.context == _summary().context
    assert "Dropped new_context" in judgment.record_notes_for(contribution.id)


def test_the_refresh_block_says_which_case_applies():
    """The prompt states the rule the engine will enforce, conditioned on what
    is actually pending — a judge told "use lifecycle ops only" on a refresh
    that still admits context would be told something false."""
    additions = _preamble(
        mode="input_change_refresh", input_changes=[_change_event(kind="published")]
    )
    assert "all additions" in additions
    assert "`new_context` is dropped" in additions

    removal = _preamble(
        mode="input_change_refresh", input_changes=[_change_event(kind="withdrawn")]
    )
    assert "`new_context` is dropped on this refresh" not in removal
    assert "restate" in removal.lower()
