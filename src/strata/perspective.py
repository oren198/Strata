"""Perspective composition — the importable library primitive (issue #83, primitive A).

Extracted from ``strata.mcp.server`` (plan item S2.1): perspective
composition — layer ordering, relation labelling, and the synthesized-empty-
summary fallback — used to live only inside the ``strata_read_perspective``
MCP tool, which ADR 0001 documents as "not cleanly importable." Hosting
consumers (e.g. strata-web) had no choice but to copy the logic by hand. This
module is now the single place composition lives; ``strata.mcp.server``
delegates to :func:`compose_perspective` after its own entitlement checks.

Implements the shipped contract from ADR 0006 D3 (peer-reference composition)
and D4 (reconciliation with the #48 read surface): layers compose root-first
— inter-stratum ancestors, then the requested scope's own layer, then
chain-referenced peers (one hop via intra-stratum edges, sorted by scope id
for deterministic order).

This branch (S2.1) was a byte-identical extraction plus one additive,
library-only parameter (``extra_context_scopes``) — nothing about layer
payloads, ordering, or labelling changed.

ADR 0007 (publication mechanism, issue #90) now lands here — the D4
amendment to ADR 0006 D3: ``compose_perspective`` gains an optional
``publication_reader``. When given, every ``peer_reference`` layer carries
that peer's CURRENT PUBLICATION — its curated, judged outward face — instead
of its full internal summary: a ``"publication": {"items": [...]}`` payload
replaces the ``"summary"`` key entirely (never both). ``publication_reader``
is a lightweight callable, not a store object, mirroring
``operator_reader``'s shape — this module stays free of SQLite/markdown-store
machinery. ``extra_context_scopes`` layers are deliberately UNAFFECTED: they
remain full summaries regardless of ``publication_reader``, because that
parameter is a distinct, operator-sanctioned hosting surface (issue #83), not
a peer reference — ADR 0007 D4 retired whole-face reads specifically for the
peer-reference channel CONTEXT.md § Intra-stratum edge describes, not for
every context-only layer this primitive can compose.

ADR 0008 (operator stratum mechanism, #91) lands here: ``compose_perspective``
gains an optional ``operator_reader`` — a callable, not a store object, so
this module stays free of SQLite/record-store machinery. For each chain
scope (ancestors + self) that has attached operator memory, an operator
layer is inserted IMMEDIATELY ABOVE that scope's own layer — verbatim,
never part of any scope's summary, so no scope-manager rewrite can ever
touch it (ADR 0008 D2). Peer and extra-context layers never get an operator
layer: operator memory binds a *chain*, and a peer's chain is not this
reader's to compose.

Vocabulary follows CONTEXT.md verbatim: scope, stratum, perspective, scope
summary, directive, context, intra-stratum edge (peer reference), operator.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from strata.fleet_config import FleetConfig
from strata.summary_store import ScopeSummary, SummaryStore


class _OperatorItemLike(Protocol):
    """Structural shape ``compose_perspective`` needs from an operator item.

    A lightweight protocol rather than importing :class:`strata.operator.OperatorItem`
    directly — this module composes perspectives from a reader callable, not
    from ``strata.operator`` or any record-store machinery (ADR 0008 D2).
    """

    id: str
    kind: str
    content: str
    subject: str | None
    created_at: str


#: Reads the current operator memory attached at one scope. Returns an empty
#: sequence for a scope with no operator memory. See
#: :func:`strata.operator.read_operator_layer` for the canonical implementation
#: — callers typically pass ``functools.partial(read_operator_layer, summaries_dir=...)``.
OperatorReader = Callable[[str], Sequence[_OperatorItemLike]]


class _PublishedItemLike(Protocol):
    """Structural shape ``compose_perspective`` needs from a published item.

    A lightweight protocol rather than importing
    :class:`strata.publication.PublishedItem` directly — same rationale as
    ``_OperatorItemLike`` above.
    """

    id: str
    kind: str
    content: str
    subject: str | None
    anchors: list[str]
    published_at: str


#: Reads the current published items for one scope (ADR 0007 D1/D4). Returns
#: an empty sequence for a scope that has published nothing yet — the
#: honestly empty face. See :func:`strata.publication.read_publication` for
#: the canonical implementation — callers typically pass
#: ``functools.partial(read_publication, summaries_dir=...)``.
PublicationReader = Callable[[str], Sequence[_PublishedItemLike]]


def _publication_item_dict(item: _PublishedItemLike) -> dict:
    """Verbatim item dict for a peer_reference layer's publication payload."""
    return {
        "id": item.id,
        "kind": item.kind,
        "content": item.content,
        "subject": item.subject,
        "anchors": list(item.anchors),
        "published_at": item.published_at,
    }


