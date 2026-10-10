"""Fleet structure changes (#247).

A change to ``fleet.yaml`` is an authority act. It is validated the same way
a load validates the file, applied when the actor is the owner scope or an
ancestor of it (or the operator), and otherwise stored pending. It is
recorded and never judged.

Nothing in this module emits a change event. Channel removal and chain
change do not have a kind yet; apply reports that limit in prose instead.
"""

from __future__ import annotations

import copy
import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

try:
    import fcntl
except ImportError:  # pragma: no cover — Windows has no fcntl
    # Same degrade as strata.locks: one process still serialises on the
    # threading lock; two processes on Windows can still race the file.
    fcntl = None  # type: ignore[assignment]

import yaml
from pydantic import ValidationError

from strata.fleet_config import (
    FleetConfig,
    FleetConfigError,
    _atomic_replace,
    _resolve_edges,
    _schema_error_to_fleet_config_error,
    _validate,
)
from strata.operator import record_fleet_structure_act
from strata.record_store import FleetChangeProposal, RecordStore

ChangeType = Literal[
    "add_scope",
    "remove_scope",
    "reparent",
    "add_edge",
    "remove_edge",
    "describe",
]

CHANGE_TYPES: tuple[str, ...] = (
    "add_scope",
    "remove_scope",
    "reparent",
    "add_edge",
    "remove_edge",
    "describe",
)

FLAG_WIDENS = "widens the proposer's own reach"
FLAG_BINDS = "changes what binds the proposer"

_ACTOR_KEYS = frozenset(
    {
        "proposer",
        "proposer_position",
        "approver",
        "approver_position",
        "bound_scope",
        "as_scope",
        "actor",
        "approver_scope_id",
        "proposer_scope_id",
        "scope_id_as",
    }
)

_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "add_scope": frozenset(
        {
            "id",
            "name",
            "stratum_id",
            "parent_id",
            "description",
            "default_skill",
            "permitted_skills",
            "references",
        }
    ),
    "remove_scope": frozenset({"scope_id"}),
    "reparent": frozenset({"scope_id", "new_parent_id"}),
    "add_edge": frozenset({"from", "to"}),
    "remove_edge": frozenset({"from", "to"}),
    "describe": frozenset({"scope_id", "description"}),
}


