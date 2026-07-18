"""Additive brownfield-install machinery — the ``strata register`` engine.

This is the stable, documented import surface for the additive install
operations Strata performs when it wires itself into a foreign project:

* the additive ``.claude/settings.json`` merge (an ``mcpServers`` entry is
  added only when absent — user state is never overwritten),
* skill copying into ``.claude/skills/`` (each skill is copied only when
  absent), and
* ``--diff`` line rendering (the read-only "what would change" view).

The rules live here, once. ``strata register`` (:mod:`strata.__main__`) is
built on this module rather than re-implementing them, and ADR 0009 D3 makes
this boundary the sanctioned dependency for the ``memfleet`` cloud client
(strata-web ``client/``): the client reuses these additive-merge semantics
instead of forking them, so there is exactly one implementation. Everything
here is import-name / CLI-name agnostic — it operates on the engine's install
artifacts (the ``strata`` MCP server entry, the ``strata*`` skills), which are
unchanged by the ADR 0009 distribution rename.

The additive rules themselves are ADR 0005 Decision 6 ("strictly additive —
never overwrite user state"); the reverse operations (:func:`remove_gitignore_block`,
:func:`skill_matches_shipped`) back ``strata unregister`` and only ever remove
an artifact that still byte-matches what register wrote.

Vocabulary follows CONTEXT.md exactly: scope, fleet, skill, scope-manager.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

__all__ = [
    "MCP_SERVER_NAME",
    "MCP_ENTRY",
    "SKILL_NAMES",
    "GITIGNORE_MARKER",
    "GITIGNORE_BLOCK",
    "CONFIG_TOML",
    "is_v1_2_shape_mcp_entry",
    "mcp_server_present",
    "merge_mcp_server",
    "copy_skill",
    "skill_matches_shipped",
    "remove_gitignore_block",
    "render_action_line",
]

# ---------------------------------------------------------------------------
# Canonical install artifacts
# ---------------------------------------------------------------------------

#: Key under ``.claude/settings.json``'s ``mcpServers`` block for the engine's
#: MCP server. This is the import/CLI name (``strata``), unchanged by the
#: ADR 0009 distribution rename — only ``pip install`` names moved.
MCP_SERVER_NAME = "strata"

#: The canonical ``mcpServers.strata`` entry ``strata register`` merges in and
#: ``strata unregister`` removes (only when the on-disk entry still matches it
#: byte-for-byte). ``strata-mcp`` is resolved on ``PATH`` (ADR 0005 Decision 1).
MCP_ENTRY: dict = {"command": "strata-mcp", "env": {}}

#: The canonical Claude Code skills vendored as package data under
#: ``strata/_skills`` and copied into a project's ``.claude/skills/``.
SKILL_NAMES = ("strata", "strata-worker", "strata-inspect")

#: Marker line identifying register's managed ``.gitignore`` block. Matched as
#: an exact line, not a loose ``# Strata`` substring — a user comment like
#: ``# Strata console output`` must not be mistaken for the managed block.
GITIGNORE_MARKER = "# Strata — managed by `strata register`"

#: The managed ``.gitignore`` block register appends (idempotent — detected by
#: :data:`GITIGNORE_MARKER`). ``fleet.yaml`` is deliberately not ignored: it is
#: the team's org chart and must be committed.
GITIGNORE_BLOCK = """\
# Strata — managed by `strata register` — do not remove this line
.strata/.venv/
.strata/strata.db*
.strata/summaries/
# fleet.yaml is intentionally NOT listed above — commit it (it is your team's org chart).
"""

#: Default ``.strata/config.toml`` contents (relative, portable storage paths).
CONFIG_TOML = """\
# Strata per-project configuration — managed by `strata register`.
# Paths are relative to this project's root.
db = ".strata/strata.db"
fleet_yaml = ".strata/fleet.yaml"
summaries_dir = ".strata/summaries"
"""


# ---------------------------------------------------------------------------
# settings.json — additive mcpServers merge (ADR 0005 Decision 6)
# ---------------------------------------------------------------------------


def is_v1_2_shape_mcp_entry(entry: dict) -> bool:
    """Return ``True`` if *entry* matches a known-stale V1.2 ``mcpServer`` shape.

    V1.2 settings shipped::

        command: python
        args: ["-m", "mcp_server.strata_mcp"]
        env: { "STRATA_BACKEND_URL": "...", ... }

    All three of those break on V1.3:

    - ``mcp_server`` is no longer a top-level module (folded into ``strata.mcp``).
    - ``STRATA_BACKEND_URL`` is no longer consumed (embedded mode, ADR 0004 D1).

    Recognising *any* of these signals is enough to warn. The caller stays
    strictly additive — it never rewrites the entry — but can surface the
    upgrade-path issue at register time, when the user is in fix-mind.
    """
    if entry.get("command") == "python":
        args = entry.get("args") or []
        if isinstance(args, list) and "-m" in args:
            tail = args[args.index("-m") + 1 :]
            if tail and "mcp_server" in tail[0]:
                return True
    env = entry.get("env") or {}
    return isinstance(env, dict) and "STRATA_BACKEND_URL" in env


def mcp_server_present(settings_data: dict, name: str = MCP_SERVER_NAME) -> bool:
    """Return whether ``settings_data['mcpServers'][name]`` already exists.

    Args:
        settings_data: Parsed ``settings.json`` contents.
        name: The ``mcpServers`` key to check (default :data:`MCP_SERVER_NAME`).
    """
    mcp_servers = settings_data.get("mcpServers", {})
    return isinstance(mcp_servers, dict) and name in mcp_servers


def merge_mcp_server(
    settings_data: dict,
    *,
    name: str = MCP_SERVER_NAME,
    entry: dict = MCP_ENTRY,
) -> bool:
    """Additively merge *entry* under ``settings_data['mcpServers'][name]``.

    Strictly additive (ADR 0005 Decision 6): an existing entry for *name* is
    left untouched and every other key in *settings_data* is preserved. The
    ``mcpServers`` block is created only when absent.

    Args:
        settings_data: Parsed ``settings.json`` contents, mutated in place.
        name: The ``mcpServers`` key to write (default :data:`MCP_SERVER_NAME`).
        entry: The entry to add when absent (default :data:`MCP_ENTRY`).

    Returns:
        ``True`` if the entry was added, ``False`` if one already existed and
        was left as the user had it.
    """
    mcp_servers = settings_data.setdefault("mcpServers", {})
    if name in mcp_servers:
        return False
    # Deep-copy so the caller owns the written entry outright: the default is
    # the shared module-level MCP_ENTRY, and a caller that later edits the
    # merged entry must not mutate that global.
    mcp_servers[name] = copy.deepcopy(entry)
    return True


# ---------------------------------------------------------------------------
# skills — additive copy into .claude/skills/ (ADR 0005 Decision 6)
# ---------------------------------------------------------------------------


def copy_skill(
    skills_root: Traversable | Path,
    skill_name: str,
    dest_skills_dir: Path,
    *,
    dry_run: bool = False,
) -> bool:
    """Copy ``<skills_root>/<skill_name>/Skill.md`` into *dest_skills_dir*.

    Strictly additive: if ``<dest_skills_dir>/<skill_name>/`` already exists it
    is left untouched — a user's customised skill is never overwritten.

    Args:
        skills_root: The vendored skills root, e.g.
            ``importlib.resources.files("strata") / "_skills"`` (a Traversable),
            or any directory ``Path`` laid out the same way.
        skill_name: One of :data:`SKILL_NAMES`.
        dest_skills_dir: The project's ``.claude/skills`` directory.
        dry_run: When ``True``, compute the outcome but write nothing.

    Returns:
        ``True`` if the skill was copied, ``False`` if the destination already
        existed and was left in place.
    """
    dest_skill_dir = Path(dest_skills_dir) / skill_name
    if dest_skill_dir.exists():
        return False
    if not dry_run:
        skill_src = skills_root / skill_name / "Skill.md"
        dest_skill_dir.mkdir(parents=True, exist_ok=True)
        (dest_skill_dir / "Skill.md").write_text(
            skill_src.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return True


def skill_matches_shipped(installed_md: Path, skill_name: str) -> bool | None:
    """Return whether an installed skill's ``Skill.md`` matches the shipped copy.

    The byte-identity check that lets ``strata unregister`` remove a skill only
    when it still matches what register wrote.

    Returns:
        - ``True``  — the installed ``Skill.md`` is byte-identical to the
          version shipped in the running distribution
          (``strata/_skills/<name>``); safe to delete.
        - ``False`` — it differs (user-edited, or an older Strata version);
          leave it and report.
        - ``None``  — the shipped reference could not be read, so a match
          cannot be proven; treat conservatively as "leave it".
    """
    import importlib.resources  # noqa: PLC0415

    try:
        shipped = importlib.resources.files("strata") / "_skills" / skill_name / "Skill.md"
        shipped_text = shipped.read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError):
        return None
    try:
        installed_text = installed_md.read_text(encoding="utf-8")
    except OSError:
        return None
    return installed_text == shipped_text


# ---------------------------------------------------------------------------
# .gitignore — managed block removal (reverse of the additive append)
# ---------------------------------------------------------------------------


def remove_gitignore_block(text: str) -> tuple[str, str]:
    """Remove register's managed ``.gitignore`` block from *text*.

    Returns ``(new_text, status)`` where *status* is one of:

    - ``"removed"``  — the verbatim managed block was found and stripped, along
      with the single blank-line separator register prepends, so the
      surrounding lines stay byte-identical.
    - ``"edited"``   — the managed marker line is present but the block no
      longer matches verbatim (the user edited inside it); *text* is returned
      unchanged so nothing user-authored is destroyed.
    - ``"absent"``   — no managed marker at all; nothing to do.
    """
    if GITIGNORE_BLOCK in text:
        # Register appends "\n" + GITIGNORE_BLOCK (a blank-line separator
        # before the block). Strip that separator too so a `.gitignore` that
        # ended in a newline before register round-trips byte-for-byte.
        sep_block = "\n" + GITIGNORE_BLOCK
        if sep_block in text:
            return text.replace(sep_block, "", 1), "removed"
        return text.replace(GITIGNORE_BLOCK, "", 1), "removed"
    if GITIGNORE_MARKER in text:
        return text, "edited"
    return text, "absent"


# ---------------------------------------------------------------------------
# --diff rendering
# ---------------------------------------------------------------------------


def render_action_line(
    action: str,
    rel_path: str | Path,
    *,
    diff_mode: bool,
    skipped: bool,
) -> str:
    """Render one register action line for the console.

    In ``--diff`` mode the wording is the read-only "what would change" view
    (``[would create/update]`` / ``[unchanged]``); otherwise it is the applied
    wording (``<action>: <path>`` / ``kept user's <path>``).

    Args:
        action: The applied-mode verb phrase, e.g. ``"created"`` or
            ``"merged strata into"``. Ignored for skipped lines.
        rel_path: Path to show, normally relative to the project root.
        diff_mode: Whether register is running read-only (``--diff``).
        skipped: Whether the artifact already existed and was left in place.

    Returns:
        A single formatted line (two-space indented), without a trailing newline.
    """
    if diff_mode:
        return f"  [unchanged]  {rel_path}" if skipped else f"  [would create/update]  {rel_path}"
    return f"  kept user's {rel_path}" if skipped else f"  {action}: {rel_path}"
