"""The publication channel — ADR 0007 (Publication Mechanism), issue #90 / #83.

**Publication** is the act by a scope of exporting a curated subset of its
memory for scopes that do not contain it — the sideways channel, counterpart
to ratification (CONTEXT.md § Publication; philosophy.md Concept 8, "the
boundary-crossing principle": memory never crosses a scope boundary raw — it
crosses downward through directives, upward through ratification, and
sideways through publication, each a judged act by the responsible
authority). Publication conveys no authority: it widens *read* reach
sideways, never binding force.

This module is the library home for the publication channel, mirroring
:mod:`strata.operator`'s shape:

- **The publication artifact** (ADR 0007 D1) — one small on-disk markdown
  file per scope, sibling to its scope summary
  (``<summaries_dir>/<scope_id>.pub.md``), holding the scope's CURRENT
  published items verbatim. Machine-written only — never LLM-rewritten; it
  changes only through publish and withdraw acts
  (:func:`read_publication`, :func:`propose_publish`, :func:`propose_withdraw`).
- **Judged publish/withdraw acts** (ADR 0007 D2) — :func:`propose_publish`
  and :func:`propose_withdraw` append the act to the scope's publication
  record FIRST (the record never lies), then invoke the scope-manager
  (:meth:`strata.scope_manager.ScopeManager.judge_publication`), then record
  the judgment, then (on accept) rewrite the artifact. A ``judge_publication()``
  failure gets the same reliability treatment the contribution path
  (:mod:`strata.app`) already has: it is recorded as a ``judge_failed``-marked
  attempt event against the act (mirroring ``judgment_attempts`` — issue #57 /
  #118), so a stranded act is visibly stranded on the record rather than
  indistinguishable from one nobody ever judged. The exception itself still
  propagates AS-IS after being recorded — no new exception type, and no
  automatic retry: a future re-judge pathway for publication is not built in
  V1, so a caller cannot yet route a retry the way ``strata_rejudge`` does for
  contributions.
- **Staleness propagation** (ADR 0007 D3) — two paths, by anchor type:
  :func:`propagate_directive_removals` is the MECHANICAL path (directive-
  anchored items, called from the three choke points that remove a directive
  from a scope's summary — no LLM in the loop); :func:`apply_judged_withdrawals`
  is the JUDGED path (subject-anchored items, driven by the contribution
  judgment's own ``withdraw_published`` verdict — ADR 0007 D3/D5).
- **Bootstrap** (ADR 0007 D4) — :func:`bootstrap_publication` is the one-shot,
  operator-initiated migration primitive: a single scope-manager call
  proposes an initial publication distilled from the scope's current summary,
  each accepted item recorded as an ordinary accepted publish act.
- **Republication** (ADR 0013 D4/D4b/D4c) — a scope may publish onward an
  item it received in another scope's publication. :func:`propose_publish`'s
  ``relay_source_scope_id``/``relay_source_item_id`` derive the item's
  ultimate origin from the source item itself (never taken from the
  caller), stored on :class:`PublishedItem` and in the record and surviving
  both composition and summary rewrites, omitted from the artifact entirely
  for a non-relay item (D7 — no migration, no back-filling of existing
  items). Withdrawing an item mechanically cascades to every downstream
  copy relayed from it, transitively, with no LLM in the loop
  (:func:`_cascade_withdraw_relays`, wired into :func:`propose_withdraw`,
  :func:`propagate_directive_removals`, and :func:`apply_judged_withdrawals`
  — a fourth choke point of D3's class). :meth:`ScopeManager.judge_publication`
  is told when a proposal relays second-hand material and shown its origin,
  with the prompt explicit that origin is information, not permission.

Every publish act carries **at least one anchor** (ADR 0007 D1) — the
provenance link obligations 2/3/7 (published ⊆ believed, trust flows home,
accountable) hang off: either a directive id currently present in the
publisher's own summary, or a subject string. Anchors are stored
prefix-tagged (``directive:<id>`` / ``subject:<text>``) so propagation can
tell them apart without re-parsing prose. Anchor validity is a STRUCTURAL
check, enforced in code BEFORE judging (mirrors the ADR 0006 D1
error-not-decline rule for structurally-refused writes): zero anchors, or a
``directive:`` anchor naming an id that is not in the current summary, is an
error — nothing gets recorded — not a scope-manager decline.

**D5 trust routing (N/A — nothing further to build in V1).** ADR 0007 D5's
"trust flows home" obligation is structural, not a feature: the anchor
stored on every published item — append-only, from day one, on every
``publication_acts`` row — IS the routing pointer a future outcome-feedback
mechanism (Horizon 3, philosophy.md Concept 6) would follow from a published
item back to its internal source. Nothing in this module severs that
pointer; there is no trust-weighting code to write yet because trust
mechanics themselves are out of scope for V1.

Vocabulary follows CONTEXT.md verbatim: publication, withdrawal, scope,
scope summary, directive, context, record, provenance, supersession,
retirement, ratification.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml

from strata.change_events import CLAIM_CORRECTED, emit_restore_notice, new_change_id
from strata.change_events import emit as emit_change_event
from strata.locks import scope_lock
from strata.record_store import (
    JUDGE_FAILED,
    ClaimCarrierCheck,
    ClaimCorrection,
    ContributorRef,
    PublicationAct,
    RecordStore,
)

if TYPE_CHECKING:
    from strata.fleet_config import FleetConfig
    from strata.scope_manager import ScopeManager
    from strata.summary_store import SummaryStore

_logger = logging.getLogger("strata.publication")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishedItem:
    """One item of a scope's publication, as read from the working artifact.

    ``id`` is the id of the ``publish`` act that created it — publication
    acts double as published-item ids (ADR 0007 D1), so a later
    :func:`propose_withdraw` names this same id. Content is stored verbatim
    (ADR 0007 D1) — the publication is never LLM-rewritten.
    """

    id: str
    kind: Literal["directive", "context"]
    content: str
    subject: str | None
    anchors: list[str]
    published_at: str
    origin_scope_id: str | None = None
    """ADR 0013 D4 — the ULTIMATE origin scope when this item relays content
    received in another scope's publication (republication). ``None`` when
    this item originated here — including every item written before this
    release (ADR 0013 D7: no migration, no back-filling)."""
    relay_scope_id: str | None = None
    """ADR 0013 D4 — the immediate predecessor scope this copy was
    republished from (the "via Y" of "according to X, via Y"). ``None``
    unless ``origin_scope_id`` is also set."""
    relay_item_id: str | None = None
    """ADR 0013 D4 — the id of the published item, in ``relay_scope_id``'s
    publication, this copy relays. ``None`` unless ``origin_scope_id`` is
    also set. Together with ``relay_scope_id`` this is the pointer the
    mechanical withdrawal cascade (D4b) follows."""


@dataclass(frozen=True)
class PublicationOutcome:
    """The result of proposing (and judging) a publish or withdraw act."""

    act_id: str
    act: Literal["publish", "withdraw", "restore"]
    decision: Literal["accept", "decline"]
    reasoning: str
    artifact_updated: bool


@dataclass(frozen=True)
class BootstrapOutcome:
    """The result of :func:`bootstrap_publication`."""

    decision: Literal["accept", "decline"]
    reasoning: str
    items: list[PublishedItem]
    trimmed: bool = False
    """Threaded from :attr:`~strata.scope_manager.BootstrapJudgment.trimmed`
    (issue #185): ``True`` when the scope-manager's own mechanical word-budget
    backstop dropped at least one item the judge proposed. ``False`` for a
    decline, and ``False`` whenever the judge's proposal already fit its
    budget — which the judge is told, so this should be the common case."""


# ---------------------------------------------------------------------------
# Publication artifact — <summaries_dir>/<scope_id>.pub.md
#
# The scope's CURRENT outward face (ADR 0007 D1). Machine-written only,
# deterministic, human-readable, VERBATIM (no LLM ever touches this file) —
# mirrors summary_store's / operator's render/parse/atomic-write discipline.
# Anchors are stored as a single-line JSON array (a deliberate pick over a
# comma-separated list: anchor text — especially subject: text — may itself
# contain commas, and JSON round-trips exactly without an escaping scheme).
# ---------------------------------------------------------------------------

_PUBLICATION_SUFFIX = ".pub.md"
_NONE_YET = "_(none yet)_"

# Matches:  ## [pub_abc123] directive
_ITEM_HEADING_RE = re.compile(r"^##\s+\[([^\]]+)\]\s+(directive|context)\s*$")
# Matches:  - subject: value
_SUBJECT_LINE_RE = re.compile(r"^-\s+subject:\s*(.*)")
# Matches:  - anchors: ["directive:c_abc123"]
_ANCHORS_LINE_RE = re.compile(r"^-\s+anchors:\s*(.*)")
# Matches:  - published_at: value
_PUBLISHED_AT_LINE_RE = re.compile(r"^-\s+published_at:\s*(.*)")
# Matches:  - origin: value           (ADR 0013 D4 — omitted for a non-relay item)
_ORIGIN_LINE_RE = re.compile(r"^-\s+origin:\s*(.*)")
# Matches:  - relay: scope/item       (ADR 0013 D4 — omitted for a non-relay item)
_RELAY_LINE_RE = re.compile(r"^-\s+relay:\s*(\S+)/(\S+)\s*$")
# Matches:  > blockquote body
_BLOCKQUOTE_RE = re.compile(r"^>\s*(.*)")


def _publication_path(summaries_dir: str, scope_id: str) -> Path:
    return Path(summaries_dir) / f"{scope_id}{_PUBLICATION_SUFFIX}"


def _render_publication(scope_id: str, items: list[PublishedItem]) -> str:
    """Serialise *items* to the canonical publication-artifact markdown format."""
    lines: list[str] = []

    frontmatter = {"scope_id": scope_id}
    lines.append("---")
    lines.append(yaml.dump(frontmatter, default_flow_style=False).rstrip())
    lines.append("---")
    lines.append("")
    lines.append(f"# Publication: {scope_id}")
    lines.append("")

    if not items:
        lines.append(_NONE_YET)
        lines.append("")
    else:
        for item in items:
            lines.append(f"## [{item.id}] {item.kind}")
            subject_value = item.subject if item.subject is not None else ""
            lines.append(f"- subject: {subject_value}")
            lines.append(f"- anchors: {json.dumps(list(item.anchors))}")
            lines.append(f"- published_at: {item.published_at}")
            # ADR 0013 D4 — omitted entirely for a non-relay item (origin_scope_id
            # is None), so an artifact predating relay fields re-renders
            # byte-identical after a parse -> re-render round trip (D7: no
            # migration, no back-filling).
            if item.origin_scope_id is not None:
                lines.append(f"- origin: {item.origin_scope_id}")
                lines.append(f"- relay: {item.relay_scope_id}/{item.relay_item_id}")
            lines.append("")
            # Blockquote every line, verbatim, so multi-line content round-trips
            # exactly instead of being flattened or truncated.
            for content_line in item.content.splitlines() or [""]:
                lines.append(f"> {content_line}")
            lines.append("")

    return "\n".join(lines)


def _parse_publication(text: str) -> list[PublishedItem]:
    """Parse a publication artifact back into its :class:`PublishedItem` list."""
    if text.startswith("---"):
        end = text.index("\n---\n", 3)
        body = text[end + 5 :]
    else:
        raise ValueError("Missing YAML frontmatter in publication artifact")

    items: list[PublishedItem] = []

    cur_id: str | None = None
    cur_kind: str | None = None
    cur_subject: str | None = None
    cur_anchors_raw: str | None = None
    cur_published_at: str | None = None
    cur_origin_scope_id: str | None = None
    cur_relay_scope_id: str | None = None
    cur_relay_item_id: str | None = None
    cur_blockquote_lines: list[str] = []

    def _flush() -> None:
        nonlocal cur_id, cur_kind, cur_subject, cur_anchors_raw
        nonlocal cur_published_at, cur_blockquote_lines
        nonlocal cur_origin_scope_id, cur_relay_scope_id, cur_relay_item_id
        if cur_id is None:
            return
        anchors: list[str] = []
        if cur_anchors_raw:
            try:
                parsed = json.loads(cur_anchors_raw)
                if isinstance(parsed, list):
                    anchors = [str(a) for a in parsed]
            except (json.JSONDecodeError, TypeError):
                anchors = []
        items.append(
            PublishedItem(
                id=cur_id,
                kind=cur_kind,  # type: ignore[arg-type]
                content="\n".join(cur_blockquote_lines),
                subject=cur_subject if cur_subject else None,
                anchors=anchors,
                published_at=cur_published_at or "",
                origin_scope_id=cur_origin_scope_id,
                relay_scope_id=cur_relay_scope_id,
                relay_item_id=cur_relay_item_id,
            )
        )
        cur_id = None
        cur_kind = None
        cur_subject = None
        cur_anchors_raw = None
        cur_published_at = None
        cur_origin_scope_id = None
        cur_relay_scope_id = None
        cur_relay_item_id = None
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

        m_anchors = _ANCHORS_LINE_RE.match(line)
        if m_anchors:
            cur_anchors_raw = m_anchors.group(1).strip()
            continue

        m_published_at = _PUBLISHED_AT_LINE_RE.match(line)
        if m_published_at:
            cur_published_at = m_published_at.group(1).strip()
            continue

        m_origin = _ORIGIN_LINE_RE.match(line)
        if m_origin:
            cur_origin_scope_id = m_origin.group(1).strip() or None
            continue

        m_relay = _RELAY_LINE_RE.match(line)
        if m_relay:
            cur_relay_scope_id = m_relay.group(1)
            cur_relay_item_id = m_relay.group(2)
            continue

        m_bq = _BLOCKQUOTE_RE.match(line)
        if m_bq:
            cur_blockquote_lines.append(m_bq.group(1))
            continue

    _flush()
    return items


def _write_publication(scope_id: str, items: list[PublishedItem], *, summaries_dir: str) -> None:
    """Atomically write *items* as *scope_id*'s publication artifact.

    Write-to-tmp-then-``os.replace`` — same discipline as
    :meth:`~strata.summary_store.SummaryStore.write` and
    :mod:`strata.operator`'s working layer — so a crashed writer never leaves
    a partial file visible to readers.
    """
    directory = Path(summaries_dir)
    directory.mkdir(parents=True, exist_ok=True)
    final = _publication_path(summaries_dir, scope_id)
    tmp = directory / f"{scope_id}{_PUBLICATION_SUFFIX}.tmp"
    tmp.write_text(_render_publication(scope_id, items), encoding="utf-8")
    os.replace(tmp, final)


def read_publication(scope_id: str, *, summaries_dir: str) -> list[PublishedItem]:
    """Return the current published items for *scope_id*.

    Returns an empty list if the scope has published nothing yet — the
    "honestly empty face" ADR 0007 D4 asks composition to preserve: a scope
    that publishes nothing is visibly quiet, not an error.
    """
    path = _publication_path(summaries_dir, scope_id)
    if not path.exists():
        return []
    return _parse_publication(path.read_text(encoding="utf-8"))


def read_publication_text(scope_id: str, *, summaries_dir: str) -> str | None:
    """Return the raw markdown text of *scope_id*'s publication artifact, or ``None``.

    Used by ``strata publication show`` to print the artifact byte-for-byte
    verbatim, rather than re-rendering it from parsed items.
    """
    path = _publication_path(summaries_dir, scope_id)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def list_scopes_with_publications(summaries_dir: str) -> list[str]:
    """Return scope ids that have a publication artifact on disk, sorted."""
    directory = Path(summaries_dir)
    if not directory.is_dir():
        return []
    ids: list[str] = []
    for entry in directory.iterdir():
        name = entry.name
        if name.startswith("."):
            continue
        if not name.endswith(_PUBLICATION_SUFFIX):
            continue
        ids.append(name[: -len(_PUBLICATION_SUFFIX)])
    return sorted(ids)


# ---------------------------------------------------------------------------
# Anchors (ADR 0007 D1) — structural validation, before judging.
# ---------------------------------------------------------------------------

_DIRECTIVE_ANCHOR_PREFIX = "directive:"
_SUBJECT_ANCHOR_PREFIX = "subject:"


def _binding_directive_ids(
    scope_id: str, *, fleet: FleetConfig, summary_store: SummaryStore
) -> set[str]:
    """Every directive id that binds *scope_id*: its own, plus its ancestors'.

    ADR 0015 D3 — an inherited directive binds the scope, so a published item
    may anchor to it. Before ADR 0015 the splice put the ancestor's row in the
    scope's own summary and this set was accidentally right; with the copy
    gone (D1) the ancestor half comes from the walk, which is the same walk
    composition and the judge read (D2).

    One set feeding one validation function, deliberately: anchor
    classification (:func:`_tag_anchor`) and anchor validation
    (:func:`_validate_anchors`) must agree about which ids are directives, or
    a bare ancestor id is silently downgraded to a free-form ``subject:``
    anchor and then passes validation as the wrong kind — an item nothing
    would ever sweep.
    """
    # Deferred import — strata.perspective is a read-side module and importing
    # it at module scope here would tangle the publication path with it.
    from strata.perspective import ancestor_directives

    own = summary_store.read(scope_id)
    ids = {d.id for d in own.directives} if own is not None else set()
    for _ancestor_scope_id, directives in ancestor_directives(
        scope_id, fleet=fleet, summary_store=summary_store
    ):
        ids.update(d.id for d in directives)
    return ids


def _tag_anchor(raw: str, *, valid_directive_ids: Collection[str]) -> str:
    """Prefix-tag one raw anchor string as ``directive:<id>`` or ``subject:<text>``.

    An anchor already carrying an explicit ``directive:``/``subject:`` prefix
    is respected verbatim (still subject to :func:`_validate_anchors` below —
    this lets a caller assert "this is a directive anchor" and have it
    checked, rather than silently downgraded to a subject anchor because the
    directive was removed). Anything else is auto-classified: an exact match
    against a directive id that currently binds the scope
    (*valid_directive_ids*, from :func:`_binding_directive_ids`) becomes a
    ``directive:`` anchor; anything else becomes a ``subject:`` anchor
    (free-form — no verification is possible or required for a subject
    reference).
    """
    if raw.startswith(_DIRECTIVE_ANCHOR_PREFIX) or raw.startswith(_SUBJECT_ANCHOR_PREFIX):
        return raw
    if raw in valid_directive_ids:
        return f"{_DIRECTIVE_ANCHOR_PREFIX}{raw}"
    return f"{_SUBJECT_ANCHOR_PREFIX}{raw}"


def _validate_anchors(
    tagged_anchors: Sequence[str], *, valid_directive_ids: Collection[str]
) -> None:
    """Structurally validate *tagged_anchors* — raises :class:`ValueError`, never a decline.

    Mirrors the ADR 0006 D1 error-not-decline rule for structurally-refused
    writes: this runs BEFORE the scope-manager is ever invoked, and a
    failure here means no act row is appended at all.

    Raises:
        ValueError: *tagged_anchors* is empty, or a ``directive:`` anchor
            names an id that does not currently bind the scope — neither its
            own nor any ancestor's (ADR 0015 D3).
    """
    if not tagged_anchors:
        _logger.warning("publication anchor validation failed: zero anchors supplied")
        raise ValueError(
            "A publish act requires at least one anchor — either a directive id "
            "that currently binds this scope (its own, or an ancestor's), or a "
            "subject string (ADR 0007 D1, ADR 0015 D3)."
        )
    for anchor in tagged_anchors:
        if anchor.startswith(_DIRECTIVE_ANCHOR_PREFIX):
            directive_id = anchor[len(_DIRECTIVE_ANCHOR_PREFIX) :]
            if directive_id not in valid_directive_ids:
                _logger.warning(
                    "publication anchor validation failed: directive %r does not bind this scope",
                    directive_id,
                )
                raise ValueError(
                    f"Anchor references directive {directive_id!r}, which does not currently "
                    "bind this scope — a publish act can only anchor to a directive the scope "
                    "holds itself or inherits from an ancestor (ADR 0007 D1, ADR 0015 D3)."
                )


def _is_directive_only_anchor_set(anchors: Sequence[str]) -> bool:
    """Return True when every anchor in *anchors* is a ``directive:`` anchor (and there is ≥1)."""
    return bool(anchors) and all(a.startswith(_DIRECTIVE_ANCHOR_PREFIX) for a in anchors)


def _anchor_directive_id(anchor: str) -> str | None:
    if anchor.startswith(_DIRECTIVE_ANCHOR_PREFIX):
        return anchor[len(_DIRECTIVE_ANCHOR_PREFIX) :]
    return None


# ---------------------------------------------------------------------------
# Provenance helpers for non-agent-proposed acts (mechanical propagation,
# bootstrap) — mirrors strata.operator's "operator" provenance constants.
# ---------------------------------------------------------------------------


def _mechanical_proposer(scope_id: str) -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id,
        skill="mechanical-propagation",
        session_id="system",
        ts=datetime.now(tz=UTC).isoformat(),
    )


def _bootstrap_proposer(scope_id: str) -> ContributorRef:
    return ContributorRef(
        scope_id=scope_id,
        skill="scope-manager",
        session_id="bootstrap",
        ts=datetime.now(tz=UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# Judged publish / withdraw (ADR 0007 D2) — agent-proposed acts.
# ---------------------------------------------------------------------------


def propose_publish(
    scope_id: str,
    content: str,
    kind: Literal["directive", "context"],
    subject: str | None,
    anchors: Sequence[str],
    proposer: ContributorRef,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    scope_manager: ScopeManager,
    relay_source_scope_id: str | None = None,
    relay_source_item_id: str | None = None,
    publication_max_words: int | None = None,
) -> PublicationOutcome:
    """Propose publishing *content* from *scope_id*'s own memory, judged by its scope-manager.

    Runs under :func:`strata.locks.scope_lock` for *scope_id*. Order of
    operations (the record never lies):

    1. Structurally validate the (tagged) anchors — :func:`_validate_anchors`
       — BEFORE anything is recorded. A failure here raises and appends
       nothing (mirrors ADR 0006 D1's error-not-decline rule).
    2. Append the ``publish`` act to the record.
    3. Invoke the scope-manager (:meth:`~strata.scope_manager.ScopeManager.judge_publication`).
    4. Record the judgment.
    5. On ``accept``, rewrite the publication artifact to include the new
       item; on ``decline``, the artifact is untouched.

    The act row from step 2 already exists when :meth:`judge_publication` is
    called in step 3, so a failure there is recorded as a ``judge_failed``-
    marked attempt event against that act (mirroring the contribution path's
    judgment-attempt machinery — issue #57 / #118) and the exception then
    propagates AS-IS — the act sits in the record with no judgment, but the
    attempt row makes the failure legible instead of indistinguishable from
    "never judged". No new exception type, and no automatic retry: a
    re-judge pathway for publication acts is a future addition, not built in
    V1.

    Args:
        scope_id: The publishing scope — always the proposer's own bound
            scope in practice (ADR 0007 D2: "there is no publishing upward
            or sideways"); this function itself does not check that,
            structural enforcement belongs to the calling surface (MCP
            ``strata_publish``).
        content: The outward wording, verbatim as judged if accepted.
        kind: ``'directive'`` or ``'context'`` *as it stands in the
            publisher's own memory* — purely informative to readers; every
            published item is non-binding regardless (ADR 0007 D1).
        subject: Optional short label.
        anchors: Raw anchor strings — either a directive id currently in
            this scope's summary, or free-form subject text. Tagged
            internally (:func:`_tag_anchor`) before storage and validation.
        proposer: Provenance of the proposing agent.
        relay_source_scope_id: ADR 0013 D4 (republication) — when this
            publish RELAYS an item *scope_id* received in another scope's
            publication, the scope that item currently lives in. Must be
            given together with *relay_source_item_id*, or not at all. The
            item's origin is DERIVED from the source item itself, never
            taken from the caller: if the source item is already a relay
            (its own ``origin_scope_id`` is set), that ORIGINAL origin
            carries forward unchanged — a caller cannot assert an origin
            that the chain of publish acts does not itself establish, which
            is exactly the provenance-laundering this ADR exists to
            prevent. This function does not check that *scope_id* is
            entitled to read *relay_source_scope_id*'s publication (one-hop
            entitlement is a fleet-topology concern) — same precedent as
            *scope_id* itself: structural enforcement belongs to the
            calling surface.
        relay_source_item_id: The id of the published item, in
            *relay_source_scope_id*'s CURRENT publication, being relayed.
        publication_max_words: ADR 0013 D3 — the word budget for
            *scope_id*'s published face, forwarded to
            :meth:`~strata.scope_manager.ScopeManager.judge_publication`,
            which declines the act mechanically (no API call) when
            publishing *content* would put the face over budget. ``None``
            (the default) resolves to
            :data:`strata.scope_manager.PUBLICATION_MAX_WORDS` — the
            engine default for library callers that do not thread
            :attr:`strata.settings.Settings.publication_max_words` through
            explicitly.

    Returns:
        A :class:`PublicationOutcome`.

    Raises:
        ValueError: *scope_id* is not found in *fleet*; the anchors fail
            structural validation; exactly one of *relay_source_scope_id* /
            *relay_source_item_id* is given; or *relay_source_item_id* is
            not present in *relay_source_scope_id*'s current publication (no
            act row is appended in any of these cases).
    """
    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise ValueError(f"Scope not found: {scope_id!r}")

    if publication_max_words is None:
        # Deferred import: strata.scope_manager imports strata.operator,
        # which imports this module — a module-level import here would be
        # circular (same reason the operator import below is deferred).
        from strata.scope_manager import PUBLICATION_MAX_WORDS

        publication_max_words = PUBLICATION_MAX_WORDS

    if (relay_source_scope_id is None) != (relay_source_item_id is None):
        raise ValueError(
            "relay_source_scope_id and relay_source_item_id must be given together, or not at all."
        )

    # ADR 0013 D4 — republication provenance, derived from the source item
    # itself before anything is recorded (mirrors the anchor structural
    # check below: a failure here appends no act row). origin_scope_id is
    # never taken from the caller: it is the source item's OWN origin when
    # the source item is itself a relay (transitive — the origin never
    # changes hands, however many relays it passes through), or the source
    # scope itself when the source item is an original publish.
    origin_scope_id: str | None = None
    relay_scope_id: str | None = None
    relay_item_id: str | None = None
    if relay_source_scope_id is not None:
        source_items = read_publication(
            relay_source_scope_id, summaries_dir=str(summary_store.summaries_dir)
        )
        source_item = next((i for i in source_items if i.id == relay_source_item_id), None)
        if source_item is None:
            raise ValueError(
                f"Relay source item {relay_source_item_id!r} is not in scope "
                f"{relay_source_scope_id!r}'s current publication."
            )
        origin_scope_id = source_item.origin_scope_id or relay_source_scope_id
        relay_scope_id = relay_source_scope_id
        relay_item_id = source_item.id

    with scope_lock(scope_id):
        current_summary = summary_store.read(scope_id)
        # ADR 0015 D3: an inherited directive binds, so its id anchors too.
        binding_ids = _binding_directive_ids(scope_id, fleet=fleet, summary_store=summary_store)
        tagged_anchors = [_tag_anchor(a, valid_directive_ids=binding_ids) for a in anchors]
        _validate_anchors(tagged_anchors, valid_directive_ids=binding_ids)

        act = record_store.append_publication_act(
            scope_id=scope_id,
            act="publish",
            kind=kind,
            content=content,
            subject=subject,
            anchors=tagged_anchors,
            withdraws=None,
            trigger=None,
            proposer=proposer,
            origin_scope_id=origin_scope_id,
            relay_scope_id=relay_scope_id,
            relay_item_id=relay_item_id,
        )

        current_publication = read_publication(
            scope_id, summaries_dir=str(summary_store.summaries_dir)
        )

        # Judge-aware rendering (ADR 0008 D3): the operator memory binding
        # this scope (attached here or at any inter-stratum ancestor) is
        # rendered to the publication judge as a binding input — a publish
        # act must not be able to contradict the operator directive binding
        # the scope it belongs to, any more than a contribution can (issue
        # #90's asymmetry between the contribution and publication paths).
        # Imported lazily: strata.operator imports from this module, so a
        # module-level import here would be circular.
        from strata.operator import operator_memory_binding

        operator_memory = operator_memory_binding(
            scope_id, fleet=fleet, summaries_dir=str(summary_store.summaries_dir)
        )

        try:
            judgment = scope_manager.judge_publication(
                scope=scope,
                act_kind="publish",
                content=content,
                kind=kind,
                subject=subject,
                anchors=tagged_anchors,
                current_summary=current_summary,
                current_publication=current_publication,
                operator_memory=operator_memory,
                relay_origin_scope_id=origin_scope_id,
                relay_via_scope_id=relay_scope_id,
                publication_max_words=publication_max_words,
            )
        except Exception as exc:
            # Record the failure as an event against the act — never as a
            # fabricated verdict — mirroring strata.app._judge_and_record.
            # Mechanical: no judge or LLM call is made to write the marker.
            record_store.record_publication_judgment_attempt(
                act_id=act.id,
                error_class=type(exc).__name__,
                message=str(exc),
                outcome=JUDGE_FAILED,
            )
            raise

        record_store.record_publication_judgment(
            act_id=act.id,
            decision=judgment.decision,
            judged_by="scope-manager",
            reasoning=judgment.reasoning,
        )

        artifact_updated = False
        if judgment.decision == "accept":
            item = PublishedItem(
                id=act.id,
                kind=kind,
                content=content,
                subject=subject,
                anchors=tagged_anchors,
                published_at=act.created_at,
                origin_scope_id=origin_scope_id,
                relay_scope_id=relay_scope_id,
                relay_item_id=relay_item_id,
            )
            current_publication.append(item)
            _write_publication(
                scope_id, current_publication, summaries_dir=str(summary_store.summaries_dir)
            )
            artifact_updated = True
            # ADR 0014 D1 — this scope's face just gained an item, which is
            # a change to what every reader composes. An addition triggers
            # exactly as a removal does: a child that never re-judges after
            # its parent added something is as wrong as one that never
            # re-judges after a withdrawal. A relayed publish is no
            # exception — the relay is THIS scope's own act, so it mints a
            # fresh change id rather than inheriting the origin's.
            emit_change_event(
                fleet=fleet,
                record_store=record_store,
                item=item.id,
                kind="published",
                source_scope_id=scope_id,
                after=content,
            )

        return PublicationOutcome(
            act_id=act.id,
            act="publish",
            decision=judgment.decision,
            reasoning=judgment.reasoning,
            artifact_updated=artifact_updated,
        )


def propose_withdraw(
    scope_id: str,
    item_id: str,
    proposer: ContributorRef,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    scope_manager: ScopeManager,
) -> PublicationOutcome:
    """Propose withdrawing published item *item_id* from *scope_id*'s publication.

    Same order of operations as :func:`propose_publish`: the act is appended
    to the record BEFORE the scope-manager is invoked, the judgment is
    recorded, and — on ``accept`` — the artifact is rewritten with the item
    removed.

    Args:
        scope_id: The publishing scope.
        item_id: The ``pub_``-prefixed id of the published item to withdraw.
            Must be present in *scope_id*'s CURRENT publication.
        proposer: Provenance of the proposing agent.

    Returns:
        A :class:`PublicationOutcome`.

    Raises:
        ValueError: *scope_id* is not found in *fleet*.
        KeyError: *item_id* is not in *scope_id*'s current publication — a
            structural check; no act row is appended.
    """
    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise ValueError(f"Scope not found: {scope_id!r}")

    with scope_lock(scope_id):
        current_summary = summary_store.read(scope_id)
        current_publication = read_publication(
            scope_id, summaries_dir=str(summary_store.summaries_dir)
        )
        item = next((i for i in current_publication if i.id == item_id), None)
        if item is None:
            raise KeyError(
                f"Published item {item_id!r} not found in scope {scope_id!r}'s current publication."
            )

        act = record_store.append_publication_act(
            scope_id=scope_id,
            act="withdraw",
            kind=None,
            content=None,
            subject=None,
            anchors=None,
            withdraws=item_id,
            trigger=None,
            proposer=proposer,
        )

        # Judge-aware rendering (ADR 0008 D3) — see propose_publish above.
        from strata.operator import operator_memory_binding

        operator_memory = operator_memory_binding(
            scope_id, fleet=fleet, summaries_dir=str(summary_store.summaries_dir)
        )

        try:
            judgment = scope_manager.judge_publication(
                scope=scope,
                act_kind="withdraw",
                withdraw_item=item,
                current_summary=current_summary,
                current_publication=current_publication,
                operator_memory=operator_memory,
            )
        except Exception as exc:
            # Record the failure as an event against the act — never as a
            # fabricated verdict — mirroring strata.app._judge_and_record.
            # Mechanical: no judge or LLM call is made to write the marker.
            record_store.record_publication_judgment_attempt(
                act_id=act.id,
                error_class=type(exc).__name__,
                message=str(exc),
                outcome=JUDGE_FAILED,
            )
            raise

        record_store.record_publication_judgment(
            act_id=act.id,
            decision=judgment.decision,
            judged_by="scope-manager",
            reasoning=judgment.reasoning,
        )

        artifact_updated = False
        if judgment.decision == "accept":
            remaining = [i for i in current_publication if i.id != item_id]
            _write_publication(scope_id, remaining, summaries_dir=str(summary_store.summaries_dir))
            artifact_updated = True
            # ADR 0014 D1/D4 — the independent input change starts here, so
            # this is where its change id is minted; the cascade below is
            # DERIVED from it and inherits it, which is what stops a
            # reference cycle from running forever (D4).
            change_ids = emit_change_event(
                fleet=fleet,
                record_store=record_store,
                item=item_id,
                kind="withdrawn",
                source_scope_id=scope_id,
                before=item.content,
            )
            # ADR 0013 D4b — mechanical, no LLM: every downstream copy
            # relayed from this item goes with it.
            _cascade_withdraw_relays(
                scope_id,
                item_id,
                fleet=fleet,
                record_store=record_store,
                summaries_dir=str(summary_store.summaries_dir),
                held_scope_id=scope_id,
                change_ids=change_ids,
            )

        return PublicationOutcome(
            act_id=act.id,
            act="withdraw",
            decision=judgment.decision,
            reasoning=judgment.reasoning,
            artifact_updated=artifact_updated,
        )


# ---------------------------------------------------------------------------
# Withdrawal cascade to relayed copies (ADR 0013 D4b) — a fourth mechanical
# choke point of the class ADR 0007 D3 established: a relay is anchored to
# its origin exactly as a published item is anchored to the directive it
# came from, and this is the one-edge-further-out counterpart of
# propagate_directive_removals. No LLM in the loop, whatever caused the
# upstream withdrawal (an agent's own propose_withdraw, mechanical
# propagation, or judged propagation) — a relay believes an item only
# because its origin does, so every path that removes an item from a
# publication must sweep its relayed copies the same way.
# ---------------------------------------------------------------------------


def _cascade_withdraw_relays(
    scope_id: str,
    item_id: str,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summaries_dir: str,
    held_scope_id: str,
    change_ids: Sequence[str],
    hop: int = 0,
    notice_kind: str = "withdrawn",
    correcting_after: str | None = None,
    correcting_claim_id: str | None = None,
) -> None:
    """Mechanically withdraw every published item relayed from ``(scope_id, item_id)``.

    Recurses: a copy withdrawn here may itself have been relayed onward
    again — including, since reference edges may form cycles (CONTEXT.md §
    Reference edge), back through a scope this cascade has already visited
    for a DIFFERENT item — and D4b's cascade can therefore change several
    strata in one act. Each withdraw act's ``trigger`` names the id of the
    item ONE hop upstream that caused it (chained, so every hop stays
    locally traceable from the record alone) and gets NO judgment row —
    mechanical, exactly like :func:`propagate_directive_removals`'s
    withdrawals. Termination does not depend on tracking visited scopes:
    every relay item id is a fresh publish act created strictly after the
    item it relays, so the chain of ``relay_item_id`` pointers this
    function follows can never loop back to an id it has already withdrawn.

    *change_ids* are the input changes this cascade is a consequence of,
    minted or inherited by whichever entry point started it — plural because
    a coalesced refresh belongs to every wave it drained (ADR 0014 D4, Phase
    A finding 2). Every relayed withdrawal emitted here INHERITS them all
    rather than minting a fresh id: with fresh ids the visited set would
    bound nothing and a reference cycle would run forever. *hop* counts
    derived hops for D4's backstop budget and grows by one per recursion.

    *held_scope_id* is the ONE scope whose :func:`strata.locks.scope_lock`
    is already held by the outermost caller of this cascade (every entry
    point calls this from inside its own ``with scope_lock(scope_id):``
    block) — the only scope this function must NOT try to re-lock (the lock
    is not reentrant). A downstream scope's lock, once taken below, is
    always released before recursing, so at most one OTHER scope's lock is
    ever held at a time; *held_scope_id* is therefore the only deadlock risk,
    and a relayed copy that loops back into it is withdrawn in place, under
    the lock the caller already holds, rather than skipped.

    *notice_kind*/*correcting_after*/*correcting_claim_id* (ADR 0017 v1.16 #221):
    when this cascade is propagating a CORRECTION (called from
    :func:`propagate_claim_correction`) rather than an ordinary withdrawal, every
    hop of the cascade — not only the first — must carry ``claim_corrected`` and
    the correcting content, or a relay two or more hops downstream would silently
    read as a bare "withdrawn" (a depth-2 relay's own reader losing the correction
    entirely). Threaded through the recursion unchanged; ``"withdrawn"``/``None``
    (the defaults) reproduce the exact original behaviour.
    """
    proposer = _mechanical_proposer(scope_id)

    def _remove_matches(other_scope_id: str) -> list[PublishedItem]:
        current = read_publication(other_scope_id, summaries_dir=summaries_dir)
        to_remove = [
            i for i in current if i.relay_scope_id == scope_id and i.relay_item_id == item_id
        ]
        if not to_remove:
            return []
        removed_ids = {i.id for i in to_remove}
        remaining = [i for i in current if i.id not in removed_ids]
        for item in to_remove:
            record_store.append_publication_act(
                scope_id=other_scope_id,
                act="withdraw",
                kind=None,
                content=None,
                subject=None,
                anchors=None,
                withdraws=item.id,
                trigger=item_id,
                proposer=proposer,
            )
            _logger.info(
                "cascaded mechanical withdrawal of relayed item %s from scope %s "
                "(relayed from %s/%s)",
                item.id,
                other_scope_id,
                scope_id,
                item_id,
            )
        _write_publication(other_scope_id, remaining, summaries_dir=summaries_dir)
        return to_remove

    for other_scope_id in list_scopes_with_publications(summaries_dir):
        candidates = read_publication(other_scope_id, summaries_dir=summaries_dir)
        matches = [
            i for i in candidates if i.relay_scope_id == scope_id and i.relay_item_id == item_id
        ]
        if not matches:
            continue

        if other_scope_id == held_scope_id:
            # Already locked by the outer caller — mutate in place rather
            # than re-acquiring (would deadlock) or skipping (would leave a
            # relayed copy asserting what its origin has retracted).
            removed = _remove_matches(other_scope_id)
        else:
            with scope_lock(other_scope_id):
                # Re-read under the lock — another writer may have changed
                # this scope's publication between the unlocked scan above
                # and here.
                removed = _remove_matches(other_scope_id)

        for item in removed:
            # ADR 0014 D4 — one derived event per relayed withdrawal, from
            # the scope that lost the copy, carrying every wave id so a
            # scope already refreshed for all of them is not woken twice.
            emit_change_event(
                fleet=fleet,
                record_store=record_store,
                item=item.id,
                kind=notice_kind,
                source_scope_id=other_scope_id,
                before=item.content,
                after=correcting_after if notice_kind == "claim_corrected" else None,
                wave_ids=change_ids,
                hop=hop + 1,
                claim_id=correcting_claim_id if notice_kind == "claim_corrected" else None,
            )
            _cascade_withdraw_relays(
                other_scope_id,
                item.id,
                fleet=fleet,
                record_store=record_store,
                summaries_dir=summaries_dir,
                held_scope_id=held_scope_id,
                change_ids=change_ids,
                hop=hop + 1,
                notice_kind=notice_kind,
                correcting_after=correcting_after,
                correcting_claim_id=correcting_claim_id,
            )


# ---------------------------------------------------------------------------
# The restore act (companion to #219 C) — undoes a published item wrongly
# withdrawn by a correction sweep (verbatim P4, #219 C `carries`, or either
# one's relay cascade), under its original id and bytes, telling exactly the
# readers who got the false notice that it was false.
# ---------------------------------------------------------------------------


def restorable_withdrawal(
    scope_id: str, item_id: str, *, record_store: RecordStore
) -> tuple[str, list[str]] | None:
    """Is *item_id*, as it stood in *scope_id*, restorable — i.e. withdrawn by a
    correction sweep (verbatim P4 or #219 C ``carries``, at any hop of either
    one's relay cascade)?

    Detected mechanically from what #221/#219 C already write: a
    ``claim_corrected`` SELF-notice (issue #197) always lands on the scope
    that just lost the item, at every hop of the cascade — never for a
    deliberate :func:`propose_withdraw` or a directive-removal propagation,
    neither of which ever emits ``claim_corrected``. No new bookkeeping
    needed to tell restorable from not.

    Returns ``(claim_id, change_ids)`` — the corrected claim's own id, and
    every distinct change id this item's correction was recorded under (a
    coalesced refresh may have inherited several, ADR 0014 D4) — or ``None``
    if *item_id* was never withdrawn by a correction at all.
    """
    events = [
        e
        for e in record_store.list_change_events(scope_id=scope_id)
        if e.self_notice and e.kind == CLAIM_CORRECTED and e.item_id == item_id
    ]
    if not events:
        return None
    claim_id = events[0].claim_id or ""
    change_ids = list(dict.fromkeys(e.change_id for e in events))
    return claim_id, change_ids


def _find_withdraw_act(
    scope_id: str, item_id: str, *, record_store: RecordStore
) -> PublicationAct | None:
    """The withdraw act that removed *item_id* from *scope_id*'s publication, if any."""
    for act in record_store.list_publication_acts(scope_id=scope_id):
        if act.act == "withdraw" and act.withdraws == item_id:
            return act
    return None


def _resolve_claim_correction(
    claim_id: str, change_ids: Sequence[str], *, record_store: RecordStore
) -> ClaimCorrection:
    for change_id in change_ids:
        correction = record_store.get_claim_correction(claim_id=claim_id, change_id=change_id)
        if correction is not None:
            return correction
    raise KeyError(
        f"No recorded claim correction found for claim {claim_id!r} under any of "
        f"{change_ids!r} — restore act design point 2 should have written one at "
        "correction time."
    )


def _published_item_from_act(act: PublicationAct) -> PublishedItem:
    """Rebuild a :class:`PublishedItem` from the ORIGINAL ``publish`` act it came
    from — this is what makes a restore's reinsertion byte-identical: the same
    stored content, never a copy that could drift."""
    return PublishedItem(
        id=act.id,
        kind=act.kind,  # type: ignore[arg-type]
        content=act.content or "",
        subject=act.subject,
        anchors=act.anchors or [],
        published_at=act.created_at,
        origin_scope_id=act.origin_scope_id,
        relay_scope_id=act.relay_scope_id,
        relay_item_id=act.relay_item_id,
    )


def _relay_rejudged_since_withdrawal(
    relay_scope_id: str,
    origin_item_id: str,
    change_ids: Sequence[str],
    *,
    record_store: RecordStore,
) -> bool:
    """Has *relay_scope_id*'s own judge PROCESSED (drained) the ``claim_corrected``
    notice for *origin_item_id* under any of *change_ids* (contract line 4)?

    A relaying scope is, by ADR 0013 D3 construction, a one-hop topological
    reader of the scope it relayed from — the only way its judge could have
    seen *origin_item_id* to relay it in the first place — so it receives an
    ORDINARY (non-self-notice) ``claim_corrected`` event for *origin_item_id*
    exactly as any other reader does, stamped ``processed_at`` only once its
    own refresh actually drains it. That is the signal: not the self-notice
    of the relay copy's OWN withdrawal, which is born processed at birth
    (issue #197) and says nothing about whether this scope's judge acted.
    """
    for event in record_store.list_change_events(scope_id=relay_scope_id):
        if (
            not event.self_notice
            and event.kind == CLAIM_CORRECTED
            and event.item_id == origin_item_id
            and event.change_id in change_ids
            and event.processed_at is not None
        ):
            return True
    return False


def _restore_item_in_scope(
    scope_id: str,
    item: PublishedItem,
    *,
    withdraw_act_id: str,
    fleet: FleetConfig,
    record_store: RecordStore,
    summaries_dir: str,
    held_scope_id: str,
) -> None:
    """Re-insert *item* into *scope_id*'s live publication, record the ``restore``
    act, and mark the withdraw act it reverses. Runs under *scope_id*'s own
    lock unless it is *held_scope_id* (already locked by the outer caller —
    mirrors :func:`_cascade_withdraw_relays`'s own re-entrancy rule)."""

    def _do() -> None:
        current = read_publication(scope_id, summaries_dir=summaries_dir)
        if any(i.id == item.id for i in current):
            return  # already present — a retried/duplicate cascade step, not an error
        current.append(item)
        _write_publication(scope_id, current, summaries_dir=summaries_dir)
        restore_act = record_store.append_publication_act(
            scope_id=scope_id,
            act="restore",
            kind=None,
            content=None,
            subject=None,
            anchors=None,
            withdraws=None,
            trigger=withdraw_act_id,
            proposer=_mechanical_proposer(scope_id),
            restores=item.id,
        )
        record_store.mark_withdraw_restored(
            withdraw_act_id=withdraw_act_id, restore_act_id=restore_act.id
        )

    if scope_id == held_scope_id:
        _do()
    else:
        with scope_lock(scope_id):
            _do()


def _restore_relay_cascade(
    item_id: str,
    *,
    claim_id: str,
    change_ids: Sequence[str],
    fleet: FleetConfig,
    record_store: RecordStore,
    summaries_dir: str,
    held_scope_id: str,
    notified_items: set[str],
) -> None:
    """Mirror of :func:`_cascade_withdraw_relays`, driven by the RECORD rather
    than today's live publications (the relay copies are gone) — every
    ``withdraw`` act anywhere whose own ``trigger`` is *item_id* is one relay
    hop downstream of it.

    Each hop restores mechanically unless
    :func:`_relay_rejudged_since_withdrawal` says that relaying scope's
    standing changed since (contract line 4) — in which case this branch
    stops: no mechanical restore, no further descent (there is nothing to
    cascade FROM if the relay was never recreated). Every item id this walk
    reaches — restored or not — is queued in *notified_items* for a single
    ``claim_restored`` emission per id at the end (point 8 handles the
    "evidence either way" case uniformly: a scope that stopped the cascade
    still gets the notice, exactly like a scope that only ever read the
    correction and never itself relayed).
    """
    for withdraw_act in record_store.find_publication_acts_by_trigger(item_id):
        if withdraw_act.act != "withdraw" or withdraw_act.withdraws is None:
            continue
        relay_scope_id = withdraw_act.scope_id
        relay_item_id = withdraw_act.withdraws
        notified_items.add(relay_item_id)
        if _relay_rejudged_since_withdrawal(
            relay_scope_id, item_id, change_ids, record_store=record_store
        ):
            continue
        original_relay_act = record_store.get_publication_act(relay_item_id)
        if original_relay_act is None:
            continue
        _restore_item_in_scope(
            relay_scope_id,
            _published_item_from_act(original_relay_act),
            withdraw_act_id=withdraw_act.id,
            fleet=fleet,
            record_store=record_store,
            summaries_dir=summaries_dir,
            held_scope_id=held_scope_id,
        )
        _restore_relay_cascade(
            relay_item_id,
            claim_id=claim_id,
            change_ids=change_ids,
            fleet=fleet,
            record_store=record_store,
            summaries_dir=summaries_dir,
            held_scope_id=held_scope_id,
            notified_items=notified_items,
        )


def _render_restore_notice(item_id: str, reversed_change_id: str, content: str) -> str:
    """Contract line 2: the owner's act, naming the earlier notice as wrong —
    never the engine's confession, since the ``carries`` call that withdrew
    it (if #219 C) was the owner's own judge's."""
    return (
        f"[Restore — item {item_id} is restored by its owner.]\n"
        f"- the earlier notice ({reversed_change_id}) saying this item carried the "
        "refuted claim was wrong.\n"
        "- the correction of the refuted claim itself is unchanged and still stands.\n"
        f"- restored content:\n    {content}\n"
    )


def _apply_restore(
    origin_item_id: str,
    origin_withdraw_act: PublicationAct,
    restore_act_id: str,
    original_item: PublishedItem,
    *,
    claim_id: str,
    change_ids: Sequence[str],
    fleet: FleetConfig,
    record_store: RecordStore,
    summaries_dir: str,
    held_scope_id: str,
) -> None:
    """Everything an ACCEPTED restore (owner or operator path) does, after the
    (optional) judgment is already recorded: reinsert the origin item,
    cascade to its relays per contract line 4, then notify (contract lines
    2/3/6) every item id the walk touched, mechanically restored or not.
    """
    current = read_publication(held_scope_id, summaries_dir=summaries_dir)
    if not any(i.id == original_item.id for i in current):
        current.append(original_item)
        _write_publication(held_scope_id, current, summaries_dir=summaries_dir)
    record_store.mark_withdraw_restored(
        withdraw_act_id=origin_withdraw_act.id, restore_act_id=restore_act_id
    )

    notified_items: set[str] = {origin_item_id}
    _restore_relay_cascade(
        origin_item_id,
        claim_id=claim_id,
        change_ids=change_ids,
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
        held_scope_id=held_scope_id,
        notified_items=notified_items,
    )

    restore_change_id = new_change_id()
    content = _render_restore_notice(origin_item_id, change_ids[0], original_item.content)
    for item_id in notified_items:
        emit_restore_notice(
            record_store=record_store,
            item_id=item_id,
            original_change_ids=change_ids,
            restore_change_id=restore_change_id,
            claim_id=claim_id,
            content=content,
        )


def propose_restore(
    scope_id: str,
    item_id: str,
    reason: str | None,
    proposer: ContributorRef,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    scope_manager: ScopeManager,
) -> PublicationOutcome:
    """Propose restoring a correction-withdrawn published item (owner path, judged).

    Same order-of-operations shape as :func:`propose_withdraw`: the act is
    appended to the record BEFORE the scope-manager is invoked. Judged
    through :meth:`~strata.scope_manager.ScopeManager.judge_publication`
    (``act_kind='restore'``) — the structural test only (contract line 1):
    still believed by *scope_id*'s CURRENT memory, and does not re-assert the
    refuted claim. A decline leaves the item withdrawn; the operator path is
    the escape (design point 2).

    Raises:
        ValueError: *scope_id* is not found in *fleet*; *item_id* was not
            withdrawn by a correction sweep (restorable_withdrawal is
            ``None`` — a deliberate withdrawal is re-published, not restored
            this way); or it was already restored.
        KeyError: no withdraw act is on record for *item_id*, or no
            :class:`~strata.record_store.ClaimCorrection` row exists for its
            claim (should always exist by design point 2 — a missing row is
            a bug upstream, not a user error).
    """
    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise ValueError(f"Scope not found: {scope_id!r}")

    with scope_lock(scope_id):
        withdraw_act = _find_withdraw_act(scope_id, item_id, record_store=record_store)
        if withdraw_act is None:
            raise KeyError(f"No withdraw act found for item {item_id!r} in scope {scope_id!r}.")
        if withdraw_act.restored_by is not None:
            raise ValueError(f"Item {item_id!r} has already been restored.")
        restorable = restorable_withdrawal(scope_id, item_id, record_store=record_store)
        if restorable is None:
            raise ValueError(
                f"Item {item_id!r} was not withdrawn by a correction sweep — only a "
                "sweep withdrawal (verbatim or #219 C) can be restored this way. A "
                "deliberate withdrawal is re-published instead."
            )
        claim_id, change_ids = restorable
        correction = _resolve_claim_correction(claim_id, change_ids, record_store=record_store)
        original_act = record_store.get_publication_act(withdraw_act.withdraws)  # type: ignore[arg-type]
        if original_act is None:
            raise KeyError(f"Original publish act {withdraw_act.withdraws!r} not found.")
        restore_item = _published_item_from_act(original_act)

        current_summary = summary_store.read(scope_id)
        current_publication = read_publication(
            scope_id, summaries_dir=str(summary_store.summaries_dir)
        )

        act = record_store.append_publication_act(
            scope_id=scope_id,
            act="restore",
            kind=None,
            content=None,
            subject=None,
            anchors=None,
            withdraws=None,
            trigger=withdraw_act.id,
            proposer=proposer,
            restores=item_id,
        )

        from strata.operator import operator_memory_binding

        operator_memory = operator_memory_binding(
            scope_id, fleet=fleet, summaries_dir=str(summary_store.summaries_dir)
        )

        try:
            judgment = scope_manager.judge_publication(
                scope=scope,
                act_kind="restore",
                current_summary=current_summary,
                current_publication=current_publication,
                restore_item=restore_item,
                corrected_claim_content=correction.corrected_claim_content,
                correcting_content=correction.correcting_content,
                operator_memory=operator_memory,
            )
        except Exception as exc:
            record_store.record_publication_judgment_attempt(
                act_id=act.id,
                error_class=type(exc).__name__,
                message=str(exc),
                outcome=JUDGE_FAILED,
            )
            raise

        record_store.record_publication_judgment(
            act_id=act.id,
            decision=judgment.decision,
            judged_by="scope-manager",
            reasoning=judgment.reasoning,
        )

        artifact_updated = False
        if judgment.decision == "accept":
            _apply_restore(
                item_id,
                withdraw_act,
                act.id,
                restore_item,
                claim_id=claim_id,
                change_ids=change_ids,
                fleet=fleet,
                record_store=record_store,
                summaries_dir=str(summary_store.summaries_dir),
                held_scope_id=scope_id,
            )
            artifact_updated = True

        return PublicationOutcome(
            act_id=act.id,
            act="restore",
            decision=judgment.decision,
            reasoning=judgment.reasoning,
            artifact_updated=artifact_updated,
        )


def operator_restore(
    scope_id: str,
    item_id: str,
    reason: str | None,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summaries_dir: str,
) -> PublicationOutcome:
    """Restore a correction-withdrawn published item in person (operator path, ADR
    0008 D4) — no judgment row, operator provenance, the Console's Restore
    button.

    Same restorability check as :func:`propose_restore`; unlike that path,
    accepts unconditionally (the operator's own review IS the ground —
    design point 2's "the operator path is the escape" when the owner's
    judge declines).

    Unjudged: the operator decides — the Console shows the refuted claim
    beside the item (and the correcting content) precisely so that decision
    is informed. By design, this WILL restore a genuine carrier of the
    refuted claim if the operator chooses to (ADR 0008's in-person
    authority) — there is no mechanical refusal here; the structural
    "still believed, doesn't carry" test is the OWNER path's own judge, not
    this one.

    Raises:
        ValueError: *scope_id* is not found in *fleet*, *item_id* was not
            withdrawn by a correction sweep, or it was already restored.
        KeyError: no withdraw act is on record for *item_id*, or no
            :class:`~strata.record_store.ClaimCorrection` row exists for its
            claim.
    """
    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise ValueError(f"Scope not found: {scope_id!r}")

    with scope_lock(scope_id):
        withdraw_act = _find_withdraw_act(scope_id, item_id, record_store=record_store)
        if withdraw_act is None:
            raise KeyError(f"No withdraw act found for item {item_id!r} in scope {scope_id!r}.")
        if withdraw_act.restored_by is not None:
            raise ValueError(f"Item {item_id!r} has already been restored.")
        restorable = restorable_withdrawal(scope_id, item_id, record_store=record_store)
        if restorable is None:
            raise ValueError(
                f"Item {item_id!r} was not withdrawn by a correction sweep — only a "
                "sweep withdrawal (verbatim or #219 C) can be restored this way."
            )
        claim_id, change_ids = restorable
        original_act = record_store.get_publication_act(withdraw_act.withdraws)  # type: ignore[arg-type]
        if original_act is None:
            raise KeyError(f"Original publish act {withdraw_act.withdraws!r} not found.")
        restore_item = _published_item_from_act(original_act)

        proposer = ContributorRef(
            scope_id="operator",
            skill="operator",
            session_id="operator",
            ts=datetime.now(tz=UTC).isoformat(),
        )
        act = record_store.append_publication_act(
            scope_id=scope_id,
            act="restore",
            kind=None,
            content=None,
            subject=None,
            anchors=None,
            withdraws=None,
            trigger=withdraw_act.id,
            proposer=proposer,
            restores=item_id,
        )
        _apply_restore(
            item_id,
            withdraw_act,
            act.id,
            restore_item,
            claim_id=claim_id,
            change_ids=change_ids,
            fleet=fleet,
            record_store=record_store,
            summaries_dir=summaries_dir,
            held_scope_id=scope_id,
        )
        return PublicationOutcome(
            act_id=act.id,
            act="restore",
            decision="accept",
            reasoning=reason or "operator restore",
            artifact_updated=True,
        )


@dataclass(frozen=True)
class CorrectionWithdrawalRow:
    """One row of the "Correction withdrawals" detection surface (Console view
    / ``strata record --swept`` / the backend endpoint) — a withdrawal made by
    a correction sweep, with everything the row needs to show side by side."""

    act: PublicationAct
    """The withdraw act itself — ``act.withdraws`` is the withdrawn item's id,
    ``act.acknowledged``/``act.restored_by`` its review state."""
    correction: ClaimCorrection | None
    """The refuted claim's text and the correcting text, when on record (should
    always be, by design — see :class:`ClaimCorrection`'s own docstring)."""
    carrier_check: ClaimCarrierCheck | None
    """#219 C's own audit row, when the method was a judged ``carries``
    decision — ``None`` for a verbatim match or a relay-cascade hop, neither
    of which gets one."""
    reader_count: int
    """Distinct reader scopes notified of this item's withdrawal (every
    ``claim_corrected`` event for this item under its own change ids,
    excluding the owning scope's self-notice)."""

    @property
    def method(self) -> str:
        if self.carrier_check is not None:
            return f"judge {self.carrier_check.outcome}"
        if (self.act.trigger or "").startswith("pub_"):
            return "relay cascade"
        return "verbatim"


def list_correction_withdrawals(
    scope_id: str, *, record_store: RecordStore, include_acknowledged: bool = False
) -> list[CorrectionWithdrawalRow]:
    """The Console's "Correction withdrawals" view / ``strata record --swept``:
    every withdraw act in *scope_id* caused by a correction sweep, newest
    first, each with its claim/correcting text (when found) and #219 C's own
    audit row (when the method was a judged ``carries`` decision rather than
    a verbatim match — absent otherwise).

    *include_acknowledged*: by default, a withdrawal already marked
    "keep withdrawn" OR already restored is left out — the "to review"
    filter design point 10 describes (nothing left to review once either
    has happened). ``True`` returns every restorable withdrawal regardless.
    """
    acts = record_store.list_publication_acts(scope_id=scope_id)
    carrier_checks = {
        c.item_id: c for c in record_store.list_claim_carrier_checks(scope_id=scope_id)
    }
    out: list[CorrectionWithdrawalRow] = []
    for act in acts:
        if act.act != "withdraw":
            continue
        if (act.acknowledged or act.restored_by is not None) and not include_acknowledged:
            continue
        restorable = restorable_withdrawal(scope_id, act.withdraws or "", record_store=record_store)
        if restorable is None:
            continue
        claim_id, change_ids = restorable
        try:
            correction = _resolve_claim_correction(claim_id, change_ids, record_store=record_store)
        except KeyError:
            correction = None
        reader_scopes: set[str] = set()
        for change_id in change_ids:
            for event in record_store.list_change_events_by_change_id(
                change_id=change_id, item_id=act.withdraws, kind=CLAIM_CORRECTED
            ):
                reader_scopes.add(event.scope_id)
        reader_scopes.discard(scope_id)
        out.append(
            CorrectionWithdrawalRow(
                act=act,
                correction=correction,
                carrier_check=carrier_checks.get(act.withdraws or ""),
                reader_count=len(reader_scopes),
            )
        )
    out.sort(key=lambda row: row.act.created_at, reverse=True)
    return out


def list_unresolved_carrier_checks(
    scope_id: str, *, record_store: RecordStore
) -> list[ClaimCarrierCheck]:
    """#219 C's own unresolved/overflow rows for *scope_id*, newest first — flagged
    in the same "Correction withdrawals" view (detection surface design point 1),
    since by definition nothing was withdrawn for these: the item was never
    classified (overflow) or the judge's decision could not be read (unreadable),
    so there is no withdraw act to find them through."""
    rows = [
        c
        for c in record_store.list_claim_carrier_checks(scope_id=scope_id)
        if c.outcome in ("unresolved_overflow", "unresolved_unreadable")
    ]
    rows.sort(key=lambda c: c.created_at, reverse=True)
    return rows


def acknowledge_correction_withdrawal(
    scope_id: str, item_id: str, *, record_store: RecordStore
) -> PublicationAct:
    """The Console's "keep withdrawn" action: mark the withdraw act for *item_id*
    in *scope_id* acknowledged, hiding it from :func:`list_correction_withdrawals`'s
    default "to review" filter without restoring it.

    Raises:
        KeyError: no withdraw act is on record for *item_id* in *scope_id*.
    """
    withdraw_act = _find_withdraw_act(scope_id, item_id, record_store=record_store)
    if withdraw_act is None:
        raise KeyError(f"No withdraw act found for item {item_id!r} in scope {scope_id!r}.")
    record_store.acknowledge_withdraw(withdraw_act.id)
    updated = record_store.get_publication_act(withdraw_act.id)
    assert updated is not None  # noqa: S101 — just wrote it, must exist
    return updated


# ---------------------------------------------------------------------------
# Staleness propagation (ADR 0007 D3) — two paths, by anchor type.
# ---------------------------------------------------------------------------


def propagate_directive_removals(
    scope_id: str,
    removed_directive_ids: Collection[str],
    trigger_id: str,
    *,
    surviving_directive_ids: Collection[str],
    fleet: FleetConfig,
    record_store: RecordStore,
    summaries_dir: str,
    change_ids: Sequence[str] = (),
    hop: int = 0,
) -> list[PublishedItem]:
    """Mechanically withdraw published items none of whose anchors still stand.

    ADR 0007 D3's mechanical path: no LLM in the loop. A directive-only-
    anchored item is withdrawn only when BOTH hold:

    (a) at least one of its anchors is in *removed_directive_ids* — the ids
        THIS call removed; and
    (b) none of its anchors still stands — it has no ``subject:`` anchor, and
        no ``directive:`` anchor whose id is in *surviving_directive_ids*
        (the ids present in the scope's summary AFTER this event's write).

    (b) is the ADR's rule that an item lives while ANY anchor lives, checked
    against the summary's CURRENT state rather than against
    *removed_directive_ids* alone: a multi-anchored item loses its anchors
    across SEPARATE supersession/retirement events, and "withdraws any
    published item whose anchors all vanished" must fire on the event that
    removes the LAST one, regardless of when the others went.

    (a) is what makes the withdrawal attributable to *trigger_id*. ONE
    amendment can remove directives on behalf of several contributions (a
    batch — ADR 0011 D3), and every per-trigger call then sees the same
    post-write summary, so (b) alone cannot tell those calls apart: the first
    would sweep every item the whole amendment un-anchored and stamp them all
    with the first trigger. Under (a) an item is swept by the call for the
    trigger that removed one of ITS anchors, so each withdraw act names the
    member that actually motivated it (ADR 0011 D3's attribution obligation).

    The caller MUST already hold ``strata.locks.scope_lock(scope_id)`` — this
    is called from the three choke points that remove a directive from a
    scope's summary (:func:`strata.app._judge_and_record`,
    :func:`strata.operator.operator_supersede`,
    :func:`strata.operator.operator_retire`), all already inside that lock,
    after the new summary is written.

    Each withdrawal appends a ``withdraw`` act with ``trigger=trigger_id``
    and NO judgment row (a mechanical consequence of an already-judged
    event, not a fresh judgment on the publication itself — see the 0005
    migration's header comment).

    Args:
        scope_id: The publishing scope whose directives changed.
        removed_directive_ids: Directive ids that just left the scope's
            summary (superseded or retired) on *trigger_id*'s behalf —
            condition (a). In a batch this is one member's ops, not the
            whole amendment's.
        trigger_id: The record id of the triggering event (a contribution id
            or an operator retirement id) — carried on each withdraw act.
        surviving_directive_ids: Directive ids present in the scope's summary
            after this event's write — the set anchor survival is checked
            against.
        fleet: Read to compute the affected set of each withdrawal this call
            makes (ADR 0014 D3). Required rather than optional: a withdrawal
            nobody downstream is told about is exactly the evidence-blindness
            ADR 0014 exists to end.
        change_ids: The input changes this propagation is a consequence of,
            when it has any (ADR 0014 D4 — a judgment's own ``wave_ids`` on
            a refresh, plural because a refresh coalesces). Empty makes each
            emitted event an independent change, which is right for an
            ordinary contribution's ops.
        hop: How far along the wave the judgment that ordered this sat, so
            D4's backstop budget keeps counting across the refresh instead
            of restarting at zero.

    Returns:
        The published items that were withdrawn (empty if none qualified).
    """
    if not removed_directive_ids:
        return []

    current_publication = read_publication(scope_id, summaries_dir=summaries_dir)
    if not current_publication:
        return []

    removed = set(removed_directive_ids)
    surviving = set(surviving_directive_ids)
    to_withdraw: list[PublishedItem] = []
    for item in current_publication:
        if not _is_directive_only_anchor_set(item.anchors):
            continue
        directive_ids = {_anchor_directive_id(a) for a in item.anchors}
        if (directive_ids & removed) and not (directive_ids & surviving):
            to_withdraw.append(item)

    if not to_withdraw:
        return []

    proposer = _mechanical_proposer(scope_id)
    for item in to_withdraw:
        record_store.append_publication_act(
            scope_id=scope_id,
            act="withdraw",
            kind=None,
            content=None,
            subject=None,
            anchors=None,
            withdraws=item.id,
            trigger=trigger_id,
            proposer=proposer,
        )
        _logger.info(
            "mechanically withdrew published item %s from scope %s (trigger=%s)",
            item.id,
            scope_id,
            trigger_id,
        )

    withdrawn_ids = {item.id for item in to_withdraw}
    remaining = [item for item in current_publication if item.id not in withdrawn_ids]
    _write_publication(scope_id, remaining, summaries_dir=summaries_dir)

    # ADR 0013 D4b — mechanical, no LLM: sweep every downstream copy relayed
    # from each item this call just withdrew. ADR 0014: each withdrawal is
    # also a change to this scope's face, so each is announced to its readers
    # and the cascade below inherits that announcement's id.
    for item in to_withdraw:
        item_change_ids = emit_change_event(
            fleet=fleet,
            record_store=record_store,
            item=item.id,
            kind="withdrawn",
            source_scope_id=scope_id,
            before=item.content,
            wave_ids=change_ids,
            hop=hop,
        )
        _cascade_withdraw_relays(
            scope_id,
            item.id,
            fleet=fleet,
            record_store=record_store,
            summaries_dir=summaries_dir,
            held_scope_id=scope_id,
            change_ids=item_change_ids,
            hop=hop,
        )

    return to_withdraw


def _carries_claim(published_content: str, claim_content: str) -> bool:
    """Does *published_content* still assert *claim_content* VERBATIM (ADR 0017 P4)?

    Mirrors :func:`strata.perspective._context_contributions_absent`'s exact
    normalisation and substring test (also duplicated, for the same reason, in
    :func:`strata.record_store._claim_content_absent`) — every one of the three
    call sites needs the identical rule and none may import across the others'
    module boundaries without a cycle. KNOWN LIMIT, same direction as #202's own:
    a published item that PARAPHRASES the claim is not caught here — that is the
    judge's job (the OUTCOME REPORT / INPUT CHANGES instruction), not the engine's;
    the engine only closes the gap qwen's own optional-field pattern leaves open
    for the exact bytes.
    """
    haystack = " ".join(published_content.split())
    needle = " ".join(claim_content.split())
    return bool(needle) and needle in haystack


def propagate_claim_correction(
    scope_id: str,
    *,
    claim_id: str,
    corrected_claim_content: str,
    correcting_content: str,
    trigger_id: str,
    already_withdrawn: Collection[str],
    fleet: FleetConfig,
    record_store: RecordStore,
    summaries_dir: str,
    change_ids: Sequence[str] = (),
    hop: int = 0,
) -> list[PublishedItem]:
    """Mechanically withdraw *scope_id*'s OWN published items that still carry a
    claim its own outcome judgment (same-scope) or refresh (cross-scope) just found
    WRONG (ADR 0017 P4, CEO decision A: the engine enforces published-within-believed
    for the exact bytes; the judge is asked about everything else — see the OUTCOME
    REPORT and INPUT CHANGES instruction lines).

    No LLM in the loop — the mechanical sibling of :func:`propagate_directive_removals`,
    same shape: no judgment row (the correction was already judged; withdrawing a
    published item that still asserts it is closing a contradiction the judgment
    already settled, not a fresh judgment on the publication itself), a mechanical
    ``proposer``, and the SAME wave id so a reader sees ONE correction, never two.

    Skips *already_withdrawn* (the judge's own ``withdraw_published``, resolved to
    published item ids by the caller) so an item the judge itself named is never
    withdrawn — and so never emitted — a second time (acceptance criterion 3: one
    notice per reader either way, whichever path found it).

    The caller MUST already hold ``strata.locks.scope_lock(scope_id)`` — called from
    :func:`strata.app._judge_and_record`, already inside it, after the judge's own
    ``withdraw_published`` has been applied.

    Args:
        scope_id: The scope whose OWN publication is checked — never another
            scope's (D6: a failed_superseded outcome, and a directive target, never
            reach this function at all — the caller gates that).
        claim_id: The corrected claim's own id (``acted_on``) — carried on the
            notice alongside the withdrawn item's id (see :func:`emit`'s
            ``claim_id``).
        corrected_claim_content: The claim's own content, as it stood — what a
            still-published item must carry verbatim to qualify.
        correcting_content: The outcome's own observation — the notice's ``after``.
        trigger_id: The record id of the triggering event (the outcome contribution,
            same-scope; the refresh's own notice contribution, cross-scope) —
            carried on each withdraw act, mirroring :func:`propagate_directive_removals`.
        already_withdrawn: Published item ids the JUDGE already withdrew via
            ``withdraw_published`` — skipped here (criterion 3).
        change_ids: The change id(s) this correction is a consequence of (ADR 0014
            D4) — the P3 audit row's own id (same-scope) or the refresh's own
            ``wave_ids`` (cross-scope). Never a fresh id: a reader must see this
            correction under the ONE id it already knows.
        hop: Threaded through unchanged, matching every other propagation function.

    Returns:
        The published items actually withdrawn (empty if none carried the claim).
    """
    current_publication = read_publication(scope_id, summaries_dir=summaries_dir)
    if not current_publication:
        return []

    skip = set(already_withdrawn)
    to_withdraw = [
        item
        for item in current_publication
        if item.id not in skip and _carries_claim(item.content, corrected_claim_content)
    ]
    if not to_withdraw:
        return []

    return _withdraw_and_cascade_carriers(
        scope_id,
        to_withdraw,
        current_publication,
        claim_id=claim_id,
        correcting_content=correcting_content,
        trigger_id=trigger_id,
        reason="still carried corrected claim %s verbatim (ADR 0017 P4)",
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
        change_ids=change_ids,
        hop=hop,
    )


def _withdraw_and_cascade_carriers(
    scope_id: str,
    to_withdraw: Sequence[PublishedItem],
    current_publication: Sequence[PublishedItem],
    *,
    claim_id: str,
    correcting_content: str,
    trigger_id: str,
    reason: str,
    fleet: FleetConfig,
    record_store: RecordStore,
    summaries_dir: str,
    change_ids: Sequence[str],
    hop: int,
) -> list[PublishedItem]:
    """Shared tail of :func:`propagate_claim_correction` and
    :func:`check_claim_carriers` (issue #219 C): given the items ALREADY
    decided to carry a corrected claim — by verbatim match or by the
    owner-judge's own ``carries`` decision — withdraw each, rewrite the
    publication artifact, and cascade the withdrawal to every relay, exactly
    once, under one wave id, however the carrier was found.

    *reason* is a ``%``-style log template taking *claim_id* — the two
    callers log a different cause for the same mechanical act.
    """
    proposer = _mechanical_proposer(scope_id)
    for item in to_withdraw:
        record_store.append_publication_act(
            scope_id=scope_id,
            act="withdraw",
            kind=None,
            content=None,
            subject=None,
            anchors=None,
            withdraws=item.id,
            trigger=trigger_id,
            proposer=proposer,
        )
        _logger.info(
            "mechanically withdrew published item %s from scope %s: " + reason,
            item.id,
            scope_id,
            claim_id,
        )

    withdrawn_ids = {item.id for item in to_withdraw}
    remaining = [item for item in current_publication if item.id not in withdrawn_ids]
    _write_publication(scope_id, remaining, summaries_dir=summaries_dir)

    for item in to_withdraw:
        item_change_ids = emit_change_event(
            fleet=fleet,
            record_store=record_store,
            item=item.id,
            kind="claim_corrected",
            source_scope_id=scope_id,
            before=item.content,
            after=correcting_content,
            wave_ids=change_ids,
            hop=hop,
            by_owner=True,
            claim_id=claim_id,
        )
        _cascade_withdraw_relays(
            scope_id,
            item.id,
            fleet=fleet,
            record_store=record_store,
            summaries_dir=summaries_dir,
            held_scope_id=scope_id,
            change_ids=item_change_ids,
            hop=hop,
            notice_kind="claim_corrected",
            correcting_after=correcting_content,
            correcting_claim_id=claim_id,
        )

    return list(to_withdraw)


_CARRIER_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "as",
        "and",
        "or",
        "but",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "from",
        "into",
        "about",
        "than",
        "then",
        "so",
        "not",
        "no",
    }
)


def _content_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.casefold())
    return {w for w in words if w not in _CARRIER_STOPWORDS}


def _carrier_rank_score(claim_content: str, candidate_content: str) -> float:
    """Issue #219 C: rank a published item against a corrected claim for
    ORDERING ONLY, never as a gate — the offline measurement killed a
    threshold outright, since one either misses paraphrases or admits
    subject swaps. Key-token overlap (how many of the claim's own content
    words the candidate shares) plus content-word coverage (what fraction of
    the claim's content words that overlap is), so a short candidate that
    restates the whole claim scores above a long one that shares only a
    couple of words.

    A tokenless claim (no content words survive stripping stopwords) scores
    every candidate 0.0 — ties then fall back to the face's own stable
    order, so every candidate still reaches the judge call rather than being
    filtered out mechanically; see :func:`check_claim_carriers`.
    """
    claim_words = _content_words(claim_content)
    if not claim_words:
        return 0.0
    candidate_words = _content_words(candidate_content)
    overlap = claim_words & candidate_words
    coverage = len(overlap) / len(claim_words)
    return len(overlap) + coverage


_NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
    "sixty": "60",
    "seventy": "70",
    "eighty": "80",
    "ninety": "90",
    "hundred": "100",
    "thousand": "1000",
    "million": "1000000",
}


