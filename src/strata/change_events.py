"""Change events — who a changed input affects, and how they are told (ADR 0014).

A scope's memory changes only when an agent contributes to it. The inputs
that memory rests on can change with no agent involved: an upstream
publication is withdrawn, amended or added to; an ancestor adds or retires a
directive; a referenced peer changes its face; the operator corrects a
binding directive. Between contributions the scope is *evidence-blind* — it
goes on asserting what its inputs no longer support (ADR 0014, Context).

This module is the trigger half of the fix:

- :func:`affected_scopes` — ADR 0014 D3's **one topological rule**, applied
  identically to an addition, an amendment and a withdrawal. For a
  publication item: the source's chain children plus every scope whose
  reference edge points at the source. For a directive: the holding scope's
  chain descendants; for an operator directive attached at S, S and its
  descendants. There is no presented index and no second rule (D3's rejected
  alternative — a strict subset of this set, buying precision at the cost of
  a second mechanism the two could disagree across).
- :func:`emit` — ADR 0014 D5's notice: for each affected scope, a
  ``subject="manager-refresh"`` contribution in that scope's record carrying
  the change payload, and the :class:`~strata.record_store.ChangeEvent` row
  that is the same event machine-readable. Mechanical, no LLM (matching ADR
  0013 D4b), which is what makes the notice permanent and auditable rather
  than prose a word budget can condense away.

What bounds a wave is the **change id** (ADR 0014 D4). Every independent
input change mints one; every change derived from processing it inherits it
(``inherit_from``); a scope **refreshes** for a given change id at most
once, however many notices of it reach the scope. Chain
edges form a tree and would need none of this — reference edges may form
cycles and need all of it. :data:`HOP_BUDGET` is the backstop for bugs, not
the mechanism.

Consuming what this module writes — the drain, the refresh judge mode and
the perspective's ``input_changes`` section — belongs to later phases; this
module only ever writes.

Vocabulary follows CONTEXT.md: scope, contribution, record, publication,
directive, change event.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from strata.record_store import ContributorRef

if TYPE_CHECKING:
    from strata.fleet_config import FleetConfig
    from strata.record_store import RecordStore

_logger = logging.getLogger("strata.change_events")

# ---------------------------------------------------------------------------
# The settled vocabulary (ADR 0014 D1; the CHECK constraint in migration
# 0011 is the same list, and the two must stay in step).
# ---------------------------------------------------------------------------

PUBLICATION_KINDS = frozenset({"published", "amended", "withdrawn"})
"""Changes to a scope's outward face — one-hop readers are affected."""

DIRECTIVE_KINDS = frozenset({"directive_appended", "directive_superseded", "directive_retired"})
"""Changes to a scope's own binding directives — its subtree is affected."""

OPERATOR_DIRECTIVE_CHANGED = "operator_directive_changed"
"""A change to the operator layer attached at a scope (ADR 0008 D1/D2). The
one kind whose affected set includes the attachment scope ITSELF: the
operator's layer composes ABOVE that scope and binds it, so the scope is a
reader of the change, not its author."""

HOP_BUDGET = 8
"""ADR 0014 D4's backstop, not its termination mechanism — that is the
change id's once-per-scope rule. A wave this deep means a bug in
inheritance, so the event is still RECORDED (silence would hide the bug)
but is never enqueued for a refresh."""


def new_change_id() -> str:
    """Mint the id of one independent input change (ADR 0014 D4).

    Same id style as the record's own ids. Distinct from a change EVENT's id
    (``ce_``, minted per affected scope by the record store): this is the
    wave, that is one scope's notice of it.
    """
    return f"chg_{secrets.token_hex(8)}"


# ---------------------------------------------------------------------------
# The affected set (ADR 0014 D3)
# ---------------------------------------------------------------------------


