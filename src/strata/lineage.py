"""Condensation lineage and the order-inversion check (v1.20 item 7(d)).

A condensing judge rewrites a scope's context, and nothing recorded WHICH notes it kept: the
engine could only see a drop for a note that survived verbatim, and a rewording judge leaves
none. The judge now reports, per line of its new context, the ids of the notes the line rests
on (`context_lineage`). The engine checks the ids are admitted notes of this scope, compares
the old lineage with the new, and records whether the drops followed the order the judge was
told: context superseded by newer context, then unexamined notes oldest first, then examined
notes oldest first.

Measure only. A write the engine cannot compare is `unmeasured` with a reason, counted apart
from "measured, no inversion"; nothing is rejected and no judgment changes because of it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strata.record_store import RecordStore

LINEAGE_FROM_FRACTION = 0.5
"""The judge is offered the lineage block and field once the context is at least this
fraction of the word budget, i.e. when a condensation is plausible."""

TIER_SUPERSEDED, TIER_UNEXAMINED, TIER_EXAMINED = 0, 1, 2
TIER_LABELS = {
    TIER_SUPERSEDED: "superseded",
    TIER_UNEXAMINED: "unexamined",
    TIER_EXAMINED: "examined",
}

CONTEXT_LINEAGE_PROPERTY: dict = {
    "type": ["array", "null"],
    "items": {
        "type": "object",
        "properties": {
            "line": {"type": "integer", "description": "1-based line number in your new_context."},
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Contribution ids this line rests on (several when it merges notes); "
                    "empty when it rests on no admitted note."
                ),
            },
        },
        "required": ["line", "sources"],
    },
    "description": (
        "v1.20: one entry per line of your new_context, naming the admitted contribution "
        "ids that line rests on, ids exactly as shown in this message. Record only: it "
        "changes nothing about your verdict. Omit or null when you leave the context as it is."
    ),
}


def context_lines(text: str | None) -> list[str]:
    """The numbered lines of a context: its non-blank lines, in order."""
    return [line for line in (text or "").split("\n") if line.strip()]


def context_hash(text: str | None) -> str:
    """Identity of a context text, so stored lineage ties to the exact text it describes."""
    return hashlib.sha256("\n".join(context_lines(text)).encode("utf-8")).hexdigest()


def lineage_offered(
    context: str | None, summary_max_words: int, *, mode: str, acted_on: bool = False
) -> bool:
    """Whether a judgment call is offered the lineage block and field.

    Only an ordinary or refresh call that may rewrite the context, and only when the context
    is already at least :data:`LINEAGE_FROM_FRACTION` of the budget. Whether any lineage is
    stored does not matter: the first offer is what bootstraps the chain."""
    if acted_on or mode not in ("ordinary", "input_change_refresh"):
        return False
    words = len((context or "").split())
    return words > 0 and words >= LINEAGE_FROM_FRACTION * summary_max_words


@dataclass(frozen=True)
class LineageEntry:
    line: int
    sources: tuple[str, ...]


@dataclass(frozen=True)
class LineageLineView:
    """One stored line as the judge is shown it: its number and, per source note, the id,
    admission date and examined state."""

    line_no: int
    items: tuple[tuple[str, str, str], ...]


def render_lineage_block(view: Sequence[LineageLineView] | None, context: str | None) -> str:
    """The CONTEXT LINES block of a message that is offered the lineage field.

    With stored lineage each line carries its notes' ids, admission dates and states. Without
    it (a scope's first offered call) the per-line metadata is OMITTED, not shown as unknown:
    the judge is given the rule and the instruction only, and cites the ids it can see."""
    lines = [
        "CONTEXT LINES — this context is near its budget, so a condensation is likely.",
        "Under budget pressure drop, in order: context superseded by newer context; then "
        "unexamined context, oldest first; then examined context, oldest first.",
        "Report `context_lineage`: for every line of your new_context, the contribution ids "
        "it rests on (several if you merge notes; none if it rests on no admitted note).",
    ]
    if view:
        by_line = {v.line_no: v for v in view}
        for n in range(1, len(context_lines(context)) + 1):
            entry = by_line.get(n)
            if entry is None or not entry.items:
                lines.append(f"[{n}] rests on no admitted note")
                continue
            notes = ", ".join(f"{i} ({d}, {s})" for i, d, s in entry.items)
            lines.append(f"[{n}] rests on {notes}")
    return "\n".join(lines) + "\n\n"


def validate_lineage(
    raw: object,
    new_context: str,
    *,
    allowed_ids: Collection[str],
    foreign: Callable[[str], bool] = lambda _id: False,
) -> tuple[list[LineageEntry] | None, str | None]:
    """The judge's `context_lineage` as entries, or ``(None, reason)`` when the write is
    unmeasured. Never raises, never rejects the judgment.

    Missing lines become unattributed entries. Reasons: ``absent``, ``malformed``,
    ``bad_line``, ``invalid_id``, ``foreign_id``."""
    if raw is None:
        return None, "absent"
    if not isinstance(raw, list):
        return None, "malformed"
    n_lines = len(context_lines(new_context))
    seen: dict[int, tuple[str, ...]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            return None, "malformed"
        line, sources = item.get("line"), item.get("sources")
        if isinstance(line, bool) or not isinstance(line, int):
            return None, "malformed"
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            return None, "malformed"
        if line < 1 or line > n_lines:
            return None, "bad_line"
        if line in seen:
            return None, "malformed"
        for source in sources:
            if source not in allowed_ids:
                return None, "foreign_id" if foreign(source) else "invalid_id"
        seen[line] = tuple(dict.fromkeys(sources))
    return [LineageEntry(n, seen.get(n, ())) for n in range(1, n_lines + 1)], None


def order_check(
    old: Sequence[LineageEntry],
    new: Sequence[LineageEntry],
    info: Callable[[str], tuple[int, str]],
) -> dict:
    """Compare old and new lineage. *info(id)* gives ``(tier, admitted_at)``.

    dropped = ids in old and not in new; kept = in both. Inversion A: a dropped unexamined
    note newer than a kept unexamined note. Inversion B: a dropped note of a higher tier
    than some kept note. A dropped id counts once per kind."""
    old_ids = {i for e in old for i in e.sources}
    new_ids = {i for e in new for i in e.sources}
    dropped, kept = sorted(old_ids - new_ids), sorted(old_ids & new_ids)
    facts = {i: info(i) for i in [*dropped, *kept]}
    a_pairs: list[list[str]] = []
    b_pairs: list[list[str]] = []
    for d in dropped:
        d_tier, d_time = facts[d]
        a_hit = b_hit = None
        for k in kept:
            k_tier, k_time = facts[k]
            if a_hit is None and d_tier == k_tier == TIER_UNEXAMINED and d_time > k_time:
                a_hit = k
            if b_hit is None and d_tier > k_tier:
                b_hit = k
        if a_hit is not None:
            a_pairs.append([d, a_hit])
        if b_hit is not None:
            b_pairs.append([d, b_hit])
    return {
        "dropped": dropped,
        "kept": kept,
        "unattributed": sum(1 for e in new if not e.sources),
        "inversions_recency": a_pairs,
        "inversions_tier": b_pairs,
        "inversions_n": len(a_pairs) + len(b_pairs),
    }


def record_context_write(
    *,
    scope_id: str,
    previous_context: str | None,
    new_context: str,
    summary_version: int | None,
    raw_lineage: object,
    offered: bool,
    judged_ids: Sequence[str],
    allowed_ids: Collection[str],
    foreign: Callable[[str], bool],
    info: Callable[[str], tuple[int, str]],
    record_store: RecordStore,
) -> dict | None:
    """After a write that changed the context: store the lineage when it validates, and write
    one `condensation_order_checks` row saying measured or not. Returns the row's fields, or
    ``None`` when the context did not change (nothing is recorded and the chain is kept)."""
    if context_hash(previous_context) == context_hash(new_context):
        return None
    previous = record_store.context_lineage_for(scope_id, context_hash(previous_context))
    if not context_lines(previous_context):
        previous = []  # an empty context is a valid, empty chain base
    entries: list[LineageEntry] | None = None
    reason: str | None = None
    old_lines = context_lines(previous_context)
    appended = _appended_lines(previous_context, new_context)
    if (
        appended is not None
        and previous is not None
        and len(previous) == len(old_lines)
        and len(appended) == 1
    ):
        # The engine (a held proposal, a departure label) or the judge added ONE line at the
        # end and touched nothing else: old lines keep their sources, the new one rests on
        # the notes this write admitted.
        entries = [*previous, LineageEntry(len(old_lines) + 1, tuple(judged_ids))]
    elif offered:
        entries, reason = validate_lineage(
            raw_lineage, new_context, allowed_ids=allowed_ids, foreign=foreign
        )
    else:
        reason = "not_offered"
    if entries is None and appended is not None and reason in ("absent", "not_offered"):
        reason = "engine_line_mismatch" if previous is not None else "no_previous"
    row = {
        "measured": 0,
        "reason": reason,
        "dropped_n": 0,
        "kept_n": 0,
        "unattributed_n": 0,
        "inversions_n": 0,
        "detail": {},
    }
    if entries is not None:
        record_store.store_context_lineage(
            scope_id,
            context_hash(new_context),
            [(e.line, list(e.sources)) for e in entries],
            summary_version,
        )
        if previous is None:
            row["reason"] = "no_previous"  # stored as the chain base; the next write is measurable
        else:
            check = order_check(previous, entries, info)
            row.update(
                measured=1,
                reason=None,
                dropped_n=len(check["dropped"]),
                kept_n=len(check["kept"]),
                unattributed_n=check["unattributed"],
                inversions_n=check["inversions_n"],
                detail=check,
            )
    record_store.append_condensation_order_check(
        scope_id=scope_id,
        summary_version=summary_version,
        measured=row["measured"],
        reason=row["reason"],
        dropped_n=row["dropped_n"],
        kept_n=row["kept_n"],
        unattributed_n=row["unattributed_n"],
        inversions_n=row["inversions_n"],
        detail=json.dumps(row["detail"], sort_keys=True),
    )
    return row


def _appended_lines(previous: str | None, new: str) -> list[str] | None:
    """The lines added when *new* is *previous* plus lines at the end, else ``None``."""
    old_lines, new_lines = context_lines(previous), context_lines(new)
    if len(new_lines) > len(old_lines) and new_lines[: len(old_lines)] == old_lines:
        return new_lines[len(old_lines) :]
    return None