#: :func:`_value_tokens`'s closed-class polarity words (#219 C live-gate
#: measurement): words naming one of a pair of opposite states rather than a
#: measurable quantity — content-word/key-token tokenisers both treat these
#: as ordinary vocabulary, which is exactly why realistic corrections sharing
#: subject/action wording with the refuted claim broke two earlier attempts
#: at this veto (measured against 1,881 real judge answers from the re-gate:
#: a content-word variant caught 20/21 inversions but vetoed 51 TRUE
#: carriers). Restricting the veto to values alone — numbers, identifiers,
#: quoted spans, and this closed list — measured 14/21 inversions vetoed,
#: 0/198 true carriers vetoed.
_POL_WORDS = frozenset(
    {
        "on",
        "off",
        "before",
        "after",
        "always",
        "never",
        "required",
        "optional",
        "enabled",
        "disabled",
        "open",
        "closed",
        "allowed",
        "forbidden",
        "prohibited",
        "shared",
        "private",
        "include",
        "exclude",
        "min",
        "max",
        "above",
        "below",
        "first",
        "last",
    }
)


def _value_tokens(text: str) -> set[str]:
    """VALUE tokens only, for :func:`observed_value_veto` (#219 C live-gate
    measurement) — never ordinary content words, which is what made two
    earlier attempts at this veto over-fire on realistic corrections that
    share subject/action vocabulary with the refuted claim. Four kinds:

    - numbers, with grouping commas collapsed ("40,000" and "40000" are the
      same token) and spelled-out number words normalised to digits
      ("forty" is also "40000"'s own partial match via the per-word pass);
    - identifiers: a dot/slash/underscore-joined token ("pyproject.toml",
      "a/b", "a_b") always counts; a HYPHENATED token counts only when it
      contains a digit ("45-minute", "j4-822") — an ordinary hyphenated word
      ("well-known") does not;
    - a quoted span, kept whole rather than split into its own words;
    - any word that is one of :data:`_POL_WORDS`.
    """
    casefolded = text.casefold()
    tokens: set[str] = set()
    for match in re.finditer(r"\b\d[\d,]*(?:\.\d+)?\b", casefolded):
        tokens.add(match.group(0).replace(",", ""))
    for word in re.findall(r"[a-z]+", casefolded):
        if word in _NUMBER_WORDS:
            tokens.add(_NUMBER_WORDS[word])
    for match in re.finditer(r"[\w]+(?:[./_][\w]+)+", casefolded):
        tokens.add(match.group(0))
    for match in re.finditer(r"[\w]+(?:-[\w]+)+", casefolded):
        token = match.group(0)
        if any(c.isdigit() for c in token):
            tokens.add(token)
    for match in re.finditer(r'"([^"]+)"', text):
        tokens.add(match.group(1).strip().casefold())
    tokens |= set(re.findall(r"[a-z]+", casefolded)) & _POL_WORDS
    return tokens


