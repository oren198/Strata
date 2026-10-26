"""Render a judge-check score and remember it under the user's state directory.

The JSON report is ``JudgeCheckScore.to_dict()`` plus a run header. Nothing here
re-scores a run, and nothing here imports strata-evals. The text summary prints the
engine's verdict and the engine's own figures. Fields the frozen interface does not
tell us to print stay in the JSON and are left out of the text.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SET_VERSION = "v1"

_HEADER_KEYS = (
    "model",
    "provider",
    "base_url_host",
    "engine_version",
    "set_version",
    "set_sha256",
    "date",
    "reps",
    "spent_usd",
)

# Operational keys the frozen interface lists for the text summary. Relation counts are
# printed on their own line. Anything else in ``operational`` (for example
# ``hearsay_labels_on_first_hand``) stays in the JSON only.
_OPERATIONAL_LINES = (
    "judgments",
    "calls",
    "tool_call_failures",
    "tool_call_recovered",
    "judge_errors",
    "errors_by_status",
    "timeouts",
    "error_rate",
    "timeout_rate",
    "only_429_errors",
    "meets",
)

_SEGMENT_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def today_iso() -> str:
    """The UTC date of a run, ``YYYY-MM-DD``."""
    return datetime.now(UTC).date().isoformat()


def user_state_dir() -> Path:
    """``$XDG_STATE_HOME/strata/judge-check``, or ``~/.local/state/strata/judge-check``.

    Never the project ``.strata/`` directory and never the current working directory.
    """
    raw = os.environ.get("XDG_STATE_HOME")
    base = Path(raw).expanduser() if raw else Path.home() / ".local" / "state"
    return base / "strata" / "judge-check"


def _segment(value: object) -> str:
    """One path component. Slashes and ``..`` cannot escape the state directory."""
    if value is None:
        return "none"
    text = str(value).strip()
    if not text:
        return "none"
    cleaned = _SEGMENT_UNSAFE.sub("_", text).strip("._")
    if cleaned in {"", ".", ".."}:
        return "none"
    return cleaned[:180]


def report_dir(*, engine_version: str, model: str, provider: object, host: object) -> Path:
    """Directory keyed by engine version, model, provider and base-URL host."""
    return (
        user_state_dir()
        / _segment(engine_version)
        / _segment(model)
        / _segment(provider)
        / _segment(host)
    )


def run_header(
    described: dict[str, Any],
    *,
    engine_version: str,
    set_sha256: str,
    reps: int,
    spent_usd: float,
    date: str | None = None,
) -> dict[str, Any]:
    """The only judge facts a report may carry: model, provider, host.

    ``described`` is :func:`strata.judgecheck.describe_judge`. Only those three fields
    are copied. A key, a full URL, a query string, or any other environment value is
    not a header field, so it cannot be written.
    """
    header = {
        "model": described.get("model"),
        "provider": described.get("provider"),
        "base_url_host": described.get("base_url_host"),
        "engine_version": engine_version,
        "set_version": SET_VERSION,
        "set_sha256": set_sha256,
        "date": today_iso() if date is None else date,
        "reps": reps,
        "spent_usd": spent_usd,
    }
    return {key: header[key] for key in _HEADER_KEYS}


def build_report(score_dict: dict[str, Any], header: dict[str, Any]) -> dict[str, Any]:
    """``to_dict()`` unchanged, plus a ``header`` object.

    Nested values are the engine's objects. This does not recompute a score.
    """
    report = dict(score_dict)
    report["header"] = {key: header[key] for key in _HEADER_KEYS}
    return report


def _show(value: object) -> str:
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def render_text(report: dict[str, Any]) -> str:
    """Text summary. The verdict line is the engine's string, copied verbatim."""
    header = report["header"]
    lines = [
        f"model: {header['model']}",
        f"provider: {header['provider']}",
        f"base URL host: {header['base_url_host']}",
        f"engine version: {header['engine_version']}",
        f"set version: {header['set_version']}",
        f"set sha256: {header['set_sha256']}",
        f"date: {header['date']}",
        f"reps: {header['reps']}",
        f"spent_usd: {header['spent_usd']}",
        "",
        str(report.get("verdict", "")),
        "",
        "suites:",
    ]
    for name, entry in (report.get("suites") or {}).items():
        lines.append(f"  {name}: {_show(entry)}")
    lines.append("")
    lines.append("hard legs:")
    hard = report.get("hard_legs") or {}
    if "security" in hard:
        lines.append(f"  security: {_show(hard['security'])}")
    defences = hard.get("engine_defences") or {}
    if isinstance(defences, dict) and defences.get("label"):
        lines.append(f"  {defences['label']}")
    elif isinstance(defences, dict) and "held" in defences and "items" in defences:
        lines.append(f"  engine defences held: {defences['held']}/{defences['items']}")
    for name in ("must_admit", "p5_label"):
        if name in hard:
            lines.append(f"  {name}: {_show(hard[name])}")
    lines.append("")
    lines.append("operational:")
    operational = report.get("operational") or {}
    for key in _OPERATIONAL_LINES:
        if key in operational:
            lines.append(f"  {key}: {_show(operational[key])}")
    if all(key in operational for key in ("relation_calls", "relation_errors", "main_call_errors")):
        lines.append(
            "  relation re-asks: "
            f"{operational['relation_calls']} calls, "
            f"{operational['relation_errors']} errors; "
            f"main-call errors: {operational['main_call_errors']}"
        )
    lines.append("")
    lines.append("Reason honesty beyond the regex check is not measured in v1.")
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], text: str, *, out: Path | None = None) -> Path:
    """Write JSON and text under the state dir. ``out``, when set, also receives the JSON."""
    header = report["header"]
    directory = report_dir(
        engine_version=str(header["engine_version"]),
        model=str(header["model"]),
        provider=header["provider"],
        host=header["base_url_host"],
    )
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    path = directory / "report.json"
    path.write_text(payload, encoding="utf-8")
    (directory / "report.txt").write_text(text, encoding="utf-8")
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
    return path


