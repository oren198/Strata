"""A read-only check on what the record says should bind (v1.22 P2).

``strata doctor`` uses it to report a directive that the record says was admitted but that is
missing from its scope's summary with no retirement and no supersession on record: the footprint
of a death between a summary write and its after-write steps (see the amendment intent journal),
or of anything else that lost an explanation. It reports only. It repairs nothing, and a hit never
changes doctor's exit status: the record-side inference is a heuristic (operator acts and the
engine's own sweeps change directive sets by other paths, each of which writes a retirement), so
it can name a directive that was legitimately removed by a path it does not model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strata.fleet_config import FleetConfig
    from strata.record_store import RecordStore
    from strata.summary_store import SummaryStore

#: The engine's decision prefix names the ops that applied: "[accept_as_directive; append c_x]".
_MINTING_PREFIX = re.compile(r"^\[accept_as_directive;[^\]]*\b(?:append|publish)\b")


@dataclass(frozen=True)
class MissingDirective:
    scope_id: str
    directive_id: str


def missing_directives(
    *, fleet: FleetConfig, record_store: RecordStore, summary_store: SummaryStore
) -> list[MissingDirective]:
    """Directives the record says were admitted, absent from the summary, with neither a
    retirement nor a supersession on record."""
    found: list[MissingDirective] = []
    for scope in fleet.scopes:
        summary = summary_store.read(scope.id)
        live = {d.id for d in summary.directives} if summary is not None else set()
        retired = {r.directive_id for r in record_store.list_retirements()}
        for contribution in record_store.list_contributions(scope_id=scope.id):
            judgment = record_store.get_judgment(contribution.id)
            if (
                judgment is None
                or judgment.decision != "accept_as_directive"
                or judgment.disposition not in (None, "approved")
                or not _MINTING_PREFIX.match(judgment.notes or "")
            ):
                continue
            if contribution.id in live or contribution.id in retired:
                continue
            if record_store.is_superseded(contribution.id):
                continue
            found.append(MissingDirective(scope.id, contribution.id))
    return found
