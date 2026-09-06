"""Fleet configuration — the in-memory mirror of ``fleet.yaml``.

This module is the single owner of ``fleet.yaml`` ↔ in-memory state under
ADR 0002. It loads, validates, and mutates fleet configuration, ensuring
``fleet.yaml`` is always the source of truth.

- :class:`FleetConfig` is the top-level model and public API.
- All mutations acquire an in-process lock, write atomically, and refresh
  in-memory state from the rendered output.
- Validation raises :class:`FleetConfigError` on the first failure; the
  ``kind`` attribute identifies which invariant was violated.

Edges come in exactly two kinds (ADR 0010): a **chain edge** binds a scope to
its single parent in the stratum immediately above, and a **reference edge**
links any scope pair at any stratum distance, carrying the referenced scope's
publication only. ``kind`` is optional in the file — an untyped edge is
inferred, adjacent ones per child scope rather than per edge (ADR 0010 D5) —
and ``load`` materializes the inferred kind and orients every chain edge
child→parent, so no edge that validates is meaningless and no fleet that
loaded before ADR 0010 stops loading after it.

Vocabulary follows CONTEXT.md verbatim: stratum, scope, edge, chain edge,
reference edge, peer reference, fleet.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class FleetConfigError(Exception):
    """Raised when a fleet.yaml invariant is violated.

    ``kind`` is a stable token identifying the invariant; ``message`` is
    human-readable and always names the offending item.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# ---------------------------------------------------------------------------
# Schema-error humanizing (issue #182)
#
# ``FleetConfig.model_validate`` raises pydantic's own ``ValidationError`` for
# shape problems (a missing field, a wrong type) — invariant checks in
# ``_validate`` below never see these, because the model does not even exist
# yet. Left uncaught, that error's ``str()`` dumps the pydantic class name, a
# dotted field path, a ``type=`` tag, and a link to pydantic's own error
# reference — none of it meaningful to someone editing fleet.yaml. This
# rewrites each underlying error into the same vocabulary the rest of this
# module speaks (stratum, scope, edge), using only ``ValidationError.errors()``
# — never the library's rendered ``str()`` — so no internal detail leaks
# through. Both ``strata bootstrap`` and the Console read the identical
# message, because both go through :meth:`FleetConfig.load` /
# :meth:`FleetConfig._commit`.
# ---------------------------------------------------------------------------

#: Section name in the raw YAML -> singular noun used in messages.
_SECTION_SINGULAR = {"strata": "stratum", "scopes": "scope", "edges": "edge"}


def _item_label(singular: str, index: int, item: object) -> str:
    """Return e.g. ``"scope #1 (id 'g_boss')"`` or ``"edge #3 ('g_a' -> 'g_b')"``.

    Falls back to a bare ``"{singular} #{n}"`` when the offending item isn't a
    dict, or carries none of the identifying fields (which happens when the
    identifying field is itself the one missing).
    """
    label = f"{singular} #{index + 1}"
    if not isinstance(item, dict):
        return label
    if singular == "edge":
        frm = item.get("from", item.get("from_"))
        to = item.get("to")
        if frm is not None and to is not None:
            return f"{label} ({frm!r} -> {to!r})"
        return label
    ident = item.get("id")
    return f"{label} (id {ident!r})" if ident is not None else label


def _describe_schema_error(err: dict, raw: object) -> str:
    """Render one entry of :meth:`ValidationError.errors` in fleet vocabulary.

    *raw* is the parsed YAML document the error came from — used to look up
    the offending item by section/index, since ``err["input"]`` is the whole
    item for a *missing*-field error but only the bad value itself for a
    wrong-type error, and only the former identifies the item.
    """
    loc = err["loc"]
    msg = err["msg"]
    if not loc:
        return msg
    section = loc[0] if isinstance(loc[0], str) else None
    singular = _SECTION_SINGULAR.get(section)
    if singular is None or len(loc) < 2 or not isinstance(loc[1], int):
        # Not a per-item field error (e.g. "scopes" itself absent or not a
        # list) — name the raw path, still without any pydantic internals.
        path = ".".join(str(p) for p in loc)
        return f"{path}: {msg}" if path else msg
    index = loc[1]
    item = None
    if isinstance(raw, dict):
        section_list = raw.get(section)
        if isinstance(section_list, list) and 0 <= index < len(section_list):
            item = section_list[index]
    label = _item_label(singular, index, item)
    if len(loc) == 2:
        # The item itself is malformed (e.g. not a mapping at all).
        return f"{label}: {msg}"
    field = loc[2]
    if err["type"] == "missing":
        return f"{label} is missing its {field}."
    return f"{label}: {field} — {msg}."


def _schema_error_to_fleet_config_error(exc: ValidationError, raw: object) -> FleetConfigError:
    """Convert a raw pydantic :class:`ValidationError` into a :class:`FleetConfigError`.

    ``kind`` is the single stable token ``"invalid_schema"`` — unlike the
    invariant checks below, a schema error is pydantic's own field-shape
    complaint rather than one of this module's named checks, so there is no
    finer-grained kind to report. ``message`` lists every underlying error
    (fleet.yaml can fail this in more than one place at once), each one
    naming the offending scope/stratum/edge and field.
    """
    message = " ".join(_describe_schema_error(e, raw) for e in exc.errors())
    return FleetConfigError(kind="invalid_schema", message=message)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class Stratum(BaseModel):
    """A horizontal layer of scopes."""

    id: str
    name: str
    ordinal: int


