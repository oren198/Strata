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

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strata.fleet_config import Scope, Stratum
from strata.scope_manager import _build_judge_preamble
from strata.summary_store import Directive, ScopeSummary

PARENT_CONTEXT = "PARENT-PRIVATE-WORKING-NOTE the migration is half finished"
PARENT_DIRECTIVE = "PARENT-DIRECTIVE all deploys need a rollback plan"


def _parent_walk() -> list[tuple[str, list[Directive]]]:
    """The ancestor walk a one-deep chain produces (ADR 0015 D2).

    PARENT_CONTEXT never appears in it at all: the walk carries directives,
    and there is no second path by which an ancestor's context could arrive.
    """
    return [
        (
            "g_parent",
            [
                Directive(
                    id="c_parent_1",
                    content=PARENT_DIRECTIVE,
                    subject="deploys",
                    source_scope_id="g_parent",
                    source_skill=None,
                    created_at="2026-09-01T10:00:00+00:00",
                )
            ],
        )
    ]


def _preamble() -> str:
    return _build_judge_preamble(
        scope=Scope(id="g_child", name="Child", stratum_id="L1"),
        stratum=Stratum(id="L1", name="Team", ordinal=1),
        ancestor_directives=_parent_walk(),
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


def test_the_ancestor_block_is_still_labelled_as_inherited():
    """The judge must still know these are the ancestor's, not the child's own."""
    preamble = _preamble()
    assert "ANCESTOR DIRECTIVES — g_parent (inherited, binding)" in preamble


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
        ancestor_directives=_parent_walk(),
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


# ---------------------------------------------------------------------------
# ADR 0015 D2 — one ancestor walk, two consumers
# ---------------------------------------------------------------------------
#
# `parent_summary` delivered ONE ancestor's directives to the judge while
# composition walked the whole chain. A grandparent's directive bound the
# scope on the read side and was invisible on the judgment side — two views of
# what binds a scope, free to drift. The walk is now one function and both
# consumers read it.

ROOT_DIRECTIVE = "ROOT-DIRECTIVE every service ships with a runbook"
ROOT_CONTEXT = "ROOT-PRIVATE-WORKING-NOTE the runbook template is a draft"


def _three_deep_stores(tmp_path: Path):
    """root -> g_a -> g_b, each ancestor with its own directive and its own context."""
    from strata.fleet_config import FleetConfig
    from strata.summary_store import SummaryStore

    fleet = FleetConfig.model_validate(
        {
            "strata": [
                {"id": "L0", "name": "Fleet", "ordinal": 0},
                {"id": "L1", "name": "Division", "ordinal": 1},
                {"id": "L2", "name": "Team", "ordinal": 2},
            ],
            "scopes": [
                {"id": "g_root", "name": "Root", "stratum_id": "L0"},
                {"id": "g_a", "name": "A", "stratum_id": "L1"},
                {"id": "g_b", "name": "B", "stratum_id": "L2"},
            ],
            "edges": [
                {"from_": "g_root", "to": "g_a", "type": "chain"},
                {"from_": "g_a", "to": "g_b", "type": "chain"},
            ],
        }
    )
    store = SummaryStore(tmp_path / "summaries")
    store.write(
        "g_root",
        ScopeSummary(
            scope_id="g_root",
            directives=[
                Directive(
                    id="c_root_1",
                    content=ROOT_DIRECTIVE,
                    subject="ops",
                    source_scope_id="g_root",
                    source_skill=None,
                    created_at="2026-09-01T10:00:00+00:00",
                )
            ],
            context=ROOT_CONTEXT,
            updated_at="2026-09-01T10:00:00+00:00",
            version=1,
        ),
    )
    store.write(
        "g_a",
        ScopeSummary(
            scope_id="g_a",
            directives=[
                Directive(
                    id="c_a_1",
                    content=PARENT_DIRECTIVE,
                    subject="deploys",
                    source_scope_id="g_a",
                    source_skill=None,
                    created_at="2026-09-01T10:00:00+00:00",
                )
            ],
            context=PARENT_CONTEXT,
            updated_at="2026-09-01T10:00:00+00:00",
            version=1,
        ),
    )
    return fleet, store


def test_the_walk_is_root_first_and_carries_no_ancestor_context(tmp_path: Path) -> None:
    """ADR 0015 D2: one function, root-first, directives only."""
    from strata.perspective import ancestor_directives

    fleet, store = _three_deep_stores(tmp_path)
    walk = ancestor_directives("g_b", fleet=fleet, summary_store=store)

    assert [scope_id for scope_id, _ in walk] == ["g_root", "g_a"]
    assert [d.id for _, directives in walk for d in directives] == ["c_root_1", "c_a_1"]


def test_the_judge_sees_every_ancestor_once_under_its_own_scope(tmp_path: Path) -> None:
    """The grandparent's directive binds g_b, so g_b's judge must see it — once."""
    from strata.perspective import ancestor_directives

    fleet, store = _three_deep_stores(tmp_path)
    text = _build_judge_preamble(
        scope=Scope(id="g_b", name="B", stratum_id="L2"),
        stratum=Stratum(id="L2", name="Team", ordinal=2),
        ancestor_directives=ancestor_directives("g_b", fleet=fleet, summary_store=store),
        current_summary=None,
        recent_contributions=[],
        judged_contribution_ids=[],
    )

    assert text.count(ROOT_DIRECTIVE) == 1
    assert text.count(PARENT_DIRECTIVE) == 1
    assert "ANCESTOR DIRECTIVES — g_root (inherited, binding)" in text
    assert "ANCESTOR DIRECTIVES — g_a (inherited, binding)" in text
    # Root-first, so the broader stratum's block is read first.
    assert text.index("ANCESTOR DIRECTIVES — g_root") < text.index("ANCESTOR DIRECTIVES — g_a")
    # ADR 0013 D1 holds at every depth, not just the parent's.
    assert ROOT_CONTEXT not in text
    assert PARENT_CONTEXT not in text


def test_the_perspective_shows_a_grandparent_directive_exactly_once(tmp_path: Path) -> None:
    """The other consumer of the same walk (ADR 0013 D1): one layer, one copy."""
    from strata.perspective import compose_perspective

    fleet, store = _three_deep_stores(tmp_path)
    perspective = compose_perspective("g_b", fleet=fleet, summary_store=store)

    root_layers = [
        layer
        for layer in perspective["layers"]
        if layer.get("scope_id") == "g_root" and layer.get("relation") == "ancestor"
    ]
    assert len(root_layers) == 1
    assert [d["content"] for d in root_layers[0]["directives"]] == [ROOT_DIRECTIVE]
    assert json.dumps(perspective).count(ROOT_DIRECTIVE) == 1