def affected_scopes(
    fleet: FleetConfig,
    *,
    item: str,
    kind: str,
    source_scope_id: str,
    by_operator: bool = False,
) -> list[str]:
    """Return the ids of the scopes that compose *item*, in a deterministic order.

    Topological and uniform (ADR 0014 D3): the same walk answers an
    addition, an amendment and a withdrawal, because the question is "who
    would :func:`~strata.perspective.compose_perspective` show this to", and
    that does not depend on which way the item moved.

    Terminates by construction — every walk is one hop (publication) or a
    parent-pointer climb (directives), so a reference cycle cannot make it
    loop. What bounds the *wave* a cycle can start is the change id, not this
    function (see :func:`emit`).

    Args:
        item: The changed item's id — a published item id, a directive id or
            an operator item id. Not used to select scopes (topology alone
            decides, D3); carried for logging and for the caller's payload.
        kind: One of the settled kinds — :data:`PUBLICATION_KINDS`,
            :data:`DIRECTIVE_KINDS` or :data:`OPERATOR_DIRECTIVE_CHANGED`.
        source_scope_id: The scope holding the item that changed. An id not
            in the fleet resolves to no children, no readers and no
            descendants, so the answer is empty rather than an error — a
            scope can be removed from ``fleet.yaml`` between an act and its
            emission.
        by_operator: The change was authored by the OPERATOR, from outside
            the fleet (ADR 0008 D4's in-person correction). A directive
            change then reaches the holding scope itself as well as its
            descendants — implementation pin 2's "an operator directive
            change on S affects S and its descendants". The scope is a
            reader of what the operator did to it, not its author. Ignored
            for a publication change, which never reaches its own source.

    Returns:
        Affected scope ids, sorted, each at most once. *source_scope_id* is
        included only when the change came from outside the scope's own
        authority: :data:`OPERATOR_DIRECTIVE_CHANGED`, whose layer binds the
        attachment scope, and any directive change with *by_operator*.

    Raises:
        ValueError: *kind* is outside the settled vocabulary. Guessing a rule
            for an unknown kind would deliver notice to the wrong scopes,
            which is worse than failing loudly.
    """
    if kind in PUBLICATION_KINDS:
        # ADR 0013 D2/D3 — publication travels exactly one edge: the chain
        # children it composes into, and the scopes whose own reference edge
        # points at this source. A grandchild receives the face only if the
        # child relays it, and that relay is the child's own publish act,
        # minting its own derived change.
        affected = {s.id for s in fleet.chain_children(source_scope_id)}
        affected |= {s.id for s in fleet.referenced_by(source_scope_id)}
    elif kind in DIRECTIVE_KINDS:
        # A directive binds the holding scope's whole subtree. The scope
        # itself is excluded when the change is its OWN — ADR 0014 D1: a
        # scope's contribution is not a trigger for the scope, because it
        # already has a path. An OPERATOR correction is not the scope's own
        # act: it comes from outside, and the scope is as blind to it as any
        # descendant (implementation pin 2).
        affected = {s.id for s in fleet.chain_descendants(source_scope_id)}
        if by_operator:
            _add_source_if_active(affected, fleet, source_scope_id)
    elif kind == OPERATOR_DIRECTIVE_CHANGED:
        # ADR 0008 D2: attached at S, the operator layer composes above S and
        # binds S's subtree — so S is affected too. Nobody in the fleet
        # authored this change; the operator did, from outside.
        affected = {s.id for s in fleet.chain_descendants(source_scope_id)}
        _add_source_if_active(affected, fleet, source_scope_id)
    else:
        raise ValueError(
            f"Unknown change-event kind {kind!r} for item {item!r} in scope "
            f"{source_scope_id!r} — expected one of "
            f"{sorted(PUBLICATION_KINDS | DIRECTIVE_KINDS | {OPERATOR_DIRECTIVE_CHANGED})}."
        )
    return sorted(affected)


def _add_source_if_active(affected: set[str], fleet: FleetConfig, source_scope_id: str) -> None:
    """Include the source scope in *affected*, if the fleet still has it active.

    An archived scope has no judge to wake, and a scope removed from
    ``fleet.yaml`` between an act and its emission is not addressable.
    """
    scope = fleet.get_scope(source_scope_id)
    if scope is not None and scope.status == "active":
        affected.add(source_scope_id)


# ---------------------------------------------------------------------------
# Emission (ADR 0014 D4/D5)
# ---------------------------------------------------------------------------


def _render_notice(
    *,
    change_id: str,
    item: str,
    kind: str,
    source_scope_id: str,
    before: str | None,
    after: str | None,
    note: str | None = None,
) -> str:
    """Render the change payload the affected scope's judge is shown (ADR 0014 D5).

    Deterministic and mechanical — no LLM writes this, and it is the notice,
    the judge's input and the permanent audit row's prose half all at once.
    Every field D5 names is present and labelled, so a judge (and an
    operator reading the record years later) can tell what moved without a
    second lookup.
    """
    lines = [
        f"[Input change {change_id} — an input this scope's memory rests on has changed.]",
        f"- change: {kind}",
        f"- item: {item}",
        f"- source scope: {source_scope_id}",
        f"- before: {before if before is not None else '(nothing — this is an addition)'}",
        f"- after: {after if after is not None else '(nothing — this input is gone)'}",
    ]
    if note is not None:
        lines.append(f"- note: {note}")
    return "\n".join(lines)


