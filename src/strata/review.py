"""Human review (ADR 0018, #126): a person disposes of every directive change at level 1.

In a scope whose ``human_review`` level is 1 the judge still judges every contribution and its
verdict is recorded verbatim, but a judgment that would change the scope's directives is HELD
(disposition ``held``) and nothing is applied. A bound session's own ``strata_retire`` becomes a
held retirement request. The operator then approves or rejects each item.

This module is mechanical. It takes no scope manager, so an approval can never call the judge:
before it applies anything it re-runs the engine's mechanical checks against the CURRENT state
(that the targets still exist, that the contributor still holds the position, and that the
directive does not contradict what the scope now inherits). Approvals therefore work while the
judge is down.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from strata.locks import scope_lock

if TYPE_CHECKING:
    from strata.fleet_config import FleetConfig
    from strata.record_store import RecordStore, RetirementRequest, ReviewAct
    from strata.summary_store import SummaryStore

#: The judge ops that change a scope's directives (ADR 0018 D5).
_HELD_OPS = ("append", "publish", "supersede", "retire")

#: The review dispositions (ADR 0018 D8).
HELD, APPROVED, REJECTED, LAPSED = "held", "approved", "rejected", "lapsed"


class ReviewRefusal(Exception):  # noqa: N818 — a refusal of an act, named like PositionGateRefusal
    """An approval the re-check refused. Nothing changed; the item is still held."""


def review_holds(judgment: Any) -> bool:
    """Would this judgment change the scope's directives?

    A judgment the judge reads as binding (``accept_as_directive``) is held whatever the
    contribution was proposed as, and so is any judgment that carries an append, publish,
    supersede or retire op (ADR 0018 D5, D7). Context and decline are not."""
    if judgment.decision == "decline":
        return False
    return judgment.decision == "accept_as_directive" or any(
        op.op in _HELD_OPS for op in getattr(judgment, "directive_ops", None) or []
    )


def encode_held(judgment_json: str, summary_version: int) -> str:
    """The stored form of a held judgment: the judge's whole answer plus the version of the
    scope's summary it was judged against, so an approval can tell whether the scope moved on."""
    return json.dumps({"summary_version": summary_version, "judgment": json.loads(judgment_json)})


def _decode_held(raw: str) -> tuple[str, int | None]:
    data = json.loads(raw)
    if isinstance(data, dict) and "judgment" in data:
        return json.dumps(data["judgment"]), data.get("summary_version")
    return raw, None  # a bare judgment (no version recorded)


# -- the held list ----------------------------------------------------------------------------


@dataclass(frozen=True)
class HeldItem:
    id: str
    kind: str  # "judgment" or "retirement_request"
    scope_id: str
    held_at: str
    summary: str
    requester: str | None = None
    """The session that asked, for a retirement request; ``None`` for a judge-held item."""


@dataclass(frozen=True)
class ReviewList:
    items: list[HeldItem]
    count: int
    oldest_age_seconds: float | None