def _decomposed_value_tokens(text: str) -> set[str]:
    """:func:`_value_tokens`, with one change: a hyphen- or slash-joined
    compound CONTAINING a digit ("under-12", "j4-822") is decomposed into
    its atoms — the digit atom normalised as a bare number — INSTEAD OF
    kept whole, so a value spelled as a compound on one side compares
    against the same value spelled as plain words on the other ("under-12"
    vs "under 12") — v1.17 item 1's architect review of 10327f6's bridge
    replay, round 2: adding the atom BESIDE the whole compound is not
    enough, because the whole compound ("under-12") then never matches
    anything on a side that spells it as two plain words, and a strict
    subset check still fails on that leftover token. Number WORDS still
    normalise via the same :data:`_NUMBER_WORDS` table :func:`_value_tokens`
    already uses — nothing new there.

    A SEPARATE function from :func:`_value_tokens`, not a change to it:
    :func:`observed_value_veto`'s own #219 C gate relies on the whole
    compound counting as one identifier-shaped token (``j4-822`` as a work
    item id), and this function's own callers (the attribution and relation
    re-checks' value-conflict comparisons) need the opposite — this is a
    deliberate, reported fork, not a shared improvement to the original.
    """
    tokens = set(_value_tokens(text))
    for match in re.finditer(r"[\w]+(?:[-/][\w]+)+", text.casefold()):
        compound = match.group(0)
        if not any(c.isdigit() for c in compound):
            continue
        tokens.discard(compound)
        for atom in re.split(r"[-/]", compound):
            if atom.isdigit():
                tokens.add(atom)
            elif atom in _NUMBER_WORDS:
                tokens.add(_NUMBER_WORDS[atom])
    return tokens


