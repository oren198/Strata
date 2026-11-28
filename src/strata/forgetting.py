"""Directive forgetting, core (v1.20; design: a directive stops binding only through an act of
its authority, never by decay).

This module holds the acts and checks around a directive's END CONDITION: validating the one a
contribution carries, the date-lapse sweep, the unjudged retire act, the owner-only
confirmation of a judged condition, and the SQL-only retirement candidates. Nothing here makes a
judge call, and nothing here creates a directive.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from strata.change_events import emit as emit_change_event
from strata.change_events import new_change_id
from strata.locks import scope_lock
from strata.record_store import (
    ContributorRef,
    Retirement,
    RetirementCandidate,
    RetirementRequest,
)
from strata.summary_store import Directive, EndCondition

if TYPE_CHECKING:
    from strata.fleet_config import FleetConfig
    from strata.record_store import RecordStore
    from strata.summary_store import SummaryStore

_logger = logging.getLogger("strata.forgetting")

RetireKind = Literal["currency", "correction"]
"""``currency``: a release (right to follow, no longer binds). ``correction``: a retraction
(should not have bound). The reader-facing labels are "retired" and "retracted"."""

RETIRE_KINDS: tuple[str, ...] = ("currency", "correction")

ENGINE_ACTOR = "engine"
"""The actor of a mechanical act (a date lapse). Never accepted through the public act."""

DATE_NEAR_DAYS = 7
CANDIDATE_AGE_DAYS = 90
CANDIDATE_DISUSE_DAYS = 60
_CANDIDATE_RECOMPUTE_SECONDS = 3600


class PositionGateRefusal(PermissionError):
    """A retire act by a session that is not bound to the owning scope."""


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


# -- the retire act ------------------------------------------------------------------------


def retire_directive(
    scope_id: str,
    directive_id: str,
    kind: RetireKind,
    notice: str,
    actor: ContributorRef,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
) -> Retirement | RetirementRequest:
    """Retire one of *scope_id*'s own directives, in person, with a notice. NOT judged.

    In a level-1 review scope (ADR 0018) this records a held retirement REQUEST instead and
    returns it; the directive keeps binding until the operator approves.

    The authority's act: only a session bound to the owning scope may make it. *actor* MUST
    come from the server-side binding of the calling session, never from a tool argument; a
    caller that builds its own :class:`ContributorRef` can name any scope, and this function
    cannot tell. It refuses an actor whose scope differs from *scope_id* and the
    :data:`ENGINE_ACTOR` session, so the public path can never forge a mechanical act.

    No new directive is created. The directive leaves the owner's summary at once (no grace
    period), the ordinary ``directive_retired`` change event goes to every reader, and the
    retirement row (kind, notice, actor) is the permanent record event.

    Raises:
        PositionGateRefusal: *actor* is not bound to *scope_id*, or is the engine actor.
        ValueError: unknown *kind*, a blank *notice*, or an unknown scope.
        KeyError: *directive_id* is not in the scope's current summary.
    """
    if actor.scope_id != scope_id or actor.session_id == ENGINE_ACTOR:
        raise PositionGateRefusal(
            f"Only a session bound to {scope_id!r} may retire its directives "
            f"(got a session of {actor.scope_id!r})."
        )
    if kind not in RETIRE_KINDS:
        raise ValueError(f"kind must be one of {RETIRE_KINDS}, got {kind!r}.")
    scope = fleet.get_scope(scope_id)
    if scope is not None and scope.human_review >= 1:
        # ADR 0018 D5: at level 1 a bound session's own retire is a HELD request. The directive
        # keeps binding for everyone and no notice goes out until the operator enacts it.
        from strata.review import request_retirement  # noqa: PLC0415

        return request_retirement(
            scope_id,
            directive_id,
            kind,
            notice,
            actor_session_id=actor.session_id,
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
        )
    with scope_lock(scope_id):
        return _retire_locked(
            scope_id,
            directive_id,
            kind,
            notice,
            actor_session_id=actor.session_id,
            retired_by="scope-session",
            cause="act",
            end_condition=None,
            evidence_contribution_id=None,
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
        )


def _retire_locked(
    scope_id: str,
    directive_id: str,
    kind: str,
    notice: str,
    *,
    actor_session_id: str,
    retired_by: str,
    cause: str,
    end_condition: EndCondition | None,
    evidence_contribution_id: str | None,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
) -> Retirement:
    """The retire act itself. PRIVATE: the caller holds ``scope_lock(scope_id)`` and has done
    the authority check; ``cause``, the end condition and the evidence are set only by the
    date-lapse sweep, never by a public caller. A confirmed judged end condition is recorded on
    its own path (``app._write_amendment``), not through this function."""

    if fleet.get_scope(scope_id) is None:
        raise ValueError(f"Scope not found: {scope_id!r}")
    if not notice or not notice.strip():
        raise ValueError("A retirement needs a notice of at least one sentence.")
    summary = summary_store.read(scope_id)
    existing = None
    if summary is not None:
        existing = next((d for d in summary.directives if d.id == directive_id), None)
    if summary is None or existing is None:
        raise KeyError(
            f"Directive {directive_id!r} is not in scope {scope_id!r}'s current summary."
        )
    # v1.22: the retire act goes through the amendment intent journal. The intent comes first, then
    # the summary file, then the retirement row, the withdrawals and the reader notice, each
    # idempotent per intent, then the intent is marked done. A death in between leaves a pending
    # intent that recovery replays (the file was written) or abandons (it was not).
    remaining = [d for d in summary.directives if d.id != directive_id]
    plan = {
        "type": "retire_act",
        "judged_ids": [],
        "directive_id": directive_id,
        "kind": kind,
        "notice": notice,
        "actor_session_id": actor_session_id,
        "retired_by": retired_by,
        "cause": cause,
        "end_condition": end_condition.model_dump_json() if end_condition is not None else None,
        "evidence_contribution_id": evidence_contribution_id,
        "before": existing.content,
        "remaining_ids": sorted(d.id for d in remaining),
        "change_id": new_change_id(),
    }
    intent_id = record_store.create_amendment_intent(
        scope_id=scope_id,
        expected_version=summary.version + 1,
        payload_json=json.dumps(plan),
    )
    summary_store.write(
        scope_id,
        summary.model_copy(
            update={"directives": remaining, "updated_at": datetime.now(tz=UTC).isoformat()}
        ),
    )
    retirement = apply_retire_effects(
        plan,
        scope_id=scope_id,
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        intent_id=intent_id,
    )
    record_store.mark_amendment_intent(intent_id, "done")
    return retirement


def apply_retire_effects(
    plan: dict,
    *,
    scope_id: str,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    intent_id: str,
) -> Retirement:
    """The after-write steps of a retire act, each idempotent per intent, so the act and a
    recovery replay run the same code: the retirement row (unique per scope, directive and intent),
    the withdrawal of published items anchored to the directive, the reader notice (one per change
    id and item, and the change id is in the plan), and the candidates it closes."""
    from strata.publication import propagate_directive_removals  # noqa: PLC0415

    directive_id = plan["directive_id"]
    retirement = record_store.append_retirement(
        scope_id=scope_id,
        directive_id=directive_id,
        retired_by=plan["retired_by"],
        reason=plan["notice"],
        kind=plan["kind"],
        notice=plan["notice"],
        actor_session_id=plan["actor_session_id"],
        cause=plan["cause"],
        end_condition=plan["end_condition"],
        evidence_contribution_id=plan["evidence_contribution_id"],
        intent_id=intent_id,
    )
    propagate_directive_removals(
        scope_id,
        {directive_id},
        retirement.id,
        surviving_directive_ids=set(plan["remaining_ids"]),
        fleet=fleet,
        record_store=record_store,
        summaries_dir=str(summary_store.summaries_dir),
    )
    emit_change_event(
        fleet=fleet,
        record_store=record_store,
        item=directive_id,
        kind="directive_retired",
        source_scope_id=scope_id,
        before=plan["before"],
        wave_ids=[plan["change_id"]],
    )
    for candidate in record_store.list_retirement_candidates(scope_id=scope_id):
        if candidate.directive_id == directive_id:
            record_store.set_retirement_candidate_state(candidate.id, state="acted")
    return retirement


# -- the date-lapse sweep ------------------------------------------------------------------


def _due_dates(summary, today: date) -> list[Directive]:  # noqa: ANN001
    """Directives whose date end condition is over: ``until`` is inclusive, so a directive
    binds through 23:59:59 UTC on that day and is due from the next one."""
    due = []
    for d in summary.directives if summary is not None else []:
        end = d.end_condition
        if end is not None and end.kind == "date" and end.until:
            try:
                if date.fromisoformat(end.until) < today:
                    due.append(d)
            except ValueError:
                continue  # an unparseable date never lapses by mechanism
    return due


def _lapse_notice(directive: Directive, record_store: RecordStore) -> str:
    who = ""
    if directive.set_by:
        setter = record_store.get_contribution(directive.set_by)
        if setter is not None:
            who = f" ({setter.contributor.session_id})"
    return (
        f"end condition set by {directive.set_by or 'its author'}{who}: "
        f"bound until {directive.end_condition.until}."  # type: ignore[union-attr]
    )


def sweep_date_lapses_locked(
    scope_id: str,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    now: datetime | None = None,
) -> list[Retirement]:
    """Retire every directive of *scope_id* whose date end condition is over. No judge call.

    The caller already holds ``scope_lock(scope_id)`` (a judgment runs under it). Idempotent:
    a lapsed directive is gone from the summary, so a second sweep finds nothing."""
    today = (now or datetime.now(tz=UTC)).astimezone(UTC).date()
    done = []
    for directive in _due_dates(summary_store.read(scope_id), today):
        done.append(
            _retire_locked(
                scope_id,
                directive.id,
                "currency",
                _lapse_notice(directive, record_store),
                actor_session_id=ENGINE_ACTOR,
                retired_by="engine:date-lapse",
                cause="date_lapse",
                end_condition=directive.end_condition,
                evidence_contribution_id=None,
                fleet=fleet,
                record_store=record_store,
                summary_store=summary_store,
            )
        )
    return done


def sweep_date_lapses(
    scope_id: str,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    now: datetime | None = None,
) -> list[Retirement]:
    """:func:`sweep_date_lapses_locked` for a caller that does not hold the scope lock.

    Reads the summary first and takes the lock only when something is due, so a quiet read
    never waits behind an in-flight judgment."""
    today = (now or datetime.now(tz=UTC)).astimezone(UTC).date()
    if not _due_dates(summary_store.read(scope_id), today):
        return []
    with scope_lock(scope_id):
        return sweep_date_lapses_locked(
            scope_id,
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
            now=now,
        )


def sweep_for_read(
    scope_id: str,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    now: datetime | None = None,
) -> list[Retirement]:
    """Before a perspective of *scope_id* is composed: lapse every date end condition that is
    over in the scope and in each of its chain ancestors (their directives bind it), and open
    the scope's candidates. No judge call, and no lock unless something is due. Never raises:
    a read must not fail on housekeeping."""
    from strata.perspective import ancestor_directives  # noqa: PLC0415

    done: list[Retirement] = []
    try:
        owners = [
            sid
            for sid, _ds in ancestor_directives(scope_id, fleet=fleet, summary_store=summary_store)
        ]
        for owner in [*owners, scope_id]:
            done += sweep_date_lapses(
                owner, fleet=fleet, record_store=record_store, summary_store=summary_store, now=now
            )
        from strata.operator import sweep_operator_date_lapses  # noqa: PLC0415

        for owner in [*owners, scope_id]:
            sweep_operator_date_lapses(
                owner,
                record_store=record_store,
                summaries_dir=str(summary_store.summaries_dir),
                fleet=fleet,
                now=now,
            )
        compute_retirement_candidates(
            scope_id, record_store=record_store, summary_store=summary_store, now=now
        )
    except Exception:  # noqa: BLE001
        _logger.exception("forgetting housekeeping failed for scope %r", scope_id)
    return done


# -- fulfils and the owner's confirmation (R2) ---------------------------------------------


def validate_fulfils(
    directive_id: str,
    contributor: ContributorRef,
    *,
    fleet: FleetConfig,
    summary_store: SummaryStore,
) -> tuple[str, Directive]:
    """The ``(owning scope id, directive)`` a ``fulfils`` names, or a ValueError.

    The directive must be in the contributor's read surface (its own scope or an ancestor)
    and must carry a judged end condition. Writes nothing."""
    from strata.perspective import ancestor_directives  # noqa: PLC0415

    own = summary_store.read(contributor.scope_id)
    surface = [(contributor.scope_id, list(own.directives) if own else [])]
    surface += [
        (sid, list(ds))
        for sid, ds in ancestor_directives(
            contributor.scope_id, fleet=fleet, summary_store=summary_store
        )
    ]
    from strata.operator import read_operator_layer  # noqa: PLC0415

    for sid in [contributor.scope_id, *[o for o, _ds in surface[1:]]]:
        layer = read_operator_layer(sid, summaries_dir=str(summary_store.summaries_dir))
        surface.append(
            (
                sid,
                [
                    Directive(
                        id=it.id,
                        content=it.content,
                        subject=it.subject,
                        source_scope_id=sid,
                        created_at=it.created_at,
                        end_condition=it.end_condition,
                        set_by=it.set_by,
                    )
                    for it in layer
                    if it.kind == "directive"
                ],
            )
        )
    for owner, directives in surface:
        for d in directives:
            if d.id == directive_id:
                if d.end_condition is None or d.end_condition.kind != "judged":
                    raise ValueError(f"Directive {directive_id!r} has no judged end condition.")
                return owner, d
    raise ValueError(f"Directive {directive_id!r} is not in the contributor's read surface.")


def judged_end_condition_directive(
    scope_id: str, directive_id: str, *, summary_store: SummaryStore
) -> Directive | None:
    """The directive when it is *scope_id*'s own and carries a judged end condition."""
    summary = summary_store.read(scope_id)
    for d in summary.directives if summary is not None else []:
        if d.id == directive_id and d.end_condition and d.end_condition.kind == "judged":
            return d
    return None


