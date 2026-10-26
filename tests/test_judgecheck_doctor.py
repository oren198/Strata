"""``strata doctor`` gains one judge-check line and does not change its exit status."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from tests.judgecheck_peripherals_support import (
    install_offline_guards,
    invoke,
    judge_must_decline_id,
    report_json,
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    return install_offline_guards(monkeypatch, tmp_path)


def _doctor(capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    from strata.__main__ import cmd_doctor

    rc = cmd_doctor(argparse.Namespace())
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _judge_lines(out: str, err: str) -> list[str]:
    return [line for line in (out + err).splitlines() if "Judge check:" in line]


def test_doctor_unmeasured_for_this_engine_version(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, err = _doctor(capsys)
    lines = _judge_lines(out, err)
    assert lines == ["  ✓ Judge check: unmeasured for this engine version"] or _ascii(lines)
    assert len(lines) == 1
    assert "Judge check: unmeasured for this engine version" in out
    assert "Judge check:" not in err
    # An unregistered directory fails doctor's hard checks. The new line does not
    # own that status: it is on stdout, with the passing checks.
    assert rc == 1


def _ascii(lines: list[str]) -> bool:
    return lines == ["  OK Judge check: unmeasured for this engine version"]


def test_doctor_meets_line_names_the_report_date(
    _offline, capsys: pytest.CaptureFixture[str]
) -> None:
    _box, state = _offline
    rc, _out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 0, err
    report = json.loads(report_json(state).read_text(encoding="utf-8"))
    assert report["verdict"].startswith("meets the judge-check v1 bar")
    doc_rc, out, err = _doctor(capsys)
    assert f"Judge check: meets the v1 bar (report {report['header']['date']})" in out
    assert len(_judge_lines(out, err)) == 1
    assert "Judge check:" not in err
    assert doc_rc == 1


def test_doctor_does_not_meet_line_names_the_failed_legs(
    _offline, capsys: pytest.CaptureFixture[str]
) -> None:
    box, state = _offline
    box.flip_ids.add(judge_must_decline_id())
    rc, _out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 1, err
    report = json.loads(report_json(state).read_text(encoding="utf-8"))
    legs = ", ".join(report["failed_legs"])
    doc_rc, out, err = _doctor(capsys)
    assert f"Judge check: does not meet ({legs})" in out
    assert "security leg" in legs
    assert len(_judge_lines(out, err)) == 1
    assert "Judge check:" not in err
    assert doc_rc == 1


def test_doctor_line_does_not_change_exit_status(
    _offline,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A registered project stays exit 0 when the saved report does not meet."""
    from strata.__main__ import cmd_register
    from strata.migrator import run_migrations

    box, state = _offline
    # Register at tmp_path itself. The suite pins project-config discovery to a
    # child of tmp_path and walks upward, so the config has to sit on that walk.
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_root")
    monkeypatch.setenv("STRATA_AGENT_SKILL", "strata-worker")
    monkeypatch.setenv("STRATA_AGENT_SESSION_ID", "sess_test")
    register_rc = cmd_register(
        argparse.Namespace(path=str(tmp_path), diff=False, bootstrap_venv=False)
    )
    assert register_rc == 0
    run_migrations(str(tmp_path / ".strata" / "strata.db"))
    capsys.readouterr()

    before_rc, before_out, before_err = _doctor(capsys)
    assert before_rc == 0, before_out + before_err
    assert "Judge check: unmeasured for this engine version" in before_out
    assert len(_judge_lines(before_out, before_err)) == 1

    box.flip_ids.add(judge_must_decline_id())
    run_rc, _out, err = invoke(
        capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"]
    )
    assert run_rc == 1, err
    report = json.loads(report_json(state).read_text(encoding="utf-8"))
    after_rc, after_out, after_err = _doctor(capsys)
    assert after_rc == 0, after_out + after_err
    legs = ", ".join(report["failed_legs"])
    assert f"Judge check: does not meet ({legs})" in after_out
    assert len(_judge_lines(after_out, after_err)) == 1
    assert before_rc == after_rc


