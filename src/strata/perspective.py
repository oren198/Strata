"""Perspective composition — the importable library primitive (issue #83, primitive A).

Extracted from ``strata.mcp.server`` (plan item S2.1): perspective
composition — layer ordering, relation labelling, and the synthesized-empty-
summary fallback — used to live only inside the ``strata_read_perspective``
MCP tool, which ADR 0001 documents as "not cleanly importable." Consumers
embedding the engine had no choice but to copy the logic by hand. This
module is now the single place composition lives; ``strata.mcp.server``
delegates to :func:`compose_perspective` after its own entitlement checks.

ADR 0013 (publication as the only sharing channel) is the current shipped
composition rule and amends ADR 0006 D3 and ADR 0007 D4 into one rule:
publication travels one edge, directives travel the whole chain. A
perspective assembles (CONTEXT.md § Perspective):

- The requested scope's own **summary** (directives and context — unchanged;
  a scope's own context still feeds its own judgments and its own choice of
  what to publish).
- Every inter-stratum ancestor's **directives** — full fidelity, full walk,
  root-first — never their context (ADR 0013 D1). A directive binds every
  descendant regardless of depth, so hiding one is a correctness hazard, not
  a privacy feature; context is the scope's own working memory and simply
  stops leaving the scope on its own.
- The **publication** of every scope one edge away from the requested scope:
  its immediate chain parent (``relation="parent_publication"``), and every
  scope it itself references via a reference edge
  (``relation="peer_reference"``) — never a grandparent's publication, and
  never a publication reached only through an ancestor's own reference edge
  (ADR 0013 D2/D3: publication travels exactly one edge, uniformly for chain
  and reference; each stratum is a curation checkpoint, not a pass-through).

Every layer carries ``binding`` — ``True`` for self/ancestor (directive)
layers, ``False`` for every publication layer, wherever it came from — the
honest discriminator ADR 0013's Consequences section names; ``relation``
keeps its existing labels for existing layer kinds and adds
``"parent_publication"`` for the one new layer kind this ADR introduces (a
chain edge's publication delivery is not a reference edge, so it does not
share ``peer_reference``'s label — that would misrepresent its provenance).

ADR 0008 (operator stratum mechanism, #91) lands here too, narrowed by ADR
0013 D5/D7: ``compose_perspective`` gains an optional ``operator_reader`` —
a callable, not a store object, so this module stays free of SQLite/record-
store machinery. For each chain scope (ancestors + self) that has at least
one DIRECTIVE-kind operator item, an operator layer — directives only, never
a ``"context"`` key — is inserted IMMEDIATELY ABOVE that scope's own layer
(ADR 0008 D2). A chain scope whose operator memory is entirely legacy
``context``-kind items (stored before ADR 0013, never rewritten — D7) gets
no operator layer at all: that memory stays on disk but stops composing.
Peer and extra-context layers never get an operator layer: operator memory
binds a *chain*, and a peer's chain is not this reader's to compose.

Vocabulary follows CONTEXT.md verbatim: scope, stratum, perspective, scope
summary, directive, context, chain edge, reference edge, peer reference,
publication, operator.
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
    """Verbatim item dict for a parent_publication/peer_reference layer's payload.

    Seam for republication provenance (ADR 0013 D4/D4b/D4c, owned by
    :mod:`strata.publication`/:mod:`strata.record_store`): once a
    ``PublishedItem`` carries an origin/relay-path field, this dict must
    include it — a relayed item's "according to X, via Y" attribution has to
    survive into the composed layer, or a reader can't tell a relay from an
    original. ``_PublishedItemLike`` above is the structural contract this
    function reads; widen it alongside ``PublishedItem`` when that field
    lands.
    """
    return {
        "id": item.id,
        "kind": item.kind,
        "content": item.content,
        "subject": item.subject,
        "anchors": list(item.anchors),
        "published_at": item.published_at,
    }


def _operator_directives(items: Sequence[_OperatorItemLike]) -> list[dict]:
    """Filter *items* to directive-kind items, as verbatim item dicts (ADR 0013 D5).

    A stored legacy ``context``-kind operator item (written before ADR 0013)
    stays on disk exactly as it is — it is never rewritten — but it stops
    composing here: the operator layer is directives only. Filtering at this
    serving boundary, not inside :func:`strata.operator.read_operator_layer`,
    is deliberate — that reader is also the read-modify-write path every
    operator write goes through, and filtering there would silently drop the
    stored item from the file on the next write (migration by stealth, the
    thing ADR 0013 D7 forbids).
    """

    def _item_dict(item: _OperatorItemLike) -> dict:
        return {
            "id": item.id,
            "content": item.content,
            "subject": item.subject,
            "created_at": item.created_at,
        }

    return [_item_dict(i) for i in items if i.kind == "directive"]


def _operator_layer(attachment_scope_id: str, directives: list[dict]) -> dict:
    """Build the operator layer dict for *attachment_scope_id* (ADR 0008 D2, ADR 0013 D5).

    Verbatim: item dicts carry exactly ``id``, ``content``, ``subject``,
    ``created_at`` — no rewriting, no summarisation. Directives only — the
    operator's layer collapsed to directives-only (ADR 0013 D5): there is no
    ``"context"`` key at all, honest about the operator having no non-binding
    channel any more. Never a scope summary shape either (an operator layer
    is never part of any scope's summary — ADR 0008 D2).
    """
    return {
        "scope_id": attachment_scope_id,
        "stratum_id": "operator",
        "relation": "operator",
        "binding": True,
        "operator_memory": {"directives": directives},
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
    """Compose *scope_id*'s perspective: own summary, ancestor directives, one-hop publications.

    A perspective assembles (CONTEXT.md § Perspective): the scope's own
    summary; the **directives** (never the context) of every inter-stratum
    ancestor up to the root, full walk, root-first; and the **publications**
    of the scopes exactly one edge away — the immediate chain parent, and
    every scope this scope itself references (never a grandparent's
    publication, and never one reached only through an ancestor's own
    reference edge — ADR 0013 D2/D3). Layers are ordered root-first:
    ancestor directive layers, then the requested scope's own layer, then
    the parent's publication layer (if any), then referenced-scope
    publication layers (sorted by scope id for deterministic ordering).

    Every layer carries ``relation`` (``"self"``, ``"ancestor"``,
    ``"parent_publication"``, or ``"peer_reference"``) and ``binding``
    (``True`` for self/ancestor layers, ``False`` for every publication
    layer, wherever it came from — ADR 0013's honest discriminator).
    Publication layers are non-binding at any stratum distance or edge type:
    a reference edge two strata up is exactly as non-binding as the
    immediate parent's own publication (ADR 0010 D2 extended by ADR 0013
    D2). References-of-references are not traversed, and an ancestor's own
    reference edges are not this scope's to compose — only ``scope_id``'s
    own outgoing reference edges count (one hop, per
    ``FleetConfig.references_from``).

    Publication layer payload (ADR 0013 D2/D3, superseding the ADR 0007 D4
    peer-summary fallback — there is exactly one composition rule now, no
    compatibility path):

    - When *publication_reader* is given, the parent's layer and each
      referenced-scope layer carry ``"publication": {"items": [<item dicts:
      id, kind, content, subject, anchors, published_at>]}`` — that scope's
      CURRENT published items, verbatim, and NO ``"summary"`` key. A source
      that has published nothing gets ``{"items": []}`` — the honestly empty
      face stays visible (composition is provenance-preserving even when
      empty: "you reference this scope; it publishes nothing" is itself
      information). Never the source's internal summary — publishing is a
      judged act distinct from internal acceptance (ADR 0007 D2), and
      composing raw internal memory into a reader who never judged it for
      export is exactly what this ADR ends for every one-hop edge, chain or
      reference alike.
    - When *publication_reader* is ``None``, no ``parent_publication`` or
      ``peer_reference`` layer is composed at all — there is no legacy
      full-summary fallback (ADR 0013 D7: one composition rule, not two
      running side by side).

    If a chain scope has no summary on disk yet, its own/ancestor layer is
    still included with an honestly empty payload so the structure stays
    visible; a self layer's synthesized summary reports ``version=0``/
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
        operator_reader: ADR 0008 D2, narrowed by ADR 0013 D5. When given,
            called once per chain scope (ancestors + self) with that scope's
            id; for each chain scope with at least one DIRECTIVE-kind
            operator item, an operator layer — ``{scope_id, stratum_id:
            "operator", relation: "operator", binding: True, operator_memory:
            {directives}}`` with VERBATIM item dicts, no ``"context"`` key —
            is inserted immediately above that chain scope's own layer. A
            scope whose operator memory is entirely legacy ``context``-kind
            items gets no operator layer. Peer and extra-context layers
            never get an operator layer. ``None`` (the default) composes
            zero operator layers.
        publication_reader: ADR 0013 D2/D3. When given, called once for the
            chain parent (if any) and once per scope this scope itself
            references, each producing a ``{"publication": {"items":
            [...]}}`` layer (see above). Does NOT affect
            ``extra_context_scopes`` layers, which always carry a full
            summary regardless — that parameter is a distinct,
            operator-sanctioned hosting surface (issue #83), never a
            publication-carrying edge. ``None`` (the default) composes zero
            publication layers.

    Returns:
        ``{scope_id: <requested>, layers: [{scope_id, stratum_id, relation,
        binding, summary | directives | publication | operator_memory}],
        _layers_count: N}`` ordered root-first: ancestor directive layers,
        self, the parent's publication layer (if any), sorted
        referenced-scope publication layers, then sorted extra-context
        layers. Self/extra-context layers carry ``"summary"``; ancestor
        layers carry ``"directives"`` (a list of directive dicts) and never
        ``"summary"`` or ``"context"``; publication layers carry
        ``"publication"``. When *operator_reader* is given, an operator
        layer (see above) precedes each chain layer that has at least one
        directive-kind operator item.

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
            directives = _operator_directives(operator_reader(s.id))
            if directives:
                # ADR 0008 D2: the operator layer sits immediately above its
                # attachment scope's own layer — inserted here, before the
                # chain scope's layer itself is appended below. ADR 0013 D5:
                # a scope whose operator memory is entirely legacy
                # context-kind items yields no directives, hence no layer.
                layers.append(_operator_layer(s.id, directives))
        if s.id == scope_id:
            # Self: unaffected by ADR 0013 — full summary, directives and
            # context alike, still feeds this scope's own judgments.
            layers.append(
                {
                    "scope_id": s.id,
                    "stratum_id": s.stratum_id,
                    "summary": summary_for_scope(s.id, summary_store=summary_store),
                    "relation": "self",
                    "binding": True,
                }
            )
        else:
            # Ancestor (ADR 0013 D1): directives only, full fidelity — never
            # this ancestor's context. No "summary" key at all in this shape.
            ancestor_summary = summary_for_scope(s.id, summary_store=summary_store)
            layers.append(
                {
                    "scope_id": s.id,
                    "stratum_id": s.stratum_id,
                    "directives": ancestor_summary["directives"],
                    "relation": "ancestor",
                    "binding": True,
                }
            )

    # ADR 0013 D2/D3: publication travels exactly one edge, uniformly for
    # chain and reference edges. The chain parent's publication (one hop via
    # the chain edge) composes first, then this scope's OWN references (one
    # hop via a reference edge) — never an ancestor's own reference, and
    # never a grandparent's publication.
    if publication_reader is not None:
        parent = fleet.inter_stratum_parent(scope_id)
        if parent is not None:
            parent_items = publication_reader(parent.id)
            layers.append(
                {
                    "scope_id": parent.id,
                    "stratum_id": parent.stratum_id,
                    "relation": "parent_publication",
                    "binding": False,
                    "publication": {"items": [_publication_item_dict(i) for i in parent_items]},
                }
            )

        for s in fleet.references_from(scope_id):
            items = publication_reader(s.id)
            layers.append(
                {
                    "scope_id": s.id,
                    "stratum_id": s.stratum_id,
                    "relation": "peer_reference",
                    "binding": False,
                    "publication": {"items": [_publication_item_dict(i) for i in items]},
                }
            )

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
