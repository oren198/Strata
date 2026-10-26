"""``strata judge-check``: CLI wiring, report rendering, cost guard, compare pass-through.

No live judge calls. The runner is replaced; the score, the price parser and the
verdict strings are the engine's.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.judgecheck_peripherals_support import (
    first_hand_id,
    install_offline_guards,
    invoke,
    judge_must_decline_id,
    report_json,
)

_KEY = "sk-or-v1-THISKEYMUSTNOTAPPEAR"
_QUERY = "token=QUERYSECRET"
_ENV_SECRET = "sk-ant-api03-ENVSECRETVALUE"
_URL = f"https://user:pw@openrouter.ai/api/v1?{_QUERY}"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    box, state = install_offline_guards(monkeypatch, tmp_path)
    return box, state


def test_yes_runs_and_a_golden_judge_meets_exits_0(
    _offline, capsys: pytest.CaptureFixture[str]
) -> None:
    box, state = _offline
    rc, out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 0, err
    assert err == ""
    report = json.loads(report_json(state).read_text(encoding="utf-8"))
    assert report["schema"] == "judgecheck_score_v1"
    assert report["verdict"].startswith("meets the judge-check v1 bar for Strata ")
    assert f"\n{report['verdict']}\n" in out
    assert box.calls and box.calls[0]["reps"] == 1
    assert box.calls[0]["price_source"] == "--price"
    assert "estimate:" in out


def test_prompt_n_does_not_run_and_exits_3(
    _offline, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    box, state = _offline
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: "n")
    rc, out, _err = invoke(capsys, ["judge-check", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 3
    assert box.calls == []
    assert "estimate:" in out
    assert "Not running." in out
    assert list(state.rglob("report.json")) == []


def test_yes_does_not_prompt(
    _offline, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("prompted")

    monkeypatch.setattr("builtins.input", boom)
    rc, _out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 0, err


def test_does_not_meet_security_leg_exits_1(_offline, capsys: pytest.CaptureFixture[str]) -> None:
    box, state = _offline
    box.flip_ids.add(judge_must_decline_id())
    rc, out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 1, err
    report = json.loads(report_json(state).read_text(encoding="utf-8"))
    assert report["verdict"].startswith("does not meet the judge-check v1 bar for Strata ")
    assert "security leg" in report["verdict"]
    assert report["verdict"] in out
    assert "security leg" in report["failed_legs"]


def test_tool_call_failure_exits_1(_offline, capsys: pytest.CaptureFixture[str]) -> None:
    box, state = _offline
    box.tool_error_ids.add(judge_must_decline_id())
    rc, out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 1, err
    report = json.loads(report_json(state).read_text(encoding="utf-8"))
    assert "tool-call failures" in report["verdict"]
    assert report["verdict"] in out


def test_incomplete_cap_exits_2(_offline, capsys: pytest.CaptureFixture[str]) -> None:
    from strata.judgecheck import IncompleteInfo

    box, state = _offline
    box.incomplete = IncompleteInfo("cap reached", 4, 20)
    rc, out, err = invoke(
        capsys,
        ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1", "--max-usd", "0.01"],
    )
    assert rc == 2, err
    report = json.loads(report_json(state).read_text(encoding="utf-8"))
    assert report["verdict"] == "incomplete: cap reached at 4/20"
    assert report["verdict"] in out
    assert box.calls[0]["max_usd"] == 0.01


def test_unmeasured_when_this_build_has_no_bars(
    _offline, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _box, state = _offline
    monkeypatch.setattr("strata.judgecheck.BARS_V1", None)
    rc, out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 2, err
    report = json.loads(report_json(state).read_text(encoding="utf-8"))
    assert report["verdict"] == "unmeasured: no reference bars in this build"
    assert report["verdict"] in out


def test_price_unknown_is_printed_verbatim_and_exits_3(
    _offline, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import strata.judgecheck.cost as cost

    box, _state = _offline
    monkeypatch.setattr(cost, "fetch_price", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("JUDGE_MODEL", "price-unknown-model")
    try:
        cost.resolve_price(None, "https://openrouter.ai/api", "price-unknown-model", provider=None)
    except cost.PriceUnknown as exc:
        expected = str(exc)
    else:  # pragma: no cover
        raise AssertionError("resolve_price should refuse")
    assert "--price" in expected
    rc, out, err = invoke(capsys, ["judge-check", "--reps", "1"])
    assert rc == 3
    assert expected in err
    assert expected not in out or expected in err
    assert box.calls == []


def test_tampered_set_message_exits_3(
    _offline, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from strata.judgecheck import SetIntegrityError

    box, _state = _offline
    message = (
        "judge-check set sha256 deadbeef does not match the abc this build "
        "was released with; refusing to run."
    )

    def boom(*_args: object, **_kwargs: object) -> None:
        raise SetIntegrityError(message)

    monkeypatch.setattr("strata.judgecheck.load_set", boom)
    rc, _out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 3
    assert message in err
    assert box.calls == []


def test_bad_price_exits_3(_offline, capsys: pytest.CaptureFixture[str]) -> None:
    box, _state = _offline
    rc, _out, err = invoke(capsys, ["judge-check", "--yes", "--price", "nope", "--reps", "1"])
    assert rc == 3
    assert "--price" in err
    assert box.calls == []


def test_price_flag_wins_and_resolve_price_receives_the_provider_pin(
    _offline, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import strata.judgecheck as judgecheck

    box, _state = _offline
    seen: dict[str, tuple] = {}
    real = judgecheck.resolve_price

    def spy(cli_price, base_url, model, *, provider=None, http_get=None):  # noqa: ANN001
        seen["call"] = (cli_price, base_url, model, provider)
        return real(cli_price, base_url, model, provider=provider, http_get=http_get)

    monkeypatch.setattr(judgecheck, "resolve_price", spy)
    monkeypatch.setenv("JUDGE_PROVIDER", "Parasail")
    monkeypatch.setenv("JUDGE_BASE_URL", _URL)
    rc, _out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 0, err
    assert seen["call"] == ("0.2,0.8", _URL, "qwen/qwen3-235b-a22b-2507", "Parasail")
    assert box.calls[0]["input_per_m"] == 0.2
    assert box.calls[0]["output_per_m"] == 0.8
    assert box.calls[0]["providers"] == ["Parasail"]


def test_report_header_omits_a_key_shaped_fixture_and_a_url_query(
    _offline, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from strata import __version__
    from strata.judgecheck.items import SET_V1_SHA256

    _box, state = _offline
    monkeypatch.setenv("JUDGE_PROVIDER", "Parasail")
    monkeypatch.setenv("JUDGE_BASE_URL", _URL)
    monkeypatch.setenv("JUDGE_API_KEY", _KEY)
    monkeypatch.setenv("STRATA_UNRELATED_ENV", _ENV_SECRET)
    out_path = tmp_path / "asked" / "report.json"
    rc, out, err = invoke(
        capsys,
        [
            "judge-check",
            "--yes",
            "--price",
            "0.2,0.8",
            "--reps",
            "1",
            "--out",
            str(out_path),
        ],
    )
    assert rc == 0, err
    path = report_json(state)
    text = (path.parent / "report.txt").read_text(encoding="utf-8")
    payload = path.read_text(encoding="utf-8")
    copied = out_path.read_text(encoding="utf-8")
    assert copied == payload
    blob = out + err + payload + text
    for secret in (_KEY, _QUERY, "QUERYSECRET", "user:pw", _ENV_SECRET, "https://", _URL):
        assert secret not in blob, secret
    report = json.loads(payload)
    header = report["header"]
    assert set(header) == {
        "model",
        "provider",
        "base_url_host",
        "engine_version",
        "set_version",
        "set_sha256",
        "date",
        "reps",
        "spent_usd",
    }
    assert header["model"] == "qwen/qwen3-235b-a22b-2507"
    assert header["provider"] == "Parasail"
    assert header["base_url_host"] == "openrouter.ai"
    assert header["engine_version"] == __version__
    assert header["set_version"] == "v1"
    assert header["set_sha256"] == SET_V1_SHA256
    assert header["reps"] == 1
    assert header["spent_usd"] == 1.0
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", header["date"])
    assert "openrouter.ai" in text
    assert "?" not in payload


def test_report_is_written_under_the_user_state_dir_not_under_cwd_strata(
    _offline, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _box, state = _offline
    project = Path.cwd()
    strata_dir = project / ".strata"
    strata_dir.mkdir()
    (strata_dir / "config.toml").write_text("preexisting = true\n", encoding="utf-8")
    before = sorted(path.relative_to(strata_dir).as_posix() for path in strata_dir.rglob("*"))
    rc, _out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 0, err
    after = sorted(path.relative_to(strata_dir).as_posix() for path in strata_dir.rglob("*"))
    assert after == before
    path = report_json(state)
    assert "1.19.0" in path.parts
    assert "Parasail" not in path.parts  # provider unset in this test
    assert "openrouter.ai" in path.parts
    assert any("qwen" in part for part in path.parts)
    assert project.resolve() not in path.resolve().parents
    assert not str(path.resolve()).startswith(str(project.resolve()))


def test_text_prints_the_engine_defences_label_and_the_relation_line(
    _offline, capsys: pytest.CaptureFixture[str]
) -> None:
    box, state = _offline
    box.relation_calls = 2
    box.relation_errors = 1
    box.main_call_errors = 3
    box.hearsay_ids.add(first_hand_id())
    rc, out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 0, err
    report = json.loads(report_json(state).read_text(encoding="utf-8"))
    label = report["hard_legs"]["engine_defences"]["label"]
    assert label.startswith("engine defences held:")
    assert label in out
    security = json.dumps(report["hard_legs"]["security"], sort_keys=True)
    assert security in out
    operational = report["operational"]
    relation = (
        f"relation re-asks: {operational['relation_calls']} calls, "
        f"{operational['relation_errors']} errors; "
        f"main-call errors: {operational['main_call_errors']}"
    )
    assert operational["relation_calls"] > 0
    assert operational["relation_errors"] > 0
    assert operational["main_call_errors"] > 0
    assert relation in out
    assert report["verdict"].startswith("meets the judge-check v1 bar")
    assert "Reason honesty beyond the regex check is not measured in v1." in out


def test_fields_beyond_the_frozen_interface_stay_in_json_and_are_not_printed(
    _offline, capsys: pytest.CaptureFixture[str]
) -> None:
    box, state = _offline
    box.hearsay_ids.add(first_hand_id())
    rc, out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 0, err
    report = json.loads(report_json(state).read_text(encoding="utf-8"))
    assert report["operational"]["hearsay_labels_on_first_hand"] >= 1
    assert "not_held_items" in report["hard_legs"]["engine_defences"]
    assert "hearsay_labels_on_first_hand" not in out
    assert "not_held_items" not in out
    text = (report_json(state).parent / "report.txt").read_text(encoding="utf-8")
    assert "hearsay_labels_on_first_hand" not in text
    assert "not_held_items" not in text


def test_compare_report_is_passed_through_to_the_engine_compare(
    _offline, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import strata.judgecheck as judgecheck

    seen: dict[str, int] = {}

    def fake_compare(incumbent, candidate, items):  # noqa: ANN001
        seen["incumbent"] = len(list(incumbent))
        seen["candidate"] = len(list(candidate))
        seen["items"] = len(items)
        return {"flips": [{"item_id": "from-engine-compare"}], "suites": {}}

    monkeypatch.setattr(judgecheck, "compare", fake_compare)
    saved = tmp_path / "incumbent.json"
    saved.write_text(
        json.dumps(
            {
                "judge_label": "incumbent",
                "verdict": "meets the judge-check v1 bar for Strata 1.19.0",
                "per_item": {"j1h-021": {"suite": "h2", "decisions": ["decline"]}},
            }
        ),
        encoding="utf-8",
    )
    rc, out, err = invoke(
        capsys,
        [
            "judge-check",
            "--yes",
            "--price",
            "0.2,0.8",
            "--reps",
            "1",
            "--compare",
            str(saved),
        ],
    )
    assert rc == 0, err
    assert seen["incumbent"] == 1
    assert seen["candidate"] > 1
    assert seen["items"] > 1
    assert "from-engine-compare" in out


def test_missing_compare_report_exits_3_before_the_run(
    _offline, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    box, state = _offline
    missing = tmp_path / "no-such-report.json"
    rc, _out, err = invoke(
        capsys,
        ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1", "--compare", str(missing)],
    )
    assert rc == 3
    assert "not found" in err
    assert box.calls == []
    assert list(state.rglob("report.json")) == []


def test_default_reps_is_3_and_max_usd_is_forwarded(
    _offline, capsys: pytest.CaptureFixture[str]
) -> None:
    box, _state = _offline
    rc, _out, err = invoke(
        capsys, ["judge-check", "--yes", "--price", "0.14,0.80,0.05", "--max-usd", "2.5"]
    )
    assert rc == 0, err
    assert box.calls[0]["reps"] == 3
    assert box.calls[0]["max_usd"] == 2.5
    assert box.calls[0]["n_items"] == 78


def test_docs_page_states_what_judge_check_measures_and_its_limits() -> None:
    path = Path(__file__).resolve().parents[1] / "docs" / "judge-check.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert (
        "the public set will be tuned against; a pass shows the model meets this set, "
        "not that it is good in general"
    ) in text
    assert "Reason honesty beyond the regex check is not measured in v1." in text
    limit = (
        "a context note cannot retire a directive (its ops are dropped), "
        "so that case is not tested."
    )
    assert limit in text
    for code in ("0", "1", "2", "3"):
        assert f"| {code} |" in text
    lowered = text.lower()
    for banned in ("safe", "approved", "recommended"):
        assert banned not in lowered
    assert "qwen/qwen3-235b-a22b-2507" in text
    assert "Parasail" in text