def evidence_acceptable(evidence: str | None) -> bool:
    """A confirmation states what the judge checked. It is never "verified" (reason honesty)."""
    return bool(evidence and evidence.strip()) and "verified" not in evidence.lower()


def condition_met_reason(condition: str, evidence: str) -> str:
    """The recorded reason of a confirmed judged end condition."""
    return f'end condition judged met: "{condition}" / {evidence.strip()}'


# -- candidates: prompts, never retirements ------------------------------------------------

_last_computed: dict[tuple[str, str], float] = {}


def compute_retirement_candidates(
    scope_id: str,
    *,
    record_store: RecordStore,
    summary_store: SummaryStore,
    now: datetime | None = None,
    age_days: int = CANDIDATE_AGE_DAYS,
    disuse_days: int = CANDIDATE_DISUSE_DAYS,
    force: bool = False,
) -> list[RetirementCandidate]:
    """Open the candidates for *scope_id*'s directives. SQL only; no judge; at most once an
    hour per scope (``force`` skips the guard). Returns the newly opened ones.

    Grounds: ``date_near`` (the end date is within :data:`DATE_NEAR_DAYS` days),
    ``condition_evidence`` (an admitted ``fulfils`` contributed outside the owning scope),
    ``age`` and ``disuse`` (these two only prompt the question). Nothing here touches a
    directive."""
    now = (now or datetime.now(tz=UTC)).astimezone(UTC)
    key = (str(record_store.db_path), scope_id)
    if (
        not force
        and time.monotonic() - _last_computed.get(key, -1e12) < _CANDIDATE_RECOMPUTE_SECONDS
    ):
        return []
    _last_computed[key] = time.monotonic()
    summary = summary_store.read(scope_id)
    if summary is None:
        return []
    snoozed = {
        (c.directive_id, c.ground)
        for c in record_store.list_retirement_candidates(scope_id=scope_id, states=("dismissed",))
        if c.dismissed_until and c.dismissed_until > now.isoformat()
    }
    opened: list[RetirementCandidate] = []

    def _open(directive_id: str, ground: str, evidence: Sequence[str] = ()) -> None:
        if (directive_id, ground) in snoozed:
            return
        row = record_store.open_retirement_candidate(
            directive_id=directive_id, scope_id=scope_id, ground=ground, evidence_ids=evidence
        )
        if row is not None:
            opened.append(row)

    from strata.operator import read_operator_layer  # noqa: PLC0415

    operator_items = [
        it
        for it in read_operator_layer(scope_id, summaries_dir=str(summary_store.summaries_dir))
        if it.kind == "directive"
    ]
    # An operator item's owner is the operator, so its candidates are the operator's to see.
    for d in [*summary.directives, *operator_items]:
        end = d.end_condition
        if end is not None and end.kind == "date" and end.until:
            try:
                left = (date.fromisoformat(end.until) - now.date()).days
            except ValueError:
                left = None
            if left is not None and 0 <= left <= DATE_NEAR_DAYS:
                _open(d.id, "date_near")
        if end is not None and end.kind == "judged":
            # an operator item is the operator's: evidence from anywhere is worth showing it
            ids = record_store.fulfils_evidence_ids(
                directive_id=d.id,
                outside_scope_id=None if d.id.startswith("op_") else scope_id,
            )
            if ids:
                _open(d.id, "condition_evidence", ids)
        try:
            created = datetime.fromisoformat(d.created_at)
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if now - created > timedelta(days=age_days):
            _open(d.id, "age")
        if now - created > timedelta(
            days=disuse_days
        ) and not record_store.directive_acted_on_since(
            d.id, since=(now - timedelta(days=disuse_days)).isoformat()
        ):
            _open(d.id, "disuse")
    return opened


