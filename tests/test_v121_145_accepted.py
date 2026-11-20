"""#145: one public definition of "accepted" (v1.21).

``strata.is_accepted(decision)`` is the one question a host asks ("was this admitted?"), and
``ContributionState.accepted`` answers it on every read surface. No engine code tests acceptance
by naming the two accepting verdicts together.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import strata
from strata.record_store import ContributorRef, RecordStore
from tests.test_mcp_server import _make_db

SCOPE = "g_backend"
DIRECTIVE = "accept_as_directive"
CONTEXT = "accept_as_context"


# -- a. the predicate ------------------------------------------------------------------


@pytest.mark.parametrize("decision", [DIRECTIVE, CONTEXT])
def test_is_accepted_is_true_for_the_two_accepting_verdicts(decision: str) -> None:
    assert strata.is_accepted(decision) is True


@pytest.mark.parametrize("decision", ["decline", None, "", "held", "Accept_As_Context"])
def test_is_accepted_is_false_for_everything_else(decision: str | None) -> None:
    assert strata.is_accepted(decision) is False


def test_is_accepted_agrees_with_the_kept_constant() -> None:
    assert {d for d in (DIRECTIVE, CONTEXT, "decline") if strata.is_accepted(d)} == set(
        strata.ACCEPTED_DECISIONS
    )


# -- b. the state field, through the real derivation -------------------------------------


def _contributor() -> ContributorRef:
    return ContributorRef(
        scope_id=SCOPE, skill="s", session_id="sess", ts="2026-11-21T00:00:00+00:00"
    )


def _append(store: RecordStore, subject: str) -> str:
    return store.append_contribution(
        scope_id=SCOPE,
        content=f"The dock pumps were switched off at 20:00 ({subject}).",
        proposed_classification="context",
        subject=subject,
        supersedes=None,
        contributor=_contributor(),
    ).id


def test_the_state_carries_accepted_in_every_state(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    with RecordStore(db_path) as store:
        ids = {
            "directive": _append(store, "d"),
            "context": _append(store, "c"),
            "decline": _append(store, "x"),
            "failed": _append(store, "f"),
            "pending": _append(store, "p"),
            "note": _append(store, "change-emission-failed"),
        }
        store.record_judgment(contribution_id=ids["directive"], decision=DIRECTIVE, judged_by="t")
        store.record_judgment(contribution_id=ids["context"], decision=CONTEXT, judged_by="t")
        store.record_judgment(contribution_id=ids["decline"], decision="decline", judged_by="t")
        store.record_judgment_attempt(
            contribution_id=ids["failed"], error_class="Boom", outcome="judge_failed"
        )
        states = {s.contribution_id: s for s in store.list_contribution_states(scope_id=SCOPE)}
        paged = {
            s.contribution_id: s for s in store.page_record(scope_id=SCOPE).contribution_states
        }
        by_id = {
            s.contribution_id: s for s in [store.get_record_entry(i).state for i in ids.values()]
        }

    expected = {
        "directive": ("judged", True),
        "context": ("judged", True),
        "decline": ("judged", False),
        "failed": ("judge_failed", False),
        "pending": ("pending", False),
        "note": ("note", False),
    }
    for surface in (states, paged, by_id):
        for name, (state, accepted) in expected.items():
            row = surface[ids[name]]
            assert row.state == state, name
            assert row.accepted is accepted, name
            assert row.accepted == strata.is_accepted(row.decision), name


# -- c. nobody else names the two verdicts together ----------------------------------------

SRC = Path(strata.__file__).parent
_TEXT_SUFFIXES = {".sql", ".jsx", ".jsonl", ".js", ".html", ".md", ".txt", ".json", ".yaml"}
_SQL_IN = re.compile(
    rf"\bIN\s*\([^)]*\b({DIRECTIVE}|{CONTEXT})\b[^)]*\b({DIRECTIVE}|{CONTEXT})\b", re.IGNORECASE
)

#: EXPLICIT file paths (relative to src/strata), each with why it may name both verdicts.
ALLOWED = {
    "record_store.py": "defines ACCEPTED_DECISIONS and is_accepted, the one definition",
    "scope_manager.py": "the judge contract: tool-schema enums and the batch decision tuple "
    "tell the judge its three choices; they are not an acceptance test",
    "_migrations/0001_initial.sql": "CHECK constraint on the judgments.decision column",
    "_migrations/0002_drop_fleet_tables.sql": "CHECK constraint on the judgments.decision column",
    "_migrations/0006_optional_skill.sql": "CHECK constraint on the judgments.decision column",
    "_migrations/0018_raised_from_operator_evidence.sql": "CHECK constraint on the "
    "judgments.decision column",
    "_judgecheck/set-v1.jsonl": "frozen data: the set's item text and goldens",
}


def _in_annotation_nodes(tree: ast.AST) -> set[int]:
    """Node ids inside a subscript (``Literal["a", "b"]`` annotations declare a type)."""
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            inside.update(id(n) for n in ast.walk(node.slice))
    return inside


def _python_hits(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _in_annotation_nodes(tree)
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple | ast.Set | ast.List) and id(node) not in skip:
            names = {
                e.value
                for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            }
            if DIRECTIVE in names and CONTEXT in names:
                lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _SQL_IN.search(node.value):  # a SQL ``decision IN ('..', '..')`` in a string
                lines.append(node.lineno)
    return lines


def _text_hits(path: Path) -> list[int]:
    return [
        n
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if DIRECTIVE in line and CONTEXT in line
    ]


def _all_hits() -> dict[str, list[int]]:
    hits: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix == ".py":
            found = _python_hits(path)
        elif path.suffix in _TEXT_SUFFIXES:
            found = _text_hits(path)
        else:
            continue
        if found:
            hits[path.relative_to(SRC).as_posix()] = found
    return hits


def test_no_code_outside_the_definition_names_both_accepting_verdicts() -> None:
    stray = {f: lines for f, lines in _all_hits().items() if f not in ALLOWED}
    assert stray == {}, (
        "name the pair through strata.is_accepted / ACCEPTED_DECISIONS instead, or add the "
        "file to ALLOWED with a one-line reason"
    )


def test_every_allowed_file_still_needs_its_allowance() -> None:
    assert all(not f.endswith("*") and "*" not in f for f in ALLOWED)
    unused = sorted(set(ALLOWED) - set(_all_hits()))
    assert unused == [], f"stale allow-list entries: {unused}"