def _operator_layer(attachment_scope_id: str, items: Sequence[_OperatorItemLike]) -> dict:
    """Build the operator layer dict for *attachment_scope_id* (ADR 0008 D2).

    Verbatim: item dicts carry exactly ``id``, ``content``, ``subject``,
    ``created_at`` — no rewriting, no summarisation. Directives and context
    are split into separate lists so the shape mirrors a scope summary's own
    two sections without reusing the ``summary`` key (an operator layer is
    never a scope summary — ADR 0008 D2's "not part of any scope's summary").
    """

    def _item_dict(item: _OperatorItemLike) -> dict:
        return {
            "id": item.id,
            "content": item.content,
            "subject": item.subject,
            "created_at": item.created_at,
        }

    return {
        "scope_id": attachment_scope_id,
        "stratum_id": "operator",
        "relation": "operator",
        "binding": True,
        "operator_memory": {
            "directives": [_item_dict(i) for i in items if i.kind == "directive"],
            "context": [_item_dict(i) for i in items if i.kind == "context"],
        },
    }


def summary_for_scope(scope_id: str, *, summary_store: SummaryStore) -> dict:
    """Return a scope's summary as a plain dict, synthesizing an empty one if none exists on disk.

    The synthesized summary reports ``version=0``/``exists=False`` so it is
    never mistaken for a real first write (``version=1``, ``exists=True``) —
    see :class:`strata.summary_store.ScopeSummary` (issue #59).
    """
    existing = summary_store.read(scope_id)
    if existing is not None:
        return existing.model_dump()
    empty = ScopeSummary(
        scope_id=scope_id,
        directives=[],
        context="",
        updated_at=datetime.now(tz=UTC).isoformat(),
        version=0,
        exists=False,
    )
    return empty.model_dump()


