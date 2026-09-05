"""The judge must not receive an ancestor's context (#187).

ADR 0013 D1 removed raw context from what a chain edge carries: a scope's
context is its own internal working memory and never leaves the scope. That
was implemented on the READ path — ``compose_perspective`` gives a descendant
its ancestors' directives only.

The JUDGE path was missed. ``strata.app`` resolves the inter-stratum parent's
full ``ScopeSummary`` and ``_build_judge_preamble`` renders it whole, context
section included — and the system prompt did not merely tolerate that, it
instructed the judge to absorb it ("Context from the parent may be paraphrased
or summarised into ``new_context``"). So the parent's internal memory reached
the child by a second route and, once written into ``new_context``, became the
child's own context: indistinguishable on the read side from something the
child observed itself, with the origin laundered on the way in.

That is the identity collapse D1 exists to prevent, arriving through judgment
instead of composition.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.fleet_config import Scope, Stratum
from strata.scope_manager import _build_judge_preamble
from strata.summary_store import Directive, ScopeSummary

PARENT_CONTEXT = "PARENT-PRIVATE-WORKING-NOTE the migration is half finished"
PARENT_DIRECTIVE = "PARENT-DIRECTIVE all deploys need a rollback plan"


def _parent_summary() -> ScopeSummary:
    return ScopeSummary(
        scope_id="g_parent",
        directives=[
            Directive(
                id="c_parent_1",
                content=PARENT_DIRECTIVE,
                subject="deploys",
                source_scope_id="g_parent",
                source_skill=None,
                created_at="2026-09-01T10:00:00+00:00",
            )
        ],
        context=PARENT_CONTEXT,
        updated_at="2026-09-01T10:00:00+00:00",
        version=3,
    )


def _preamble() -> str:
    return _build_judge_preamble(
        scope=Scope(id="g_child", name="Child", stratum_id="L1"),
        stratum=Stratum(id="L1", name="Team", ordinal=1),
        parent_summary=_parent_summary(),
        current_summary=ScopeSummary(
            scope_id="g_child",
            directives=[],
            context="the child's own working note",
            updated_at="2026-09-01T10:00:00+00:00",
            version=1,
        ),
        recent_contributions=[],
        judged_contribution_ids=[],
    )


def test_parent_context_never_reaches_the_judge():
    """The half that leaked: an ancestor's context in the judge's inputs."""
    assert PARENT_CONTEXT not in _preamble()


def test_parent_directives_still_reach_the_judge():
    """The half that must not regress: directives bind, so the judge sees them."""
    assert PARENT_DIRECTIVE in _preamble()


def test_the_parent_block_is_still_labelled_as_inherited():
    """The judge must still know these are the parent's, not the child's own."""
    preamble = _preamble()
    assert "PARENT" in preamble.upper()


def test_prompt_does_not_invite_the_judge_to_absorb_parent_context():
    """The other half of the fix, and the one that would be forgotten.

    Removing the context from the rendered block is not enough while the
    system prompt still tells the judge it may paraphrase parent context into
    ``new_context`` — the instruction outlives the input it referred to, and a
    model follows it whenever a parent block is present for any other reason.
    """
    from strata.scope_manager import _SYSTEM_PROMPT as SYSTEM_PROMPT

    lowered = SYSTEM_PROMPT.lower()
    assert "context from the parent" not in lowered


def test_parent_publication_reaches_the_judge_but_its_context_still_does_not():
    """ADR 0014 (Phase A finding 1) added the parent's FACE, not its notes.

    The parent's publication is curated for outside readers — that is what
    makes it shareable, and what a context digest is not. Adding one must not
    reopen the other route into the child's `new_context` that #187 closed.
    """
    from strata.publication import PublishedItem

    text = _build_judge_preamble(
        scope=Scope(id="g_child", name="Child", stratum_id="L1"),
        stratum=Stratum(id="L1", name="Team", ordinal=1),
        parent_summary=_parent_summary(),
        current_summary=None,
        recent_contributions=[],
        judged_contribution_ids=[],
        parent_publication=(
            "g_parent",
            [
                PublishedItem(
                    id="p_1",
                    kind="directive",
                    content="PARENT-PUBLISHED rollbacks are mandatory",
                    subject="deploys",
                    anchors=["c_parent_1"],
                    published_at="2026-09-01T10:00:00+00:00",
                )
            ],
        ),
    )

    assert "PARENT-PUBLISHED rollbacks are mandatory" in text
    assert PARENT_CONTEXT not in text