#: #219's own closed POL_WORDS set, paired into antonyms — bidirectional.
#: "forbidden" and "prohibited" are both treated as the antonym of
#: "allowed" (near-synonyms of each other, not antonyms).
_POL_ANTONYMS: dict[str, frozenset[str]] = {
    "on": frozenset({"off"}),
    "off": frozenset({"on"}),
    "before": frozenset({"after"}),
    "after": frozenset({"before"}),
    "always": frozenset({"never"}),
    "never": frozenset({"always"}),
    "required": frozenset({"optional"}),
    "optional": frozenset({"required"}),
    "enabled": frozenset({"disabled"}),
    "disabled": frozenset({"enabled"}),
    "open": frozenset({"closed"}),
    "closed": frozenset({"open"}),
    "allowed": frozenset({"forbidden", "prohibited"}),
    "forbidden": frozenset({"allowed"}),
    "prohibited": frozenset({"allowed"}),
    "shared": frozenset({"private"}),
    "private": frozenset({"shared"}),
    "include": frozenset({"exclude"}),
    "exclude": frozenset({"include"}),
    "min": frozenset({"max"}),
    "max": frozenset({"min"}),
    "above": frozenset({"below"}),
    "below": frozenset({"above"}),
    "first": frozenset({"last"}),
    "last": frozenset({"first"}),
}

