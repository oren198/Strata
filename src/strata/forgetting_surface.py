"""Display and counts for directive forgetting (v1.20 peripherals).

Nothing here retires a directive, and nothing here writes a candidate's
``note``. The engine stays in :mod:`strata.forgetting`. Age and disuse
carry the words "prompts the question only" on the way out only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict

from strata.record_store import RetirementCandidate

PROMPT_ONLY = "prompts the question only"
"""Appended to the display of an age or disuse candidate. Never stored."""

OPERATOR_ITEMS_NOTE = "operator items not counted"
"""One line on every metrics surface. Operator-item forgetting is left out."""

DISMISS_DAYS_MIN = 1
DISMISS_DAYS_MAX = 365

_GROUNDS = ("date_near", "condition_evidence", "age", "disuse")
_STATES = ("open", "dismissed", "acted")
_PROMPT_GROUNDS = frozenset({"age", "disuse"})


def reject_dismiss_days(days: object) -> int:
    """The surface limit on a dismissal snooze. Call this before the engine.

    The engine only requires ``days >= 1``. A value outside 1..365, including
    366, is a surface error and must not reach ``dismiss_candidate``.

    Raises:
        ValueError: *days* is not an integer from 1 to 365.
    """
    if isinstance(days, bool) or not isinstance(days, int):
        raise ValueError("days must be an integer from 1 to 365.")
    if days < DISMISS_DAYS_MIN or days > DISMISS_DAYS_MAX:
        raise ValueError("days must be an integer from 1 to 365.")
    return days


def present_candidate(candidate: RetirementCandidate) -> dict:
    """One candidate for a surface. ``note`` is the stored value, unchanged.

    Age and disuse gain ``display``, the stored note with
    :data:`PROMPT_ONLY` appended. Other grounds leave ``display`` as the
    stored note. The caller does not write either field back.
    """
    row = asdict(candidate)
    stored = (candidate.note or "").strip()
    if candidate.ground in _PROMPT_GROUNDS:
        row["display"] = f"{stored} {PROMPT_ONLY}".strip()
    else:
        row["display"] = candidate.note
    return row


def forgetting_metrics(
    record_store,
    *,
    scope_ids: Sequence[str],
    scope_id: str | None = None,
) -> dict:
    """Scope-directive counts. Operator items are not included.

    Lapses, releases, retractions, and condition_judged are separate counts
    over the scope ``retirements`` table. Candidates are open / dismissed /
    acted per ground, excluding ``op_`` rows. Condensation is the store's
    own dict, including ``unmeasured`` and ``unmeasured_by_reason``.
    """
    scopes = [scope_id] if scope_id is not None else list(scope_ids)
    retirements = [
        row
        for sid in scopes
        for row in record_store.list_retirements(scope_id=sid)
        if not row.directive_id.startswith("op_")
    ]
    candidates = {ground: {state: 0 for state in _STATES} for ground in _GROUNDS}
    for sid in scopes:
        rows = record_store.list_retirement_candidates(scope_id=sid, states=_STATES)
        for row in rows:
            if row.directive_id.startswith("op_"):
                continue
            bucket = candidates.get(row.ground)
            if bucket is not None and row.state in bucket:
                bucket[row.state] += 1
    return {
        "lapses": sum(1 for row in retirements if row.cause == "date_lapse"),
        "releases": sum(1 for row in retirements if row.kind == "currency"),
        "retractions": sum(1 for row in retirements if row.kind == "correction"),
        "condition_judged": sum(1 for row in retirements if row.cause == "condition_judged"),
        "candidates": candidates,
        "condensation": record_store.condensation_order_metrics(scope_id=scope_id),
        "operator_items": OPERATOR_ITEMS_NOTE,
    }


def format_forgetting_metrics(report: dict) -> str:
    """Text form. Measured, unmeasured, and inversions stay separate numbers."""
    lines = [
        f"lapses: {report['lapses']}",
        f"releases: {report['releases']}",
        f"retractions: {report['retractions']}",
        f"condition_judged: {report['condition_judged']}",
        "candidates:",
    ]
    for ground, counts in report["candidates"].items():
        lines.append(
            f"  {ground}: open {counts['open']}, "
            f"dismissed {counts['dismissed']}, acted {counts['acted']}"
        )
    condensation = report["condensation"]
    lines.extend(
        [
            f"condensation writes: {condensation['writes']}",
            f"condensation measured: {condensation['measured']}",
            f"condensation unmeasured: {condensation['unmeasured']}",
            f"condensation unmeasured_by_reason: {condensation['unmeasured_by_reason']}",
            f"condensation inversions: {condensation['inversions']}",
            f"condensation measured_with_inversion: {condensation['measured_with_inversion']}",
            f"condensation coverage: {condensation['coverage']}",
            report["operator_items"],
        ]
    )
    return "\n".join(lines)