def _read_report(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _one_line_date(value: object) -> str | None:
    """A report date that can sit inside one doctor line."""
    if isinstance(value, str) and value and "\n" not in value and "\r" not in value:
        return value
    return None


def _saved_report_date() -> str | None:
    """The saved report's date, when this engine version has one single-line date."""
    from strata import __version__
    from strata.judgecheck.config import describe_judge
    from strata.settings import Settings

    settings = Settings()
    described = describe_judge(settings)
    path = (
        report_dir(
            engine_version=__version__,
            model=str(described.get("model")),
            provider=described.get("provider"),
            host=described.get("base_url_host"),
        )
        / "report.json"
    )
    report = _read_report(path)
    if not isinstance(report, dict):
        return None
    header = report.get("header")
    if not isinstance(header, dict) or header.get("engine_version") != __version__:
        return None
    return _one_line_date(header.get("date"))


def report_not_understood_message() -> str:
    """Suffix for an unrecognised report. One line, and this does not raise.

    The date is the saved report's date when that date is a single line.
    Otherwise it is ``unknown``. The verdict text is not copied.
    """
    date = "unknown"
    try:
        date = _saved_report_date() or "unknown"
    except Exception:  # noqa: BLE001 — a date lookup must not raise into doctor
        date = "unknown"
    text = f"report not understood (report {date}); re-run"
    if "\n" in text or "\r" in text:
        return "report not understood (report unknown); re-run"
    return text


def doctor_judge_check_message() -> str:
    """The suffix ``strata doctor`` prints after ``Judge check:``.

    Reads a local file only: no judge call, no prompt. No report for this
    engine version, or the verdict ``unmeasured: no reference bars in this
    build``, is the unmeasured line. An incomplete verdict, an unrecognised
    verdict string, and a lookup exception each have their own line.
    """
    unmeasured = "unmeasured for this engine version"
    try:
        from strata import __version__
        from strata.judgecheck.config import describe_judge
        from strata.settings import Settings

        settings = Settings()
        described = describe_judge(settings)
        path = (
            report_dir(
                engine_version=__version__,
                model=str(described.get("model")),
                provider=described.get("provider"),
                host=described.get("base_url_host"),
            )
            / "report.json"
        )
        report = _read_report(path)
    except Exception as exc:  # noqa: BLE001 — this line must not raise into doctor
        return f"could not read the report ({type(exc).__name__})"
    if report is None:
        return unmeasured
    header = report.get("header")
    if not isinstance(header, dict) or header.get("engine_version") != __version__:
        return unmeasured
    verdict = report.get("verdict")
    if not isinstance(verdict, str):
        return unmeasured
    date = _one_line_date(header.get("date")) or "unknown"
    if verdict.startswith("meets the judge-check v1 bar"):
        return f"meets the v1 bar (report {date})"
    if verdict.startswith("does not meet"):
        legs = report.get("failed_legs")
        inside = ", ".join(str(leg) for leg in legs) if isinstance(legs, list) else ""
        return f"does not meet ({inside})"
    if verdict.startswith("incomplete"):
        return f"incomplete (report {date}); re-run"
    if verdict == "unmeasured: no reference bars in this build":
        return unmeasured
    return f"report not understood (report {date}); re-run"