class Scope(BaseModel):
    """A bounded region of the fleet for which memory is relevant and authoritative."""

    id: str
    name: str
    stratum_id: str
    status: Literal["active", "archived"] = "active"
    default_skill: str | None = None
    permitted_skills: list[str] | None = None


#: The two kinds of edge a fleet may declare (ADR 0010). A **chain edge**
#: is the binding inter-stratum edge — adjacent strata, at most one per
#: scope, carrying the ancestor layers. A **reference edge** is the weak
#: link — any scope pair at any stratum distance, carrying the referenced
#: scope's publication only, never binding. See CONTEXT.md § Chain edge and
#: § Reference edge.
EdgeKind = Literal["chain", "reference"]


class Edge(BaseModel):
    """A directed link between two scopes.

    ``from_`` maps to the YAML key ``from`` (a Python keyword) via the alias.

    ``kind`` is optional in ``fleet.yaml``. An untyped edge on one stratum is
    a ``reference`` (the peer reference); one spanning two or more strata has
    no default at all and must be declared ``reference`` explicitly
    (ADR 0010 D4). An untyped edge between *adjacent* strata is a chain-edge
    candidate, and which candidate actually binds is decided per child scope,
    not per edge — see :func:`_resolve_edges` (ADR 0010 D5). A candidate that
    does not get the slot resolves to a ``reference``.
    :meth:`FleetConfig.load` materializes the resolved kind, so every edge on
    a loaded config carries an explicit ``kind``; on disk the key stays
    optional until a mutation makes a demotion explicit.

    For a chain edge, ``from_``/``to`` as authored say nothing: the parent is
    the lower-ordinal endpoint either way, and ``load`` canonicalizes the edge
    to child→parent. For a declared reference edge, direction is the whole
    meaning — ``from_`` references ``to``.
    """

    from_: Annotated[str, Field(alias="from")]
    to: str
    kind: EdgeKind | None = None

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Edge kind resolution (ADR 0010)
#
# Every question about an edge — is it binding, who is the parent, does it
# compose a publication — reduces to its kind plus the stratum ordinals of its
# endpoints. Resolution derives both from ordinals rather than trusting the
# authored direction, so it answers identically on a canonicalized config and
# on one built directly (``FleetConfig(...)`` skips validation).
#
# An untyped adjacent edge cannot be resolved on its own: whether it is this
# child's chain edge depends on what else points at the same child. Resolution
# is therefore per CHILD SCOPE, not per edge (ADR 0010 D5, "load-lenient,
# write-strict") — that is what keeps a fleet that loaded before ADR 0010
# loading after it.
# ---------------------------------------------------------------------------


def _scope_ordinals(config: FleetConfig) -> dict[str, int]:
    """Map each scope id in *config* to its stratum's ordinal.

    Scopes whose ``stratum_id`` names no declared stratum are omitted — an
    unvalidated config may contain them, and every caller here treats a
    missing ordinal as "this edge derives nothing."
    """
    ordinals = {s.id: s.ordinal for s in config.strata}
    return {s.id: ordinals[s.stratum_id] for s in config.scopes if s.stratum_id in ordinals}


def _chain_endpoints(edge: Edge, ordinals: dict[str, int]) -> tuple[str, str]:
    """Return *edge*'s ``(child, parent)`` scope ids, regardless of how it was authored.

    The parent of a chain edge is its lower-ordinal endpoint — ordinal 0 is
    the broadest stratum (ADR 0002) — so direction carries no information and
    an inverted edge means exactly what a correctly authored one means
    (issue #123). Only meaningful for an edge between adjacent strata.
    """
    if ordinals[edge.to] < ordinals[edge.from_]:
        return edge.from_, edge.to
    return edge.to, edge.from_


@dataclass(frozen=True)
class _ResolvedEdge:
    """One edge's effective meaning, once the whole fleet has been considered.

    ``kind`` is ``None`` only when the edge derives nothing at all — an
    endpoint with no ordinal, or an untyped edge spanning two or more strata
    (which :func:`_validate` refuses outright).

    ``from_``/``to`` are the *effective* endpoints: a chain edge always runs
    child→parent, and so does a demoted edge (see ``demoted``). Every other
    edge keeps the direction it was authored with, because for a reference
    edge direction is the whole meaning.

    ``demoted`` marks an untyped adjacent edge that did not get its child's
    chain slot and resolved to a reference instead. The write path
    materializes ``kind: reference`` for exactly these, so a mutated file
    stops depending on the resolution rules and says what it means.
    """

    kind: EdgeKind | None
    from_: str
    to: str
    demoted: bool