#: A span restating a claim under negation ("not waived", "no longer
#: required") states the opposite of the unnegated claim — checked
#: separately from :data:`_POL_ANTONYMS` because the negated word need not
#: be one of :data:`_POL_WORDS` at all ("waived" is ordinary vocabulary).
_NEGATION_MARKERS: tuple[str, ...] = (
    "no longer",
    "not",
    "never",
    "isn't",
    "aren't",
    "doesn't",
    "don't",
    "won't",
    "cannot",
    "can't",
)

#: R5c (architect ruling, round 5): a CHANGE marker in the claim with none
#: anywhere in the reference means the claim states a CHANGE from the
#: reference's own value, not a restatement of it — "security findings can
#: now be triaged within a week INSTEAD OF 48 hours" against a reference
#: that only ever says "48 hours" is a flip, even with no antonym pair and
#: no negation marker involved at all.
_CHANGE_MARKERS: tuple[str, ...] = (
    "instead of",
    "rather than",
    "no longer",
    "anymore",
    "except",
    "exception",
    "now can",
    "can now",
)


def value_polarity_flip(
    claim_text: str,
    reference_text: str,
    extra_antonym_pairs: Sequence[tuple[str, str]] = (),
) -> bool:
    """#219's polarity guard, as a FLIP check — architect review round 2 of
    10327f6's bridge replay: the PRIOR shape (every :data:`_POL_WORDS` word
    in the claim must also appear in the reference) over-fires on ordinary
    vocabulary that happens to be in the closed set ("the clinics directive
    ON two-person counts" has no polarity assertion at all; "on" is common
    word, not a claim about on/off state). A polarity word (or a negation)
    appearing in *claim_text* with NO antonym anywhere in *reference_text*
    is not a flip — it is simply not vetoed for being absent.

    Two mechanisms, either one sufficient to report a flip:

    - **antonym flip**: a :data:`_POL_ANTONYMS` word (or one of
      *extra_antonym_pairs*, for a caller-specific antonym pair the closed
      POL_WORDS set does not cover — v1.17 item 2's own relation antonyms,
      e.g. "at or below"/"at or above", not in :data:`_POL_WORDS` at all)
      appears in *claim_text*, its antonym appears in *reference_text*, and
      *claim_text* itself does not ALSO contain that antonym (a claim that
      restates both sides, e.g. quoting a change, is not penalised).
    - **negation flip**: a :data:`_NEGATION_MARKERS` word directly precedes
      a word in *claim_text* that *reference_text* also states UNNEGATED —
      "not waived" against a reference stating "waived" is a flip; "waived"
      against "waived" is not.
    - **change-marker flip** (R5c): a :data:`_CHANGE_MARKERS` phrase
      ("instead of", "rather than", "no longer", "anymore", "except",
      "exception", "now can"/"can now") appears anywhere in *claim_text*
      with NONE of them anywhere in *reference_text* — the claim states a
      CHANGE from the reference's own value with no antonym pair or
      negation marker necessarily involved at all.

    Pure and reusable: shared by v1.17 item 1's own
    ``directive_or_publication`` ground check and item 2's ``refines``/
    ``tightens`` fact-mode polarity guard (its own relation antonyms are
    supplied as *extra_antonym_pairs*) — ONE flip function, not two.
    """
    claim_cf = claim_text.casefold()
    ref_cf = reference_text.casefold()

    antonym_pairs = list(extra_antonym_pairs)
    for word, antonyms in _POL_ANTONYMS.items():
        antonym_pairs.extend((word, antonym) for antonym in antonyms)

    for first, second in antonym_pairs:
        # A bare `\b` lets a POL_WORD match inside an unrelated hyphenated
        # compound ("on-call", "sign-off" both contain "on"/"off" as
        # word-bounded substrings, with no on/off state assertion at all).
        # Excluding a match directly adjacent to a hyphen on either side
        # closes this without narrowing the real single-word case.
        first_re = re.compile(rf"(?<!-)\b{re.escape(first)}\b(?!-)")
        second_re = re.compile(rf"(?<!-)\b{re.escape(second)}\b(?!-)")
        claim_has_first, claim_has_second = (
            bool(first_re.search(claim_cf)),
            bool(second_re.search(claim_cf)),
        )
        ref_has_first, ref_has_second = (
            bool(first_re.search(ref_cf)),
            bool(second_re.search(ref_cf)),
        )
        if claim_has_first and ref_has_second and not ref_has_first:
            return True
        if claim_has_second and ref_has_first and not ref_has_second:
            return True

    negation_alternation = "|".join(re.escape(marker) for marker in _NEGATION_MARKERS)
    for marker in _NEGATION_MARKERS:
        for match in re.finditer(rf"\b{re.escape(marker)}\s+(\w+)", claim_cf):
            word = match.group(1)
            word_re = re.compile(rf"\b{re.escape(word)}\b")
            if word_re.search(ref_cf) and not re.search(
                rf"\b(?:{negation_alternation})\s+{re.escape(word)}\b", ref_cf
            ):
                return True

    return any(marker in claim_cf for marker in _CHANGE_MARKERS) and not any(
        marker in ref_cf for marker in _CHANGE_MARKERS
    )


