"""Which must-decline items measure the judge: the probe, the CLI, and the score's split."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from strata.judgecheck import (
    classify_judge_dependence,
    judge_dependent_ids,
    load_set,
)
from strata.judgecheck.dependence import JUDGE_DEPENDENT, NOT_APPLICABLE, RESCUED, main
from strata.judgecheck.items import load_items_unchecked

ITEMS = load_set()
BY_ID = {i.id: i for i in ITEMS}


def test_an_item_the_engine_declines_even_when_the_judge_admits_it_is_rescued() -> None:
    kinds = classify_judge_dependence([BY_ID["j1h-009"], BY_ID["j1d-009"]])
    assert kinds == {"j1h-009": RESCUED, "j1d-009": RESCUED}


def test_an_item_the_engine_lets_through_when_the_judge_admits_it_is_judge_dependent() -> None:
    kinds = classify_judge_dependence([BY_ID["j1h-021"], BY_ID["j1d-044"], BY_ID["j3-017"]])
    assert set(kinds.values()) == {JUDGE_DEPENDENT}


def test_only_must_decline_items_of_the_security_suites_are_classified() -> None:
    kinds = classify_judge_dependence(ITEMS)
    for item in ITEMS:
        applicable = item.suite in ("h2", "drift", "j3") and item.golden["decision"] == "decline"
        assert (kinds[item.id] != NOT_APPLICABLE) is applicable, item.id
    counts = {k: list(kinds.values()).count(k) for k in (RESCUED, JUDGE_DEPENDENT)}
    assert counts[RESCUED] + counts[JUDGE_DEPENDENT] == 31


def test_the_classifier_runs_on_any_items_file_without_the_sets_sha(tmp_path: Path, capsys) -> None:  # noqa: ANN001
    rows = [BY_ID[i].data for i in ("j1h-009", "j1h-021", "j1r-002") if i in BY_ID]
    refinement = next(i for i in ITEMS if i.suite == "refinement")
    rows = [BY_ID["j1h-009"].data, BY_ID["j1h-021"].data, refinement.data]
    path = tmp_path / "private-items.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert [i.id for i in load_items_unchecked(path)] == [r["id"] for r in rows]
    assert main([str(path), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "j1h-009": RESCUED,
        "j1h-021": JUDGE_DEPENDENT,
        refinement.id: NOT_APPLICABLE,
    }
    assert main([str(path)]) == 0
    assert "j1h-021\th2\tjudge_dependent" in capsys.readouterr().out


def test_a_probe_that_breaks_never_reads_as_the_engine_letting_it_through(monkeypatch) -> None:  # noqa: ANN001
    from strata.judgecheck import dependence  # noqa: PLC0415

    monkeypatch.setattr(dependence._AdmitClient, "create", lambda self, **kw: 1 / 0)  # noqa: ARG005
    with pytest.raises(RuntimeError, match="probe failed"):
        dependence.classify_item(BY_ID["j1h-009"])


def test_the_probe_does_not_leave_the_working_directory_or_env_changed(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRATA_JUDGE_DAILY_TOKEN_CAP", "7")
    classify_judge_dependence([BY_ID["j1h-009"]])
    assert os.environ["STRATA_JUDGE_DAILY_TOKEN_CAP"] == "7"
    assert [p.name for p in tmp_path.iterdir() if not p.name.startswith("_")] == []


def test_judge_dependent_ids_is_the_set_the_pool_tool_would_label_judge() -> None:
    assert judge_dependent_ids(ITEMS) == {
        i for i, k in classify_judge_dependence(ITEMS).items() if k == JUDGE_DEPENDENT
    }
