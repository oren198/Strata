"""The operator stratum — ADR 0008 (Operator Stratum Mechanism), issue #91 / #83B.

The **operator** is the human authority that defines the fleet and from
which all scope authority is delegated (CONTEXT.md § Operator). This module
is the library home for everything the operator does, in the **two distinct
capacities** ADR 0008's Context section names:

1. **Writing the operator stratum** — publishing, superseding, or retiring
   *operator* memory attached above some scope. This exercises the
   operator's own stratum authority, which is not delegated, so it is
   **never judged**: :func:`operator_publish`, :func:`operator_supersede_item`,
   and :func:`operator_retire_item` append to the operator's OWN record
   (``operator_acts`` — :class:`~strata.record_store.OperatorAct`) and
   rewrite the operator's working layer file for the attachment scope. This
   memory composes into perspectives verbatim, as its own labelled layer
   (:mod:`strata.perspective`) — never rewritten by any scope-manager — and
   is rendered to judges as a binding input (:mod:`strata.scope_manager`).

2. **Correcting a scope's native memory** — superseding or retiring an item
   *inside* some scope's own summary, in person, exercising **that scope's**
   authority in place of its standing delegate (the scope-manager). This
   genuinely IS a judgment — made by the operator — so
   :func:`operator_supersede` and :func:`operator_retire` append to the
   *target scope's own record* (a contribution + a ``judged_by="operator"``
   judgment for supersede; a ``retirements`` event for retire) and
   mechanically splice the target scope's summary. No LLM call: a human
   ruling is not raw material for paraphrase.

Same human, two capacities, two records — operator-stratum acts never enter
a scope's record, and scope corrections never enter the operator's own
record (ADR 0008 D1/D4).

An in-person correction (capacity 2) that removes a directive from a scope's
summary is also one of the three ADR 0007 D3 mechanical-propagation choke
points: :func:`operator_supersede` and :func:`operator_retire` call
:func:`strata.publication.propagate_directive_removals` after their splice,
under the same lock, so a published item anchored only to the
superseded/retired directive is withdrawn from that scope's publication with
no LLM in the loop — exactly as it would be for an ordinary contribution
judgment's rewrite (:func:`strata.app._judge_and_record`).

This module also provides :func:`operator_memory_binding` (what operator
memory binds a scope, for judge-aware rendering — ADR 0008 D3) and
:func:`operator_health` (the constitutional-not-operational size/churn
signal — ADR 0008 D6).

Vocabulary follows CONTEXT.md verbatim: operator, directive, context,
scope, scope summary, record, contribution, provenance, retirement,
supersession.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml

from strata.fleet_config import FleetConfig
from strata.locks import scope_lock
from strata.publication import propagate_directive_removals
from strata.record_store import ContributorRef, OperatorAct, RecordStore, Retirement
from strata.summary_store import Directive, SummaryStore

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperatorItem:
    """One item of operator memory attached to a scope, as read from the working layer.

    Mirrors :class:`~strata.record_store.OperatorAct` but is the item's
    CURRENT state (after any supersession) rather than a single historical
    act — ``id`` is the id of the act that produced this item (a
    ``publish`` or the winning ``supersede``), so it can itself be cited in
    a later ``supersede``/``retire`` call.
    """

    id: str
    kind: Literal["directive", "context"]
    content: str
    subject: str | None
    created_at: str


# ---------------------------------------------------------------------------
# Working layer file — <summaries_dir>/operator/<scope_id>.md
#
# Canonical current operator memory for one attachment scope (ADR 0008 D1).
# Machine-written only, deterministic, human-readable, VERBATIM (no LLM ever
# touches this file). Mirrors summary_store's render/parse/atomic-write
# discipline, simplified: no version/parent_version bookkeeping — operator
# memory has no descendant-staleness concept, only the record's append-only
# history.
# ---------------------------------------------------------------------------

_OPERATOR_LAYER_DIRNAME = "operator"
_NONE_YET = "_(none yet)_"

# Matches:  ## [op_abc123] directive
_ITEM_HEADING_RE = re.compile(r"^##\s+\[([^\]]+)\]\s+(directive|context)\s*$")
# Matches:  - subject: value
_SUBJECT_LINE_RE = re.compile(r"^-\s+subject:\s*(.*)")
# Matches:  - created_at: value
_CREATED_AT_LINE_RE = re.compile(r"^-\s+created_at:\s*(.*)")
# Matches:  > blockquote body
_BLOCKQUOTE_RE = re.compile(r"^>\s*(.*)")


def _operator_layer_dir(summaries_dir: str) -> Path:
    return Path(summaries_dir) / _OPERATOR_LAYER_DIRNAME


def _operator_layer_path(summaries_dir: str, scope_id: str) -> Path:
    return _operator_layer_dir(summaries_dir) / f"{scope_id}.md"


def _render_operator_layer(scope_id: str, items: list[OperatorItem]) -> str:
    """Serialise *items* to the canonical operator working-layer markdown format."""
    lines: list[str] = []

    frontmatter = {"scope_id": scope_id}
    lines.append("---")
    lines.append(yaml.dump(frontmatter, default_flow_style=False).rstrip())
    lines.append("---")
    lines.append("")
    lines.append(f"# Operator memory: {scope_id}")
    lines.append("")

    if not items:
        lines.append(_NONE_YET)
        lines.append("")
    else:
        for item in items:
            lines.append(f"## [{item.id}] {item.kind}")
            subject_value = item.subject if item.subject is not None else ""
            lines.append(f"- subject: {subject_value}")
            lines.append(f"- created_at: {item.created_at}")
            lines.append("")
            # Blockquote every line, verbatim, so multi-line content round-trips
            # exactly instead of being flattened or truncated (mirrors
            # summary_store's directive-content discipline).
            for content_line in item.content.splitlines() or [""]:
                lines.append(f"> {content_line}")
            lines.append("")

    return "\n".join(lines)


def _parse_operator_layer(text: str) -> list[OperatorItem]:
    """Parse an operator working-layer file back into its :class:`OperatorItem` list."""
    if text.startswith("---"):
        end = text.index("\n---\n", 3)
        body = text[end + 5 :]
    else:
        raise ValueError("Missing YAML frontmatter in operator layer file")

    items: list[OperatorItem] = []

    cur_id: str | None = None
    cur_kind: str | None = None
    cur_subject: str | None = None
    cur_created_at: str | None = None
    cur_blockquote_lines: list[str] = []

    def _flush() -> None:
        nonlocal cur_id, cur_kind, cur_subject, cur_created_at, cur_blockquote_lines
        if cur_id is None:
            return
        items.append(
            OperatorItem(
                id=cur_id,
                kind=cur_kind,  # type: ignore[arg-type]
                content="\n".join(cur_blockquote_lines),
                subject=cur_subject if cur_subject else None,
                created_at=cur_created_at or "",
            )
        )
        cur_id = None
        cur_kind = None
        cur_subject = None
        cur_created_at = None
        cur_blockquote_lines = []

    for raw_line in body.splitlines():
        line = raw_line.rstrip()

        m_heading = _ITEM_HEADING_RE.match(line)
        if m_heading:
            _flush()
            cur_id = m_heading.group(1)
            cur_kind = m_heading.group(2)
            continue

        if cur_id is None:
            # Title line, blank lines, or the "_(none yet)_" sentinel before
            # the first item heading — nothing to capture.
            continue

        m_subject = _SUBJECT_LINE_RE.match(line)
        if m_subject:
            cur_subject = m_subject.group(1).strip() or None
            continue

        m_created = _CREATED_AT_LINE_RE.match(line)
        if m_created:
            cur_created_at = m_created.group(1).strip()
            continue

        m_bq = _BLOCKQUOTE_RE.match(line)
        if m_bq:
            cur_blockquote_lines.append(m_bq.group(1))
            continue

    _flush()
    return items


def _write_operator_layer(scope_id: str, items: list[OperatorItem], *, summaries_dir: str) -> None:
    """Atomically write *items* as *scope_id*'s operator working layer.

    Write-to-tmp-then-``os.replace`` — same discipline as
    :meth:`~strata.summary_store.SummaryStore.write` — so a crashed writer
    never leaves a partial file visible to readers.
    """
    directory = _operator_layer_dir(summaries_dir)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{scope_id}.md"
    tmp = directory / f"{scope_id}.md.tmp"
    tmp.write_text(_render_operator_layer(scope_id, items), encoding="utf-8")
    os.replace(tmp, final)


def read_operator_layer(scope_id: str, *, summaries_dir: str) -> list[OperatorItem]:
    """Return the current operator memory attached at *scope_id*.

    Returns an empty list if no operator layer file exists for this
    attachment scope yet — an unattached scope has no operator memory, not
    an error.
    """
    path = _operator_layer_path(summaries_dir, scope_id)
    if not path.exists():
        return []
    return _parse_operator_layer(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Capacity 1 — writing the operator stratum (ADR 0008 D1). Not judged: the
# operator's own stratum authority is not delegated. Each call appends to the
# operator's OWN record (operator_acts) and rewrites the working layer file
# for the attachment scope. Guarded by a lock in a namespace disjoint from
# native scope ids ("operator:<scope_id>") so concurrent operator-stratum
# writes to the same attachment scope never race, without contending with
# that scope's own contribute-path lock (strata.locks.scope_lock).
# ---------------------------------------------------------------------------


def _operator_layer_lock_key(target_scope_id: str) -> str:
    return f"operator:{target_scope_id}"


def operator_publish(
    target_scope_id: str,
    content: str,
    kind: Literal["directive", "context"],
    subject: str | None = None,
    *,
    record_store: RecordStore,
    summaries_dir: str,
) -> OperatorItem:
    """Publish a new piece of operator memory attached at *target_scope_id*.

    Appends a ``publish`` act to the operator's own record and rewrites the
    operator working layer for *target_scope_id* to include the new item.
    Not judged (ADR 0008 D1) — the operator's stratum authority is not
    delegated.

    Args:
        target_scope_id: The attachment scope — the operator layer's reach
            point (ADR 0008 D2): attached at S, it composes above S and
            binds S's subtree.
        content: Verbatim operator memory text.
        kind: ``'directive'`` (binds the attachment scope's subtree) or
            ``'context'`` (informs without binding), exactly like any
            stratum's memory.
        subject: Optional short subject line.

    Returns:
        The newly published :class:`OperatorItem`.
    """
    with scope_lock(_operator_layer_lock_key(target_scope_id)):
        act = record_store.append_operator_act(
            act="publish",
            target_scope_id=target_scope_id,
            kind=kind,
            content=content,
            subject=subject,
        )
        item = OperatorItem(
            id=act.id, kind=kind, content=content, subject=subject, created_at=act.created_at
        )
        items = read_operator_layer(target_scope_id, summaries_dir=summaries_dir)
        items.append(item)
        _write_operator_layer(target_scope_id, items, summaries_dir=summaries_dir)
        return item


def operator_supersede_item(
    target_scope_id: str,
    item_id: str,
    content: str,
    subject: str | None = None,
    *,
    record_store: RecordStore,
    summaries_dir: str,
) -> OperatorItem:
    """Supersede an existing operator item attached at *target_scope_id*.

    Appends a ``supersede`` act (``supersedes=item_id``) to the operator's
    own record and rewrites the working layer: the old item is replaced,
    in place, by a new item under a new id. Not judged (ADR 0008 D1). The
    new item keeps the superseded item's ``kind``.

    Args:
        target_scope_id: The attachment scope the item lives at.
        item_id: The ``op_``-prefixed id of the operator item being replaced.
        content: Verbatim replacement content.
        subject: Optional short subject line for the replacement; defaults
            to the superseded item's subject when omitted.

    Returns:
        The new :class:`OperatorItem`.

    Raises:
        KeyError: *item_id* is not in *target_scope_id*'s current operator layer.
    """
    with scope_lock(_operator_layer_lock_key(target_scope_id)):
        items = read_operator_layer(target_scope_id, summaries_dir=summaries_dir)
        index = next((i for i, it in enumerate(items) if it.id == item_id), None)
        if index is None:
            raise KeyError(
                f"Operator item {item_id!r} not found in the operator layer attached at "
                f"{target_scope_id!r}."
            )
        existing = items[index]
        effective_subject = subject if subject is not None else existing.subject

        act = record_store.append_operator_act(
            act="supersede",
            target_scope_id=target_scope_id,
            kind=existing.kind,
            content=content,
            subject=effective_subject,
            supersedes=item_id,
        )
        new_item = OperatorItem(
            id=act.id,
            kind=existing.kind,
            content=content,
            subject=effective_subject,
            created_at=act.created_at,
        )
        items[index] = new_item
        _write_operator_layer(target_scope_id, items, summaries_dir=summaries_dir)
        return new_item


def operator_retire_item(
    target_scope_id: str,
    item_id: str,
    *,
    record_store: RecordStore,
    summaries_dir: str,
) -> OperatorAct:
    """Retire an operator item attached at *target_scope_id* without replacement.

    Appends a ``retire`` act (``retires=item_id``) to the operator's own
    record and rewrites the working layer with the item removed. No new
    memory enters — ``kind``/``content`` are ``None`` on the recorded act,
    matching :func:`operator_retire`'s scope-level counterpart. Not judged
    (ADR 0008 D1).

    Args:
        target_scope_id: The attachment scope the item lives at.
        item_id: The ``op_``-prefixed id of the operator item being retired.

    Returns:
        The ``retire`` :class:`~strata.record_store.OperatorAct`.

    Raises:
        KeyError: *item_id* is not in *target_scope_id*'s current operator layer.
    """
    with scope_lock(_operator_layer_lock_key(target_scope_id)):
        items = read_operator_layer(target_scope_id, summaries_dir=summaries_dir)
        if not any(it.id == item_id for it in items):
            raise KeyError(
                f"Operator item {item_id!r} not found in the operator layer attached at "
                f"{target_scope_id!r}."
            )
        act = record_store.append_operator_act(
            act="retire",
            target_scope_id=target_scope_id,
            kind=None,
            content=None,
            retires=item_id,
        )
        remaining = [it for it in items if it.id != item_id]
        _write_operator_layer(target_scope_id, remaining, summaries_dir=summaries_dir)
        return act


# ---------------------------------------------------------------------------
# Composition input — what operator memory binds a scope (ADR 0008 D2/D3).
# ---------------------------------------------------------------------------


def operator_memory_binding(
    scope_id: str,
    *,
    fleet: FleetConfig,
    summaries_dir: str,
) -> list[tuple[str, list[OperatorItem]]]:
    """Return the operator memory binding *scope_id*, root-first.

    "Binding" here means: attached at *scope_id* itself, or at any of its
    inter-stratum ancestors — the same chain :mod:`strata.perspective`
    composes (ADR 0008 D2) and :mod:`strata.scope_manager` renders to the
    scope-manager (ADR 0008 D3). Only chain scopes that actually HAVE
    operator memory are included — an unattached ancestor contributes no
    entry.

    Args:
        scope_id: The scope whose binding operator memory to resolve.
        fleet: The loaded fleet configuration (for the ancestor chain).
        summaries_dir: The summaries directory operator layers live under.

    Returns:
        ``[(attachment_scope_id, items), ...]`` root-first, one entry per
        chain scope that has operator memory.

    Raises:
        ValueError: *scope_id* is not found in *fleet*.
    """
    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise ValueError(f"Scope not found: {scope_id!r}")

    chain = [*fleet.inter_stratum_ancestors(scope_id), scope]
    binding: list[tuple[str, list[OperatorItem]]] = []
    for s in chain:
        items = read_operator_layer(s.id, summaries_dir=summaries_dir)
        if items:
            binding.append((s.id, items))
    return binding


# ---------------------------------------------------------------------------
# Health signal — constitutional, not operational (ADR 0008 D6).
# ---------------------------------------------------------------------------


def operator_health(
    *,
    record_store: RecordStore,
    summaries_dir: str,
    churn_window_days: int = 30,
) -> dict:
    """Return the operator memory health signal.

    Doctrine (ADR 0008 D6, philosophy.md Concept 3): operator memory is
    **constitutional, not operational** — it should be small, rare, and
    mostly stable. A fleet steered day-to-day through the operator layer has
    replaced the self-correcting system Strata exists to build. This helper
    surfaces the numbers that reading is checked against; it renders no
    verdict of its own.

    Args:
        record_store: The operator's own record (``operator_acts``).
        summaries_dir: The summaries directory operator layers live under.
        churn_window_days: Trailing window, in days, for the churn count.
            Defaults to 30.

    Returns:
        ``{"per_scope": {scope_id: {"items": N, "words": N}, ...},
        "total_items": N, "total_words": N, "total_acts": N,
        "acts_last_N_days": N, "churn_window_days": N}``.
    """
    layer_dir = _operator_layer_dir(summaries_dir)
    per_scope: dict[str, dict[str, int]] = {}
    total_items = 0
    total_words = 0

    if layer_dir.is_dir():
        for entry in sorted(layer_dir.iterdir()):
            if entry.name.startswith(".") or entry.suffix != ".md":
                continue
            scope_id = entry.stem
            items = read_operator_layer(scope_id, summaries_dir=summaries_dir)
            if not items:
                continue
            words = sum(len(item.content.split()) for item in items)
            per_scope[scope_id] = {"items": len(items), "words": words}
            total_items += len(items)
            total_words += words

    all_acts = record_store.list_operator_acts()
    cutoff = datetime.now(tz=UTC) - timedelta(days=churn_window_days)
    recent_acts = 0
    for act in all_acts:
        ts = _parse_sqlite_timestamp(act.created_at)
        if ts is not None and ts >= cutoff:
            recent_acts += 1

    return {
        "per_scope": per_scope,
        "total_items": total_items,
        "total_words": total_words,
        "total_acts": len(all_acts),
        "acts_last_N_days": recent_acts,
        "churn_window_days": churn_window_days,
    }


def _parse_sqlite_timestamp(value: str) -> datetime | None:
    """Parse a SQLite ``datetime('now')`` string (``YYYY-MM-DD HH:MM:SS``, UTC) or an ISO one.

    ``operator_acts.created_at`` defaults to SQLite's ``datetime('now')``
    (naive, UTC, space-separated); tests may also feed ISO-8601 timestamps
    directly. Returns ``None`` for anything unparseable rather than raising —
    the health signal degrades gracefully rather than crashing on a
    malformed row.
    """
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Capacity 2 — correcting a scope's native memory in person (ADR 0008 D4,
# #83 primitive B). This IS a judgment: the operator exercises the TARGET
# SCOPE's authority in place of its standing delegate. Serializes under the
# same per-scope lock the contribute path uses (strata.locks.scope_lock) so
# a concurrent contribution and an in-person correction can never interleave.
# ---------------------------------------------------------------------------

_OPERATOR_PROVENANCE_SCOPE = "operator"
_OPERATOR_PROVENANCE_SKILL = "operator"
_OPERATOR_PROVENANCE_SESSION = "operator"

_SUPERSEDE_NOTE = "Operator correction (ADR 0008 D4): in-person supersession."


def operator_supersede(
    scope_id: str,
    directive_id: str,
    content: str,
    subject: str | None = None,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
) -> Directive:
    """Supersede a directive inside *scope_id*'s own summary, in person.

    Exercises *scope_id*'s authority as the operator, in place of its
    standing delegate (the scope-manager) — this genuinely is a judgment,
    made by the operator (ADR 0008 D4). Appends a contribution to
    *scope_id*'s own record under operator provenance
    (``ContributorRef(scope_id="operator", skill="operator",
    session_id="operator", ...)``) carrying ``supersedes=directive_id``,
    plus a judgment row with ``judged_by="operator"``. The summary is then
    spliced MECHANICALLY — the superseded directive replaced by the new one,
    keyed to the new contribution id — never rewritten by an LLM: a human
    ruling is not raw material for paraphrase.

    Args:
        scope_id: The scope whose own summary is being corrected.
        directive_id: The contribution id of the directive being superseded.
            Must be present in *scope_id*'s CURRENT summary.
        content: Verbatim replacement directive text.
        subject: Optional short subject line for the replacement.

    Returns:
        The new :class:`~strata.summary_store.Directive`, spliced into the
        summary already written.

    Raises:
        ValueError: *scope_id* is not found in *fleet*.
        KeyError: *directive_id* is not in *scope_id*'s current summary.
    """
    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise ValueError(f"Scope not found: {scope_id!r}")

    with scope_lock(scope_id):
        current_summary = summary_store.read(scope_id)
        existing = None
        if current_summary is not None:
            existing = next((d for d in current_summary.directives if d.id == directive_id), None)
        if existing is None:
            raise KeyError(
                f"Directive {directive_id!r} not found in scope {scope_id!r}'s current summary "
                "— operator corrections target only directives currently in the record's summary."
            )

        ts = datetime.now(tz=UTC).isoformat()
        contributor = ContributorRef(
            scope_id=_OPERATOR_PROVENANCE_SCOPE,
            skill=_OPERATOR_PROVENANCE_SKILL,
            session_id=_OPERATOR_PROVENANCE_SESSION,
            ts=ts,
        )
        contribution = record_store.append_contribution(
            scope_id=scope_id,
            content=content,
            proposed_classification="directive",
            subject=subject,
            supersedes=directive_id,
            contributor=contributor,
        )
        record_store.record_judgment(
            contribution_id=contribution.id,
            decision="accept_as_directive",
            judged_by="operator",
            notes=_SUPERSEDE_NOTE,
        )

        new_directive = Directive(
            id=contribution.id,
            content=content,
            subject=subject,
            source_scope_id=_OPERATOR_PROVENANCE_SCOPE,
            source_skill=_OPERATOR_PROVENANCE_SKILL,
            created_at=contribution.created_at,
        )
        new_directives = [d for d in current_summary.directives if d.id != directive_id]
        new_directives.append(new_directive)
        to_write = current_summary.model_copy(
            update={"directives": new_directives, "updated_at": ts}
        )
        summary_store.write(scope_id, to_write)

        # ADR 0007 D3 mechanical propagation: the superseded directive's id
        # just vanished from the summary — withdraw any published item
        # anchored only to it. No LLM in the loop.
        propagate_directive_removals(
            scope_id,
            {directive_id},
            contribution.id,
            record_store=record_store,
            summaries_dir=str(summary_store.summaries_dir),
        )
        return new_directive


def operator_retire(
    scope_id: str,
    directive_id: str,
    reason: str | None = None,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
) -> Retirement:
    """Retire a directive from *scope_id*'s own summary, in person, without replacement.

    No new memory enters, so no contribution row is fabricated (ADR 0008
    D4): the act appends a **retirement event**
    (:class:`~strata.record_store.Retirement`) to *scope_id*'s own record
    and mechanically filters the directive out of the summary.

    Args:
        scope_id: The scope whose own summary is being corrected.
        directive_id: The contribution id of the directive being retired.
            Must be present in *scope_id*'s CURRENT summary.
        reason: Optional free-text rationale.

    Returns:
        The appended :class:`~strata.record_store.Retirement`.

    Raises:
        ValueError: *scope_id* is not found in *fleet*.
        KeyError: *directive_id* is not in *scope_id*'s current summary.
    """
    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise ValueError(f"Scope not found: {scope_id!r}")

    with scope_lock(scope_id):
        current_summary = summary_store.read(scope_id)
        existing = None
        if current_summary is not None:
            existing = next((d for d in current_summary.directives if d.id == directive_id), None)
        if existing is None:
            raise KeyError(
                f"Directive {directive_id!r} not found in scope {scope_id!r}'s current summary "
                "— operator corrections target only directives currently in the record's summary."
            )

        retirement = record_store.append_retirement(
            scope_id=scope_id,
            directive_id=directive_id,
            retired_by="operator",
            reason=reason,
        )

        new_directives = [d for d in current_summary.directives if d.id != directive_id]
        to_write = current_summary.model_copy(
            update={"directives": new_directives, "updated_at": retirement.created_at}
        )
        summary_store.write(scope_id, to_write)

        # ADR 0007 D3 mechanical propagation: same as operator_supersede's —
        # withdraw any published item anchored only to the retired directive.
        propagate_directive_removals(
            scope_id,
            {directive_id},
            retirement.id,
            record_store=record_store,
            summaries_dir=str(summary_store.summaries_dir),
        )
        return retirement