def visible_candidates(
    scope_id: str, actor: ContributorRef, *, record_store: RecordStore
) -> list[RetirementCandidate]:
    """The open candidates, for a session bound to *scope_id* only. Never a descendant's."""
    if actor.scope_id != scope_id:
        return []
    return [
        c
        for c in record_store.list_retirement_candidates(scope_id=scope_id)
        if not c.directive_id.startswith("op_")
    ]


def operator_candidates(
    record_store: RecordStore, *, scope_id: str | None = None
) -> list[RetirementCandidate]:
    """The open candidates on OPERATOR items (their owner is the operator), for the operator's
    own surfaces only. Never shown to a scope's sessions."""
    scopes = (
        [scope_id]
        if scope_id is not None
        else sorted({c.scope_id for c in record_store.list_all_retirement_candidates()})
    )
    return [
        c
        for sid in scopes
        for c in record_store.list_retirement_candidates(scope_id=sid)
        if c.directive_id.startswith("op_")
    ]


def dismiss_operator_candidate(
    candidate_id: str, *, reason: str, days: int, record_store: RecordStore
) -> None:
    """The operator snoozes a candidate on one of its own items. The item is unchanged.

    Raises:
        KeyError: no such candidate, or it is not on an operator item.
        ValueError: a blank reason, or *days* < 1."""
    candidate = record_store.get_retirement_candidate(candidate_id)
    if candidate is None or not candidate.directive_id.startswith("op_"):
        raise KeyError(candidate_id)
    _snooze(candidate_id, reason=reason, days=days, record_store=record_store)


