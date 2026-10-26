"""Directive forgetting, core (v1.20; design: a directive stops binding only through an act of
its authority, never by decay).

This module holds the acts and checks around a directive's END CONDITION: validating the one a
contribution carries, the date-lapse sweep, the unjudged retire act, the owner-only
confirmation of a judged condition, and the SQL-only retirement candidates. Nothing here makes a
judge call, and nothing here creates a directive.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime

from strata.summary_store import EndCondition

CONDITION_MAX_CHARS = 200
"""A judged end condition is a short sentence, not a document."""


def validate_ends(
    ends: Mapping[str, object] | None, *, now: datetime | None = None
) -> EndCondition | None:
    """The :class:`EndCondition` a contribution's ``ends`` argument names, or ``None``.

    ``{"until": "YYYY-MM-DD"}`` is a date: it must parse and must not be in the past. Today is
    accepted (the directive binds through 23:59:59 UTC that day). ``{"when": "<text>"}`` is a
    judged condition: at least one character after stripping, at most
    :data:`CONDITION_MAX_CHARS`. Exactly one of the two keys; nothing else.

    Raises:
        ValueError: any other shape, an unparseable or past date, an empty or over-long
            condition.
    """
    if ends is None:
        return None
    if not isinstance(ends, Mapping):
        raise ValueError("ends must be an object with exactly one of 'until' or 'when'.")
    keys = set(ends)
    if keys == {"until"}:
        raw = ends["until"]
        if not isinstance(raw, str):
            raise ValueError("ends.until must be a date string, YYYY-MM-DD.")
        try:
            until = date.fromisoformat(raw.strip())
        except ValueError as exc:
            raise ValueError(f"ends.until {raw!r} is not a date (YYYY-MM-DD).") from exc
        today = (now or datetime.now(tz=UTC)).astimezone(UTC).date()
        if until < today:
            raise ValueError(f"ends.until {until.isoformat()} is in the past.")
        return EndCondition(kind="date", until=until.isoformat())
    if keys == {"when"}:
        raw = ends["when"]
        if not isinstance(raw, str):
            raise ValueError("ends.when must be text.")
        condition = raw.strip()
        if not condition:
            raise ValueError("ends.when must not be empty.")
        if len(condition) > CONDITION_MAX_CHARS:
            raise ValueError(f"ends.when is longer than {CONDITION_MAX_CHARS} characters.")
        return EndCondition(kind="judged", condition=condition)
    raise ValueError("ends must be an object with exactly one of 'until' or 'when'.")