#: Hole B (architect review round 3, bridge attack on 1ffc4e4): an
#: unrelated directive can be cited and still pass when the span's value
#: tokens are vacuous (no number/id/quote at all, so the subset check holds
#: trivially). Overlap is NECESSARY, never sufficient (the design note's
#: own contract line) — these stay EXCLUDED from the count even though some
#: are content-bearing words, because they recur in nearly every citation
#: regardless of subject.
_CONTENT_OVERLAP_STOPWORDS = frozenset(
    {
        "directive",
        "policy",
        "rule",
        "says",
        "already",
        "according",
        # Architect's bridge forced-gate fix (the j1a-025 coverage
        # artefact): attribution-FRAME verbs recur in nearly every
        # citation regardless of subject, the same reasoning as the
        # original six words above.
        "requires",
        "require",
        "required",
        "states",
        "stated",
        "mandates",
        "mandated",
        "notes",
        "following",
        "under",
        "per",
        "quoted",
        "ratified",
    }
)


def _overlap_stem(word: str) -> str:
    """A simple plural/-ed/-ing stem — just enough to match "products" to
    "product" and "stickers" to "sticker" — never a real stemmer; this is a
    necessary-overlap gate, not the value check itself."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _overlap_words(text: str) -> set[str]:
    words: set[str] = set()
    for word in re.findall(r"[a-z]+", text.casefold()):
        if len(word) < 4 or word in _CONTENT_OVERLAP_STOPWORDS:
            continue
        words.add(_overlap_stem(word))
    return words


def _shared_six_word_sequence(a: str, b: str) -> bool:
    a_tokens = re.findall(r"[a-z]+", a.casefold())
    b_tokens = re.findall(r"[a-z]+", b.casefold())
    if len(a_tokens) < 6 or len(b_tokens) < 6:
        return False
    b_sequences = {tuple(b_tokens[i : i + 6]) for i in range(len(b_tokens) - 5)}
    return any(tuple(a_tokens[i : i + 6]) in b_sequences for i in range(len(a_tokens) - 5))


#: F3 (architect review round 4, adversarial attack on 067183a: 12 passes
#: through a weak "≥2 shared words" bar, including the whole j4-812 text).
#: Splits the SPAN itself on coordinators, dashes and semicolons, so a
#: requirement clause added beyond what the reference covers is caught
#: even when the span's OVERALL coverage happens to clear the bar.
_OVERLAP_CLAUSE_SPLIT_RE = re.compile(
    r"\s*(?:--|;| and | AND | also | ALSO | plus | PLUS | as well as )\s*", re.IGNORECASE
)


def _coverage_ratio(subject_words: set[str], reference_words: set[str]) -> float:
    if not subject_words:
        return 0.0
    return len(subject_words & reference_words) / len(subject_words)


def content_overlap_required(
    span: str,
    reference_text: str,
    exclude_words: Collection[str] = (),
) -> bool:
    """Hole B / F3's fix: at least 60% of *span*'s own content words
    (casefolded, 4+ letters, simple plural/-ed/-ing stripping, excluding
    :data:`_CONTENT_OVERLAP_STOPWORDS` and *exclude_words*) must appear in
    *reference_text* — COVERAGE, not a flat "≥2 shared words" count (which
    a long span could clear while adding whole unattributed clauses) — OR
    a shared contiguous 6-word sequence.

    *exclude_words* (architect's bridge forced-gate fix): raw words (e.g.
    a scope's own id/name) the caller knows are NOT new information in
    this span — a directive's own SOURCE scope, or any rendered ancestor
    scope, named again in the span states nothing new ("the OBSERVATORY
    directive..." citing the observatory's own rule), the same reasoning
    as :data:`_CONTENT_OVERLAP_STOPWORDS`, just caller-specific rather
    than universal. Stemmed and casefolded the same way as the span's own
    words, so a plural or an inflected form still matches.

    F3's own further rule: the span is ALSO split on coordinators, dashes
    and semicolons, and every resulting clause that contains a REQUIREMENT
    verb (:data:`_PARTIAL_GROUNDING_REQUIREMENT_RE`-shaped — imported
    lazily from `strata.scope_manager` to avoid a circular import) must
    ITSELF reach 60% coverage — this is what catches a span whose overall
    coverage passes only because an earlier, genuinely-grounded clause
    carries it (j4-812's own shape: "transactions over $500 require
    step-up authentication AND transactions over $500 from newly-seen
    devices must be blocked outright" shares "transactions", "500" with
    the reference overall, but the SECOND clause's own value — "blocked
    outright" — never appears there at all).

    This is a NECESSARY gate, never a sufficient one on its own — the
    value and polarity checks still apply on top.
    """
    from strata.scope_manager import (  # noqa: PLC0415 — avoids a circular import
        _PARTIAL_GROUNDING_REQUIREMENT_RE,
    )

    excluded = _overlap_words(" ".join(exclude_words))
    reference_words = _overlap_words(reference_text)
    span_words = _overlap_words(span) - excluded
    # Found while verifying F3 against the full adversarial attack: a span
    # with only ONE surviving content word ("As the branches directive
    # already says" — "directive"/"already"/"says" are all stopwords, only
    # "branches" is left) can coincidentally match an UNRELATED reference
    # that happens to share that one word, at 100% coverage. Coverage
    # alone cannot distinguish "the whole span matches" from "the only
    # word left happens to match" — at least 2 surviving words are
    # required for the ratio to mean anything.
    if len(span_words) < 2 and not _shared_six_word_sequence(span, reference_text):
        return False
    if _coverage_ratio(span_words, reference_words) < 0.6 and not _shared_six_word_sequence(
        span, reference_text
    ):
        return False

    for clause in _OVERLAP_CLAUSE_SPLIT_RE.split(span):
        if not _PARTIAL_GROUNDING_REQUIREMENT_RE.search(clause):
            continue
        clause_words = _overlap_words(clause)
        if _coverage_ratio(clause_words, reference_words) < 0.6:
            return False
    return True


def observed_value_veto(refuted_claim: str, correcting_content: str, item_content: str) -> bool:
    """#219 C live-gate addition (CEO, standing rule 1 — never trust prompt
    text alone): a mechanical veto that can only PREVENT a withdrawal, never
    cause one. VALUE-token only (:func:`_value_tokens` — never ordinary
    content words; see that function's own docstring for why): the judge's
    own ``carries`` answer is overridden when the item's values include at
    least one value the correction states that the refuted claim does not
    (``c_only``), and none of the values the item is being blocked on
    (``block``: the refuted claim's own values the correction does NOT also
    repeat — or, when the correction repeats every one of them, every one of
    them, so a correction that merely adds detail never backs off the
    block). A polarity word in the correction counts only when the refuted
    claim itself has one too. A tokenless claim, or a correction with no
    value the refuted claim lacks, never vetoes.

    Two further clauses were tried in re-gate 2 and both dropped:
    - An ANTONYM FLIP (a closed before/after, on/off, etc. pair list,
      independent of the correction text) measured 0 overrides in the
      held-out run — the errors there were SUBJECT SWAPS ("fridges" for
      "freezers"), which no antonym pair can see. No held-out evidence it
      helps, so the CEO's rule drops it.
    - An ADDED EXCEPTION ("PII is redacted in logs except in debug builds",
      against refuted "PII is always redacted") is deliberately NOT a veto
      clause — the philosopher's ruling: such a text carries the claim's
      VALUE but not its SCOPE, and whether it should be withdrawn depends on
      what the correction actually hit (a changed value means the exception
      text is wrong too; the correction BEING the exception means it must
      stay), a fact this function cannot tell from the text alone. A veto
      that kept these would reintroduce the CEO's own named failure (keeping
      a refuted value published), so this abstains on an exception-only
      item in both directions and leaves the judge's own answer standing.

    Returns ``True`` when the item should be KEPT (the withdrawal is
    vetoed), ``False`` otherwise. The caller only ever consults this for an
    item the judge already marked ``carries`` — this never turns a
    ``does_not_carry`` into a ``carries``.
    """
    refuted_values = _value_tokens(refuted_claim)
    correcting_values = _value_tokens(correcting_content)
    if not (refuted_values & _POL_WORDS):
        correcting_values = correcting_values - _POL_WORDS
    c_only = correcting_values - refuted_values
    if not refuted_values or not c_only:
        return False
    block = (refuted_values - correcting_values) or refuted_values
    item_values = _value_tokens(item_content)
    return bool(item_values & c_only) and not (item_values & block)


def check_claim_carriers(
    scope_id: str,
    *,
    claim_id: str,
    corrected_claim_content: str,
    correcting_content: str,
    trigger_id: str,
    already_withdrawn: Collection[str],
    scope_manager: ScopeManager,
    fleet: FleetConfig,
    record_store: RecordStore,
    summaries_dir: str,
    change_ids: Sequence[str] = (),
    hop: int = 0,
    cap: int = 20,
) -> list[PublishedItem]:
    """Issue #219 C: one owner-judge call per correction, deciding whether the
    scope's own published face — beyond what the mechanical verbatim sweep
    and the judge's own ``withdraw_published`` already caught — still carries
    a claim this scope's own outcome or refresh just found wrong.

    Called from BOTH correction sites, right after
    :func:`propagate_claim_correction`, under the SAME wave id: the same-scope
    ``failed_corrected`` outcome path (:func:`strata.app._write_amendment`),
    and the cross-scope refresh's centralised sweep (:func:`strata.app.drain_scope`,
    #221) — it runs unconditionally there too, whatever the refresh judgment did.

    Candidates are the WHOLE current face minus *already_withdrawn* — never
    relays: a relay is a copy of a face item, and a face item judged
    ``carries`` takes its own relays down through the same
    :func:`_cascade_withdraw_relays` cascade the verbatim path uses. Ranked by
    :func:`_carrier_rank_score` (ordering only) and capped at *cap* (20):
    anything past the cap is recorded ``unresolved_overflow``, never silently
    dropped and never sent to the judge.

    Every candidate the judge call does classify gets its own row — ``carries``
    or ``does_not_carry`` is itself a judgment made, so the row exists either
    way (the philosopher: "carries / does_not_carry are acts, not labels");
    an id the judge named with no readable decision, or never named at all,
    is recorded ``unresolved_unreadable``. A ``carries`` answer additionally
    passes through :func:`observed_value_veto` (CEO, standing rule 1 — a
    judge's own wording is never trusted alone): when the item's own key
    tokens state what was actually OBSERVED rather than the refuted claim,
    the withdrawal is vetoed and the row records ``kept_by_guard`` instead.
    Nothing here is withdrawn except the items that end up decided
    ``carries`` after that veto.

    *scope_manager* is duck-typed, not required to be a real
    :class:`~strata.scope_manager.ScopeManager`: one that predates this
    method (a lighter test double elsewhere in the fleet) degrades every
    candidate to ``unresolved_unreadable`` exactly as an unreadable response
    would, rather than raising ``AttributeError`` into the write this
    function runs inside.
    """
    current_publication = read_publication(scope_id, summaries_dir=summaries_dir)
    if not current_publication:
        return []

    skip = set(already_withdrawn)
    face = [item for item in current_publication if item.id not in skip]
    if not face:
        return []

    ranked = sorted(
        enumerate(face),
        key=lambda pair: (
            -_carrier_rank_score(corrected_claim_content, pair[1].content),
            pair[0],
        ),
    )
    candidates = [item for _, item in ranked[:cap]]
    overflow = [item for _, item in ranked[cap:]]

    rows: list[tuple[str, str]] = [(item.id, "unresolved_overflow") for item in overflow]

    to_withdraw: list[PublishedItem] = []
    if candidates:
        # A scope-manager duck-type that predates this method (a lighter
        # test double elsewhere in the fleet, or a future rolling deploy
        # where the engine and the judge implementation are briefly out of
        # step) degrades the same way an unreadable decision does — fails
        # closed per candidate, never crashes the drain/write it runs
        # inside.
        check = getattr(scope_manager, "check_claim_carriers", None)
        decisions = (
            check(
                refuted_claim_content=corrected_claim_content,
                correcting_content=correcting_content,
                candidates=[(item.id, item.content) for item in candidates],
            )
            if check is not None
            else {}
        )
        for item in candidates:
            outcome = decisions.get(item.id, "unresolved_unreadable")
            if outcome not in ("carries", "does_not_carry", "unresolved_unreadable"):
                outcome = "unresolved_unreadable"
            if outcome == "carries" and observed_value_veto(
                corrected_claim_content, correcting_content, item.content
            ):
                # The mechanical veto (CEO, standing rule 1): the judge said
                # carries, but the item's own key tokens state what was
                # OBSERVED, not the refuted claim — never trust the prompt
                # text alone. Can only PREVENT a withdrawal.
                _logger.info(
                    "claim-carrier guard kept published item %s in scope %s: judge said "
                    "carries, but the item states the observed value, not the refuted "
                    "one (issue #219 C)",
                    item.id,
                    scope_id,
                )
                outcome = "kept_by_guard"
            rows.append((item.id, outcome))
            if outcome == "carries":
                to_withdraw.append(item)

    if rows:
        record_store.append_claim_carrier_checks(
            change_id=change_ids[0] if change_ids else trigger_id,
            scope_id=scope_id,
            corrected_claim_id=claim_id,
            checks=rows,
        )

    if not to_withdraw:
        return []

    return _withdraw_and_cascade_carriers(
        scope_id,
        to_withdraw,
        current_publication,
        claim_id=claim_id,
        correcting_content=correcting_content,
        trigger_id=trigger_id,
        reason="the owner-judge said it carries corrected claim %s (issue #219 C)",
        fleet=fleet,
        record_store=record_store,
        summaries_dir=summaries_dir,
        change_ids=change_ids,
        hop=hop,
    )


def apply_judged_withdrawals(
    scope_id: str,
    item_ids: Sequence[str],
    *,
    judged_by: str,
    reasoning: str | None,
    fleet: FleetConfig,
    record_store: RecordStore,
    summaries_dir: str,
    change_ids: Sequence[str] = (),
    hop: int = 0,
    notice_kind: str = "withdrawn",
    correcting_after: str | None = None,
    correcting_claim_id: str | None = None,
) -> list[PublishedItem]:
    """Withdraw published items named by a contribution judgment's ``withdraw_published``.

    (ADR 0007 D3/D5.)

    The JUDGED propagation path — subject-anchored items have no mechanical
    signal, so the publishing scope-manager's own contribution judgment
    names items to withdraw when a summary rewrite drops or contradicts the
    belief behind them (:mod:`strata.scope_manager`'s ``withdraw_published``
    field). Unlike :func:`propagate_directive_removals`, each withdrawal here
    DOES get a judgment row — it was judged, just as part of the contribution
    judgment call rather than a fresh one — carrying the SAME ``judged_by``
    and ``reasoning`` as that contribution judgment.

    The caller MUST already hold ``strata.locks.scope_lock(scope_id)`` (this
    is called from :func:`strata.app._judge_and_record`, already inside it).

    Args:
        scope_id: The publishing scope.
        item_ids: Published item ids named for withdrawal. Ids not currently
            published are ignored (logged, not an error) — per ADR 0007 D3's
            "the judge stays a single API call" simplicity, a stale or
            hallucinated id must not crash the choke point.
        judged_by: The judging authority (mirrors the originating
            contribution judgment's ``judged_by``, typically
            ``"scope-manager"``).
        reasoning: The originating contribution judgment's reasoning,
            carried onto each derived withdraw act's judgment row.
        fleet: Read to compute each withdrawal's affected set (ADR 0014 D3).
        change_ids: The judgment's own ``wave_ids``, when it has any — a
            withdrawal made on a refresh is DERIVED from the changes that
            triggered that refresh and inherits every one of their ids (ADR
            0014 D4/D8: the ids are threaded in as a parameter, never looked
            up). Empty makes each withdrawal an independent change.
        hop: How far along the wave the judgment that ordered this sat (see
            :func:`propagate_directive_removals`).
        notice_kind: ADR 0017 P4. ``"withdrawn"`` (the default) for an ordinary
            withdrawal. ``"claim_corrected"`` when this withdrawal is the
            HOLDING scope's own response to a correction — its own outcome
            judgment (same-scope) or a refresh reacting to one (cross-scope) —
            so the fan-out to ITS readers carries the correction, not a bare
            removal. Threaded straight to :func:`~strata.change_events.emit`;
            never invented here.
        correcting_after: ``claim_corrected`` only — the correcting content to
            carry as ``after`` instead of the withdrawal's usual ``None``
            ("this input is gone"): a correction replaces, it does not merely
            remove (P3 ruling line (d)).
        correcting_claim_id: ``claim_corrected`` only (ADR 0017 P4) — the
            corrected claim's own id, carried on the notice alongside the
            withdrawn item's id (see :func:`emit`'s ``claim_id``).

    Returns:
        The published items actually withdrawn.
    """
    if not item_ids:
        return []

    current_publication = read_publication(scope_id, summaries_dir=summaries_dir)
    if not current_publication:
        return []

    by_id = {item.id: item for item in current_publication}
    withdrawn: list[PublishedItem] = []
    proposer = _mechanical_proposer(scope_id)

    for item_id in item_ids:
        item = by_id.get(item_id)
        if item is None:
            _logger.warning(
                "judged withdrawal named published item %r, which is not currently "
                "published in scope %r — ignored",
                item_id,
                scope_id,
            )
            continue
        act = record_store.append_publication_act(
            scope_id=scope_id,
            act="withdraw",
            kind=None,
            content=None,
            subject=None,
            anchors=None,
            withdraws=item_id,
            trigger=None,
            proposer=proposer,
        )
        record_store.record_publication_judgment(
            act_id=act.id,
            decision="accept",
            judged_by=judged_by,
            reasoning=reasoning,
        )
        withdrawn.append(item)

    if not withdrawn:
        return []

    withdrawn_ids = {item.id for item in withdrawn}
    remaining = [item for item in current_publication if item.id not in withdrawn_ids]
    _write_publication(scope_id, remaining, summaries_dir=summaries_dir)

    # ADR 0013 D4b — mechanical, no LLM: this withdrawal was judged (as part
    # of the triggering contribution judgment), but the relay cascade it
    # sets off never is — a relay believes an item only because its origin
    # does.
    for item in withdrawn:
        item_change_ids = emit_change_event(
            fleet=fleet,
            record_store=record_store,
            item=item.id,
            kind=notice_kind,
            source_scope_id=scope_id,
            before=item.content,
            after=correcting_after if notice_kind == "claim_corrected" else None,
            wave_ids=change_ids,
            hop=hop,
            claim_id=correcting_claim_id if notice_kind == "claim_corrected" else None,
        )
        _cascade_withdraw_relays(
            scope_id,
            item.id,
            fleet=fleet,
            record_store=record_store,
            summaries_dir=summaries_dir,
            held_scope_id=scope_id,
            change_ids=item_change_ids,
            hop=hop,
            notice_kind=notice_kind,
            correcting_after=correcting_after,
            correcting_claim_id=correcting_claim_id,
        )

    return withdrawn


# ---------------------------------------------------------------------------
# Bootstrap (ADR 0007 D4) — the migration story for fleets relying on the
# retired D3 whole-face peer layers. Deliberate, per-scope, operator-
# initiated — never automatic (see the ADR's "Alternatives considered").
# ---------------------------------------------------------------------------


def bootstrap_publication(
    scope_id: str,
    *,
    fleet: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    scope_manager: ScopeManager,
    publication_max_words: int | None = None,
) -> BootstrapOutcome:
    """Bootstrap *scope_id*'s initial publication from its current summary (ADR 0007 D4).

    One scope-manager call
    (:meth:`~strata.scope_manager.ScopeManager.judge_bootstrap_publication`)
    receives the scope's rendered current summary and returns either a
    decline, or an initial set of candidate published items (each carrying
    its own anchors). Every returned item is structurally validated
    (:func:`_validate_anchors`) exactly like an ordinary publish; an item
    that fails validation is dropped (logged) rather than aborting the whole
    bootstrap — a deliberate deviation from the single-item error-not-decline
    rule, justified by this being a best-effort BATCH primitive run once, by
    a human, not a per-item agent proposal. Every item that passes is
    recorded as an ORDINARY accepted publish act (``judged_by="scope-manager"``)
    and the artifact is rewritten once with the accumulated set.

    Runs under :func:`strata.locks.scope_lock` for *scope_id*.

    Args:
        scope_id: The scope to bootstrap.
        publication_max_words: ADR 0013 D3 — the word budget for the
            bootstrapped face, forwarded to
            :meth:`~strata.scope_manager.ScopeManager.judge_bootstrap_publication`,
            which trims the candidate set mechanically to fit. ``None``
            (the default) resolves to
            :data:`strata.scope_manager.PUBLICATION_MAX_WORDS`.

    Returns:
        A :class:`BootstrapOutcome` — ``decision="decline"`` with an empty
        ``items`` list when the scope-manager finds nothing fit to publish
        yet; otherwise the accepted items (which may be fewer than the
        scope-manager proposed, if any failed anchor validation or were
        trimmed to fit the budget).

    Raises:
        ValueError: *scope_id* is not found in *fleet*.
    """
    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise ValueError(f"Scope not found: {scope_id!r}")

    if publication_max_words is None:
        # Deferred import — see propose_publish above: a module-level
        # import here would be circular.
        from strata.scope_manager import PUBLICATION_MAX_WORDS

        publication_max_words = PUBLICATION_MAX_WORDS

    with scope_lock(scope_id):
        current_summary = summary_store.read(scope_id)
        # Read BEFORE judging (ADR 0013 D3): bootstrapping is not always a
        # scope's first publication — the trim inside judge_bootstrap_publication
        # must see what is already published so the combined face cannot
        # land over budget.
        existing = read_publication(scope_id, summaries_dir=str(summary_store.summaries_dir))

        judgment = scope_manager.judge_bootstrap_publication(
            scope=scope,
            current_summary=current_summary,
            publication_max_words=publication_max_words,
            current_publication=existing,
        )

        if judgment.decision == "decline" or not judgment.items:
            return BootstrapOutcome(
                decision="decline",
                reasoning=judgment.reasoning,
                items=[],
                trimmed=judgment.trimmed,
            )

        proposer = _bootstrap_proposer(scope_id)
        recorded: list[PublishedItem] = []

        binding_ids = _binding_directive_ids(scope_id, fleet=fleet, summary_store=summary_store)
        for candidate in judgment.items:
            tagged_anchors = [
                _tag_anchor(a, valid_directive_ids=binding_ids) for a in candidate.anchors
            ]
            try:
                _validate_anchors(tagged_anchors, valid_directive_ids=binding_ids)
            except ValueError as exc:
                _logger.warning(
                    "bootstrap candidate for scope %r dropped — invalid anchors: %s",
                    scope_id,
                    exc,
                )
                continue

            act = record_store.append_publication_act(
                scope_id=scope_id,
                act="publish",
                kind=candidate.kind,
                content=candidate.content,
                subject=candidate.subject,
                anchors=tagged_anchors,
                withdraws=None,
                trigger=None,
                proposer=proposer,
            )
            record_store.record_publication_judgment(
                act_id=act.id,
                decision="accept",
                judged_by="scope-manager",
                reasoning=judgment.reasoning,
            )
            recorded.append(
                PublishedItem(
                    id=act.id,
                    kind=candidate.kind,
                    content=candidate.content,
                    subject=candidate.subject,
                    anchors=tagged_anchors,
                    published_at=act.created_at,
                )
            )

        if recorded:
            _write_publication(
                scope_id, existing + recorded, summaries_dir=str(summary_store.summaries_dir)
            )
            # ADR 0014 D1 — a first face is still an addition to everyone
            # downstream: they have been composing nothing from this scope
            # and now have something. One independent change per item, since
            # a reader may believe one and not another.
            for item in recorded:
                emit_change_event(
                    fleet=fleet,
                    record_store=record_store,
                    item=item.id,
                    kind="published",
                    source_scope_id=scope_id,
                    after=item.content,
                )

        decision: Literal["accept", "decline"] = "accept" if recorded else "decline"
        return BootstrapOutcome(
            decision=decision,
            reasoning=judgment.reasoning,
            items=recorded,
            trimmed=judgment.trimmed,
        )
