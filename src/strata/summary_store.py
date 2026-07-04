"""Markdown persistence layer for scope summaries.

Each scope's summary is stored as a single human-readable, diff-friendly
markdown file under a shared summaries directory.  The layout is:

    <summaries_dir>/<scope_id>.md

Writes are atomic: the file is first written to a ``<scope_id>.md.tmp``
sibling and then renamed into place via :func:`os.replace`, so a crashed
writer never leaves a partial summary visible to readers.

Vocabulary follows ``CONTEXT.md`` verbatim: *scope*, *scope summary*,
*directive*, *context*, *contribution*, *provenance*.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Directive(BaseModel):
    """A single binding decision within a scope summary.

    Each directive retains its identity (``id``) so it can be cited,
    superseded, or retired independently.  Provenance fields are preserved
    through composition into perspectives — see ``CONTEXT.md``.
    """

    id: str
    """The contribution ID this directive originated from."""

    content: str
    """The directive text."""

    subject: str | None = None
    """Optional subject tag carried from the contribution."""

    source_scope_id: str
    """The scope where this directive was published."""

    source_skill: str
    """The skill of the agent that published this directive."""

    created_at: str
    """ISO 8601 timestamp of when the directive was created."""


class ScopeSummary(BaseModel):
    """The curated, condensed working view of a scope.

    A scope summary has two sections:

    * **directives** — listed individually, each retaining its identity.
    * **context** — a condensed digest of relevant non-binding knowledge.

    This is what gets composed into agents' *perspectives* when they inherit
    from this scope.  The raw record is consulted only for accountability,
    recovery, or forensics.

    Version stamps (ADR 0004 Decision 4):

    * ``version`` — incremented on each write; used by descendant scopes to
      detect staleness.
    * ``parent_version`` — the parent scope's ``version`` at the time this
      summary was built.  ``None`` for L0 (root) scopes which have no
      inter-stratum parent.
    """

    scope_id: str
    directives: list[Directive]
    context: str
    """Free-form prose digest of non-binding knowledge.  May be empty."""

    updated_at: str
    """ISO 8601 timestamp of the last summary rewrite."""

    version: int = 1
    """Monotonically increasing write counter.  Bumped on every :meth:`SummaryStore.write`."""

    parent_version: int | None = None
    """The parent scope's ``version`` when this summary was last refreshed.
    ``None`` for root scopes (no inter-stratum parent)."""


# ---------------------------------------------------------------------------
# Markdown serialisation helpers
# ---------------------------------------------------------------------------

_NONE_YET = "_(none yet)_"

# Matches:  ### [c_abc123] the directive heading text
_DIRECTIVE_HEADING_RE = re.compile(r"^###\s+\[([^\]]+)\]\s*(.*)")
# Matches:  - subject: value
_SUBJECT_LINE_RE = re.compile(r"^-\s+subject:\s*(.*)")
# Matches:  - source: scope=... · skill=... · at=...
_SOURCE_LINE_RE = re.compile(r"^-\s+source:\s+scope=([^\s·]+)\s+·\s+skill=([^\s·]+)\s+·\s+at=(.+)")
# Matches:  > blockquote body
_BLOCKQUOTE_RE = re.compile(r"^>\s*(.*)")


def _render_summary(summary: ScopeSummary) -> str:
    """Serialise a :class:`ScopeSummary` to the canonical markdown format."""
    lines: list[str] = []

    # --- YAML frontmatter ---
    frontmatter: dict = {
        "scope_id": summary.scope_id,
        "version": summary.version,
        "updated_at": summary.updated_at,
    }
    if summary.parent_version is not None:
        frontmatter["parent_version"] = summary.parent_version
    lines.append("---")
    lines.append(yaml.dump(frontmatter, default_flow_style=False).rstrip())
    lines.append("---")
    lines.append("")

    # --- H1 title ---
    lines.append(f"# Scope: {summary.scope_id}")
    lines.append("")

    # --- Directives section ---
    lines.append("## Directives")
    lines.append("")

    if not summary.directives:
        lines.append(_NONE_YET)
        lines.append("")
    else:
        for directive in summary.directives:
            # Heading shows the first line only (headings cannot span lines);
            # the blockquote below carries the full content and is what the
            # parser treats as canonical.
            heading_content = directive.content.splitlines()[0] if directive.content else ""
            lines.append(f"### [{directive.id}] {heading_content}")
            subject_value = directive.subject if directive.subject is not None else ""
            lines.append(f"- subject: {subject_value}")
            lines.append(
                f"- source: scope={directive.source_scope_id}"
                f" · skill={directive.source_skill}"
                f" · at={directive.created_at}"
            )
            lines.append("")
            # Blockquote every line so multi-line directives round-trip
            # instead of being truncated to their first line.
            for content_line in directive.content.splitlines() or [""]:
                lines.append(f"> {content_line}")
            lines.append("")

    # --- Context section ---
    lines.append("## Context")
    lines.append("")
    if not summary.context:
        lines.append(_NONE_YET)
        lines.append("")
    else:
        lines.append(summary.context)
        lines.append("")

    return "\n".join(lines)


def _parse_summary(text: str) -> ScopeSummary:
    """Parse a scope summary from its canonical markdown representation."""
    # Split frontmatter from body
    if text.startswith("---"):
        # Find closing ---
        end = text.index("\n---\n", 3)
        fm_text = text[4:end]
        body = text[end + 5 :]
    else:
        raise ValueError("Missing YAML frontmatter")

    fm = yaml.safe_load(fm_text)
    scope_id: str = fm["scope_id"]
    updated_at: str = fm["updated_at"]
    version: int = int(fm.get("version", 1))
    parent_version: int | None = fm.get("parent_version")

    # Parse body line by line using a simple state machine.
    # States: OUTSIDE, IN_DIRECTIVES, IN_DIRECTIVE_BLOCK, IN_CONTEXT
    State = str  # keep it simple
    state: State = "OUTSIDE"

    directives: list[Directive] = []
    context_lines: list[str] = []

    # Current directive-in-progress fields
    cur_id: str | None = None
    cur_content_from_heading: str | None = None
    cur_subject: str | None = None
    cur_source_scope: str | None = None
    cur_source_skill: str | None = None
    cur_created_at: str | None = None
    cur_blockquote_lines: list[str] = []

    def _flush_directive() -> None:
        """Save the current in-progress directive to the list."""
        nonlocal cur_id, cur_content_from_heading, cur_subject
        nonlocal cur_source_scope, cur_source_skill, cur_created_at, cur_blockquote_lines
        if cur_id is None:
            return
        # Use blockquote as canonical content (as spec requires); the heading
        # carries only the first line for display.
        if cur_blockquote_lines:
            content = "\n".join(cur_blockquote_lines)
        else:
            content = cur_content_from_heading or ""
        directives.append(
            Directive(
                id=cur_id,
                content=content,
                subject=cur_subject if cur_subject else None,
                source_scope_id=cur_source_scope or "",
                source_skill=cur_source_skill or "",
                created_at=cur_created_at or "",
            )
        )
        cur_id = None
        cur_content_from_heading = None
        cur_subject = None
        cur_source_scope = None
        cur_source_skill = None
        cur_created_at = None
        cur_blockquote_lines = []

    for raw_line in body.splitlines():
        line = raw_line.rstrip()

        # Detect section headers — but only from states where a header is
        # structurally expected. Once IN_CONTEXT, header-lookalike lines are
        # content: context text quoting "## Directives" must not flip the
        # state machine back and silently drop the rest of the section.
        if line == "## Directives" and state == "OUTSIDE":
            state = "IN_DIRECTIVES"
            continue

        if line == "## Context" and state in ("OUTSIDE", "IN_DIRECTIVES", "IN_DIRECTIVE_BLOCK"):
            _flush_directive()
            state = "IN_CONTEXT"
            continue

        if state == "IN_DIRECTIVES":
            m_heading = _DIRECTIVE_HEADING_RE.match(line)
            if m_heading:
                # Start a new directive — flush any previous one
                _flush_directive()
                cur_id = m_heading.group(1)
                cur_content_from_heading = m_heading.group(2)
                state = "IN_DIRECTIVE_BLOCK"
                continue

        if state == "IN_DIRECTIVE_BLOCK":
            m_heading = _DIRECTIVE_HEADING_RE.match(line)
            if m_heading:
                _flush_directive()
                cur_id = m_heading.group(1)
                cur_content_from_heading = m_heading.group(2)
                continue

            m_subject = _SUBJECT_LINE_RE.match(line)
            if m_subject:
                cur_subject = m_subject.group(1).strip() or None
                continue

            m_source = _SOURCE_LINE_RE.match(line)
            if m_source:
                cur_source_scope = m_source.group(1)
                cur_source_skill = m_source.group(2)
                cur_created_at = m_source.group(3).strip()
                continue

            m_bq = _BLOCKQUOTE_RE.match(line)
            if m_bq:
                cur_blockquote_lines.append(m_bq.group(1))
                continue

        if state == "IN_CONTEXT":
            context_lines.append(raw_line)

    # Any trailing directive not yet flushed
    _flush_directive()

    # Collapse context lines and strip sentinel
    raw_context = "\n".join(context_lines).strip()
    context = "" if raw_context == _NONE_YET else raw_context

    return ScopeSummary(
        scope_id=scope_id,
        directives=directives,
        context=context,
        updated_at=updated_at,
        version=version,
        parent_version=parent_version,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SummaryStore:
    """Owns the on-disk markdown files for all scope summaries.

    Each scope's summary lives at ``<summaries_dir>/<scope_id>.md``.
    Files are written atomically (write to ``.tmp``, then
    :func:`os.replace`) so readers never see a partial file.

    Args:
        summaries_dir: Root directory that holds the per-scope markdown files.
            Created on construction if it does not already exist.
    """

    def __init__(self, summaries_dir: str) -> None:
        self._dir = Path(summaries_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def path_for(self, scope_id: str) -> Path:
        """Return the deterministic path for *scope_id*'s summary file.

        Pure — performs no I/O.
        """
        return self._dir / f"{scope_id}.md"

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def read(self, scope_id: str) -> ScopeSummary | None:
        """Return the parsed :class:`ScopeSummary` for *scope_id*, or ``None``.

        Returns ``None`` if no summary file exists for the scope.
        """
        path = self.path_for(scope_id)
        if not path.exists():
            return None
        return _parse_summary(path.read_text(encoding="utf-8"))

    def write(self, scope_id: str, summary: ScopeSummary) -> ScopeSummary:
        """Persist *summary* to disk, overwriting any existing file.

        Bumps ``summary.version`` by reading the current on-disk version first
        (or defaulting to 0 if no file exists) and writing ``current + 1``.
        Returns the summary as actually written (with the bumped version).

        Writes atomically: the new content lands in a ``.tmp`` sibling first,
        then :func:`os.replace` renames it to the final path so readers never
        observe a partial write.
        """
        final = self.path_for(scope_id)
        final.parent.mkdir(parents=True, exist_ok=True)

        # Determine the next version.
        existing = self.read(scope_id)
        next_version = (existing.version if existing is not None else 0) + 1

        # Build the summary that will actually be written.
        to_write = summary.model_copy(update={"version": next_version})

        tmp = final.with_suffix(".md.tmp")
        tmp.write_text(_render_summary(to_write), encoding="utf-8")
        os.replace(tmp, final)
        return to_write

    def delete(self, scope_id: str) -> bool:
        """Remove the summary file for *scope_id*.

        Returns:
            ``True`` if a file existed and was removed; ``False`` if there was
            nothing to remove.  Idempotent.
        """
        path = self.path_for(scope_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def exists(self, scope_id: str) -> bool:
        """Return ``True`` if a summary file exists for *scope_id*."""
        return self.path_for(scope_id).exists()

    def list_scopes_with_summaries(self) -> list[str]:
        """Return scope IDs that have a summary file on disk.

        Parses filenames from the summaries directory.  Ignores:

        * Hidden files (names starting with ``.``).
        * Temporary files (names ending with ``.tmp``).
        * Anything not matching the ``<scope_id>.md`` pattern.
        """
        ids: list[str] = []
        for entry in self._dir.iterdir():
            name = entry.name
            if name.startswith("."):
                continue
            if not name.endswith(".md"):
                continue
            if entry.suffix != ".md":
                # extra guard — shouldn't be needed given the above check
                continue
            ids.append(name[:-3])  # strip ".md"
        return sorted(ids)