def dismiss_candidate(
    candidate_id: str,
    *,
    reason: str,
    days: int,
    actor: ContributorRef,
    record_store: RecordStore,
) -> None:
    """Snooze a candidate for *days* days with a recorded reason. The directive is unchanged.

    Raises:
        PositionGateRefusal: *actor* is not bound to the candidate's scope.
        ValueError: a blank reason, or *days* < 1.
        KeyError: no such candidate."""
    candidate = record_store.get_retirement_candidate(candidate_id)
    if candidate is None:
        raise KeyError(candidate_id)
    if actor.scope_id != candidate.scope_id or actor.session_id == ENGINE_ACTOR:
        raise PositionGateRefusal("Only a session bound to the owning scope may dismiss it.")
    if candidate.directive_id.startswith("op_"):
        raise PositionGateRefusal("A candidate on an operator item is the operator's to dismiss.")
    _snooze(candidate_id, reason=reason, days=days, record_store=record_store)


def _snooze(candidate_id: str, *, reason: str, days: int, record_store: RecordStore) -> None:
    if not reason or not reason.strip():
        raise ValueError("A dismissal needs a reason.")
    if days < 1:
        raise ValueError("days must be at least 1.")
    until = (datetime.now(tz=UTC) + timedelta(days=days)).isoformat()
    record_store.set_retirement_candidate_state(
        candidate_id, state="dismissed", dismissed_until=until, note=reason.strip()
    )


def retirement_notice(retirement: Retirement) -> dict:
    """The reader-facing shape of a retirement, for a perspective's ``input_changes``."""
    return {
        "retirement_id": retirement.id,
        "label": retirement.label,
        "kind": retirement.kind,
        "notice": retirement.notice or retirement.reason,
        "cause": retirement.cause,
        "actor_scope_id": retirement.scope_id,
    }
