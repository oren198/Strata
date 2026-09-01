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

from strata.locks import scope_lock
from strata.record_store import JUDGE_FAILED, ContributorRef, RecordStore

if TYPE_CHECKING:
    from strata.fleet_config import FleetConfig
    from strata.scope_manager import ScopeManager
    from strata.summary_store import ScopeSummary, SummaryStore

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
    act: Literal["publish", "withdraw"]
    decision: Literal["accept", "decline"]
    reasoning: str
    artifact_updated: bool


@dataclass(frozen=True)
class BootstrapOutcome:
    """The result of :func:`bootstrap_publication`."""

    decision: Literal["accept", "decline"]
    reasoning: str
    items: list[PublishedItem]


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


def _tag_anchor(raw: str, *, current_summary: ScopeSummary | None) -> str:
    """Prefix-tag one raw anchor string as ``directive:<id>`` or ``subject:<text>``.

    An anchor already carrying an explicit ``directive:``/``subject:`` prefix
    is respected verbatim (still subject to :func:`_validate_anchors` below —
    this lets a caller assert "this is a directive anchor" and have it
    checked, rather than silently downgraded to a subject anchor because the
    directive was removed). Anything else is auto-classified: an exact match
    against a directive id currently in *current_summary* becomes a
    ``directive:`` anchor; anything else becomes a ``subject:`` anchor
    (free-form — no verification is possible or required for a subject
    reference).
    """
    if raw.startswith(_DIRECTIVE_ANCHOR_PREFIX) or raw.startswith(_SUBJECT_ANCHOR_PREFIX):
        return raw
    directive_ids = {d.id for d in (current_summary.directives if current_summary else [])}
    if raw in directive_ids:
        return f"{_DIRECTIVE_ANCHOR_PREFIX}{raw}"
    return f"{_SUBJECT_ANCHOR_PREFIX}{raw}"


def _validate_anchors(
    tagged_anchors: Sequence[str], *, current_summary: ScopeSummary | None
) -> None:
    """Structurally validate *tagged_anchors* — raises :class:`ValueError`, never a decline.

    Mirrors the ADR 0006 D1 error-not-decline rule for structurally-refused
    writes: this runs BEFORE the scope-manager is ever invoked, and a
    failure here means no act row is appended at all.

    Raises:
        ValueError: *tagged_anchors* is empty, or a ``directive:`` anchor
            names an id that is not present in *current_summary*'s
            directives.
    """
    if not tagged_anchors:
        _logger.warning("publication anchor validation failed: zero anchors supplied")
        raise ValueError(
            "A publish act requires at least one anchor — either a directive id "
            "currently present in this scope's summary, or a subject string "
            "(ADR 0007 D1)."
        )
    directive_ids = {d.id for d in (current_summary.directives if current_summary else [])}
    for anchor in tagged_anchors:
        if anchor.startswith(_DIRECTIVE_ANCHOR_PREFIX):
            directive_id = anchor[len(_DIRECTIVE_ANCHOR_PREFIX) :]
            if directive_id not in directive_ids:
                _logger.warning(
                    "publication anchor validation failed: directive %r not in current summary",
                    directive_id,
                )
                raise ValueError(
                    f"Anchor references directive {directive_id!r}, which is not in this "
                    "scope's CURRENT summary — a publish act can only anchor to a directive "
                    "the scope currently holds (ADR 0007 D1)."
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
        tagged_anchors = [_tag_anchor(a, current_summary=current_summary) for a in anchors]
        _validate_anchors(tagged_anchors, current_summary=current_summary)

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
            # ADR 0013 D4b — mechanical, no LLM: every downstream copy
            # relayed from this item goes with it.
            _cascade_withdraw_relays(
                scope_id,
                item_id,
                record_store=record_store,
                summaries_dir=str(summary_store.summaries_dir),
                held_scope_id=scope_id,
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
    record_store: RecordStore,
    summaries_dir: str,
    held_scope_id: str,
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

    *held_scope_id* is the ONE scope whose :func:`strata.locks.scope_lock`
    is already held by the outermost caller of this cascade (every entry
    point calls this from inside its own ``with scope_lock(scope_id):``
    block) — the only scope this function must NOT try to re-lock (the lock
    is not reentrant). A downstream scope's lock, once taken below, is
    always released before recursing, so at most one OTHER scope's lock is
    ever held at a time; *held_scope_id* is therefore the only deadlock risk,
    and a relayed copy that loops back into it is withdrawn in place, under
    the lock the caller already holds, rather than skipped.
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
            _cascade_withdraw_relays(
                other_scope_id,
                item.id,
                record_store=record_store,
                summaries_dir=summaries_dir,
                held_scope_id=held_scope_id,
            )


# ---------------------------------------------------------------------------
# Staleness propagation (ADR 0007 D3) — two paths, by anchor type.
# ---------------------------------------------------------------------------


def propagate_directive_removals(
    scope_id: str,
    removed_directive_ids: Collection[str],
    trigger_id: str,
    *,
    surviving_directive_ids: Collection[str],
    record_store: RecordStore,
    summaries_dir: str,
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
    # from each item this call just withdrew.
    for item in to_withdraw:
        _cascade_withdraw_relays(
            scope_id,
            item.id,
            record_store=record_store,
            summaries_dir=summaries_dir,
            held_scope_id=scope_id,
        )

    return to_withdraw


def apply_judged_withdrawals(
    scope_id: str,
    item_ids: Sequence[str],
    *,
    judged_by: str,
    reasoning: str | None,
    record_store: RecordStore,
    summaries_dir: str,
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
        _cascade_withdraw_relays(
            scope_id,
            item.id,
            record_store=record_store,
            summaries_dir=summaries_dir,
            held_scope_id=scope_id,
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
            return BootstrapOutcome(decision="decline", reasoning=judgment.reasoning, items=[])

        proposer = _bootstrap_proposer(scope_id)
        recorded: list[PublishedItem] = []

        for candidate in judgment.items:
            tagged_anchors = [
                _tag_anchor(a, current_summary=current_summary) for a in candidate.anchors
            ]
            try:
                _validate_anchors(tagged_anchors, current_summary=current_summary)
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

        decision: Literal["accept", "decline"] = "accept" if recorded else "decline"
        return BootstrapOutcome(decision=decision, reasoning=judgment.reasoning, items=recorded)