def _resolve_edges(config: FleetConfig) -> list[_ResolvedEdge]:
    """Resolve every edge's effective kind and direction, in ``config.edges`` order.

    Declared kinds are honoured as declared. Untyped edges are inferred, and
    the adjacent ones are inferred **per child scope** rather than per edge
    (ADR 0010 D5):

    1. An untyped adjacent edge authored upward (``from`` is the child) takes
       that child's chain slot. This is precisely what pre-ADR-0010 loads
       honoured, so a fleet that loaded then still loads now.
    2. An untyped adjacent edge authored inverted — inert before ADR 0010 —
       promotes into the chain slot only when the slot is free and it is the
       sole candidate for that child (the issue #123 fix).
    3. Any other inverted candidate demotes to a reference. Two candidates
       and an empty slot means both demote: the engine never guesses which
       one the author meant, so the child stays parentless exactly as it was
       before the upgrade, while both edges still deliver a publication —
       strictly more than the inert edge gave, and never wrong.

    A demoted edge keeps the direction of flow the author drew, at
    non-binding strength: the would-be child references the would-be parent,
    so it reads that scope's publication. Reversing it to the authored
    ``from``→``to`` would invert the flow the author asked for.
    """
    ordinals = _scope_ordinals(config)
    resolved: list[_ResolvedEdge | None] = [None] * len(config.edges)

    # Pass 1 — everything decidable from one edge alone. What is left over is
    # the untyped adjacent edges, collected per child for pass 2.
    declared_chain_children: set[str] = set()
    candidates: dict[str, list[int]] = {}
    for i, edge in enumerate(config.edges):
        from_ordinal = ordinals.get(edge.from_)
        to_ordinal = ordinals.get(edge.to)
        if from_ordinal is None or to_ordinal is None:
            resolved[i] = _ResolvedEdge(None, edge.from_, edge.to, False)
            continue
        distance = abs(from_ordinal - to_ordinal)
        if edge.kind == "chain":
            child, parent = _chain_endpoints(edge, ordinals)
            declared_chain_children.add(child)
            resolved[i] = _ResolvedEdge("chain", child, parent, False)
        elif edge.kind == "reference" or distance == 0:
            resolved[i] = _ResolvedEdge("reference", edge.from_, edge.to, False)
        elif distance == 1:
            child, _parent = _chain_endpoints(edge, ordinals)
            candidates.setdefault(child, []).append(i)
        else:
            resolved[i] = _ResolvedEdge(None, edge.from_, edge.to, False)

    # Pass 2 — the untyped adjacent edges, decided per child.
    for child, indices in candidates.items():
        upward = {i for i in indices if config.edges[i].from_ == child}
        inverted = [i for i in indices if i not in upward]
        slot_free = not upward and child not in declared_chain_children
        promoted = inverted[0] if slot_free and len(inverted) == 1 else None
        for i in indices:
            child_id, parent_id = _chain_endpoints(config.edges[i], ordinals)
            if i in upward or i == promoted:
                resolved[i] = _ResolvedEdge("chain", child_id, parent_id, False)
            else:
                resolved[i] = _ResolvedEdge("reference", child_id, parent_id, True)

    # Both passes together assign every index, so this narrows the type rather
    # than filtering anything out. If a future branch ever left one unassigned,
    # the shortened list would trip every caller's ``zip(..., strict=True)``.
    return [r for r in resolved if r is not None]


def _canonicalize(config: FleetConfig) -> None:
    """Materialize every edge's resolved kind and direction on the in-memory model.

    Applied by :meth:`FleetConfig.load` after validation so consumers read one
    shape: ``kind`` is always set, and a chain edge's ``from_`` is always the
    child. Edges that resolve to nothing are left untouched for the caller
    that built the config without validating it.
    """
    for edge, resolution in zip(config.edges, _resolve_edges(config), strict=True):
        if resolution.kind is None:
            continue
        edge.kind = resolution.kind
        edge.from_, edge.to = resolution.from_, resolution.to


def _canonicalize_raw_edges(config: FleetConfig, raw_edges: list) -> None:
    """Write each edge's resolved direction — and any demoted kind — into the raw entries.

    Every mutation runs this before writing, so an inverted chain edge is
    corrected on disk the first time anything else in the file changes and
    ``fleet.yaml`` never drifts from what the loaded config means. A demoted
    edge additionally gains an explicit ``kind: reference``, so the file stops
    relying on the per-child resolution rules to mean what it means and
    converges on a self-describing form (ADR 0010 D5). An inferred kind that
    was not demoted stays inferred on disk.
    """
    for resolution, entry in zip(_resolve_edges(config), raw_edges, strict=True):
        if not isinstance(entry, dict) or resolution.kind is None:
            continue
        entry["from" if "from" in entry else "from_"] = resolution.from_
        entry["to"] = resolution.to
        if resolution.demoted:
            entry["kind"] = "reference"


@dataclass(frozen=True)
class EntitlementView:
    """A scope's entitlement surface, derived from ``fleet.yaml`` (ADR 0006 D2).

    Groups every scope in the fleet relative to one judged scope:

    - ``chain`` — the scope itself plus its inter-stratum ancestors (root
      first, scope last). Entitled for both directives and context — this is
      the binding surface.
    - ``descendants`` — every active scope below the judged scope (its
      authority region), any depth. Entitled: evidence proposed upward from
      below is the normal, legitimate inflow the scope-manager exists to
      judge — the evidence→ratification channel (philosophy Concept 3), and
      ADR 0006 D1 permits exactly these agents to write here. Without this
      group the rendered ENTITLEMENT block would instruct the judge to
      decline the very flow D1 legitimizes.
    - ``referenced_peers`` — active scopes referenced one hop away via a
      **reference edge** from any scope on ``chain``. Since ADR 0010 this is
      every stratum distance, not only the same-stratum peer reference: an
      upward reference to a non-parent scope and a downward reference deliver
      exactly the same capacity, so they group together. Entitled for context
      only, never a directive at the contributor's request (CONTEXT.md
      § Reference edge). No transitive reference-of-reference traversal —
      only edges whose source is itself on ``chain`` count, and direction is
      load-bearing: an edge *into* a chain scope references nothing outward.
      A scope that is both referenced and a descendant appears in **both**
      groups: each names a real, entitled relationship, and dropping either
      would cost the judge information it uses (the reference is the read
      capacity, the descendancy is the upward-evidence capacity).
    - ``others`` — every remaining scope in the fleet, **including archived
      scopes** (archived chain members excepted — the chain is structural).
      The judge distinguishes fleet-internal origins from external material
      by exact name matching against this enumeration; an archived scope
      that vanished from the list would read as external and slip past the
      admission rule. Not entitled: material substantively originating from
      these scopes must not enter the judged scope.

    This is the single source of truth for entitlement grouping (ROADMAP
    principle 8): :mod:`strata.scope_manager` renders it into the judge's
    user message, and ADR 0006 D3's peer-reference composition reuses the
    same grouping.
    """

    chain: list[Scope]
    descendants: list[Scope]
    referenced_peers: list[Scope]
    others: list[Scope]


