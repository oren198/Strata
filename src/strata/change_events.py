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
- :func:`_emit_self_notice` — the same notice turned inward when a scope
  RETRACTS (ADR 0014 D1 as amended, issue #197): the retracting scope's own
  readers are inside the audience, so the row is written to the scope itself,
  born processed, owing a reader a delivery rather than a judge a refresh.

What bounds a wave is the **change id** (ADR 0014 D4). Every independent
input change mints one; every change derived from processing it inherits
them all (``wave_ids`` — a refresh coalesces several pending changes into
one judgment, so what it derives belongs to every wave it drained); a scope
**refreshes** for a given change id at most once, however many notices of
it reach the scope. Chain
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

import json
import logging
import re
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from strata.record_store import ENGINE_NOTE_SUBJECT, ContributorRef

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

RETRACTION_KINDS = frozenset({"withdrawn", "directive_retired", "directive_superseded"})
"""The kinds that take something AWAY from what a reader already holds.

ADR 0014 D1 says a scope's own contribution is never a REFRESH trigger for
itself — it already has a path. It is still notice to the scope's own READERS
when it retracts (issue #197): a scope is a mix of agents, not one mind, and
another agent in it may have read the item and acted on it. Silent removal is
how decay behaves; a retraction is a correction, and a correction owes notice.

Additions are deliberately outside this set. Nothing a reader holds stops being
true when a directive is appended, and its own next read composes the addition
anyway.
"""

DIRECTIVE_KINDS = frozenset({"directive_appended", "directive_superseded", "directive_retired"})

CLAIM_CORRECTED = "claim_corrected"
"""ADR 0017 P4: a context claim the judge found WRONG (``failed_corrected``),
reaching every reader it did — never ``claim_superseded`` (recorded, no notice; P3's
"the item is replaced; no notice" stands). Not in :data:`PUBLICATION_KINDS`: a
correction is a distinct fact from a publication change, even though its topological
audience (see :func:`affected_scopes`) is computed the same way."""

CLAIM_RESTORED = "claim_restored"
"""Restore act design (companion to #219 C): a ``claim_corrected`` withdrawal was
reversed by its owner. Deliberately NOT routed through :func:`emit`/:func:`affected_scopes`
— its audience is read from the ORIGINAL ``claim_corrected`` events by change id and item
id, never recomputed from today's topology (contract line 3) — see :func:`emit_restore_notice`.
Evidence only: a reader's judge decides whether to re-admit or re-trust what it had
doubted; nothing is inserted into its memory on its behalf (contract line 6)."""

_SELF_NOTICING_KINDS = RETRACTION_KINDS | frozenset({CLAIM_CORRECTED})
"""Kinds whose SOURCE, when excluded from its own change's affected set, is still
owed a self-notice (issue #197's rule, extended by ADR 0017 P4 to the claim owner's
readers: a correction is a retraction of the wrong claim's standing, and a correction
owes notice exactly as a retraction does)."""

#: The one-off unsplice of a legacy spliced row (ADR 0015 D5). NOT in
#: :data:`DIRECTIVE_KINDS`: those three say a scope's directive set moved and
#: are due every descendant a refresh. This one says the opposite — a row that
#: was never this scope's has stopped pretending to be, and the directive it
#: copied is unchanged in its owner's summary — so it is recorded and stamped
#: processed at birth, never drained, and never emitted to a descendant.
DIRECTIVE_UNSPLICED = "directive_unspliced"
"""Changes to a scope's own binding directives — its subtree is affected."""

OPERATOR_DIRECTIVE_CHANGED = "operator_directive_changed"
"""A change to the operator layer attached at a scope (ADR 0008 D1/D2). The
one kind whose affected set includes the attachment scope ITSELF: the
operator's layer composes ABOVE that scope and binds it, so the scope is a
reader of the change, not its author."""

CHANNEL_REMOVED = "channel_removed"
"""ADR 0014 D8: a reference edge a scope read through was removed. Sent to the
reader. Not in :data:`RETRACTION_KINDS`, :data:`PUBLICATION_KINDS` or
:data:`DIRECTIVE_KINDS`: nothing was withdrawn and no directive was retired."""

CHAIN_CHANGED = "chain_changed"
"""ADR 0014 D8: the chain above a scope moved (a re-parent). Sent to the moved
scope and every chain descendant. In none of the three sets above, for the
reason :data:`CHANNEL_REMOVED` gives."""

STRUCTURE_KINDS = frozenset({CHANNEL_REMOVED, CHAIN_CHANGED})
"""The kinds a fleet structure change writes. Each stores a JSON object in
``after`` carrying ``act_id`` (and its conflict lines or notices) — the read
contract of :func:`events_for_act` and :func:`structure_conflict_lines`."""

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
    by_owner: bool = True,
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
        by_owner: :data:`CLAIM_CORRECTED` only (ADR 0017 P4). ``True`` (the
            default) when the HOLDING scope's own judge authored the
            correction (a same-scope outcome) — its DOWNSTREAM readers
            (chain children, referencing peers) are the affected set, and the
            holding scope itself is excluded, reached instead by
            :func:`emit`'s self-notice. ``False`` when a DIFFERENT scope's
            outcome caused it (a cross-scope, chain-only ``acted_on`` per P1):
            the affected set is the holding scope ALONE — never its readers
            directly, since nobody but the holding scope has necessarily seen
            the claim. Its readers are reached only if and when the holding
            scope's OWN subsequent refresh withdraws something, which fans
            out through this same rule with ``by_owner=True``, under the
            SAME change id (ADR 0014 D4). Ignored for every other kind.

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
    elif kind == CLAIM_CORRECTED:
        # ADR 0017 P4 (fixed after strata-evals' unpublished-claim control caught
        # the first version over-notifying): a corrected claim's DOWNSTREAM
        # audience is who reads the HOLDING scope's PUBLICATION — never reached
        # directly from the correction itself, only from the holding scope's OWN
        # subsequent withdrawal (the `by_owner=True`/same-scope path, exercised a
        # second time when a cross-scope correction's own refresh withdraws
        # something). A cross-scope correction (`by_owner=False`) therefore tells
        # ONLY the holding scope — never its chain children or referencing peers
        # directly, whether or not the corrected claim was ever actually
        # published: nobody but the holding scope itself may have seen it.
        if by_owner:
            affected = {s.id for s in fleet.chain_children(source_scope_id)}
            affected |= {s.id for s in fleet.referenced_by(source_scope_id)}
        else:
            affected = set()
            _add_source_if_active(affected, fleet, source_scope_id)
    else:
        known = sorted(
            PUBLICATION_KINDS | DIRECTIVE_KINDS | {OPERATOR_DIRECTIVE_CHANGED, CLAIM_CORRECTED}
        )
        raise ValueError(
            f"Unknown change-event kind {kind!r} for item {item!r} in scope "
            f"{source_scope_id!r} — expected one of {known}."
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
    claim_id: str | None = None,
) -> str:
    """Render the change payload the affected scope's judge is shown (ADR 0014 D5).

    Deterministic and mechanical — no LLM writes this, and it is the notice,
    the judge's input and the permanent audit row's prose half all at once.
    Every field D5 names is present and labelled, so a judge (and an
    operator reading the record years later) can tell what moved without a
    second lookup.

    *claim_id* (ADR 0017 P4, claim_corrected only): the corrected claim's own id
    (``acted_on``) — a DIFFERENT fact from *item*, which here is the withdrawn
    PUBLISHED item's id. A reader needs both: which of ITS OWN published items just
    went stale (*item*), and which upstream claim caused it, so an operator or a
    later query can trace the two back to each other. ``None`` (every other kind)
    renders nothing extra.
    """
    lines = [
        f"[Input change {change_id} — an input this scope's memory rests on has changed.]",
        f"- change: {kind}",
        f"- item: {item}",
        f"- source scope: {source_scope_id}",
        f"- before: {before if before is not None else '(nothing — this is an addition)'}",
        f"- after: {after if after is not None else '(nothing — this input is gone)'}",
    ]
    if claim_id is not None:
        lines.append(f"- corrected claim: {claim_id}")
    if kind in STRUCTURE_KINDS:
        payload = _structure_payload(after)
        listed = payload.get("conflict_lines" if kind == CHAIN_CHANGED else "notices") or []
        lines.extend(f"- notice: {line}" for line in listed if isinstance(line, str))
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
    wave_ids: Sequence[str] = (),
    hop: int = 0,
    by_operator: bool = False,
    by_owner: bool = True,
    claim_id: str | None = None,
    audience: Sequence[str] | None = None,
) -> list[str]:
    """Notify every scope affected by a change to *item*, and return its change ids.

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

    **One row per (change id, affected scope).** A change derived from a
    COALESCED refresh belongs to every wave that refresh drained (ADR 0014
    D4, Phase A finding 2), and each of those waves gets its own notice
    rather than one notice carrying a list. That keeps the once-per-id check
    a row lookup, and gives D4's union semantics for free: an affected scope
    refreshes if ANY of the inherited ids is unseen, and is told without
    refreshing only when it has already refreshed for every one of them.

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
        wave_ids: The change ids to inherit (ADR 0014 D4) when this change
            is DERIVED from processing others — a relayed withdrawal, a
            refresh's admitted directive. Plural because a refresh coalesces
            (pin 1): read them off the judgment's ``wave_ids``, never off one
            of the two fields behind it. Empty mints ONE fresh id, which is
            correct only for an independent input change.
        hop: How many derived hops from the originating change this is. A
            caller that inherits ids passes its own hop + 1.
        by_operator: The operator authored this change from outside the
            fleet, which widens a directive change's affected set to include
            the holding scope (see :func:`affected_scopes`).
        by_owner: Threaded straight to :func:`affected_scopes` — see its own
            docstring for what it means per kind.
        audience: A structure kind's recorded audience (ADR 0014 D8), used
            instead of :func:`affected_scopes`. Refused for any other kind.
        claim_id: ADR 0017 P4, ``claim_corrected`` only — the corrected claim's
            own id (``acted_on``), rendered as an extra line in the notice
            alongside *item* (the withdrawn published item's id): a reader
            needs both facts. ``None`` for every other kind.

    Returns:
        The change ids — minted or inherited, deduplicated and
        order-preserving — so the caller can thread them into anything it
        derives (ADR 0014 D8: the change id is a parameter, never a lookup).
    """
    change_ids = list(dict.fromkeys(wave_ids)) or [new_change_id()]
    try:
        if audience is not None:
            # ADR 0014 D8: a structure change names its audience from the act's
            # RECORDED topology, never from today's fleet; no walk is made here.
            if kind not in STRUCTURE_KINDS:
                raise ValueError(f"an explicit audience is only for {sorted(STRUCTURE_KINDS)}")
            scope_ids = sorted(dict.fromkeys(audience))
        else:
            scope_ids = affected_scopes(
                fleet,
                item=item,
                kind=kind,
                source_scope_id=source_scope_id,
                by_operator=by_operator,
                by_owner=by_owner,
            )
    except Exception:  # noqa: BLE001 — the notice, never the act
        # An unknown kind is a bug in the caller and a broken traversal a bug
        # here; neither is a reason to undo an act that already succeeded.
        # Log it loudly and deliver nothing rather than to a guessed set.
        _logger.exception(
            "change(s) %s (%s of %s in %s) could not be routed; no notice emitted",
            ", ".join(change_ids),
            kind,
            item,
            source_scope_id,
        )
        return change_ids

    over_budget = hop > HOP_BUDGET
    if over_budget:
        _logger.error(
            "change(s) %s exceeded the hop budget (%s) at %s of %s in %s — recording the "
            "event without enqueueing a refresh (ADR 0014 D4)",
            ", ".join(change_ids),
            HOP_BUDGET,
            kind,
            item,
            source_scope_id,
        )

    for scope_id, change_id in ((s, c) for s in scope_ids for c in change_ids):
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
                    claim_id=claim_id,
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
                claim_id=claim_id,
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

    if kind in _SELF_NOTICING_KINDS and source_scope_id not in scope_ids:
        # ADR 0014 D1 as amended (issue #197), extended by ADR 0017 P4: the
        # retracting/correcting scope's OWN readers are inside the audience.
        # Skipped when the source is already in the affected set — an
        # operator correction (`by_operator`, OPERATOR_DIRECTIVE_CHANGED)
        # reaches the scope as an ordinary refresh trigger, and so does a
        # cross-scope `claim_corrected` (`by_owner=False`) — either way a
        # second row would be the same notice twice.
        _emit_self_notice(
            record_store,
            change_ids=change_ids,
            item=item,
            kind=kind,
            source_scope_id=source_scope_id,
            before=before,
            after=after,
            hop=hop,
            claim_id=claim_id,
        )

    return change_ids