def _notice_contributor(scope_id: str) -> ContributorRef:
    """Provenance for a mechanically-appended notice — the scope's own manager.

    Mirrors the launch-time manager refresh (``strata launch``), which
    appends its refresh request the same way: ADR 0014 D5 deliberately reuses
    that vehicle rather than minting a new row type.
    """
    return ContributorRef(
        scope_id=scope_id,
        skill="scope-manager",
        session_id="change-event",
        ts=datetime.now(tz=UTC).isoformat(),
    )


def emit(
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    item: str,
    kind: str,
    source_scope_id: str,
    before: str | None = None,
    after: str | None = None,
    inherit_from: str | None = None,
    hop: int = 0,
    by_operator: bool = False,
) -> str:
    """Notify every scope affected by a change to *item*, and return the change id.

    For each scope :func:`affected_scopes` names, appends a
    ``subject="manager-refresh"`` contribution carrying the rendered payload
    and the :class:`~strata.record_store.ChangeEvent` row linked to it — one
    event, two halves, written in one transaction (ADR 0014 D5). Nothing is
    judged here and no LLM is called: the writer of an input change writes
    its events and returns (D6).

    **The notice is never suppressed; only the refresh is.** ADR 0014 D4
    bounds a wave at one refresh per scope per change id, and that is a
    statement about judging, not about telling. Every affected scope is told
    what changed, and the record keeps every notice — a notice that can
    vanish is not notice (D5). What varies is whether the notice is
    ENQUEUED:

    - The scope has this change **pending**: the new notice is pending too.
      The drain batches everything pending for a scope into ONE refresh
      (implementation pin 1), so it still refreshes exactly once.
    - The scope has **already refreshed** for this change id: the notice is
      written stamped processed at birth, carrying a note saying why no
      refresh follows.
    - Past the **hop budget** (:data:`HOP_BUDGET`): the same, with a note
      naming the backstop — hitting it is recorded, never silent.

    The one thing not written twice is the same ITEM under the same change
    id: a cascade that revisits it has nothing to add.

    This function never raises. Emission is not the originating act: a
    publish that succeeded must not be undone because its notice could not be
    written, so a store failure is logged and recorded as a note in the
    source scope's own record, and the caller carries on.

    Args:
        item: The changed item's id.
        kind: One of the settled kinds (see :func:`affected_scopes`).
        source_scope_id: The scope holding the changed item.
        before/after: The item's previous and current state, rendered for the
            judge. ``None`` reads as "there was none": an addition has no
            before, a withdrawal no after.
        inherit_from: The change id to inherit (ADR 0014 D4) when this change
            is DERIVED from processing another — a relayed withdrawal, a
            refresh's admitted directive. ``None`` mints a fresh id, which is
            correct only for an independent input change.
        hop: How many derived hops from the originating change this is. A
            caller that inherits an id passes its own hop + 1.
        by_operator: The operator authored this change from outside the
            fleet, which widens a directive change's affected set to include
            the holding scope (see :func:`affected_scopes`).

    Returns:
        The change id — minted or inherited — so the caller can thread it
        into anything it derives (ADR 0014 D8: the change id is a parameter,
        never a lookup).
    """
    change_id = inherit_from if inherit_from is not None else new_change_id()
    try:
        scope_ids = affected_scopes(
            fleet,
            item=item,
            kind=kind,
            source_scope_id=source_scope_id,
            by_operator=by_operator,
        )
    except Exception:  # noqa: BLE001 — the notice, never the act
        # An unknown kind is a bug in the caller and a broken traversal a bug
        # here; neither is a reason to undo an act that already succeeded.
        # Log it loudly and deliver nothing rather than to a guessed set.
        _logger.exception(
            "change %s (%s of %s in %s) could not be routed; no notice emitted",
            change_id,
            kind,
            item,
            source_scope_id,
        )
        return change_id

    over_budget = hop > HOP_BUDGET
    if over_budget:
        _logger.error(
            "change %s exceeded the hop budget (%s) at %s of %s in %s — recording the "
            "event without enqueueing a refresh (ADR 0014 D4)",
            change_id,
            HOP_BUDGET,
            kind,
            item,
            source_scope_id,
        )

    for scope_id in scope_ids:
        try:
            already_announced, already_refreshed = _prior_notices(
                record_store, scope_id=scope_id, change_id=change_id, item=item
            )
            if already_announced:
                # This exact item, under this exact wave, has already been
                # announced to this scope — a cascade revisiting it has
                # nothing to add.
                continue

            # ADR 0014 D4 bounds the REFRESH, never the NOTICE. A scope that
            # has already refreshed for this change id is still TOLD what
            # happened — the row is written, stamped processed at birth, so
            # no second refresh follows for one change. A scope with the
            # change still PENDING takes this notice pending too: the drain
            # batches everything pending into one refresh (implementation
            # pin 1), so it still refreshes exactly once.
            processed = over_budget or already_refreshed
            note = _suppression_note(
                change_id=change_id,
                hop=hop,
                over_budget=over_budget,
                already_refreshed=already_refreshed,
            )
            record_store.append_change_notice(
                scope_id=scope_id,
                content=_render_notice(
                    change_id=change_id,
                    item=item,
                    kind=kind,
                    source_scope_id=source_scope_id,
                    before=before,
                    after=after,
                    note=note,
                ),
                contributor=_notice_contributor(scope_id),
                change_id=change_id,
                source_scope_id=source_scope_id,
                item_id=item,
                kind=kind,
                before=before,
                after=after,
                hop=hop,
                processed=processed,
            )
        except Exception:  # noqa: BLE001 — one scope's notice, not the act
            _logger.exception(
                "failed to enqueue change %s (%s of %s in %s) for scope %s",
                change_id,
                kind,
                item,
                source_scope_id,
                scope_id,
            )
            _record_emission_failure(
                record_store,
                change_id=change_id,
                item=item,
                kind=kind,
                source_scope_id=source_scope_id,
                affected_scope_id=scope_id,
            )

    return change_id