def _parse(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp.replace(" ", "T"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def list_held(
    *, record_store: RecordStore, scope_id: str | None = None, now: datetime | None = None
) -> ReviewList:
    """Every held item (judgments and session retirement requests), with the count and the age
    of the oldest (ADR 0018 D4: a hold nobody gets to must be visible)."""
    items: list[HeldItem] = []
    for held in record_store.list_held_judgments(scope_id=scope_id):
        contribution = record_store.get_contribution(held.contribution_id)
        text = contribution.content if contribution is not None else ""
        items.append(HeldItem(held.contribution_id, "judgment", held.scope_id, held.held_at, text))
    for request in record_store.list_retirement_requests(scope_id=scope_id, disposition=HELD):
        items.append(
            HeldItem(
                request.id,
                "retirement_request",
                request.scope_id,
                request.requested_at,
                f"retire {request.directive_id} ({request.kind}): {request.notice}",
                requester=request.actor_session_id,
            )
        )
    items.sort(key=lambda item: _parse(item.held_at))
    now = now or datetime.now(tz=UTC)
    oldest = (now - _parse(items[0].held_at)).total_seconds() if items else None
    return ReviewList(items=items, count=len(items), oldest_age_seconds=oldest)


# -- what the contributing scope sees (ADR 0018 D6) ---------------------------------------------


def perspective_notes(scope_id: str, *, record_store: RecordStore) -> list[dict[str, str]]:
    """The labelled lines the CONTRIBUTING scope reads about its own reviewed items: a held
    proposal ("held for the operator: proposes ...; not binding"), then "approved -> directive
    <id>" or "rejected: <reason>". Descendants and publications never see these."""
    notes: list[dict[str, str]] = []
    for item in list_held(record_store=record_store, scope_id=scope_id).items:
        notes.append(
            {
                "id": item.id,
                "status": HELD,
                "text": f"held for the operator: proposes {item.summary}; not binding",
            }
        )
    for act in record_store.list_review_acts():
        if act.scope_id != scope_id:
            continue
        if act.action == "approve":
            target = act.directive_id or act.item_id
            text = f"approved → directive {target}"
        elif act.action == "reject":
            text = f"rejected: {act.reason}"
        else:
            text = "lapsed: the directive it targeted changed while it was held"
        notes.append({"id": act.item_id, "status": act.action, "text": text})
    return notes


# -- the held session request -------------------------------------------------------------------


def request_retirement(
    scope_id: str,
    directive_id: str,
    kind: str,
    notice: str,
    *,
    actor_session_id: str,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
) -> RetirementRequest:
    """Record a bound session's own retire as a HELD request (level 1). Nothing changes: the
    directive keeps binding for everyone, and no notice goes out until the operator enacts it.

    The caller (``forgetting.retire_directive``) has already done the position check."""
    from strata.forgetting import RETIRE_KINDS  # noqa: PLC0415

    if fleet.get_scope(scope_id) is None:
        raise ValueError(f"Unknown scope {scope_id!r}.")
    if kind not in RETIRE_KINDS:
        raise ValueError(f"kind must be one of {RETIRE_KINDS}, got {kind!r}.")
    if not (notice and notice.strip()):
        raise ValueError("A retirement needs a notice.")
    summary = summary_store.read(scope_id)
    if summary is None or directive_id not in {d.id for d in summary.directives}:
        raise KeyError(
            f"Directive {directive_id!r} not found in scope {scope_id!r}'s current summary."
        )
    return record_store.append_retirement_request(
        scope_id=scope_id,
        directive_id=directive_id,
        kind=kind,  # type: ignore[arg-type]
        notice=notice.strip(),
        actor_session_id=actor_session_id,
    )


# -- approve / reject ---------------------------------------------------------------------------


def approve(
    item_id: str,
    *,
    operator: str,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    summary_max_words: int = 500,
) -> ReviewAct:
    """The operator approves a held item: an act, recorded as one (ADR 0018 D4).

    The judge's ops are applied unchanged through the SAME amendment writer today's path uses,
    after the mechanical re-check; the resulting directive is the scope's own. A failed check
    raises :class:`ReviewRefusal` and nothing changes. A directive that was superseded or
    retired while the item was held makes it lapse instead."""
    if item_id.startswith("rr_"):
        return _approve_request(
            item_id,
            operator=operator,
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
        )
    return _approve_judgment(
        item_id,
        operator=operator,
        fleet=fleet,
        record_store=record_store,
        summary_store=summary_store,
        summary_max_words=summary_max_words,
    )


def reject(
    item_id: str,
    reason: str,
    *,
    operator: str,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
) -> ReviewAct:
    """The operator rejects a held item. The reason is recorded and reaches the scope as
    evidence; nothing enters. A rejected item stays in the record."""
    if not (reason and reason.strip()):
        raise ValueError("A reject needs a reason.")
    reason = reason.strip()
    if item_id.startswith("rr_"):
        request = _held_request(item_id, record_store)
        with scope_lock(request.scope_id):
            record_store.set_request_disposition(item_id, REJECTED, reason=reason)
            return record_store.append_review_act(
                item_id=item_id,
                scope_id=request.scope_id,
                action="reject",
                actor=operator,
                reason=reason,
                directive_id=request.directive_id,
            )
    judgment, contribution = _held_judgment(item_id, record_store)
    with scope_lock(contribution.scope_id):
        record_store.set_judgment_disposition(item_id, REJECTED)
        return record_store.append_review_act(
            item_id=item_id,
            scope_id=contribution.scope_id,
            action="reject",
            actor=operator,
            reason=reason,
        )


def _held_request(item_id: str, record_store: RecordStore) -> RetirementRequest:
    request = record_store.get_retirement_request(item_id)
    if request is None or request.disposition != HELD:
        raise ReviewRefusal(f"{item_id} is not a held retirement request.")
    return request


def _held_judgment(item_id: str, record_store: RecordStore):  # noqa: ANN202
    judgment = record_store.get_judgment(item_id)
    contribution = record_store.get_contribution(item_id)
    if judgment is None or contribution is None or judgment.disposition != HELD:
        raise ReviewRefusal(f"{item_id} is not a held item.")
    return judgment, contribution


def _approve_request(
    item_id: str,
    *,
    operator: str,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
) -> ReviewAct:
    from strata.forgetting import _retire_locked  # noqa: PLC0415

    request = _held_request(item_id, record_store)
    with scope_lock(request.scope_id):
        summary = summary_store.read(request.scope_id)
        live = {d.id for d in summary.directives} if summary is not None else set()
        if request.directive_id not in live and any(
            r.directive_id == request.directive_id
            and r.retired_by == "scope-session"
            and r.actor_session_id == request.actor_session_id
            for r in record_store.list_retirements(scope_id=request.scope_id)
        ):
            # a crash after the retirement and before the disposition: record the approval only
            record_store.set_request_disposition(item_id, APPROVED)
            return record_store.append_review_act(
                item_id=item_id,
                scope_id=request.scope_id,
                action="approve",
                actor=operator,
                reason="already enacted; recorded the approval",
                directive_id=request.directive_id,
            )
        if summary is None or request.directive_id not in live:
            # the SAME directive must still stand; if it was superseded or retired meanwhile
            # the request lapses and nothing is applied (ADR 0018 D5)
            record_store.set_request_disposition(item_id, LAPSED)
            return record_store.append_review_act(
                item_id=item_id,
                scope_id=request.scope_id,
                action="lapse",
                actor=operator,
                directive_id=request.directive_id,
            )
        _retire_locked(
            request.scope_id,
            request.directive_id,
            request.kind,
            request.notice,
            actor_session_id=request.actor_session_id,  # the requester
            retired_by="scope-session",
            cause="act",
            end_condition=None,
            evidence_contribution_id=None,
            fleet=fleet,
            record_store=record_store,
            summary_store=summary_store,
        )
        record_store.set_request_disposition(item_id, APPROVED)
        return record_store.append_review_act(  # the enactor
            item_id=item_id,
            scope_id=request.scope_id,
            action="approve",
            actor=operator,
            directive_id=request.directive_id,
        )


def _approve_judgment(
    item_id: str,
    *,
    operator: str,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    summary_max_words: int,
) -> ReviewAct:
    from strata.app import _read_judge_inputs, _write_amendment  # noqa: PLC0415
    from strata.scope_manager import (  # noqa: PLC0415
        ScopeManagerJudgment,
        _apply_amendment,
        _first_inherited_conflict,
        _inherited_directive_texts,
    )

    held_row, contribution = _held_judgment(item_id, record_store)
    scope = fleet.get_scope(contribution.scope_id)
    if scope is None:
        raise ReviewRefusal(f"scope {contribution.scope_id!r} no longer exists.")
    stored = record_store.get_held_payload(item_id)
    if stored is None:
        raise ReviewRefusal(f"{item_id} has no stored judgment to apply.")
    payload_json, held_version = _decode_held(stored)
    payload = ScopeManagerJudgment.model_validate_json(payload_json)
    with scope_lock(scope.id):
        current = summary_store.read(scope.id)
        live = {d.id for d in current.directives} if current is not None else set()
        if _already_applied(
            payload, contribution.id, scope.id, live, record_store, written=held_row.summary_version
        ):
            # a crash after the write and before the disposition: record the approval only
            record_store.set_judgment_disposition(item_id, APPROVED)
            return record_store.append_review_act(
                item_id=item_id,
                scope_id=scope.id,
                action="approve",
                actor=operator,
                reason="already applied; recorded the approval",
                directive_id=_minted_id(payload, contribution.id),
            )
        # -- the mechanical re-check: no judge call ------------------------------------------
        lapsed = False
        for op in payload.directive_ops:
            if op.op == "supersede" and op.id not in live:
                raise ReviewRefusal(
                    f"supersede target {op.id} is no longer in {scope.id}'s directives; "
                    "reject the item, or have the contributor re-propose."
                )
            if op.op == "retire" and op.id not in live:
                lapsed = True
        if lapsed:
            record_store.set_judgment_disposition(item_id, LAPSED)
            return record_store.append_review_act(
                item_id=item_id,
                scope_id=scope.id,
                action="lapse",
                actor=operator,
                reason="the directive it targeted was superseded or retired while held",
            )
        admits = [op for op in payload.directive_ops if op.op in ("append", "publish")]
        binds = bool(admits) or payload.decision == "accept_as_directive"
        if binds and contribution.contributor.scope_id != scope.id:
            raise ReviewRefusal(
                f"the contributor is bound to {contribution.contributor.scope_id!r}, not "
                f"{scope.id!r}: only a session bound to the scope may propose its directives."
            )
        if admits:
            inputs = _read_judge_inputs(
                scope=scope, fleet=fleet, record_store=record_store, summary_store=summary_store
            )
            inherited = _inherited_directive_texts(
                inputs.ancestor_directives, inputs.operator_memory
            )
            for op in admits:
                hit = _first_inherited_conflict(op.content or contribution.content, inherited)
                if hit is not None:
                    directive_id, origin, _reason = hit
                    raise ReviewRefusal(
                        f"it now contradicts the inherited directive {directive_id} ({origin}); "
                        "a scope's own directive may never contradict what it inherits, even "
                        "when enacted in person."
                    )
        # -- apply, through the same writer the accept path uses -----------------------------
        directive_id = _minted_id(payload, contribution.id)
        # The judge's context rewrite REPLACES the whole context. Apply it only if the scope's
        # summary has not moved since the hold; otherwise keep the current context and apply the
        # directive ops alone, so context admitted meanwhile is never wiped.
        current_version = current.version if current is not None else 0
        apply_context = held_version is not None and current_version == held_version
        new_context = payload.new_context if apply_context else None
        if payload.new_context is None:
            how = None
        elif apply_context:
            how = "applied the judge's context rewrite"
        else:
            how = "kept the current context: the scope's summary changed since the hold"
        if payload.new_summary is not None:
            fresh = payload.model_copy(
                update={
                    "new_summary": _apply_amendment(
                        scope=scope,
                        current_summary=current,
                        contribution=contribution,
                        ops=payload.directive_ops,
                        new_context=new_context,
                    )
                }
            )
            _write_amendment(
                fresh,
                scope=scope,
                fleet=fleet,
                record_store=record_store,
                summary_store=summary_store,
                summary_max_words=summary_max_words,
                previous_summary=current,
                retirements=[
                    (d, payload.reasoning, payload.retirement_circumstances().get(d))
                    for d in payload.retired_directive_ids
                ],
                removals=[(d, contribution.id) for d in payload.removed_directive_ids],
                withdraw_reasoning=payload.reasoning,
                judged_contribution_ids=[contribution.id],
            )
        record_store.set_judgment_disposition(item_id, APPROVED)
        return record_store.append_review_act(
            item_id=item_id,
            scope_id=scope.id,
            action="approve",
            actor=operator,
            reason=how,
            directive_id=directive_id,
        )


def _minted_id(payload: Any, contribution_id: str) -> str | None:
    """The directive an append/publish op mints: its id is the contribution's own."""
    admits = any(op.op in ("append", "publish") for op in payload.directive_ops)
    return contribution_id if admits else None


def _already_applied(
    payload: Any,
    contribution_id: str,
    scope_id: str,
    live: set[str],
    record_store: RecordStore,
    *,
    written: int | None = None,
) -> bool:
    """Did an earlier approval of this item write its amendment and die before recording the
    disposition? The record says so first: a held judgment has no ``summary_version`` until its
    own amendment is written (the writer stamps it), so a stamp means this item's write happened.
    That also covers a supersede-only judgment, which mints nothing and removes a target the
    summary alone cannot attribute to this item. Failing that, an admitting op is applied when the
    directive it mints is live, and a retire-only judgment when every target already has a
    retirement carrying this judgment's reasoning."""
    if written is not None:
        return True
    if _minted_id(payload, contribution_id) is not None:
        return contribution_id in live
    retires = [op.id for op in payload.directive_ops if op.op == "retire"]
    if not retires:
        return False
    done = {
        r.directive_id
        for r in record_store.list_retirements(scope_id=scope_id)
        if r.reason == payload.reasoning
    }
    return all(target in done for target in retires)