def _emit_self_notice(
    record_store: RecordStore,
    *,
    change_ids: Sequence[str],
    item: str,
    kind: str,
    source_scope_id: str,
    before: str | None,
    after: str | None,
    hop: int,
    claim_id: str | None = None,
) -> None:
    """Tell the retracting scope's own readers what it just took away (issue #197).

    Born processed, and that is the whole distinction ADR 0014 D1 now draws:
    the scope's own judge authored this retraction, so nothing is owed a
    refresh — ``drain_is_noop`` stays True and no judge is ever woken by it —
    while the row is still composed into the scope's ``input_changes`` until a
    read delivers it (``awaiting_show``). A notice for readers, not for a
    refresh.

    Written on the SOURCE's own row rather than through
    :func:`affected_scopes`, which answers a different question (who composes
    this item) and would have to grow a special case to answer this one.

    A relayed withdrawal self-notices its relaying scope too: the relay is that
    scope's own published face, and its readers relied on it exactly as they
    would on an original.

    Never raises, for :func:`emit`'s reason: the retraction already happened.
    """
    for change_id in change_ids:
        try:
            if any(
                _is_self_notice(event) and event.change_id == change_id and event.item_id == item
                for event in record_store.list_change_events(scope_id=source_scope_id)
            ):
                # This item, under this wave, has already been announced to the
                # scope's own readers — a cascade revisiting it adds nothing.
                # Its own check rather than `_prior_notices`, which deliberately
                # cannot see a self-notice.
                continue
            record_store.append_change_notice(
                scope_id=source_scope_id,
                content=_render_notice(
                    change_id=change_id,
                    item=item,
                    kind=kind,
                    source_scope_id=source_scope_id,
                    before=before,
                    after=after,
                    note=(
                        "this scope's own retraction — recorded for its readers, not "
                        "enqueued for a refresh: the scope's judge authored it "
                        "(ADR 0014 D1, as amended by issue #197)"
                    ),
                    claim_id=claim_id,
                ),
                contributor=_notice_contributor(source_scope_id),
                change_id=change_id,
                source_scope_id=source_scope_id,
                item_id=item,
                kind=kind,
                before=before,
                after=after,
                hop=hop,
                processed=True,
                awaiting_show=True,
                claim_id=claim_id,
            )
        except Exception:  # noqa: BLE001 — one scope's notice, not the act
            _logger.exception(
                "failed to record the own-scope notice for change %s (%s of %s in %s)",
                change_id,
                kind,
                item,
                source_scope_id,
            )


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

    A SELF-notice (:func:`_is_self_notice`) counts for neither. It was never a
    refresh trigger — the scope's own judge wrote the retraction it announces —
    so reading it as "this scope has already refreshed for this wave" would
    silence a real notice arriving later in the same wave: exactly what happens
    on a reference cycle, where a scope's own withdrawal comes back to it as a
    peer's reaction under the same change id.
    """
    announced = False
    refreshed = False
    for event in record_store.list_change_events(scope_id=scope_id):
        if event.change_id != change_id or _is_self_notice(event):
            continue
        if event.item_id == item:
            announced = True
        if event.processed_at is not None:
            refreshed = True
    return announced, refreshed


def _is_self_notice(event) -> bool:  # noqa: ANN001 — a ChangeEvent, without the import cycle
    """Is *event* a scope's notice of its OWN retraction (issue #197)?

    The row says so itself (``self_notice``, migration 0014), and deliberately
    not "same scope, and processed": an operator correction to a scope is also
    ``source_scope_id == scope_id``, and the moment its refresh drains it is
    processed too — so that test would read a wave the scope HAS refreshed for
    as one it has not, and hand it a second refresh (ADR 0014 D4).
    """
    return bool(event.self_notice)


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
            subject=ENGINE_NOTE_SUBJECT,
            supersedes=None,
            contributor=_notice_contributor(source_scope_id),
        )
    except Exception:  # noqa: BLE001 — the record itself is unreachable
        _logger.exception(
            "could not record the emission failure for change %s in scope %s",
            change_id,
            source_scope_id,
        )


def emit_restore_notice(
    *,
    record_store: RecordStore,
    item_id: str,
    original_change_ids: Sequence[str],
    restore_change_id: str,
    claim_id: str,
    content: str,
) -> None:
    """Tell exactly the readers of a reversed ``claim_corrected`` notice that it was wrong.

    Restore act design, point 8/contract lines 2-3: the audience is read
    from the ORIGINAL ``claim_corrected`` events, by change id and item id —
    never recomputed from today's topology (:func:`affected_scopes` answers
    a different question and would include a reader added since the
    withdrawal, which contract line 3 says gets no notice at all). For each
    distinct ``(scope_id)`` that holds a ``claim_corrected`` row for *item_id*
    under any id in *original_change_ids* (plural: a coalesced refresh may
    have recorded the same correction under several inherited wave ids, ADR
    0014 D4), one ``claim_restored`` row is written, inheriting
    *restore_change_id* — the ONE new id this restore mints (contract line
    3's "one new change id, inherited").

    Evidence only (contract line 6): this writes change-event rows exactly
    like :func:`emit` does, but is never enqueued for a refresh — the
    reader's own judge decides what to do with the evidence, on its own next
    ordinary act, exactly as a self-notice already works for retractions.
    Every row is therefore stamped ``processed`` at birth, mirroring a
    self-notice's own shape (``processed=True``).

    *content* is the rendered notice (mechanical, no LLM — the caller
    renders it once since it is the same for every reader).

    Never raises — mirrors :func:`emit`'s own stance: a restore that
    succeeded must not be undone because a notice could not be written.
    """
    seen_scopes: set[str] = set()
    for original_change_id in dict.fromkeys(original_change_ids):
        for event in record_store.list_change_events_by_change_id(
            change_id=original_change_id, item_id=item_id, kind=CLAIM_CORRECTED
        ):
            if event.scope_id in seen_scopes:
                continue
            seen_scopes.add(event.scope_id)
            try:
                record_store.append_change_notice(
                    scope_id=event.scope_id,
                    content=content,
                    contributor=_notice_contributor(event.scope_id),
                    change_id=restore_change_id,
                    source_scope_id=event.source_scope_id,
                    item_id=item_id,
                    kind=CLAIM_RESTORED,
                    before=event.before,
                    after=event.after,
                    hop=event.hop,
                    processed=True,
                    claim_id=claim_id,
                )
            except Exception:  # noqa: BLE001 — one reader's notice, not the restore
                _logger.exception(
                    "failed to record the claim_restored notice for item %s in scope %s",
                    item_id,
                    event.scope_id,
                )


def _structure_payload(after: str | None) -> dict:
    """The parsed ``after`` of a structure event; ``{}`` when it is not a JSON object."""
    try:
        payload = json.loads(after or "")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def events_for_act(record_store: RecordStore, act_id: str) -> list[dict]:
    """Every structure event written for fleet structure act *act_id*.

    ADR 0014 D8's read contract. Matches only :data:`STRUCTURE_KINDS` rows
    whose JSON ``after`` has ``act_id`` equal to *act_id*; free text (a notice,
    a publication quoting an act id) is never read. Ordered by ``created_at``
    then ``rowid``. Each dict has ``id``, ``change_id``, ``scope_id``, ``kind``,
    ``created_at`` and ``payload`` (the parsed ``after``).
    """
    rows = record_store.list_events_by_act(act_id=act_id, kinds=sorted(STRUCTURE_KINDS))
    return [
        {
            "id": row.id,
            "change_id": row.change_id,
            "scope_id": row.scope_id,
            "kind": row.kind,
            "created_at": row.created_at,
            "payload": _structure_payload(row.after),
        }
        for row in rows
    ]


def structure_conflict_lines(event_row: dict) -> list[str]:
    """The conflict lines a structure event stored, as the core wrote them.

    ``payload["conflict_lines"]`` for ``chain_changed``, ``payload["notices"]``
    for ``channel_removed``; ``[]`` for any other kind. Nothing is re-derived
    from text.
    """
    kind = event_row.get("kind")
    key = {CHAIN_CHANGED: "conflict_lines", CHANNEL_REMOVED: "notices"}.get(kind)
    if key is None:
        return []
    lines = (event_row.get("payload") or {}).get(key)
    return [line for line in lines if isinstance(line, str)] if isinstance(lines, list) else []


# ---------------------------------------------------------------------------
# ADR 0014 D8: the notice texts (verbatim from the ADR), and the conflicts
# the engine finds when a chain moves.
# ---------------------------------------------------------------------------


def chain_conflict_line(
    *, own_id: str, inherited_id: str, inherited_scope: str, scope_id: str, reason: str
) -> str:
    """The ``chain_changed`` conflict line for one own directive (ADR 0014 D8 (a))."""
    return (
        f"Own directive {own_id} conflicts with newly inherited {inherited_id} "
        f"({inherited_scope}): {reason}. Where they conflict, the inherited directive "
        f"governs. {own_id} remains in {scope_id}'s memory until a session bound to "
        f"{scope_id} revises or retires it."
    )


def channel_removed_notice(*, own_id: str, source_scope: str, scope_id: str) -> str:
    """The ``channel_removed`` notice for one own directive adopted from the cut source."""
    return (
        f"Own directive {own_id} was adopted from {source_scope}'s publication, which "
        f"{scope_id} no longer reads. It still binds. A session bound to {scope_id} "
        "decides whether to keep it on its own account."
    )


def find_chain_conflicts(
    *,
    scope_id: str,
    own: Sequence[tuple[str, str]],
    gained: Sequence[tuple[str, str, str]],
) -> list[dict]:
    """Run the 1.17 inherited check on each own directive against each newly inherited one.

    Args:
        scope_id: The receiving scope.
        own:      ``(id, text)`` of each of its own directives.
        gained:   ``(id, scope label, text)`` of each directive it newly inherits.

    Returns one ``{"own_id", "inherited_id", "line"}`` per conflict, own
    directives first-to-last, each against the gained directives in order. The
    check's stated limits apply: a conflict phrased with no value, polarity or
    marker is not found.
    """
    from strata.scope_manager import (  # lazy: heavy, one-way dependency  # noqa: PLC0415
        inherited_conflict,
        inherited_context_words,
    )

    context_words = inherited_context_words([text for *_, text in gained])
    found: list[dict] = []
    for own_id, own_text in own:
        for inherited_id, inherited_scope, inherited_text in gained:
            reason = inherited_conflict(own_text, inherited_text, context_words)
            if reason is None:
                continue
            found.append(
                {
                    "own_id": own_id,
                    "inherited_id": inherited_id,
                    "line": chain_conflict_line(
                        own_id=own_id,
                        inherited_id=inherited_id,
                        inherited_scope=inherited_scope,
                        scope_id=scope_id,
                        reason=reason,
                    ),
                }
            )
    return found


def structure_conflicts(event) -> list[dict]:  # noqa: ANN001 — any object with kind/after
    """The structured conflicts a ``chain_changed`` event stored.

    Each is ``{"own_id", "inherited_id", "line"}``.

    Read from ``payload["conflicts"]`` only. ``[]`` for any other kind, or a
    payload that is not the engine's own JSON. This is what the self-layer
    annotation reads, so no free text is ever parsed to find a conflict.
    """
    if getattr(event, "kind", None) != CHAIN_CHANGED:
        return []
    raw = _structure_payload(getattr(event, "after", None)).get("conflicts")
    if not isinstance(raw, list):
        return []
    return [
        c
        for c in raw
        if isinstance(c, dict)
        and all(isinstance(c.get(k), str) for k in ("own_id", "inherited_id", "line"))
    ]


# ---------------------------------------------------------------------------
# ADR 0014 D8: emit_structure_change(act_id)
# ---------------------------------------------------------------------------


def _resolve_summaries_dir(record_store: RecordStore, summaries_dir: str | None) -> str:
    """The directory holding the per-scope summaries, publications and operator layers.

    An explicit *summaries_dir* wins. Otherwise the project's storage paths, when they
    point at THIS store's database; otherwise the ``summaries`` directory beside it.
    """
    if summaries_dir is not None:
        return summaries_dir
    from pathlib import Path  # noqa: PLC0415

    db_path = Path(record_store._db_path).resolve()  # noqa: SLF001
    try:
        from strata.project_config import resolve_storage_paths  # noqa: PLC0415

        paths = resolve_storage_paths()
        if Path(paths.db_path).expanduser().resolve() == db_path:
            return paths.summaries_dir
    except Exception:  # noqa: BLE001 — fall back to the convention
        _logger.debug("no project storage paths; using the summaries beside the database")
    return str(db_path.parent / "summaries")


def _summaries_or_none(summaries_dir: str):  # noqa: ANN202
    """A SummaryStore over *summaries_dir*, or ``None`` when it does not exist: reading
    must never create a directory (the store's constructor does)."""
    from pathlib import Path  # noqa: PLC0415

    from strata.summary_store import SummaryStore  # noqa: PLC0415

    return SummaryStore(summaries_dir) if Path(summaries_dir).is_dir() else None


def _chain_before_after(act, moved: str) -> tuple[list[str], list[str]]:  # noqa: ANN001
    before = (act.before_topology.get(moved) or {}).get("chain")
    after = (act.after_topology.get(moved) or {}).get("chain")
    if not isinstance(before, list) or not isinstance(after, list):
        raise ValueError(f"act {act.id} records no chain for {moved}")
    return [str(x) for x in before], [str(x) for x in after]


def _inherited_by_scope(
    scope_ids: Sequence[str], *, summaries_dir: str
) -> list[tuple[str, str, str]]:
    """``(id, label, text)`` of the directives *scope_ids* hold, plus the operator
    directives attached at them (an operator layer at S binds S's subtree)."""
    from strata.operator import read_operator_layer  # noqa: PLC0415

    store = _summaries_or_none(summaries_dir)
    found: list[tuple[str, str, str]] = []
    if store is None:
        return found
    for scope_id in scope_ids:
        summary = store.read(scope_id)
        if summary is not None:
            found.extend((d.id, scope_id, d.content) for d in summary.directives)
        found.extend(
            (item.id, f"operator, {scope_id}", item.content)
            for item in read_operator_layer(scope_id, summaries_dir=summaries_dir)
            if item.kind == "directive"
        )
    return found


def _moved_scope(act) -> str:  # noqa: ANN001
    moved = next(
        (
            scope_id
            for scope_id, after in act.after_topology.items()
            if isinstance(after, dict)
            and isinstance(act.before_topology.get(scope_id), dict)
            and after.get("parent") != act.before_topology[scope_id].get("parent")
        ),
        None,
    )
    if moved is None:
        raise ValueError(f"act {act.id} records no moved scope")
    return moved


def _removed_edges(act) -> list[tuple[str, str]]:  # noqa: ANN001
    """``(reader, source)`` for each reference edge the act's recorded topology lost."""
    edges: list[tuple[str, str]] = []
    for reader, before in act.before_topology.items():
        if not isinstance(before, dict):
            continue
        after = act.after_topology.get(reader)
        kept = set(after.get("references") or []) if isinstance(after, dict) else set()
        edges.extend((reader, s) for s in (before.get("references") or []) if s not in kept)
    return edges


def _recorded_receivers(act) -> list[str]:  # noqa: ANN001
    """The scopes the act's RECORDED topology says must be told, in a stable order."""
    if act.change_type == "reparent":
        moved = _moved_scope(act)
        descendants = (act.after_topology.get(moved) or {}).get("descendants") or []
        return list(dict.fromkeys([moved, *[str(d) for d in descendants]]))
    if act.change_type == "remove_edge":
        return list(dict.fromkeys(reader for reader, _source in _removed_edges(act)))
    return []


def unemitted_receivers(record_store: RecordStore, act_id: str) -> list[str]:
    """The recorded receivers of structure act *act_id* that have no event for it yet.

    ``[]`` when emission is complete, and for every change type that owes nothing. The
    receivers are read from the act's recorded topology, the events from
    :func:`events_for_act`; nothing is read from text.
    """
    act = record_store.get_fleet_structure_act(act_id)
    told = {row["scope_id"] for row in events_for_act(record_store, act_id)}
    return [r for r in _recorded_receivers(act) if r not in told]


def _emit_chain_changed(
    record_store: RecordStore,
    act,
    *,
    summaries_dir: str,  # noqa: ANN001
    skip: frozenset[str] = frozenset(),
) -> list[dict]:
    moved = _moved_scope(act)
    chain_before, chain_after = _chain_before_after(act, moved)
    # The receiving set is the RECORDED one: the moved scope and its subtree at apply time.
    receivers = [r for r in _recorded_receivers(act) if r not in skip]
    gained_scopes = [s for s in chain_after if s not in chain_before]
    lost_scopes = [s for s in chain_before if s not in chain_after]
    gained = _inherited_by_scope(gained_scopes, summaries_dir=summaries_dir)
    lost = _inherited_by_scope(lost_scopes, summaries_dir=summaries_dir)
    summaries = _summaries_or_none(summaries_dir)
    change_ids = [new_change_id()]
    emitted: list[dict] = []
    for receiver in receivers:
        summary = summaries.read(receiver) if summaries is not None else None
        own = [(d.id, d.content) for d in (summary.directives if summary else [])]
        conflicts = find_chain_conflicts(scope_id=receiver, own=own, gained=gained)
        payload = {
            "act_id": act.id,
            "moved_scope": moved,
            "chain_before": chain_before,
            "chain_after": chain_after,
            "gained": [{"id": i, "scope": o, "text": t} for i, o, t in gained],
            "lost": [{"id": i, "scope": o, "text": t} for i, o, t in lost],
            "conflicts": conflicts,
            "conflict_lines": [c["line"] for c in conflicts],
        }
        emit(
            fleet=None,  # type: ignore[arg-type] — the audience is explicit; no fleet is read
            record_store=record_store,
            item=act.id,
            kind=CHAIN_CHANGED,
            source_scope_id=moved,
            before=" → ".join([*chain_before, moved]),
            after=json.dumps(payload, sort_keys=True),
            wave_ids=change_ids,
            audience=[receiver],
        )
        emitted.append({"scope_id": receiver, "kind": CHAIN_CHANGED, "conflicts": len(conflicts)})
    return emitted


def _cited_directives(
    record_store: RecordStore, reader: str, item_ids: Sequence[str], *, summaries_dir: str
) -> list[str]:
    """Ids of *reader*'s own directives whose originating contribution cites one of
    *item_ids* (published item ids of the cut source). Stated limit: only a citation by
    published-item id is found; a judge-declared ``context_sources`` is not persisted
    on the directive's record, so a directive resting on the source only that way is
    not named here."""
    store = _summaries_or_none(summaries_dir)
    summary = store.read(reader) if store is not None else None
    if summary is None or not item_ids:
        return []
    cited: list[str] = []
    for directive in summary.directives:
        origin = record_store.get_contribution(directive.id)
        text = " ".join(
            part for part in (directive.content, origin.content if origin else "") if part
        )
        if any(re.search(rf"(?<![\w-]){re.escape(i)}(?![\w-])", text) for i in item_ids):
            cited.append(directive.id)
    return cited


def _emit_channel_removed(
    record_store: RecordStore,
    act,
    *,
    summaries_dir: str,  # noqa: ANN001
    skip: frozenset[str] = frozenset(),
) -> list[dict]:
    from strata.publication import read_publication  # noqa: PLC0415

    emitted: list[dict] = []
    change_ids = [new_change_id()]
    for reader in dict.fromkeys(r for r, _s in _removed_edges(act)):
        if reader in skip:
            continue
        for source in [s for r, s in _removed_edges(act) if r == reader]:
            item_ids = [i.id for i in read_publication(source, summaries_dir=summaries_dir)]
            notices = [
                channel_removed_notice(own_id=own_id, source_scope=source, scope_id=reader)
                for own_id in _cited_directives(
                    record_store, reader, item_ids, summaries_dir=summaries_dir
                )
            ]
            payload = {
                "act_id": act.id,
                "reader": reader,
                "source": source,
                "publication_item_ids": item_ids,
                "notices": notices,
            }
            emit(
                fleet=None,  # type: ignore[arg-type] — the audience is explicit; no fleet is read
                record_store=record_store,
                item=act.id,
                kind=CHANNEL_REMOVED,
                source_scope_id=source,
                before=f"{reader} reads {source}",
                after=json.dumps(payload, sort_keys=True),
                wave_ids=change_ids,
                audience=[reader],
            )
            emitted.append({"scope_id": reader, "kind": CHANNEL_REMOVED, "notices": len(notices)})
    return emitted


class StructureEmissionError(RuntimeError):
    """Emission for a fleet structure act failed, or could not be attempted.

    Raised by :func:`emit_structure_change` after it has recorded the failure. The apply
    that called it stands (the file and the act are the truth) and reports the failure;
    ``strata fleet reemit <act_id>`` emits to the receivers still owed.
    """

    def __init__(self, act_id: str, cause: BaseException | str) -> None:
        super().__init__(f"structure emission for {act_id} failed: {cause}")
        self.act_id = act_id
        self.cause = cause


def emit_structure_change(
    act_id: str,
    *,
    record_store: RecordStore | None = None,
    summaries_dir: str | None = None,
) -> dict:
    """Tell the scopes a fleet structure act affects (ADR 0014 D8), once per receiver.

    Reads the act's RECORDED before/after topology, never today's fleet, so a later
    structure change cannot redirect the notice:

    - ``remove_edge``: one ``channel_removed`` to the reader, naming the source and the
      source's publication item ids, plus a notice per own directive that cites one;
    - ``reparent``: one ``chain_changed`` to the moved scope and each recorded
      descendant, with the directives gained and lost and the 1.17 inherited check's
      conflict lines for that receiver;
    - every other change type: nothing.

    Emission goes through :func:`emit` (change ids, once-per-scope, the notice row, the
    hop budget). Idempotent per RECEIVER: a receiver that already has an event for the act
    (see :func:`events_for_act`) is skipped and the rest are told, so a run that stopped
    part-way is completed by calling again; see :func:`unemitted_receivers`.

    Args:
        act_id: The ``fleet_structure_acts`` id.
        record_store: The store holding the act. Callers pass the one they hold.
        summaries_dir: Where the summaries, publications and operator layers live;
            resolved from the project's storage paths or beside the database when omitted.

    Returns:
        ``{"act_id", "change_type", "events": [...], "already_emitted": bool}`` on
        success; ``events`` lists only what this call emitted, and ``already_emitted`` is
        true when every recorded receiver already had its event.

    Raises:
        StructureEmissionError: Emission failed, or a recorded receiver is still without
            its event afterwards, or no *record_store* was given. The failure is recorded
            first (:func:`_record_emission_failure`) where a store is available.
    """
    if record_store is None:
        _logger.error("structure emission for %s refused: no record store", act_id)
        raise StructureEmissionError(act_id, "no record_store was given")
    result: dict = {"act_id": act_id, "events": [], "already_emitted": False}
    try:
        act = record_store.get_fleet_structure_act(act_id)
        result["change_type"] = act.change_type
        owed = unemitted_receivers(record_store, act_id)
        if act.change_type not in ("remove_edge", "reparent"):
            return result
        if not owed:
            result["already_emitted"] = True
            return result
        skip = frozenset(r for r in _recorded_receivers(act) if r not in owed)
        resolved = _resolve_summaries_dir(record_store, summaries_dir)
        if act.change_type == "reparent":
            result["events"] = _emit_chain_changed(
                record_store, act, summaries_dir=resolved, skip=skip
            )
        else:
            result["events"] = _emit_channel_removed(
                record_store, act, summaries_dir=resolved, skip=skip
            )
        # emit() reports one receiver's store failure by log and note, not by raising; a
        # receiver still owed after the loop is a failure of this call.
        still_owed = unemitted_receivers(record_store, act_id)
        if still_owed:
            raise RuntimeError(f"no event was written for: {', '.join(still_owed)}")
    except StructureEmissionError:
        raise
    except Exception as exc:  # noqa: BLE001 — recorded, then raised as one error type
        _logger.exception("structure emission for %s failed", act_id)
        scope = _failure_scope(record_store, act_id)
        if scope is not None:
            _record_emission_failure(
                record_store,
                change_id=act_id,
                item=act_id,
                kind=str(result.get("change_type", "structure change")),
                source_scope_id=scope,
                affected_scope_id=scope,
            )
        raise StructureEmissionError(act_id, exc) from exc
    return result


def _failure_scope(record_store: RecordStore, act_id: str) -> str | None:
    """A scope known to be writable for the failure note: the first touched scope of the act."""
    try:
        act = record_store.get_fleet_structure_act(act_id)
    except Exception:  # noqa: BLE001
        return None
    for scope_id, entry in act.after_topology.items():
        if isinstance(entry, dict):
            return str(scope_id)
    for scope_id in act.before_topology:
        return str(scope_id)
    return None
