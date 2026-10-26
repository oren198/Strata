"""judge-check: config, A/B comparison, the usage binding and packaging."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

from strata import judge_usage
from strata.judgecheck import (
    JudgeSpec,
    compare,
    describe_judge,
    estimate,
    item_prompt_chars,
    judge_spec_from_settings,
    load_set,
    parse_price,
    run,
)
from tests.judgecheck_mock import GoldenClient

ITEMS = load_set()
BY_ID = {i.id: i for i in ITEMS}


def test_the_report_may_name_the_model_provider_and_host_but_never_the_key_or_full_url() -> None:
    settings = SimpleNamespace(
        manager_model="qwen/qwen3-235b-a22b-2507",
        judge_provider="Parasail",
        judge_base_url="https://user:pw@openrouter.ai/api/v1?token=abc",
        judge_api_key="sk-SECRET",
    )
    described = describe_judge(settings)
    assert described == {
        "model": "qwen/qwen3-235b-a22b-2507",
        "provider": "Parasail",
        "base_url_host": "openrouter.ai",
    }
    assert "sk-SECRET" not in str(described) and "pw" not in str(described.values())


def test_a_judge_spec_takes_only_model_provider_and_client_from_the_settings() -> None:
    client = object()
    settings = SimpleNamespace(
        manager_model="m", judge_provider="P", build_judge_client=lambda: client
    )
    spec = judge_spec_from_settings(settings, label="incumbent")
    assert (spec.label, spec.client, spec.model, spec.provider) == ("incumbent", client, "m", "P")


def test_compare_lists_the_items_where_exactly_one_judge_was_right() -> None:
    target = BY_ID["j1h-021"]  # a must-decline item the engine does not rescue
    subset = [*ITEMS[:20], *([] if target in ITEMS[:20] else [target])]
    outcome = run(
        subset,
        reps=1,
        judges=[
            JudgeSpec("incumbent", GoldenClient(ITEMS), "m"),
            JudgeSpec("candidate", GoldenClient(ITEMS, flip=[target.id]), "m"),
        ],
    )
    diff = compare(outcome.results["incumbent"], outcome.results["candidate"], BY_ID)
    assert [f["item_id"] for f in diff["flips"]] == [target.id]
    flip = diff["flips"][0]
    assert flip["incumbent"]["strict"] is True and flip["candidate"]["strict"] is False
    assert diff["suites"]["h2"]["delta"] == -1


def test_the_estimate_uses_the_sets_own_prompt_sizes() -> None:
    sizes = [item_prompt_chars(i) for i in ITEMS]
    assert all(s > 1000 for s in sizes)
    est = estimate(sizes, reps=3, price=parse_price("0.22,0.88"))
    assert est.judgments == 89 * 3 and est.est_usd > 0


def test_usage_metering_is_bound_to_a_temp_ledger_during_a_run_and_restored_after() -> None:
    before = judge_usage._bound_db  # noqa: SLF001
    seen: list[str | None] = []

    class Spy(GoldenClient):
        def create(self, **kwargs):  # noqa: ANN003, ANN201
            seen.append(judge_usage._bound_db)  # noqa: SLF001
            return super().create(**kwargs)

    run(ITEMS[:3], reps=1, judges=[JudgeSpec("c", Spy(ITEMS), "m")])
    assert seen and all(path and "strata-judgecheck-" in path for path in seen)
    assert judge_usage._bound_db == before  # noqa: SLF001


def test_the_env_usage_binding_and_temp_dir_are_restored_even_on_a_keyboard_interrupt(
    monkeypatch,  # noqa: ANN001
) -> None:
    import os  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    monkeypatch.setenv("STRATA_JUDGE_DAILY_TOKEN_CAP", "42")
    before_binding = judge_usage._bound_db  # noqa: SLF001
    seen_root: list[str] = []

    class Interrupted(GoldenClient):
        def create(self, **kwargs):  # noqa: ANN003, ANN201
            seen_root.append(judge_usage._bound_db)  # noqa: SLF001
            raise KeyboardInterrupt

    try:
        run(ITEMS[:3], reps=1, judges=[JudgeSpec("c", Interrupted(ITEMS), "m")])
    except KeyboardInterrupt:
        pass
    else:  # pragma: no cover
        raise AssertionError("the interrupt must propagate")
    assert os.environ["STRATA_JUDGE_DAILY_TOKEN_CAP"] == "42"  # the user's cap is put back
    assert judge_usage._bound_db == before_binding  # noqa: SLF001
    assert seen_root and not Path(seen_root[0]).exists()  # the temp dir is gone

    monkeypatch.delenv("STRATA_JUDGE_DAILY_TOKEN_CAP")
    with contextlib.suppress(KeyboardInterrupt):
        run(ITEMS[:3], reps=1, judges=[JudgeSpec("c", Interrupted(ITEMS), "m")])
    assert "STRATA_JUDGE_DAILY_TOKEN_CAP" not in os.environ  # and removed if it was unset