def test_doctor_incomplete_report_says_re_run(_offline, capsys: pytest.CaptureFixture[str]) -> None:
    """An incomplete saved report is its own line, not the unmeasured line."""
    from strata.judgecheck.runner import IncompleteInfo

    box, state = _offline
    box.incomplete = IncompleteInfo("cap reached", 4, 20)
    rc, _out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 2, err
    report = json.loads(report_json(state).read_text(encoding="utf-8"))
    assert report["verdict"] == "incomplete: cap reached at 4/20"
    date = report["header"]["date"]
    doc_rc, out, err = _doctor(capsys)
    line = f"Judge check: incomplete (report {date}); re-run"
    assert line in out
    assert _judge_lines(out, err) == [f"  ✓ {line}"] or _judge_lines(out, err) == [f"  OK {line}"]
    assert "unmeasured" not in out
    assert "Judge check:" not in err
    assert doc_rc == 1


def test_doctor_no_bars_verdict_stays_unmeasured(
    _offline, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The no-bars verdict stays the unmeasured line. It is not called incomplete."""
    monkeypatch.setattr("strata.judgecheck.BARS_V1", None)
    _box, state = _offline
    rc, _out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 2, err
    report = json.loads(report_json(state).read_text(encoding="utf-8"))
    assert report["verdict"] == "unmeasured: no reference bars in this build"
    doc_rc, out, err = _doctor(capsys)
    assert "Judge check: unmeasured for this engine version" in out
    assert "incomplete" not in out
    assert len(_judge_lines(out, err)) == 1
    assert doc_rc == 1


def test_doctor_unrecognised_verdict_says_report_not_understood(
    _offline, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unrecognised verdict string is not printed as unmeasured."""
    _box, state = _offline
    rc, _out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 0, err
    path = report_json(state)
    report = json.loads(path.read_text(encoding="utf-8"))
    report["verdict"] = "not-a-known-line VERDICT-SECRET"
    path.write_text(json.dumps(report), encoding="utf-8")
    date = report["header"]["date"]
    doc_rc, out, err = _doctor(capsys)
    line = f"Judge check: report not understood (report {date}); re-run"
    assert line in out
    assert _judge_lines(out, err) == [f"  ✓ {line}"] or _judge_lines(out, err) == [f"  OK {line}"]
    assert "VERDICT-SECRET" not in out + err
    assert "unmeasured" not in "".join(_judge_lines(out, err))
    assert "Judge check:" not in err
    assert doc_rc == 1


def test_doctor_lookup_exception_names_the_class_only(
    _offline, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lookup exception names the class. The exception message is not printed."""

    class LookupBoom(RuntimeError):
        pass

    def explode(_settings: object) -> dict[str, str]:
        raise LookupBoom("sk-or-v1-THIS-MESSAGE-MUST-NOT-PRINT")

    monkeypatch.setattr("strata.judgecheck.config.describe_judge", explode)
    doc_rc, out, err = _doctor(capsys)
    line = "Judge check: could not read the report (LookupBoom)"
    assert line in out
    assert _judge_lines(out, err) == [f"  ✓ {line}"] or _judge_lines(out, err) == [f"  OK {line}"]
    assert "THIS-MESSAGE-MUST-NOT-PRINT" not in out + err
    assert "unmeasured" not in "".join(_judge_lines(out, err))
    assert "Judge check:" not in err
    assert doc_rc == 1


def test_doctor_multiline_message_is_report_not_understood(
    _offline, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A newline in the judge-check message is an unrecognised report."""
    _box, state = _offline
    rc, _out, err = invoke(capsys, ["judge-check", "--yes", "--price", "0.2,0.8", "--reps", "1"])
    assert rc == 0, err
    report = json.loads(report_json(state).read_text(encoding="utf-8"))
    date = report["header"]["date"]

    def multiline() -> str:
        return f"meets the v1 bar (report {date})\nMULTI-LINE-SECRET"

    monkeypatch.setattr("strata.judgecheck.report.doctor_judge_check_message", multiline)
    doc_rc, out, err = _doctor(capsys)
    line = f"Judge check: report not understood (report {date}); re-run"
    assert line in out
    assert _judge_lines(out, err) == [f"  ✓ {line}"] or _judge_lines(out, err) == [f"  OK {line}"]
    assert "MULTI-LINE-SECRET" not in out + err
    assert "unmeasured" not in "".join(_judge_lines(out, err))
    assert "\n" not in _judge_lines(out, err)[0]
    assert "Judge check:" not in err
    assert doc_rc == 1


def test_doctor_does_not_run_the_judge_or_prompt(
    _offline, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    box, _state = _offline

    def boom(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("doctor prompted")

    monkeypatch.setattr("builtins.input", boom)
    rc, out, err = _doctor(capsys)
    assert "Judge check: unmeasured for this engine version" in out
    assert rc == 1
    assert box.calls == []
    assert "Judge check:" not in err