class FleetChangeError(Exception):
    """A fleet structure change was refused before it touched the file."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class Actor:
    """Who is acting. ``scope_id is None`` is the operator, never a scope."""

    scope_id: str | None

    @property
    def position(self) -> str:
        return "operator" if self.scope_id is None else self.scope_id


@dataclass(frozen=True)
class FleetChange:
    change_type: ChangeType
    payload: dict


@dataclass(frozen=True)
class FleetChangeResult:
    status: Literal["applied", "pending", "rejected"]
    proposal_id: str | None
    act_id: str | None
    change_type: str
    proposer_position: str
    approver_position: str | None
    owner_scope_id: str | None
    widens_proposer_reach: bool
    changes_proposer_binding: bool
    notices: tuple[str, ...]
    backup: str | None


def names_an_actor(keys) -> bool:
    """True when a request mapping tries to name who is proposing or approving.

    The acting position is the MCP session binding, or the operator on the
    local HTTP API. It is never a field of the request.
    """
    return bool(_ACTOR_KEYS & set(keys))


def flag_phrases(*, widens: bool, binds: bool) -> list[str]:
    """The mechanical flag sentences, in a stable order."""
    phrases: list[str] = []
    if widens:
        phrases.append(FLAG_WIDENS)
    if binds:
        phrases.append(FLAG_BINDS)
    return phrases


def parse_change(change_type: str, payload: object) -> FleetChange:
    """Validate the shape of a change. Does not look at the fleet."""
    if change_type not in CHANGE_TYPES:
        raise FleetChangeError(
            "invalid_change",
            f"Unknown change type {change_type!r}. Expected one of {', '.join(CHANGE_TYPES)}.",
        )
    if not isinstance(payload, dict):
        raise FleetChangeError("invalid_change", "A fleet change payload must be an object.")
    actor_keys = _ACTOR_KEYS & payload.keys()
    if actor_keys:
        listed = ", ".join(sorted(actor_keys))
        raise FleetChangeError(
            "actor_not_in_payload",
            f"A fleet change cannot name who is acting ({listed}). "
            "That position comes from the session binding, or from the operator.",
        )
    unknown = set(payload) - _PAYLOAD_FIELDS[change_type]
    if unknown:
        listed = ", ".join(sorted(unknown))
        raise FleetChangeError(
            "invalid_change",
            f"{change_type} does not take {listed}.",
        )
    normalized = _normalize_payload(change_type, payload)
    return FleetChange(change_type=change_type, payload=normalized)  # type: ignore[arg-type]


def owner_scope(fleet: FleetConfig, change: FleetChange) -> str | None:
    """The scope that owns *change*, or ``None`` when only the operator does.

    ``None`` is a missing common ancestor (separate roots), a new root, or a
    change to a root that has no parent to own it.
    """
    payload = change.payload
    if change.change_type == "add_scope":
        return payload.get("parent_id")
    if change.change_type in ("remove_scope", "describe"):
        parent = fleet.inter_stratum_parent(payload["scope_id"])
        return None if parent is None else parent.id
    if change.change_type == "reparent":
        old = fleet.inter_stratum_parent(payload["scope_id"])
        nodes: list[str] = []
        if old is not None:
            nodes.append(old.id)
        nodes.append(payload["new_parent_id"])
        nodes.append(payload["scope_id"])
        return _lca(fleet, nodes)
    if change.change_type in ("add_edge", "remove_edge"):
        return _lca(fleet, [payload["from"], payload["to"]])
    raise FleetChangeError("invalid_change", f"Unknown change type {change.change_type!r}.")


def change_flags(
    fleet: FleetConfig, change: FleetChange, proposer_scope: str | None
) -> tuple[bool, bool]:
    """``(widens the proposer's own reach, changes what binds the proposer)``.

    Both are false for the operator, who is not bound to a scope. The second
    flag is true only for a re-parent of the proposer or one of its
    ancestors, or for removing an ancestor. By the LCA rule that owner sits
    above the proposer.
    """
    if proposer_scope is None:
        return False, False
    widens = change.change_type == "add_edge" and change.payload["from"] == proposer_scope
    binds = False
    if change.change_type == "reparent":
        target = change.payload["scope_id"]
        binds = target == proposer_scope or _is_ancestor(fleet, target, proposer_scope)
    elif change.change_type == "remove_scope":
        target = change.payload["scope_id"]
        binds = _is_ancestor(fleet, target, proposer_scope)
    return widens, binds


def actor_qualifies(fleet: FleetConfig, actor: Actor, owner: str | None) -> bool:
    """True when *actor* may apply a change owned by *owner*.

    The operator may apply anything. A scope may apply when it is the owner
    or an ancestor of the owner. A change with no owner (separate roots, or
    a root with no parent) is the operator's alone.
    """
    if actor.scope_id is None:
        return True
    if owner is None:
        return False
    if actor.scope_id == owner:
        return True
    return _is_ancestor(fleet, actor.scope_id, owner)


def limit_notices(fleet: FleetConfig, change: FleetChange) -> list[str]:
    """The stated-limit lines an applied change owes. Rows 1 and 5 are empty."""
    payload = change.payload
    if change.change_type == "remove_edge":
        reader = payload["from"]
        source = payload["to"]
        return [
            f"{reader} no longer reads {source}'s publication. Items in {reader}'s memory "
            f"attributed to {source} are not re-checked automatically (not built yet)."
        ]
    if change.change_type == "remove_scope":
        scope_id = payload["scope_id"]
        return [
            f"{scope_id}'s memory is kept. remove_edge already gave notice to any reader; "
            "attributed items are not re-checked automatically (not built yet)."
        ]
    if change.change_type == "reparent":
        scope_id = payload["scope_id"]
        subtree = _subtree_label(fleet, scope_id)
        chain = _chain_label(fleet, payload["new_parent_id"])
        return [
            f"{subtree} now inherits from {chain}. Its own directives are not re-checked "
            "against the new inherited rules automatically (not built yet). Review them; "
            "the 1.17 inherited check applies to new writes only."
        ]
    return []


def propose(
    fleet: FleetConfig,
    record_store: RecordStore,
    change: FleetChange,
    *,
    proposer: Actor,
) -> FleetChangeResult:
    """Validate *change* and apply it, or store it pending.

    *proposer* is the session's bound scope, or the operator. It is never
    read from *change*. A proposer who is the owner, or an ancestor of the
    owner, applies immediately. Anyone else leaves a pending row.

    The fleet file is re-read under the cross-process lock before the owner,
    the flags, and the delegation refusal are computed.
    """

    def _body() -> FleetChangeResult:
        if proposer.scope_id is None:
            raise FleetChangeError(
                "operator_uses_apply",
                "The operator applies a fleet change directly; propose is for a bound scope.",
            )
        preview = _preview(fleet, change, proposer)
        if actor_qualifies(fleet, proposer, preview.owner):
            return _apply(
                fleet,
                record_store,
                change,
                preview,
                proposer_position=proposer.position,
                approver_position=proposer.position,
                proposal_id=None,
            )
        proposal = record_store.insert_fleet_change_proposal(
            change_type=change.change_type,
            payload=change.payload,
            proposer_position=proposer.position,
            owner_scope_id=preview.owner,
            widens_proposer_reach=preview.widens,
            changes_proposer_binding=preview.binds,
        )
        return _pending_result(proposal)

    return _under_fleet_lock(fleet, _body)


def apply_as_operator(
    fleet: FleetConfig,
    record_store: RecordStore,
    change: FleetChange,
) -> FleetChangeResult:
    """Apply *change* as the operator. The operator can apply anything valid."""

    def _body() -> FleetChangeResult:
        operator = Actor(None)
        preview = _preview(fleet, change, operator)
        return _apply(
            fleet,
            record_store,
            change,
            preview,
            proposer_position="operator",
            approver_position="operator",
            proposal_id=None,
        )

    return _under_fleet_lock(fleet, _body)


def list_approvable(
    fleet: FleetConfig,
    record_store: RecordStore,
    actor: Actor,
) -> list[FleetChangeProposal]:
    """Pending proposals *actor* may approve. The operator sees all of them."""
    pending = record_store.list_fleet_change_proposals(status="pending")
    if actor.scope_id is None:
        return pending
    return [
        proposal for proposal in pending if actor_qualifies(fleet, actor, proposal.owner_scope_id)
    ]


def approve(
    fleet: FleetConfig,
    record_store: RecordStore,
    proposal_id: str,
    *,
    approver: Actor,
) -> FleetChangeResult:
    """Apply a pending proposal. Refused unless *approver* owns it or is above it.

    The claim, the fresh owner check, the write, and the act are one critical
    section under the fleet file lock. The proposer cannot approve their own
    proposal.
    """

    def _body() -> FleetChangeResult:
        proposal = _require_pending(record_store, proposal_id)
        _refuse_self_resolution(approver, proposal, verb="approve")
        change = FleetChange(change_type=proposal.change_type, payload=proposal.payload)  # type: ignore[arg-type]
        proposer_scope = (
            None if proposal.proposer_position == "operator" else proposal.proposer_position
        )
        preview = _preview(fleet, change, Actor(proposer_scope))
        if not actor_qualifies(fleet, approver, preview.owner):
            raise FleetChangeError(
                "not_authorized",
                f"{approver.position} cannot approve {proposal_id}, owned by "
                f"{preview.owner or 'the operator'}.",
            )
        if not record_store.claim_fleet_change_proposal(proposal_id, resolved_by=approver.position):
            raise FleetChangeError("not_pending", f"{proposal_id} is not pending.")
        try:
            return _apply(
                fleet,
                record_store,
                change,
                preview,
                proposer_position=proposal.proposer_position,
                approver_position=approver.position,
                proposal_id=proposal_id,
            )
        except Exception:
            record_store.reopen_fleet_change_proposal(proposal_id)
            raise

    return _under_fleet_lock(fleet, _body)


def cli_fleet(args: object) -> int:
    """Run one ``strata fleet`` subcommand. Prints notices and returns 0 or 1.

    Direct subcommands (add-scope, remove-scope, reparent, add-edge,
    remove-edge, describe) apply as the operator. ``apply`` and ``reject``
    act on a pending id, also as the operator.
    """
    db = getattr(args, "db", None)
    fleet_path = getattr(args, "fleet_path", None)
    try:
        fleet, store = _open_cli(db, fleet_path)
    except (FleetChangeError, FleetConfigError, OSError) as exc:
        message = exc.message if isinstance(exc, (FleetChangeError, FleetConfigError)) else str(exc)
        print(message, file=sys.stderr)
        return 1
    try:
        return _cli_dispatch(args, fleet, store)
    except (FleetChangeError, FleetConfigError) as exc:
        print(exc.message, file=sys.stderr)
        return 1
    finally:
        store.close()


def reject(
    fleet: FleetConfig,
    record_store: RecordStore,
    proposal_id: str,
    *,
    approver: Actor,
) -> FleetChangeResult:
    """Reject a pending proposal. Same authority rule as :func:`approve`.

    The owner is recomputed from the fleet file under the lock, not taken
    from the stored ``owner_scope_id``. The proposer cannot reject their own
    proposal.
    """

    def _body() -> FleetChangeResult:
        proposal = _require_pending(record_store, proposal_id)
        _refuse_self_resolution(approver, proposal, verb="reject")
        change = FleetChange(change_type=proposal.change_type, payload=proposal.payload)  # type: ignore[arg-type]
        owner = owner_scope(fleet, change)
        if not actor_qualifies(fleet, approver, owner):
            raise FleetChangeError(
                "not_authorized",
                f"{approver.position} cannot reject {proposal_id}, owned by "
                f"{owner or 'the operator'}.",
            )
        if not record_store.reject_fleet_change_proposal(
            proposal_id, resolved_by=approver.position
        ):
            raise FleetChangeError("not_pending", f"{proposal_id} is not pending.")
        return FleetChangeResult(
            status="rejected",
            proposal_id=proposal_id,
            act_id=None,
            change_type=proposal.change_type,
            proposer_position=proposal.proposer_position,
            approver_position=approver.position,
            owner_scope_id=owner,
            widens_proposer_reach=proposal.widens_proposer_reach,
            changes_proposer_binding=proposal.changes_proposer_binding,
            notices=(),
            backup=None,
        )

    return _under_fleet_lock(fleet, _body)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Preview:
    owner: str | None
    widens: bool
    binds: bool
    notices: tuple[str, ...]


def _normalize_payload(change_type: str, payload: dict) -> dict:
    if change_type == "add_scope":
        return {
            "id": _require_str(payload, "id"),
            "name": _require_str(payload, "name"),
            "stratum_id": _require_str(payload, "stratum_id"),
            "parent_id": _optional_str(payload.get("parent_id")),
            "description": _optional_str(payload.get("description")),
            "default_skill": _optional_str(payload.get("default_skill")),
            "permitted_skills": _optional_str_list(payload.get("permitted_skills")),
            "references": _str_list(payload.get("references") or []),
        }
    if change_type == "remove_scope":
        return {"scope_id": _require_str(payload, "scope_id")}
    if change_type == "reparent":
        return {
            "scope_id": _require_str(payload, "scope_id"),
            "new_parent_id": _require_str(payload, "new_parent_id"),
        }
    if change_type in ("add_edge", "remove_edge"):
        return {"from": _require_str(payload, "from"), "to": _require_str(payload, "to")}
    if "description" not in payload:
        raise FleetChangeError("invalid_change", "describe requires description.")
    return {
        "scope_id": _require_str(payload, "scope_id"),
        "description": _optional_str(payload.get("description")),
    }


def _require_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FleetChangeError("invalid_change", f"{key} must be a non-empty string.")
    return value.strip()


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FleetChangeError("invalid_change", "Expected a string or null.")
    stripped = value.strip()
    return stripped or None


def _every_nonempty_str(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _str_list(value: object) -> list[str]:
    if not _every_nonempty_str(value):
        raise FleetChangeError("invalid_change", "references must be a list of scope ids.")
    return [item.strip() for item in value]


def _optional_str_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if not _every_nonempty_str(value):
        raise FleetChangeError("invalid_change", "permitted_skills must be a list of strings.")
    return [item.strip() for item in value]


def _preview(fleet: FleetConfig, change: FleetChange, proposer: Actor) -> _Preview:
    _refuse_wider_reach(fleet, change, proposer)
    raw = _read_raw(fleet)
    updated = _transform(copy.deepcopy(raw), change)
    _validate_document(updated)
    owner = owner_scope(fleet, change)
    widens, binds = change_flags(fleet, change, proposer.scope_id)
    return _Preview(
        owner=owner,
        widens=widens,
        binds=binds,
        notices=tuple(limit_notices(fleet, change)),
    )


def _apply(
    fleet: FleetConfig,
    record_store: RecordStore,
    change: FleetChange,
    preview: _Preview,
    *,
    proposer_position: str,
    approver_position: str,
    proposal_id: str | None,
) -> FleetChangeResult:
    touched = _touched_scope_ids(fleet, change)
    before = _topology(fleet, touched)
    backup = fleet.mutate(lambda raw: _transform(raw, change))
    after = _topology(fleet, touched)
    try:
        act = record_fleet_structure_act(
            record_store=record_store,
            change_type=change.change_type,
            proposer_position=proposer_position,
            approver_position=approver_position,
            widens_proposer_reach=preview.widens,
            changes_proposer_binding=preview.binds,
            owner_scope_id=preview.owner,
            before_topology=before,
            after_topology=after,
            proposal_id=proposal_id,
        )
    except Exception:
        _rollback(fleet, backup)
        raise
    return FleetChangeResult(
        status="applied",
        proposal_id=proposal_id,
        act_id=act.id,
        change_type=change.change_type,
        proposer_position=proposer_position,
        approver_position=approver_position,
        owner_scope_id=preview.owner,
        widens_proposer_reach=preview.widens,
        changes_proposer_binding=preview.binds,
        notices=preview.notices,
        backup=str(backup),
    )


def _pending_result(proposal: FleetChangeProposal) -> FleetChangeResult:
    return FleetChangeResult(
        status="pending",
        proposal_id=proposal.id,
        act_id=None,
        change_type=proposal.change_type,
        proposer_position=proposal.proposer_position,
        approver_position=None,
        owner_scope_id=proposal.owner_scope_id,
        widens_proposer_reach=proposal.widens_proposer_reach,
        changes_proposer_binding=proposal.changes_proposer_binding,
        notices=(),
        backup=None,
    )


def _require_pending(record_store: RecordStore, proposal_id: str) -> FleetChangeProposal:
    try:
        proposal = record_store.get_fleet_change_proposal(proposal_id)
    except KeyError as exc:
        raise FleetChangeError("not_found", f"No fleet change {proposal_id}.") from exc
    if proposal.status != "pending":
        raise FleetChangeError("not_pending", f"{proposal_id} is {proposal.status}, not pending.")
    return proposal


def _rollback(fleet: FleetConfig, backup: Path) -> None:
    """Put the backup bytes back. Caller holds the fleet file lock."""
    assert fleet._path is not None
    _atomic_replace(fleet._path, backup.read_bytes())
    fleet.reload_from_disk()


def _refuse_self_resolution(approver: Actor, proposal: FleetChangeProposal, *, verb: str) -> None:
    """The scope that proposed a change cannot be the one that decides it."""
    if approver.position == proposal.proposer_position:
        raise FleetChangeError(
            "approver_is_proposer",
            f"{approver.position} proposed {proposal.id} and cannot {verb} it.",
        )


class _FleetYamlLock:
    """A threading lock plus a cross-process flock on a file beside ``fleet.yaml``.

    Same order as :class:`strata.locks._ScopeFileLock`: the threading lock is
    outermost, then ``fcntl.flock``. The same thread may enter again; only the
    outermost entry takes the flock, so a write inside an apply does not
    deadlock on the lock the apply already holds. Where ``fcntl`` is missing
    the threading lock still serialises this process.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._thread = threading.RLock()
        self._fh: object | None = None
        self._depth = 0

    def __enter__(self) -> _FleetYamlLock:
        self._thread.acquire()
        try:
            if self._depth == 0 and fcntl is not None:
                handle = None
                try:
                    lock_path = self._path.with_name(self._path.name + ".lock")
                    handle = lock_path.open("a", encoding="utf-8")
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except BaseException:
                    if handle is not None:
                        handle.close()
                    raise
                self._fh = handle
            self._depth += 1
        except BaseException:
            self._thread.release()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            self._depth -= 1
            if self._depth == 0 and self._fh is not None:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
                finally:
                    self._fh.close()  # type: ignore[attr-defined]
                    self._fh = None
        finally:
            self._thread.release()


_fleet_yaml_locks: dict[str, _FleetYamlLock] = {}
_fleet_yaml_locks_guard = threading.Lock()


def _fleet_yaml_lock(path: Path) -> _FleetYamlLock:
    key = str(Path(path).resolve())
    with _fleet_yaml_locks_guard:
        lock = _fleet_yaml_locks.get(key)
        if lock is None:
            lock = _FleetYamlLock(Path(key))
            _fleet_yaml_locks[key] = lock
        return lock


def _under_fleet_lock(fleet: FleetConfig, body):  # noqa: ANN001, ANN202
    """Re-read *fleet* and run *body* while holding the file lock.

    *body* sees the file as it is now. Owner, flags, the delegation refusal,
    the transform, the write, and the recorded act all happen before the lock
    is released. Approve and reject claim the proposal inside the same hold.
    """
    path = fleet._path
    if path is None:
        raise FleetChangeError("no_fleet_file", "This fleet is not backed by a file.")
    with _fleet_yaml_lock(path):
        fleet.reload_from_disk()
        return body()


def _read_raw(fleet: FleetConfig) -> dict:
    assert fleet._path is not None
    raw = yaml.safe_load(fleet._path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise FleetChangeError("invalid_change", "fleet.yaml must be a mapping.")
    raw.setdefault("strata", [])
    raw.setdefault("scopes", [])
    raw.setdefault("edges", [])
    return raw


def _validate_document(raw: dict) -> None:
    try:
        candidate = FleetConfig.model_validate(raw)
    except ValidationError as exc:
        raise _schema_error_to_fleet_config_error(exc, raw) from exc
    _validate(candidate)


def _document(raw: dict) -> tuple[FleetConfig, list]:
    try:
        config = FleetConfig.model_validate(raw)
    except ValidationError as exc:
        raise _schema_error_to_fleet_config_error(exc, raw) from exc
    return config, list(_resolve_edges(config))


def _transform(raw: dict, change: FleetChange) -> dict:
    raw.setdefault("strata", [])
    raw.setdefault("scopes", [])
    raw.setdefault("edges", [])
    kind = change.change_type
    if kind == "add_scope":
        return _transform_add(raw, change.payload)
    if kind == "remove_scope":
        return _transform_remove(raw, change.payload["scope_id"])
    if kind == "reparent":
        return _transform_reparent(raw, change.payload["scope_id"], change.payload["new_parent_id"])
    if kind == "add_edge":
        return _transform_add_edge(raw, change.payload["from"], change.payload["to"])
    if kind == "remove_edge":
        return _transform_remove_edge(raw, change.payload["from"], change.payload["to"])
    return _transform_describe(raw, change.payload["scope_id"], change.payload["description"])


def _transform_add(raw: dict, payload: dict) -> dict:
    config, _resolutions = _document(raw)
    scope_id = payload["id"]
    if config.get_scope(scope_id) is not None:
        raise FleetChangeError("duplicate_scope_id", f"Scope {scope_id!r} already exists.")
    entry: dict = {"id": scope_id, "name": payload["name"], "stratum_id": payload["stratum_id"]}
    if payload.get("description"):
        entry["description"] = payload["description"]
    if payload.get("default_skill"):
        entry["default_skill"] = payload["default_skill"]
    if payload.get("permitted_skills") is not None:
        entry["permitted_skills"] = list(payload["permitted_skills"])
    raw["scopes"].append(entry)
    parent_id = payload.get("parent_id")
    if parent_id:
        raw["edges"].append({"from": scope_id, "to": parent_id, "kind": "chain"})
    for target in payload.get("references") or []:
        raw["edges"].append({"from": scope_id, "to": target, "kind": "reference"})
    return raw


def _transform_remove(raw: dict, scope_id: str) -> dict:
    _config, resolutions = _document(raw)
    if not any(entry.get("id") == scope_id for entry in raw["scopes"]):
        raise FleetChangeError("scope_not_found", f"Scope {scope_id!r} is not in the fleet.")
    children = [item.from_ for item in resolutions if item.kind == "chain" and item.to == scope_id]
    if children:
        listed = ", ".join(sorted(children))
        raise FleetChangeError(
            "scope_has_children",
            f"Scope {scope_id!r} still has chain children ({listed}); "
            "re-parent or remove them first.",
        )
    blocking = [
        f"{item.from_} -> {item.to}"
        for item in resolutions
        if item.kind == "reference" and (item.from_ == scope_id or item.to == scope_id)
    ]
    if blocking:
        listed = ", ".join(blocking)
        raise FleetChangeError(
            "scope_has_edges",
            f"Scope {scope_id!r} still has edges ({listed}); remove those edges first.",
        )
    raw["scopes"] = [entry for entry in raw["scopes"] if entry.get("id") != scope_id]
    raw["edges"] = [
        entry
        for entry, item in zip(raw["edges"], resolutions, strict=True)
        if item.from_ != scope_id and item.to != scope_id
    ]
    return raw


def _transform_reparent(raw: dict, scope_id: str, new_parent_id: str) -> dict:
    _config, resolutions = _document(raw)
    if not any(entry.get("id") == scope_id for entry in raw["scopes"]):
        raise FleetChangeError("scope_not_found", f"Scope {scope_id!r} is not in the fleet.")
    if not any(entry.get("id") == new_parent_id for entry in raw["scopes"]):
        raise FleetChangeError("scope_not_found", f"Scope {new_parent_id!r} is not in the fleet.")
    if scope_id == new_parent_id:
        raise FleetChangeError("self_parent", f"Scope {scope_id!r} cannot be its own parent.")
    index = next(
        (
            i
            for i, item in enumerate(resolutions)
            if item.kind == "chain" and item.from_ == scope_id
        ),
        None,
    )
    current = None if index is None else resolutions[index].to
    if current == new_parent_id:
        raise FleetChangeError(
            "no_change", f"Scope {scope_id!r} is already parented at {new_parent_id!r}."
        )
    edge = {"from": scope_id, "to": new_parent_id, "kind": "chain"}
    if index is None:
        raw["edges"].append(edge)
    else:
        raw["edges"][index] = edge
    return raw


def _transform_add_edge(raw: dict, reader: str, source: str) -> dict:
    _config, resolutions = _document(raw)
    for item in resolutions:
        if item.kind == "reference" and item.from_ == reader and item.to == source:
            raise FleetChangeError("edge_exists", f"{reader!r} already reads {source!r}.")
    raw["edges"].append({"from": reader, "to": source, "kind": "reference"})
    return raw


def _transform_remove_edge(raw: dict, reader: str, source: str) -> dict:
    _config, resolutions = _document(raw)
    found = next(
        (i for i, item in enumerate(resolutions) if item.from_ == reader and item.to == source),
        None,
    )
    if found is None:
        raise FleetChangeError("edge_not_found", f"No edge from {reader!r} to {source!r}.")
    if resolutions[found].kind != "reference":
        raise FleetChangeError(
            "not_a_reference_edge",
            f"The edge from {reader!r} to {source!r} is a chain edge. Re-parent to move it.",
        )
    del raw["edges"][found]
    return raw


def _transform_describe(raw: dict, scope_id: str, description: str | None) -> dict:
    for entry in raw["scopes"]:
        if entry.get("id") != scope_id:
            continue
        if description:
            entry["description"] = description
        else:
            entry.pop("description", None)
        return raw
    raise FleetChangeError("scope_not_found", f"Scope {scope_id!r} is not in the fleet.")


def _refuse_wider_reach(fleet: FleetConfig, change: FleetChange, proposer: Actor) -> None:
    """A scope may not create a child whose reference edges it cannot already reach."""
    if proposer.scope_id is None or change.change_type != "add_scope":
        return
    parent_id = change.payload.get("parent_id")
    if parent_id is None or not _is_at_or_under(fleet, parent_id, proposer.scope_id):
        return
    reachable = _reachable(fleet, proposer.scope_id)
    extra = [target for target in change.payload.get("references") or [] if target not in reachable]
    if extra:
        raise FleetChangeError(
            "wider_reach",
            f"Scope {change.payload['id']!r} would read {extra}, which "
            f"{proposer.scope_id!r} cannot reach.",
        )


def _reachable(fleet: FleetConfig, scope_id: str) -> set[str]:
    reached = {scope_id}
    reached.update(scope.id for scope in fleet.inter_stratum_ancestors(scope_id))
    reached.update(scope.id for scope in fleet.chain_descendants(scope_id))
    reached.update(scope.id for scope in fleet.references_from(scope_id))
    return reached


def _is_ancestor(fleet: FleetConfig, ancestor_id: str, scope_id: str) -> bool:
    return any(scope.id == ancestor_id for scope in fleet.inter_stratum_ancestors(scope_id))


def _is_at_or_under(fleet: FleetConfig, scope_id: str, ancestor_id: str) -> bool:
    return scope_id == ancestor_id or _is_ancestor(fleet, ancestor_id, scope_id)


def _lca(fleet: FleetConfig, scope_ids: list[str]) -> str | None:
    chains: list[list[str]] = []
    for scope_id in scope_ids:
        if fleet.get_scope(scope_id) is None:
            return None
        chains.append([scope.id for scope in fleet.inter_stratum_ancestors(scope_id)] + [scope_id])
    if not chains:
        return None
    common = set(chains[0])
    for chain in chains[1:]:
        common &= set(chain)
    if not common:
        return None
    best = None
    best_index = -1
    for index, scope_id in enumerate(chains[0]):
        if scope_id in common and index > best_index:
            best = scope_id
            best_index = index
    return best


def _touched_scope_ids(fleet: FleetConfig, change: FleetChange) -> list[str]:
    payload = change.payload
    if change.change_type == "add_scope":
        return [payload["id"]]
    if change.change_type in ("remove_scope", "reparent", "describe"):
        return [payload["scope_id"]]
    return [payload["from"], payload["to"]]


def _topology(fleet: FleetConfig, scope_ids: list[str]) -> dict:
    snapshot: dict = {}
    for scope_id in scope_ids:
        scope = fleet.get_scope(scope_id)
        if scope is None:
            snapshot[scope_id] = None
            continue
        parent = fleet.inter_stratum_parent(scope_id)
        snapshot[scope_id] = {
            "parent": None if parent is None else parent.id,
            "references": [item.id for item in fleet.references_from(scope_id)],
        }
    return snapshot


def _subtree_label(fleet: FleetConfig, scope_id: str) -> str:
    descendants = [scope.id for scope in fleet.chain_descendants(scope_id)]
    if not descendants:
        return scope_id
    listed = ", ".join(descendants)
    return f"{scope_id} and its subtree ({listed})"


def _chain_label(fleet: FleetConfig, parent_id: str) -> str:
    ancestors = [scope.id for scope in fleet.inter_stratum_ancestors(parent_id)]
    return " → ".join([*ancestors, parent_id])


def _open_cli(db: str | None, fleet_path: str | None) -> tuple[FleetConfig, RecordStore]:
    from strata.migrator import run_migrations
    from strata.project_config import resolve_storage_paths

    paths = resolve_storage_paths()
    db_path = db or paths.db_path
    path = Path(fleet_path or paths.fleet_yaml_path)
    if not path.is_file():
        raise FleetChangeError("no_fleet", f"No fleet.yaml at {path}.")
    run_migrations(db_path)
    return FleetConfig.load(path), RecordStore(db_path)


def _cli_dispatch(args: object, fleet: FleetConfig, store: RecordStore) -> int:
    command = getattr(args, "fleet_command", None)
    operator = Actor(None)
    if command == "pending":
        _print_pending(list_approvable(fleet, store, operator))
        return 0
    if command == "apply":
        result = approve(fleet, store, args.change_id, approver=operator)
        _print_result(result)
        return 0
    if command == "reject":
        result = reject(fleet, store, args.change_id, approver=operator)
        _print_result(result)
        return 0
    change = _change_from_cli(args, command)
    result = apply_as_operator(fleet, store, change)
    _print_result(result)
    return 0


def _change_from_cli(args: object, command: str | None) -> FleetChange:
    if command == "add-scope":
        return parse_change(
            "add_scope",
            {
                "id": args.scope_id,
                "name": args.name,
                "stratum_id": args.stratum_id,
                "parent_id": getattr(args, "parent_id", None),
                "description": getattr(args, "description", None),
                "references": list(getattr(args, "reference", None) or []),
            },
        )
    if command == "remove-scope":
        return parse_change("remove_scope", {"scope_id": args.scope_id})
    if command == "reparent":
        return parse_change(
            "reparent",
            {"scope_id": args.scope_id, "new_parent_id": args.new_parent_id},
        )
    if command == "add-edge":
        return parse_change("add_edge", {"from": args.edge_from, "to": args.edge_to})
    if command == "remove-edge":
        return parse_change("remove_edge", {"from": args.edge_from, "to": args.edge_to})
    if command == "describe":
        return parse_change(
            "describe",
            {"scope_id": args.scope_id, "description": args.description},
        )
    raise FleetChangeError("invalid_change", f"Unknown fleet command {command!r}.")


def _print_pending(proposals: list[FleetChangeProposal]) -> None:
    if not proposals:
        print("No pending fleet changes.")
        return
    for proposal in proposals:
        owner = proposal.owner_scope_id or "operator"
        phrases = flag_phrases(
            widens=proposal.widens_proposer_reach,
            binds=proposal.changes_proposer_binding,
        )
        flag_text = "; ".join(phrases) if phrases else "none"
        print(
            f"{proposal.id}  {proposal.change_type}  "
            f"proposer {proposal.proposer_position}  owner {owner}"
        )
        print(f"  flags: {flag_text}")
        print(f"  payload: {json.dumps(proposal.payload, sort_keys=True)}")


def _print_result(result: FleetChangeResult) -> None:
    label = result.proposal_id or result.change_type
    act = result.act_id or "-"
    print(f"{result.status} {label} act {act} approver {result.approver_position or '-'}")
    for line in result.notices:
        print(line)
    if result.backup:
        print(f"backup: {result.backup}")