class FleetConfig(BaseModel):
    """The complete fleet definition loaded from a YAML file.

    Instantiate via :meth:`FleetConfig.load` — the classmethod validates all
    load-time invariants: the original 8 from ADR 0002 (invariant 7 restated
    per edge kind by ADR 0010), ADR 0004's at-most-one-parent invariant (now
    counted on the effective relationship), and ADR 0008's reserved
    ``"operator"`` stratum label — and then canonicalizes the edges.  Direct
    construction skips both; prefer ``load`` in production code.
    """

    strata: list[Stratum]
    scopes: list[Scope]
    edges: list[Edge]

    # File path and lock are set by ``load``; not part of the schema.
    _path: Path | None = None
    _lock: threading.Lock | None = None

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> FleetConfig:
        """Parse *path*, validate all load-time invariants, canonicalize the
        edges, and return a :class:`FleetConfig`.

        Canonicalization (ADR 0010 D3) is in-memory only — reading a fleet
        never rewrites the file, and the engine re-reads ``fleet.yaml`` on
        every tool call (ADR 0004 D1). The file catches up the next time
        something mutates it through the mutation API.

        Args:
            path: Path to a ``fleet.yaml`` file.

        Returns:
            Validated :class:`FleetConfig` with ``_path`` and ``_lock`` set,
            every edge carrying an explicit ``kind``, and every chain edge
            oriented child→parent.

        Raises:
            FileNotFoundError: If *path* does not exist.
            FleetConfigError:  On the first invariant violation; ``kind`` names
                the check, ``message`` names the offending item.
        """
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            config = cls.model_validate(raw)
        except ValidationError as exc:
            raise _schema_error_to_fleet_config_error(exc, raw) from exc
        _validate(config)
        _canonicalize(config)
        object.__setattr__(config, "_path", path)
        object.__setattr__(config, "_lock", threading.Lock())
        return config

    # ------------------------------------------------------------------
    # Convenience queries
    # ------------------------------------------------------------------

    def get_scope(self, scope_id: str) -> Scope | None:
        """Return the scope with *scope_id*, or ``None``."""
        return next((s for s in self.scopes if s.id == scope_id), None)

    def active_scopes(self) -> list[Scope]:
        """Return only scopes with ``status == 'active'``."""
        return [s for s in self.scopes if s.status == "active"]

    def auto_bind_scope(self) -> Scope | None:
        """Return the fleet's sole active scope, or ``None`` when auto-binding
        does not apply (zero or 2+ active scopes).

        Single-scope auto-binding (operator directive: a fresh install must
        work with minimum friction — the seeded fleet has one scope, so an
        unset ``STRATA_AGENT_SCOPE`` resolves to it automatically). Uses
        ``active_scopes()`` deliberately, not ``self.scopes``: an archived
        scope sitting alongside one active scope must still auto-bind — an
        archived scope was never a candidate binding target anyway.

        This is the single source of truth for the auto-bind rule; every
        call site (MCP server validation, the freshness evaluator, `strata
        doctor`, `strata launch`, `strata register`'s next-steps output)
        goes through this method so the decision can never drift between
        them.
        """
        active = self.active_scopes()
        if len(active) == 1:
            return active[0]
        return None

    def inter_stratum_parent(self, scope_id: str) -> Scope | None:
        """Return the single inter-stratum parent of *scope_id*, or ``None`` for root scopes.

        The parent comes from *scope_id*'s **chain edge** — the one edge that
        binds it to the stratum immediately above. A chain edge's parent is
        its lower-ordinal endpoint (per ADR 0002, ordinal 0 is the broadest
        stratum) whichever way the author wrote it: :meth:`load` canonicalizes
        the orientation and this walk re-derives it from the ordinals, so a
        top-down authored fleet resolves identically to a bottom-up one
        (issue #123 — an inverted edge used to derive nothing at all).

        Reference edges are never followed here, at any stratum distance: a
        reference delivers the referenced scope's publication, never ancestry.
        """
        scope_map = {s.id: s for s in self.scopes}

        if scope_id not in scope_map:
            return None

        parent_id = self._chain_parent_ids().get(scope_id)
        return scope_map.get(parent_id) if parent_id is not None else None

    def _chain_parent_ids(self) -> dict[str, str]:
        """Map each scope id to its chain parent's id, from one resolution pass.

        Callers that walk many chains — the ancestor walk, and
        :meth:`entitlement_view`'s descendant scan — build this once instead of
        re-resolving every edge at every hop, which is what would otherwise
        make those walks quadratic in fleet size. First chain edge wins, so an
        unvalidated config with two parents resolves the same way the
        edge-order scan did.
        """
        parents: dict[str, str] = {}
        for resolution in _resolve_edges(self):
            if resolution.kind == "chain":
                parents.setdefault(resolution.from_, resolution.to)
        return parents

    def inter_stratum_ancestors(self, scope_id: str) -> list[Scope]:
        """Return the ancestor chain from root (L0) down to *scope_id*'s parent.

        Follows chain edges only. Returns an empty list when *scope_id* is a
        root scope (no chain edge to a parent).  The requested scope itself is
        NOT included — callers append it.
        """
        scope_map = {s.id: s for s in self.scopes}
        parents = self._chain_parent_ids()

        ancestors: list[Scope] = []
        # A chain edge's parent always sits on a strictly lower ordinal, so a
        # validated fleet cannot loop; the seen set keeps the walk total on a
        # config built without validation.
        seen: set[str] = {scope_id}
        current_id = scope_id
        while (parent_id := parents.get(current_id)) is not None and parent_id not in seen:
            parent = scope_map.get(parent_id)
            if parent is None:
                break
            ancestors.append(parent)
            seen.add(parent_id)
            current_id = parent_id
        # Chain is built deepest-first; reverse to get root-first order.
        ancestors.reverse()
        return ancestors

    def entitlement_view(self, scope_id: str) -> EntitlementView:
        """Compute *scope_id*'s entitlement surface (ADR 0006 D2).

        Args:
            scope_id: The scope the view is relative to (the scope about to
                be judged).

        Returns:
            An :class:`EntitlementView` grouping the fleet's scopes into
            ``chain`` (this scope + inter-stratum ancestors), ``descendants``
            (active scopes below this scope — entitled upward-evidence
            sources), ``referenced_peers`` (one hop via reference edges from
            any chain scope, at any stratum distance), and ``others``
            (everything else, archived scopes included).
        """
        scope_map = {s.id: s for s in self.scopes}

        scope = scope_map.get(scope_id)
        ancestors = self.inter_stratum_ancestors(scope_id)
        chain = [*ancestors, *([scope] if scope is not None else [])]
        chain_ids = {s.id for s in chain}

        # Descendants: every active scope whose own ancestor chain passes
        # through the judged scope (any depth). These are the agents ADR 0006
        # D1 permits to propose upward into this scope, so the judge must see
        # them as entitled evidence sources, never as foreign material.
        # One shared parent map, walked per candidate — re-deriving each
        # candidate's full ancestor list here would re-resolve every edge once
        # per hop, per scope in the fleet.
        parents = self._chain_parent_ids()
        descendant_ids: set[str] = set()
        descendants: list[Scope] = []
        for candidate in self.scopes:
            if candidate.id in chain_ids or candidate.status != "active":
                continue
            walked: set[str] = {candidate.id}
            cursor = parents.get(candidate.id)
            while cursor is not None and cursor not in walked:
                if cursor == scope_id:
                    descendant_ids.add(candidate.id)
                    descendants.append(candidate)
                    break
                walked.add(cursor)
                cursor = parents.get(cursor)

        # Reference edges out of any chain scope, one hop (ADR 0010 D2). Every
        # stratum distance counts, not only the same-stratum peer reference:
        # an upward reference to a non-parent scope and a downward reference
        # deliver the identical publication-only, non-binding capacity, so
        # they group together. A scope already on the chain is skipped — an
        # ancestor is entitled more strongly than any reference could make it.
        referenced_peer_ids: list[str] = []
        seen: set[str] = set()
        for resolution in _resolve_edges(self):
            if resolution.kind != "reference":
                continue
            if resolution.from_ not in chain_ids or resolution.to in chain_ids:
                continue
            if resolution.to in seen:
                continue
            target_scope = scope_map.get(resolution.to)
            if target_scope is None or target_scope.status != "active":
                continue
            seen.add(resolution.to)
            referenced_peer_ids.append(resolution.to)

        referenced_peers = [scope_map[sid] for sid in referenced_peer_ids]

        # Everything else — including archived scopes, so every fleet name
        # the judge might meet in prose appears in exactly one group and an
        # archived origin cannot masquerade as external material.
        others = [
            s
            for s in self.scopes
            if s.id not in chain_ids and s.id not in seen and s.id not in descendant_ids
        ]

        return EntitlementView(
            chain=chain,
            descendants=descendants,
            referenced_peers=referenced_peers,
            others=others,
        )

    def references_from(self, scope_id: str) -> list[Scope]:
        """Return the active scopes *scope_id* itself references, one hop (ADR 0013 D3).

        Publication travels exactly one edge, uniformly for chain and
        reference edges: a perspective's referenced-scope layers come from
        the requested scope's OWN reference edges only, never an ancestor's
        — an ancestor's own reference is that ancestor's business to relay
        or not (curation checkpoint), not something a further descendant
        receives for free. This is deliberately narrower than
        :meth:`entitlement_view`'s ``referenced_peers`` (ADR 0006 D2 judge
        entitlement, chain-wide, unchanged by this ADR): a relay's origin
        must stay a legitimate fleet-internal name for the judge even after
        this scope stops composing that origin's publication directly.

        Args:
            scope_id: The scope whose own outgoing reference edges to
                resolve. Not validated against the fleet — an unknown id
                simply has no outgoing edges to resolve.

        Returns:
            Active target scopes, sorted by scope id for deterministic
            order. An archived target, or a scope with no reference edges of
            its own, yields an empty list.
        """
        scope_map = {s.id: s for s in self.scopes}
        ids: list[str] = []
        seen: set[str] = set()
        for resolution in _resolve_edges(self):
            if resolution.kind != "reference" or resolution.from_ != scope_id:
                continue
            if resolution.to in seen:
                continue
            target = scope_map.get(resolution.to)
            if target is None or target.status != "active":
                continue
            seen.add(resolution.to)
            ids.append(resolution.to)
        return sorted((scope_map[sid] for sid in ids), key=lambda s: s.id)

    # ------------------------------------------------------------------
    # Affected-set traversal (ADR 0014 D3)
    #
    # The three walks the topological affected set is built from: a
    # publication change reaches the source's chain children and the scopes
    # whose reference edge points AT the source; a directive change reaches
    # the holding scope's chain descendants. One rule for an addition, an
    # amendment and a withdrawal alike — there is no presented index and no
    # fallback (ADR 0014 D3's rejected alternative).
    # ------------------------------------------------------------------

    def chain_children(self, scope_id: str) -> list[Scope]:
        """Return the active scopes whose chain parent is *scope_id*, one hop.

        The publication half of ADR 0014 D3's rule: a scope's publication
        composes into its chain children (ADR 0013 D2/D3 — publication
        travels exactly one edge), so those are the scopes a change to it
        can invalidate. Grandchildren are NOT children: they receive the
        source's face only if the child relays it, which is the child's own
        publication act and mints its own change.

        Args:
            scope_id: The parent scope. Not validated against the fleet — an
                unknown id simply has no children.

        Returns:
            Active child scopes, sorted by scope id. Archived children are
            excluded: an archived scope has no judge to wake.
        """
        scope_map = {s.id: s for s in self.scopes}
        parents = self._chain_parent_ids()
        children = [
            scope_map[child_id]
            for child_id, parent_id in parents.items()
            if parent_id == scope_id and child_id in scope_map
        ]
        return sorted((c for c in children if c.status == "active"), key=lambda s: s.id)

    def chain_descendants(self, scope_id: str) -> list[Scope]:
        """Return every active scope below *scope_id* on chain edges, at any depth.

        The directive half of ADR 0014 D3's rule: a directive binds the
        holding scope's whole subtree, so a directive appended, superseded or
        retired at *scope_id* changes what every descendant's judge would be
        shown. Reference edges are never followed — a reference delivers
        publication, never ancestry (:meth:`inter_stratum_parent`).

        *scope_id* itself is NOT included; a scope's own directive change is
        its own act, and the callers that DO want the scope included (an
        operator directive attached at S binds S and its subtree, ADR 0008
        D2) add it themselves.

        Args:
            scope_id: The holding scope.

        Returns:
            Active descendant scopes, sorted by scope id.
        """
        # One shared parent map, walked per candidate — the same shape
        # entitlement_view's descendant scan uses, for the same reason:
        # re-deriving each candidate's ancestry would re-resolve every edge
        # once per hop, per scope.
        parents = self._chain_parent_ids()
        descendants: list[Scope] = []
        for candidate in self.scopes:
            if candidate.id == scope_id or candidate.status != "active":
                continue
            # A validated fleet's chain edges cannot loop (a parent sits on a
            # strictly lower ordinal); the walked set keeps this total on a
            # config built without validation.
            walked: set[str] = {candidate.id}
            cursor = parents.get(candidate.id)
            while cursor is not None and cursor not in walked:
                if cursor == scope_id:
                    descendants.append(candidate)
                    break
                walked.add(cursor)
                cursor = parents.get(cursor)
        return sorted(descendants, key=lambda s: s.id)

    def referenced_by(self, scope_id: str) -> list[Scope]:
        """Return the active scopes that reference *scope_id*, one hop — the readers.

        The exact inverse of :meth:`references_from`: that method answers
        "whose publications do I read", this one answers "who reads mine",
        which is what ADR 0014 D3 needs to name the scopes a change to
        *scope_id*'s publication affects. Same one-hop, own-edges-only rule
        on both sides, so ``B in fleet.references_from(A)`` holds exactly
        when ``A in fleet.referenced_by(B)``.

        Args:
            scope_id: The referenced scope whose readers to resolve. Not
                validated against the fleet.

        Returns:
            Active reader scopes, sorted by scope id. An archived reader is
            excluded — it has no judge to wake.
        """
        scope_map = {s.id: s for s in self.scopes}
        ids: list[str] = []
        seen: set[str] = set()
        for resolution in _resolve_edges(self):
            if resolution.kind != "reference" or resolution.to != scope_id:
                continue
            if resolution.from_ in seen:
                continue
            reader = scope_map.get(resolution.from_)
            if reader is None or reader.status != "active":
                continue
            seen.add(resolution.from_)
            ids.append(resolution.from_)
        return sorted((scope_map[sid] for sid in ids), key=lambda s: s.id)

    # ------------------------------------------------------------------
    # Mutation API
    # ------------------------------------------------------------------

    def _commit(self, raw: dict) -> None:
        """Validate *raw*, canonicalize its edges, write it, and refresh in-memory state.

        The shared tail of every mutation: nothing touches disk until the
        candidate validates, the edges written out are canonical (ADR 0010 D3
        — chain edges oriented child→parent), and the in-memory mirror is
        rebuilt from what was actually written rather than from the candidate.
        Callers hold ``self._lock``.
        """
        assert self._path is not None
        try:
            candidate = FleetConfig.model_validate(raw)
        except ValidationError as exc:
            raise _schema_error_to_fleet_config_error(exc, raw) from exc
        _validate(candidate)
        _canonicalize_raw_edges(candidate, raw["edges"])
        _atomic_write(self._path, raw)
        refreshed = FleetConfig.model_validate(
            yaml.safe_load(self._path.read_text(encoding="utf-8"))
        )
        _canonicalize(refreshed)
        self.__dict__.update(refreshed.__dict__)

    def add_stratum(self, *, id: str, name: str, ordinal: int) -> None:
        """Add a new stratum to the fleet config and persist to disk.

        Raises:
            FleetConfigError: If the ID or ordinal duplicates an existing
                stratum (invariants 1 and 3).
        """
        assert self._path is not None and self._lock is not None
        with self._lock:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            raw.setdefault("strata", [])
            raw.setdefault("scopes", [])
            raw.setdefault("edges", [])
            raw["strata"].append({"id": id, "name": name, "ordinal": ordinal})
            self._commit(raw)

    def add_scope(
        self,
        *,
        id: str,
        name: str,
        stratum_id: str,
        status: Literal["active", "archived"] = "active",
        default_skill: str | None = None,
        permitted_skills: list[str] | None = None,
    ) -> None:
        """Add a new scope and persist to disk.

        Raises:
            FleetConfigError: On duplicate ID (invariant 2) or unknown
                stratum (invariant 4) or skill drift (invariant 8).
        """
        assert self._path is not None and self._lock is not None
        with self._lock:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            raw.setdefault("strata", [])
            raw.setdefault("scopes", [])
            raw.setdefault("edges", [])
            entry: dict = {"id": id, "name": name, "stratum_id": stratum_id}
            if status != "active":
                entry["status"] = status
            if default_skill is not None:
                entry["default_skill"] = default_skill
            if permitted_skills is not None:
                entry["permitted_skills"] = permitted_skills
            raw["scopes"].append(entry)
            self._commit(raw)

    def add_edge(
        self,
        *,
        from_scope_id: str,
        to_scope_id: str,
        kind: EdgeKind | None = None,
    ) -> None:
        """Add a directed edge and persist to disk.

        *kind* declares the edge as a ``chain`` or ``reference`` edge
        (ADR 0010); omitted, it is inferred from the stratum distance the same
        way a hand-authored untyped edge is. A chain edge is written out
        child→parent whichever way it was passed in, so the argument order
        cannot produce an inert edge (issue #123).

        Raises:
            FleetConfigError: On self-loop (invariant 6), an edge kind the
                stratum distance cannot carry (invariant 7), a second chain
                edge to a parent (invariant 9), or unknown scope
                (invariant 5).
        """
        assert self._path is not None and self._lock is not None
        with self._lock:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            raw.setdefault("strata", [])
            raw.setdefault("scopes", [])
            raw.setdefault("edges", [])
            entry: dict = {"from": from_scope_id, "to": to_scope_id}
            if kind is not None:
                entry["kind"] = kind
            raw["edges"].append(entry)
            self._commit(raw)

    def archive_scope(self, scope_id: str) -> None:
        """Set ``status: archived`` on *scope_id* and persist to disk.

        Raises:
            FleetConfigError: If *scope_id* does not exist.
        """
        assert self._path is not None and self._lock is not None
        with self._lock:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
            matched = False
            for entry in raw.get("scopes", []):
                if entry.get("id") == scope_id:
                    entry["status"] = "archived"
                    matched = True
                    break
            if not matched:
                raise FleetConfigError(
                    kind="scope_not_found",
                    message=f"Scope {scope_id!r} not found in fleet config.",
                )
            self._commit(raw)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate(config: FleetConfig) -> None:
    """Validate all load-time invariants from ADR 0002 (8 original), ADR 0004 (1 new),
    ADR 0008 (1 new — reserved stratum label), and ADR 0010 (invariants 7 and 9
    restated per edge kind, and checked against the resolved kinds rather than
    the authored ones).

    Load-lenient, write-strict (ADR 0010 D5): invariant 9 fires only where the
    fleet declares two parents outright, because per-child resolution has
    already demoted every untyped candidate that could not have the slot.

    Pure: raises :class:`FleetConfigError` on the first failure and never
    mutates *config*. Canonicalization is :func:`_canonicalize`'s job, applied
    after validation passes.
    """
    # 0. Reserved stratum label (ADR 0008 D2/Consequences): "operator" (any
    # case) is the implicit stratum's reserved label in layer provenance —
    # fleet.yaml never declares it, and a fleet stratum may not claim it. The
    # operator is not a region of the fleet (ADR 0008 "Alternatives
    # Considered" — a real fleet.yaml stratum/scope for the operator was
    # rejected), so a stratum claiming this id would collide with the
    # operator layer's own ``stratum_id: "operator"`` in composed
    # perspectives.
    for stratum in config.strata:
        if stratum.id.lower() == "operator":
            raise FleetConfigError(
                kind="reserved_stratum_id",
                message=(
                    f"Stratum {stratum.id!r} claims the reserved 'operator' label "
                    "(case-insensitive) — reserved for the implicit operator stratum "
                    "in composed perspectives (ADR 0008 D2/Consequences). Choose a "
                    "different stratum id."
                ),
            )

    # 1. Duplicate stratum IDs.
    stratum_ids: list[str] = [s.id for s in config.strata]
    seen: set[str] = set()
    for sid in stratum_ids:
        if sid in seen:
            raise FleetConfigError(
                kind="duplicate_stratum_id",
                message=f"Duplicate stratum ID: {sid!r}.",
            )
        seen.add(sid)

    # 2. Duplicate scope IDs.
    seen = set()
    for scope in config.scopes:
        if scope.id in seen:
            raise FleetConfigError(
                kind="duplicate_scope_id",
                message=f"Duplicate scope ID: {scope.id!r}.",
            )
        seen.add(scope.id)

    # 3. Duplicate stratum ordinals.
    seen_ordinals: set[int] = set()
    for stratum in config.strata:
        if stratum.ordinal in seen_ordinals:
            raise FleetConfigError(
                kind="duplicate_stratum_ordinal",
                message=f"Duplicate stratum ordinal {stratum.ordinal} on stratum {stratum.id!r}.",
            )
        seen_ordinals.add(stratum.ordinal)

    # Build lookup maps for subsequent checks.
    stratum_map: dict[str, Stratum] = {s.id: s for s in config.strata}
    scope_map: dict[str, Scope] = {s.id: s for s in config.scopes}

    # 4. Scope stratum_id references a defined stratum.
    for scope in config.scopes:
        if scope.stratum_id not in stratum_map:
            raise FleetConfigError(
                kind="unknown_stratum_ref",
                message=(
                    f"Scope {scope.id!r} references stratum {scope.stratum_id!r} "
                    "which is not defined in fleet.yaml."
                ),
            )

    # 5. Edge endpoints reference defined scopes.
    for edge in config.edges:
        if edge.from_ not in scope_map:
            raise FleetConfigError(
                kind="unknown_scope_ref",
                message=(f"Edge from={edge.from_!r} references a scope not defined in fleet.yaml."),
            )
        if edge.to not in scope_map:
            raise FleetConfigError(
                kind="unknown_scope_ref",
                message=(f"Edge to={edge.to!r} references a scope not defined in fleet.yaml."),
            )

    # 6. No self-loops.
    for edge in config.edges:
        if edge.from_ == edge.to:
            raise FleetConfigError(
                kind="self_loop",
                message=f"Self-loop forbidden: scope {edge.from_!r} references itself.",
            )

    # 7. Every edge's kind must be one the stratum distance can carry
    #    (ADR 0010 D1/D4). A chain edge binds adjacent strata and nothing
    #    else; a reference edge spans any distance. An untyped edge is
    #    inferred — same stratum → reference, adjacent → chain or, when it
    #    loses the contest for its child's chain slot, reference (ADR 0010 D5)
    #    — and a wider distance has no default, so the author is told which
    #    kind to declare rather than handed an edge that derives nothing
    #    (issue #123).
    ordinals = _scope_ordinals(config)
    for edge in config.edges:
        from_ordinal = ordinals[edge.from_]
        to_ordinal = ordinals[edge.to]
        distance = abs(from_ordinal - to_ordinal)
        if edge.kind == "chain" and distance != 1:
            raise FleetConfigError(
                kind="chain_edge_not_adjacent",
                message=(
                    f"Edge from {edge.from_!r} (ordinal {from_ordinal}) "
                    f"to {edge.to!r} (ordinal {to_ordinal}) declares kind: chain but "
                    f"spans {distance} strata; a chain edge is legal only between "
                    "adjacent strata. Declare it kind: reference to compose the "
                    "referenced scope's publication instead."
                ),
            )
        if edge.kind is None and distance > 1:
            raise FleetConfigError(
                kind="stratum_distance_violation",
                message=(
                    f"Edge from {edge.from_!r} (ordinal {from_ordinal}) "
                    f"to {edge.to!r} (ordinal {to_ordinal}) spans {distance} strata "
                    "and declares no kind; an untyped edge defaults to a chain edge "
                    "(adjacent strata) or a peer reference (same stratum), and neither "
                    "spans this distance. Declare it kind: reference to compose the "
                    "referenced scope's publication."
                ),
            )

    # 8. default_skill must be in permitted_skills when both are set.
    for scope in config.scopes:
        if (
            scope.default_skill is not None
            and scope.permitted_skills is not None
            and scope.default_skill not in scope.permitted_skills
        ):
            raise FleetConfigError(
                kind="skill_drift",
                message=(
                    f"Scope {scope.id!r}: default_skill {scope.default_skill!r} "
                    f"is not in permitted_skills {scope.permitted_skills!r}."
                ),
            )

    # 9. Each scope may have at most one chain edge to a parent. Multiple such
    #    edges would create ambiguity about which scope carries the
    #    authoritative parent perspective (ADR 0004 D4). Counted on the
    #    RESOLVED relationship, so an inverted edge counts exactly like a
    #    correctly authored one (issue #123).
    #
    #    Reaching two here means the fleet says two things at once where the
    #    author declared intent, because per-child resolution (ADR 0010 D5)
    #    already demoted every untyped edge that could not have the slot.
    #    What is left is a declared `kind: chain` colliding with another
    #    parent edge, or two untyped edges both authored upward — the shape
    #    that failed this same check before ADR 0010, so refusing it breaks
    #    nothing that used to load. The error names both offending edges as
    #    written, so the author can find them.
    chain_parent_edges: dict[str, list[Edge]] = {}
    for edge, resolution in zip(config.edges, _resolve_edges(config), strict=True):
        if resolution.kind != "chain":
            continue
        chain_parent_edges.setdefault(resolution.from_, []).append(edge)
    for scope_id, edges in chain_parent_edges.items():
        if len(edges) > 1:
            listed = ", ".join(f"{e.from_} -> {e.to}" for e in edges)
            raise FleetConfigError(
                kind="multiple_inter_stratum_parents",
                message=(
                    f"Scope {scope_id!r} has {len(edges)} chain edges to a parent "
                    f"({listed}); each scope may have at most one. Authored direction "
                    "does not change this — a chain edge's parent is its lower-ordinal "
                    "endpoint whichever way it is written. Declare the edges that "
                    "should inform rather than bind as kind: reference."
                ),
            )


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: object) -> None:
    """Render *data* as YAML and write atomically to *path*."""
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, path)
