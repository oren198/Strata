"""C6 anaphora (CEO 2026-11-23, 1300Z): "it/this/that" + a marker resolves ONLY to the nearest
own-artefact noun (my/our + the closed list) in the IMMEDIATELY PRECEDING clause; anything else
is unclassified and declines. Its own switch, ``_MARKER_GOVERNOR_ANAPHORA``, nested under
``_MARKER_GOVERNOR_CHECK``, in its own commit so it reverts separately.

The in-sample live case is c_df7e102ed5f372be ("I sent him my version ... It keeps his stated
facts ... unchanged"), taken from the record Mary shared, not the store.
"""

from __future__ import annotations

import pytest

from strata import scope_manager as sm
from strata.scope_manager import verify_inherited_relation

MISSION = (
    "The operator's mission is shared memory for agent fleets. "
    "Every contribution is judged, and stated facts are checked before it counts. "
    "Users own their memory and may delete it. "
    "Reviews must not be skipped for any release."
)
UNRELATED = {"kind": "unrelated"}

C_DF7E = (
    "2026-10-27: Aron asked me to edit the README section on forgetting planned for v1.20 into "
    "plain language. I sent him my version at .work/v120-forgetting/README-forgetting-mary.md. "
    "It keeps his stated facts and the tool and command names unchanged. Nothing is committed: "
    "Grok commits it, and the full test suite runs on that docs commit. I asked Aron to confirm "
    "one point: whether --until and --when apply to both operator publish and operator supersede."
)
ADMIT_SIMPLE = "I sent him my version. It keeps his stated facts unchanged."
ADMIT_THIS = "I wrote my draft. This keeps the stated facts unchanged."

DECLINE_NO_ANTECEDENT = "The review is slow. It keeps getting skipped."
DECLINE_PERSON = "I asked Priya about the rollout. It keeps his stated facts unchanged."
DECLINE_TWO_BACK = "I wrote my draft. Nothing else changed today. It keeps the stated facts."
DECLINE_CREATES = "I sent my version. It lets staff skip the review."
DECLINE_RULE_SUBJECT = "I sent my version. This keeps the review optional."
DECLINE_NOT_OWN = "Priya sent the version. It keeps his stated facts unchanged."


def _verdict(content: str) -> tuple[str, str]:
    return verify_inherited_relation(UNRELATED, content, MISSION)


@pytest.mark.parametrize(
    "content", [ADMIT_SIMPLE, ADMIT_THIS, C_DF7E], ids=["it", "this", "c_df7e"]
)
def test_a_pronoun_that_resolves_to_the_previous_clauses_own_artefact_is_admitted(
    content: str,
) -> None:
    verdict, reason = _verdict(content)
    assert verdict == "admit", reason
    assert "own-act:anaphora" in reason


@pytest.mark.parametrize(
    "content",
    [
        DECLINE_NO_ANTECEDENT,
        DECLINE_PERSON,
        DECLINE_TWO_BACK,
        DECLINE_CREATES,
        DECLINE_RULE_SUBJECT,
        DECLINE_NOT_OWN,
    ],
    ids=["no-antecedent", "person", "two-back", "creates-exception", "rule-subject", "not-own"],
)
def test_every_other_pronoun_clause_still_declines(content: str) -> None:
    verdict, reason = _verdict(content)
    assert verdict == "decline", reason


def test_a_file_path_dot_does_not_split_the_preceding_clause() -> None:
    """A '.' inside a path ('.work/v120/README.md') is not a clause end."""
    note = "I sent him my version at .work/notes/README-mary.md. It keeps his stated facts."
    assert _verdict(note)[0] == "admit"


def test_the_list_gained_version_and_draft_and_is_otherwise_unchanged() -> None:
    assert {"version", "draft"} <= sm._OWN_ARTEFACT_NOUNS
    assert len(sm._OWN_ARTEFACT_NOUNS) == 11


def test_the_switches_are_on_by_default() -> None:
    assert sm._MARKER_GOVERNOR_ANAPHORA is True


def test_anaphora_off_keeps_the_rest_of_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "_MARKER_GOVERNOR_ANAPHORA", False)
    assert _verdict(C_DF7E)[0] == "decline"
    assert _verdict(ADMIT_SIMPLE)[0] == "decline"
    # the non-pronoun own-act path is untouched by this switch
    assert _verdict("This PR adds an optional JUDGE_PROVIDER environment variable.")[0] == "admit"


def test_the_anaphora_switch_is_nested_under_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "_MARKER_GOVERNOR_CHECK", False)
    assert _verdict(C_DF7E)[0] == "decline"