def _prior_notices(
    record_store: RecordStore, *, scope_id: str, change_id: str, item: str
) -> tuple[bool, bool]:
    """What *scope_id* already holds for *change_id* — ``(announced, refreshed)``.

    ``announced`` is True when this exact item has already been announced to
    this scope under this change id: re-announcing it would add nothing, and
    a cascade can revisit the same item.

    ``refreshed`` is True when ANY row for this change id has been processed
    — the scope's one refresh for this wave has already run (ADR 0014 D4), so
    a further notice is recorded but never enqueued.
    """
    announced = False
    refreshed = False
    for event in record_store.list_change_events(scope_id=scope_id):
        if event.change_id != change_id:
            continue
        if event.item_id == item:
            announced = True
        if event.processed_at is not None:
            refreshed = True
    return announced, refreshed


def _suppression_note(
    *, change_id: str, hop: int, over_budget: bool, already_refreshed: bool
) -> str | None:
    """Why this notice will not be drained, in the notice itself, or ``None``.

    A notice recorded but never enqueued has to say so where the notice
    lives; leaving it to a log line would make the record misleading about
    what happens next.
    """
    if over_budget:
        return (
            f"hop budget of {HOP_BUDGET} exceeded at hop {hop}; recorded, not "
            "enqueued for a refresh (ADR 0014 D4)"
        )
    if already_refreshed:
        return (
            f"already refreshed for {change_id}; recorded, not enqueued — a scope "
            "refreshes at most once per change id (ADR 0014 D4)"
        )
    return None


def _record_emission_failure(
    record_store: RecordStore,
    *,
    change_id: str,
    item: str,
    kind: str,
    source_scope_id: str,
    affected_scope_id: str,
) -> None:
    """Leave a trace in the record that a notice could not be written.

    A lost notice that leaves no trace is worse than a loud failure: the
    affected scope will go on asserting what its input no longer supports,
    and nothing would say why. The note goes to the SOURCE scope — the one
    scope known to be writable at this moment, since the originating act just
    wrote to it — and is best-effort: if even this fails, the log line
    :func:`emit` already wrote is the whole record of it.
    """
    try:
        record_store.append_contribution(
            scope_id=source_scope_id,
            content=(
                f"[Change {change_id} could not be delivered to scope {affected_scope_id}: "
                f"{kind} of {item}. That scope has NOT been told its input changed and will "
                "not refresh for this change; see the strata.change_events log for the "
                "underlying error.]"
            ),
            proposed_classification="context",
            subject="change-emission-failed",
            supersedes=None,
            contributor=_notice_contributor(source_scope_id),
        )
    except Exception:  # noqa: BLE001 — the record itself is unreachable
        _logger.exception(
            "could not record the emission failure for change %s in scope %s",
            change_id,
            source_scope_id,
        )
