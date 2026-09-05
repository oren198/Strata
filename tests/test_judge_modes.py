"""The two judge modes (ADR 0014 D2, ADR 0015 D6, implementation pin 6).

``amendment_context_only`` was a bool: refresh or not. ADR 0014 split
"refresh" in two; ADR 0015 D1 deleted the splice, and with it the third mode,
leaving the pair that genuinely differ in what the judge is allowed to do:

- ``ordinary`` — a contribution arrived; every op is available.
- ``input_change_refresh`` — ADR 0014 D2's reactive re-judgement. Admitting
  ops are ALLOWED: the refresh has a real contribution to mint a directive
  from (the change notice, ADR 0014 D5), so a minted directive carries honest
  provenance — this entered because input X changed. ``append`` is still
  dropped: it would copy the notice's own bytes.

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


def test_input_change_refresh_block_states_the_admitting_ops_are_allowed():
    text = _preamble(mode="input_change_refresh", input_changes=[_change_event()])
    for op in ("append", "publish", "retire", "supersede", "withdraw_published"):
        assert op in text


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


def test_input_change_refresh_keeps_publish_but_drops_append():
    """ADR 0014 D2: the notice is a real contribution to mint a directive FROM,
    but never one to copy. ``publish`` carries the judge's own words on the
    notice's id and provenance; ``append`` would copy the notice's mechanical
    payload verbatim into a directive whose subject is ``manager-refresh``.
    """
    judgment = _parse(
        "input_change_refresh",
        ops=[{"op": "append"}, {"op": "publish", "content": "Judge's own words.", "subject": "x"}],
    )
    assert [op.op for op in judgment.directive_ops] == ["publish"]
    assert judgment.dropped_ops == ["append"]
    assert "append" in judgment.record_notes


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


def test_an_input_change_refresh_batch_drops_append_and_keeps_publish():
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

    assert [op.op for op in judgment.directive_ops] == ["publish"]
    assert judgment.dropped_ops == ["append(contribution=c_refresh)"]


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