def compose_perspective(
    scope_id: str,
    *,
    fleet: FleetConfig,
    summary_store: SummaryStore,
    extra_context_scopes: Sequence[str] = (),
    operator_reader: OperatorReader | None = None,
    publication_reader: PublicationReader | None = None,
) -> dict:
    """Compose *scope_id*'s perspective: its own summary, ancestor chain, and referenced peers.

    A perspective assembles (CONTEXT.md § Perspective): the scope's own
    summary, the summaries of every inter-stratum ancestor up to the root,
    and — ADR 0006 D3, delivering the referenced scope's **publication** per
    the ADR 0007 D4 amendment — the outward face of any peer scopes
    referenced (one hop, via an intra-stratum edge) by a scope on that
    chain. Layers are ordered root-first: ancestors first, then the
    requested scope's own layer, then referenced-peer layers (sorted by
    scope id for deterministic ordering).

    Every layer carries ``relation`` (``"self"``, ``"ancestor"``, or
    ``"peer_reference"``) and ``binding`` (``True`` for self/ancestor layers,
    ``False`` for peer layers). Peer layers are **context only** — nothing in
    them binds the reader. Peer-of-peer references are not traversed: only
    edges whose source scope is itself on the chain count (one hop, per
    ``FleetConfig.entitlement_view``).

    Peer layer payload (ADR 0007 D4 — the ADR 0006 D3 amendment):

    - When *publication_reader* is given, each peer layer carries
      ``"publication": {"items": [<item dicts: id, kind, content, subject,
      anchors, published_at>]}`` — that peer's CURRENT published items,
      verbatim, and NO ``"summary"`` key. A peer that has published nothing
      gets ``{"items": []}`` — the honestly empty face stays visible
      (composition is provenance-preserving even when empty: "you reference
      this scope; it publishes nothing" is itself information). Never the
      peer's internal summary — publishing is a judged act distinct from
      internal acceptance (ADR 0007 D2), and composing raw internal memory
      into a reader who never judged it for export is exactly what ADR 0007
      retires.
    - When *publication_reader* is ``None`` (the legacy call shape), peer
      layers carry the peer's full ``"summary"`` exactly as before — no
      behaviour change for callers that have not adopted publication yet.

    If a chain or peer scope has no summary on disk yet, its layer is still
    included with empty directives and context so that the structure is
    visible; that layer's summary honestly reports ``version=0``/
    ``exists=False`` rather than looking like a real first write (issue #59).

    Args:
        scope_id: The scope for which to build the perspective. Must exist
            in *fleet*.
        fleet: The loaded fleet configuration to compose against.
        summary_store: The store to read scope summaries from.
        extra_context_scopes: Zero or more additional scope ids to compose as
            context-only layers, appended after the peer layers (sorted by
            scope id), each with ``relation: "extra_context"`` and
            ``binding: False``. Additive, library-only surface (issue #83)
            for consumers that need to compose in scopes beyond the chain
            and its referenced peers; the MCP server does not use it — every
            entry must exist in *fleet* or the whole call raises.
        operator_reader: ADR 0008 D2. When given, called once per chain scope
            (ancestors + self) with that scope's id; for each chain scope
            that has operator memory (a non-empty return), an operator layer
            — ``{scope_id, stratum_id: "operator", relation: "operator",
            binding: True, operator_memory: {directives, context}}`` with
            VERBATIM item dicts — is inserted immediately above that chain
            scope's own layer. Peer and extra-context layers never get an
            operator layer. ``None`` (the default) composes zero operator
            layers — existing callers see no behaviour change.
        publication_reader: ADR 0007 D4. When given, called once per
            referenced-peer scope with that scope's id; each peer layer's
            payload becomes ``{"publication": {"items": [...]}}`` (see
            above) instead of ``{"summary": {...}}``. Does NOT affect
            ``extra_context_scopes`` layers, which always carry a full
            summary regardless — that parameter is a distinct,
            operator-sanctioned hosting surface (issue #83), not the peer-
            reference channel ADR 0007 D4 retired whole-face reads for.
            ``None`` (the default) composes peer layers exactly as before —
            existing callers see no behaviour change.

    Returns:
        ``{scope_id: <requested>, layers: [{scope_id, stratum_id, relation,
        binding, summary | publication}], _layers_count: N}`` ordered
        root-first, then self, then sorted peer layers, then sorted
        extra-context layers. Self/ancestor/extra-context layers always
        carry ``"summary"``; peer layers carry ``"summary"`` when
        *publication_reader* is ``None`` and ``"publication"`` otherwise
        (never both). When *operator_reader* is given, an operator layer
        (see above) precedes each chain layer that has operator memory.

    Raises:
        ValueError: If *scope_id*, or any entry of *extra_context_scopes*, is
            not found in *fleet*.
    """
    scope = fleet.get_scope(scope_id)
    if scope is None:
        raise ValueError(f"Scope not found: {scope_id!r}")

    extra_scopes = []
    for extra_id in extra_context_scopes:
        extra_scope = fleet.get_scope(extra_id)
        if extra_scope is None:
            raise ValueError(f"Scope not found: {extra_id!r}")
        extra_scopes.append(extra_scope)

    # Build the ancestor chain (root-first), then append the requested scope.
    ancestors = fleet.inter_stratum_ancestors(scope_id)
    chain = [*ancestors, scope]

    layers = []
    for s in chain:
        if operator_reader is not None:
            operator_items = operator_reader(s.id)
            if operator_items:
                # ADR 0008 D2: the operator layer sits immediately above its
                # attachment scope's own layer — inserted here, before the
                # chain scope's layer itself is appended below.
                layers.append(_operator_layer(s.id, operator_items))
        layers.append(
            {
                "scope_id": s.id,
                "stratum_id": s.stratum_id,
                "summary": summary_for_scope(s.id, summary_store=summary_store),
                "relation": "self" if s.id == scope_id else "ancestor",
                "binding": True,
            }
        )

    # ADR 0006 D3: append one layer per peer referenced (one hop) by any
    # scope on the chain. Reuses FleetConfig.entitlement_view rather than
    # re-deriving peer logic — sorted by scope id for deterministic order.
    view = fleet.entitlement_view(scope_id)
    for s in sorted(view.referenced_peers, key=lambda peer: peer.id):
        peer_layer: dict = {
            "scope_id": s.id,
            "stratum_id": s.stratum_id,
            "relation": "peer_reference",
            "binding": False,
        }
        if publication_reader is not None:
            # ADR 0007 D4: the referenced scope's PUBLICATION, never its
            # internal summary. No "summary" key at all in this shape.
            items = publication_reader(s.id)
            peer_layer["publication"] = {"items": [_publication_item_dict(i) for i in items]}
        else:
            # Legacy call shape — unchanged behaviour for callers that have
            # not adopted publication_reader yet.
            peer_layer["summary"] = summary_for_scope(s.id, summary_store=summary_store)
        layers.append(peer_layer)

    # Issue #83 addition: library-only extra context scopes, appended last,
    # sorted by scope id. Never used by the MCP server (which only ever
    # composes a caller's own chain plus its referenced peers).
    for s in sorted(extra_scopes, key=lambda scope: scope.id):
        layers.append(
            {
                "scope_id": s.id,
                "stratum_id": s.stratum_id,
                "summary": summary_for_scope(s.id, summary_store=summary_store),
                "relation": "extra_context",
                "binding": False,
            }
        )

    return {
        "scope_id": scope_id,
        "layers": layers,
        "_layers_count": len(layers),
    }
