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
this boundary a supported public import surface: a tool that installs Strata
wiring into a project reuses these additive-merge semantics instead of forking
them, so there is exactly one implementation. Everything
here is import-name / CLI-name agnostic — it operates on the engine's install
artifacts (the ``strata`` MCP server entry, the ``strata*`` skills), which are
unchanged by the ADR 0009 distribution rename.

The additive rules themselves are ADR 0005 Decision 6 ("strictly additive —
never overwrite user state"); the reverse operations (:func:`remove_gitignore_block`,
:func:`skill_matches_shipped`) back ``strata unregister`` and only ever remove
an artifact that still byte-matches what the current release, or a known
historical release, of register would have written (:data:`_HISTORICAL_ARTIFACT_HASHES`
and ``_classify_drift`` — the same current-or-historical mechanism register's
self-update already uses).

Vocabulary follows CONTEXT.md exactly: scope, fleet, skill, scope-manager.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

__all__ = [
    "MCP_SERVER_NAME",
    "MCP_ENTRY",
    "SKILL_NAMES",
    "HOOK_SCRIPT_NAME",
    "HOOK_COMMAND",
    "HOOK_STOP_ENTRY",
    "GITIGNORE_MARKER",
    "GITIGNORE_BLOCK",
    "GITIGNORE_BLOCK_HISTORICAL",
    "CONFIG_TOML",
    "is_v1_2_shape_mcp_entry",
    "mcp_server_present",
    "merge_mcp_server",
    "copy_skill",
    "skill_matches_shipped",
    "stop_hook_present",
    "merge_stop_hook",
    "remove_stop_hook",
    "copy_hook",
    "hook_matches_shipped",
    "classify_skill_drift",
    "self_update_skill",
    "classify_hook_drift",
    "self_update_hook",
    "AGENTS_MD_END_MARKER",
    "classify_agents_md_drift",
    "self_update_agents_md_block",
    "remove_gitignore_block",
    "render_action_line",
    "CODEX_MCP_MARKER",
    "CODEX_MCP_BLOCK",
    "CODEX_MCP_BLOCK_HISTORICAL",
    "CODEX_HOOK_MARKER",
    "CODEX_HOOK_BLOCK",
    "CODEX_HOOK_BLOCK_HISTORICAL",
    "codex_config_path",
    "codex_mcp_present",
    "merge_codex_mcp_server",
    "remove_codex_mcp_server",
    "strip_orphaned_mcp_strata_tables",
    "codex_hook_present",
    "merge_codex_freshness_hook",
    "remove_codex_freshness_hook",
    "KNOWN_HARNESSES",
    "detect_harnesses",
    "set_default_harness",
    "read_default_harness_from_text",
    "AGENTS_MD_MARKER",
    "agents_md_present",
    "merge_agents_md",
    "remove_agents_md",
    "gitignore_covers_dotenv",
    "write_env_judge_key",
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

#: The vendored ``Stop``-hook script (package data under ``strata/_hooks``),
#: copied into a project's ``.claude/hooks/`` by ``strata register`` (issue #112).
#: A POSIX-``sh`` wrapper that ``exec``s ``strata freshness-hook`` so the running
#: engine is resolved on ``PATH`` like ``strata-mcp`` — no interpreter coupling.
HOOK_SCRIPT_NAME = "strata-stop-hook"

#: The shell command the merged ``hooks.Stop`` entry runs. References the
#: installed wrapper under ``$CLAUDE_PROJECT_DIR`` (the project dir Claude Code
#: exports to hooks) and runs it through ``sh`` so no executable bit is required
#: for the hook to fire. The ``strata-stop-hook`` substring is the marker
#: :func:`stop_hook_present` / :func:`remove_stop_hook` match on.
HOOK_COMMAND = 'sh "$CLAUDE_PROJECT_DIR/.claude/hooks/strata-stop-hook"'

#: Marker substring identifying Strata's own ``hooks.Stop`` command, so the
#: additive merge never mistakes a user's unrelated Stop hook for ours.
_HOOK_COMMAND_MARKER = "strata-stop-hook"

#: The canonical ``hooks.Stop`` group ``strata register`` merges in and
#: ``strata unregister`` removes (only when the on-disk group still matches it
#: byte-for-byte). A single-matcher group with one command hook, matching the
#: Claude Code ``Stop``-hook shape.
HOOK_STOP_ENTRY: dict = {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}

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
.env
# fleet.yaml is intentionally NOT listed above — commit it (it is your team's org chart).
"""

#: Block text register wrote in every release through v1.10.2 (the ``.env``
#: line was folded in at v1.10.3). ``strata unregister`` treats a project
#: registered under one of those older releases the same as one registered
#: under the current release: an unmodified historical block is OURS and is
#: removed cleanly, not flagged as user-edited (round-4 unregister fix, bug
#: B — a project registered under 1.10.2 and unregistered under 1.10.3 saw
#: this exact block misclassified as "edited").
GITIGNORE_BLOCK_HISTORICAL: tuple[str, ...] = (
    "# Strata — managed by `strata register` — do not remove this line\n"
    ".strata/.venv/\n"
    ".strata/strata.db*\n"
    ".strata/summaries/\n"
    "# fleet.yaml is intentionally NOT listed above — commit it (it is your team's org chart).\n",
)

#: Default ``.strata/config.toml`` contents (relative, portable storage paths).
CONFIG_TOML = """\
# Strata per-project configuration — managed by `strata register`.
# Paths are relative to this project's root.
db = ".strata/strata.db"
fleet_yaml = ".strata/fleet.yaml"
summaries_dir = ".strata/summaries"
"""


# ---------------------------------------------------------------------------
# Self-update — three-state resolution for shipped artifacts (register's
# self-update mechanism).
#
# ``strata register`` re-run today always keeps an existing managed artifact
# (skills, the Stop-hook script, the AGENTS.md block) exactly as it found it
# — so a guidance fix shipped in a new release never reaches an
# already-registered project unless the user deletes the file by hand. This
# table lets register tell "the shipped content changed since this file was
# written, and the user never touched it" (safe to refresh) apart from "the
# user edited this file" (never touch it, ever) — both of which today look
# identical: `installed_text != shipped_text`.
#
# For each managed artifact this records:
#
# - ``"current"``: the sha256 of the content shipped by *this* release.
# - ``"historical"``: the sha256 of every *previous* shipped version — an
#   installed file whose hash lands in this set was written by an older
#   `strata register` and never hand-edited, so it is safe to overwrite with
#   the current shipped content.
#
# MAINTENANCE — read this before editing a shipped artifact:
#
#   Every release that changes a shipped artifact's content (a skill's
#   ``Skill.md``, the Stop-hook script, or ``_templates/AGENTS-strata.md``)
#   MUST, in the same change:
#
#     1. move that artifact's current ``"current"`` hash into its
#        ``"historical"`` set, and
#     2. replace ``"current"`` with the sha256 of the new content.
#
#   Forgetting step 1 means a project running the previous release's content
#   forever reads as "user-edited" and register will never refresh it. The
#   ``test_release_discipline_hashes_are_current`` test in
#   ``tests/test_register_self_update.py`` fails the build if ``"current"``
#   here doesn't match the artifact's actual shipped content — so this table
#   can never silently go stale — but it cannot catch a forgotten step 1: it
#   only proves the *current* recorded, not that the *previous* one moved to
#   history. Do it by hand, every time.
#
# Hashes were extracted from the shipped content at tags v1.10.0, v1.10.1,
# and v1.10.2 (`git show <tag>:<path>` — see the self-update PR for the
# script). The AGENTS.md template first shipped in v1.10.1; v1.10.0 has no
# entry to contribute. All three tags shipped byte-identical content for
# every artifact except ``"strata"`` (the skill) and ``"agents-md"``, which
# both changed again after v1.10.2 — the "current" values below reflect that
# newer content.
# ---------------------------------------------------------------------------

_HISTORICAL_ARTIFACT_HASHES: dict[str, dict[str, object]] = {
    "strata": {
        "current": "1620f11a24ec0d3b1d4e776315e02ff343bcc768b6c6b955a7a4ce865058ba84",
        "historical": frozenset(
            {
                "5865b8090923c1dd0d3f747490b006b4f0b3b4d18bceffb99ef7fb2fe9e577ab",
                "81a0a8debdb183603a985459c17b7e4800e65a5f5ff7b9cb7c7f0526d133087c",
            }
        ),
    },
    "strata-worker": {
        "current": "110a29368e69e49a45a567f6a91ed3898fa2ebb779a6cb665cb0dde72f79157b",
        "historical": frozenset(
            {"2e2cb5d953d6e97377a347134ebbef989f85f53cb7604a50e53f2516153831e0"}
        ),
    },
    "strata-inspect": {
        "current": "848e7e35726620cf54cab6d19657fcb2e43d71c30ed4ca49bc1cbae2ea6f857e",
        "historical": frozenset(
            {"707793640183dfd5e503c57c48133c8d56747b74a49268bdd76b8cc77335f2ad"}
        ),
    },
    "strata-stop-hook": {
        "current": "d950ad8fcf436069b49d247543ab6a4a2c98481d6ef853e5c78a5289e0f5c582",
        "historical": frozenset(),
    },
    "codex-mcp": {
        # Round-4 unregister fix, bug B (release-discipline extension): the
        # CODEX_MCP_BLOCK constant itself carries the current shipped text
        # (and CODEX_MCP_BLOCK_HISTORICAL the historical text variants
        # remove_codex_mcp_server tries), but this entry exists purely so
        # test_release_discipline_hashes_are_current fails the build the
        # same way it does for the skills/hook/AGENTS.md the moment
        # CODEX_MCP_BLOCK's content changes without a historical entry being
        # added — the exact class of bug this whole fix wave closed.
        "current": "ffe7e644bb000b4d8ce5d154364c9646975aa0152494d7b46c3cb834056ef562",
        "historical": frozenset(),
    },
    "codex-hook": {
        # See "codex-mcp" above — same guardrail, for CODEX_HOOK_BLOCK /
        # CODEX_HOOK_BLOCK_HISTORICAL.
        "current": "df9f579f636015b4060cdabf01ee598698506c7011baba616e7b9ca1620768bf",
        "historical": frozenset(),
    },
    "agents-md": {
        "current": "d4e9b4da5543ad2faf75bedb17c426a8dab72e858955b1a48e7b718f72092aca",
        "historical": frozenset(
            {
                "f6df7e82395ba3199d3a52c1651db8afb93b2fdd139b496974f871d453535b68",
                "ceea6568ab9160d54f2d3edd7131bc9b3bf2a694832226e67e7553de7325825f",
            }
        ),
    },
}


def _sha256_text(text: str) -> str:
    """Return the hex sha256 digest of *text*, encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _historical_hashes(artifact_key: str) -> frozenset:
    return _HISTORICAL_ARTIFACT_HASHES[artifact_key]["historical"]  # type: ignore[return-value]


def _classify_drift(existing_text: str, shipped_text: str, artifact_key: str) -> str:
    """Three-state classification shared by every self-update artifact type.

    Returns:
        - ``"match"``  — *existing_text* is byte-identical to *shipped_text*.
        - ``"stale"``  — differs, but its hash is a known-historical shipped
          hash for *artifact_key* — a previous `strata register` wrote it and
          it was never hand-edited; safe to overwrite.
        - ``"edited"`` — differs, and its hash isn't recognized — user-edited
          (or from a release this table doesn't know about); never touched.
    """
    if existing_text == shipped_text:
        return "match"
    if _sha256_text(existing_text) in _historical_hashes(artifact_key):
        return "stale"
    return "edited"


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


def _read_shipped_skill_text(skill_name: str) -> str | None:
    """Read the shipped ``Skill.md`` text for *skill_name*, or ``None`` if unreadable."""
    import importlib.resources  # noqa: PLC0415

    try:
        shipped = importlib.resources.files("strata") / "_skills" / skill_name / "Skill.md"
        return shipped.read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError):
        return None


def classify_skill_drift(installed_md: Path, skill_name: str) -> str:
    """Classify an installed skill's ``Skill.md`` against shipped + historical hashes.

    The three-state resolution register's self-update mechanism is built on
    (see :data:`_HISTORICAL_ARTIFACT_HASHES`). Returns one of:

    - ``"match"``   — byte-identical to the currently shipped copy.
    - ``"stale"``   — differs, but its hash is a known-historical shipped
      hash — a previous ``strata register`` wrote it and it was never
      hand-edited; safe to self-update.
    - ``"edited"``  — differs, and its hash isn't recognized — user-edited;
      never touched.
    - ``"unknown"`` — the shipped reference or the installed file could not
      be read, so no classification can be proven; treat conservatively like
      ``"edited"`` (leave it).
    """
    shipped_text = _read_shipped_skill_text(skill_name)
    if shipped_text is None:
        return "unknown"
    try:
        installed_text = installed_md.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    return _classify_drift(installed_text, shipped_text, skill_name)


def self_update_skill(installed_md: Path, skill_name: str, *, dry_run: bool = False) -> str:
    """Self-update *installed_md* to the current shipped content when it's stale.

    Classifies via :func:`classify_skill_drift`; when the result is
    ``"stale"`` the file is overwritten with the current shipped content
    (unless *dry_run*, in which case nothing is written but the status is
    still returned so the caller can report what would happen).
    ``"match"``/``"edited"``/``"unknown"`` never write.

    Returns:
        The classification (``"match"``/``"stale"``/``"edited"``/``"unknown"``).
    """
    status = classify_skill_drift(installed_md, skill_name)
    if status == "stale" and not dry_run:
        shipped_text = _read_shipped_skill_text(skill_name)
        if shipped_text is not None:
            installed_md.write_text(shipped_text, encoding="utf-8")
    return status


def skill_matches_shipped(installed_md: Path, skill_name: str) -> bool | None:
    """Return whether an installed skill's ``Skill.md`` matches the shipped copy.

    The byte-identity check that lets ``strata unregister`` remove a skill only
    when it still matches what register wrote. Built on
    :func:`classify_skill_drift` so the two never diverge on what "matches"
    means.

    Returns:
        - ``True``  — the installed ``Skill.md`` is byte-identical to the
          version currently shipped in the running distribution
          (``strata/_skills/<name>``), OR to a known-historical shipped
          version (a previous ``strata register`` wrote it and it was never
          hand-edited — round-4 unregister fix, bug B); safe to delete
          either way.
        - ``False`` — genuinely user-edited: it differs, and its hash isn't
          recognized as any shipped version, current or historical; leave it
          and report.
        - ``None``  — the shipped reference could not be read, so a match
          cannot be proven; treat conservatively as "leave it".
    """
    status = classify_skill_drift(installed_md, skill_name)
    if status == "unknown":
        return None
    return status in ("match", "stale")


# ---------------------------------------------------------------------------
# settings.json — additive hooks.Stop merge (ADR 0005 Decision 6; issue #112)
# ---------------------------------------------------------------------------


def _stop_hook_groups(settings_data: dict) -> list:
    """Return the ``hooks.Stop`` group list, or ``[]`` when absent/malformed."""
    hooks = settings_data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    stop = hooks.get("Stop")
    return stop if isinstance(stop, list) else []


def _group_references_hook(group: object) -> bool:
    """Return whether *group* contains a command hook running Strata's Stop hook."""
    if not isinstance(group, dict):
        return False
    for hook in group.get("hooks", []) or []:
        if isinstance(hook, dict) and _HOOK_COMMAND_MARKER in str(hook.get("command", "")):
            return True
    return False


def stop_hook_present(settings_data: dict) -> bool:
    """Return whether a Strata ``Stop`` hook is already merged into *settings_data*.

    Detected by the :data:`_HOOK_COMMAND_MARKER` substring in a ``hooks.Stop``
    command — so a user's own, unrelated Stop hooks never count as present, and
    an entry the user lightly edited around our command is still recognised as
    ours (idempotence: register won't add a second copy).
    """
    return any(_group_references_hook(g) for g in _stop_hook_groups(settings_data))


def merge_stop_hook(settings_data: dict, *, entry: dict = HOOK_STOP_ENTRY) -> bool:
    """Additively merge *entry* into ``settings_data['hooks']['Stop']``.

    Strictly additive (ADR 0005 Decision 6): a user's existing ``Stop`` hooks —
    and every other key in *settings_data* and ``hooks`` — are preserved; the
    Strata group is appended, never substituted. The ``hooks`` dict and ``Stop``
    list are created only when absent.

    Returns:
        ``True`` if the Strata group was appended, ``False`` if one was already
        present and the settings were left as the user had them.
    """
    if stop_hook_present(settings_data):
        return False
    hooks = settings_data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        # A malformed user "hooks" value: never clobber it — report not-added.
        return False
    stop = hooks.setdefault("Stop", [])
    if not isinstance(stop, list):
        return False
    # Deep-copy so the caller owns the written group outright (the default is the
    # shared module-level HOOK_STOP_ENTRY — a later edit must not mutate it).
    stop.append(copy.deepcopy(entry))
    return True


def remove_stop_hook(settings_data: dict, *, entry: dict = HOOK_STOP_ENTRY) -> str:
    """Remove Strata's ``hooks.Stop`` group from *settings_data*, in place.

    The reverse of :func:`merge_stop_hook`, honouring the strict-additive rule
    in reverse: the group is removed only when it still byte-matches *entry*.
    Empty ``Stop`` / ``hooks`` containers register created are cleaned up so an
    unregister round-trips a project that had no hooks before.

    Returns one of:

    - ``"removed"`` — the canonical Strata group was found and stripped.
    - ``"edited"``  — a Strata Stop command is present but its group no longer
      matches *entry* (the user edited it); *settings_data* is left unchanged.
    - ``"absent"``  — no Strata Stop hook at all; nothing to do.
    """
    groups = _stop_hook_groups(settings_data)
    ours = [g for g in groups if _group_references_hook(g)]
    if not ours:
        return "absent"
    if not all(g == entry for g in ours):
        # A user-edited Strata group — leave everything as-is.
        return "edited"
    remaining = [g for g in groups if not _group_references_hook(g)]
    hooks = settings_data["hooks"]
    if remaining:
        hooks["Stop"] = remaining
    else:
        del hooks["Stop"]
        if not hooks:
            del settings_data["hooks"]
    return "removed"


# ---------------------------------------------------------------------------
# hooks — additive copy into .claude/hooks/ (ADR 0005 Decision 6; issue #112)
# ---------------------------------------------------------------------------


def copy_hook(
    hooks_root: Traversable | Path,
    dest_hooks_dir: Path,
    *,
    dry_run: bool = False,
) -> bool:
    """Copy the vendored ``strata-stop-hook`` script into *dest_hooks_dir*.

    Strictly additive, like :func:`copy_skill`: an existing
    ``<dest_hooks_dir>/strata-stop-hook`` is left untouched. The copied script is
    marked executable so a harness may invoke it directly, in addition to the
    merged ``sh``-prefixed :data:`HOOK_COMMAND`.

    Args:
        hooks_root: The vendored hooks root, e.g.
            ``importlib.resources.files("strata") / "_hooks"`` (a Traversable),
            or any directory ``Path`` laid out the same way.
        dest_hooks_dir: The project's ``.claude/hooks`` directory.
        dry_run: When ``True``, compute the outcome but write nothing.

    Returns:
        ``True`` if the script was copied, ``False`` if the destination already
        existed and was left in place.
    """
    dest = Path(dest_hooks_dir) / HOOK_SCRIPT_NAME
    if dest.exists():
        return False
    if not dry_run:
        src = hooks_root / HOOK_SCRIPT_NAME
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        dest.chmod(0o755)
    return True


def _read_shipped_hook_text() -> str | None:
    """Read the shipped Stop-hook script text, or ``None`` if unreadable."""
    import importlib.resources  # noqa: PLC0415

    try:
        shipped = importlib.resources.files("strata") / "_hooks" / HOOK_SCRIPT_NAME
        return shipped.read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError):
        return None


def classify_hook_drift(installed_script: Path) -> str:
    """Classify an installed hook script against shipped + historical hashes.

    Mirrors :func:`classify_skill_drift`; see its docstring for the four
    ``"match"``/``"stale"``/``"edited"``/``"unknown"`` states.
    """
    shipped_text = _read_shipped_hook_text()
    if shipped_text is None:
        return "unknown"
    try:
        installed_text = installed_script.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    return _classify_drift(installed_text, shipped_text, "strata-stop-hook")


def self_update_hook(installed_script: Path, *, dry_run: bool = False) -> str:
    """Self-update *installed_script* to the current shipped content when stale.

    Mirrors :func:`self_update_skill`. The executable bit is re-applied after
    a write, matching :func:`copy_hook`.
    """
    status = classify_hook_drift(installed_script)
    if status == "stale" and not dry_run:
        shipped_text = _read_shipped_hook_text()
        if shipped_text is not None:
            installed_script.write_text(shipped_text, encoding="utf-8")
            installed_script.chmod(0o755)
    return status


def hook_matches_shipped(installed_script: Path) -> bool | None:
    """Return whether an installed hook script matches the shipped copy.

    The byte-identity check (mirroring :func:`skill_matches_shipped`) that lets
    ``strata unregister`` remove the hook script only when it still matches what
    register wrote. Built on :func:`classify_hook_drift`.

    Returns:
        - ``True``  — byte-identical to the currently shipped
          ``strata/_hooks`` copy, OR to a known-historical shipped version
          (round-4 unregister fix, bug B, mirroring
          :func:`skill_matches_shipped`).
        - ``False`` — genuinely user-edited: it differs, and its hash isn't
          recognized as any shipped version, current or historical.
        - ``None``  — the shipped reference could not be read; treat as "leave it".
    """
    status = classify_hook_drift(installed_script)
    if status == "unknown":
        return None
    return status in ("match", "stale")


# ---------------------------------------------------------------------------
# .gitignore — managed block removal (reverse of the additive append)
# ---------------------------------------------------------------------------


def remove_gitignore_block(text: str) -> tuple[str, str]:
    """Remove register's managed ``.gitignore`` block from *text*.

    Tries the current :data:`GITIGNORE_BLOCK` first, then each block in
    :data:`GITIGNORE_BLOCK_HISTORICAL` — a project registered under an older
    Strata release wrote an older, still-unmodified block, and that is just
    as much "ours" to remove as the current one (round-4 unregister fix, bug
    B). See :func:`_remove_block` for the shared implementation.

    Returns ``(new_text, status)`` where *status* is one of:

    - ``"removed"``  — a verbatim managed block (current or historical) was
      found and stripped, along with the single blank-line separator
      register prepends, so the surrounding lines stay byte-identical.
    - ``"edited"``   — the managed marker line is present but no known block
      variant matches verbatim (the user edited inside it); *text* is
      returned unchanged so nothing user-authored is destroyed.
    - ``"absent"``   — no managed marker at all; nothing to do.
    """
    return _remove_block(text, (GITIGNORE_BLOCK, *GITIGNORE_BLOCK_HISTORICAL), GITIGNORE_MARKER)


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


# ---------------------------------------------------------------------------
# Codex CLI config.toml — additive TOML-text merge (Task 6.2, local-launch-bar)
#
# Source of truth: docs/marketing/CODEX-surface-2026-08.md (verified against
# OpenAI's Codex docs and reproduced hands-on against codex-cli 0.149.0,
# 2026-08-23). Only claims that document marks [verified] are built as
# "operational"; the Stop-hook block below is schema-verified only — the
# marketing doc could not confirm a plain command-type Stop hook actually
# fires or inherits the launching process's STRATA_AGENT_* env (no OpenAI
# credentials in that sandbox) — so it ships labelled "pending live
# verification" both in this module and in the merged TOML text itself.
#
# Codex's `config.toml` is unstructured TOML with user comments and tables
# register must not disturb, and this project has no TOML *writer* dependency
# (only stdlib `tomllib`, read-only). So — mirroring the existing
# GITIGNORE_BLOCK convention above rather than the JSON deep-merge used for
# settings.json — Codex's config is merged as an additive text append: a
# canonical block, appended once, detected by a marker so a re-run is a
# no-op and the user's own tables/comments are never rewritten.
# ---------------------------------------------------------------------------

#: Marker comment identifying Strata's managed `[mcp_servers.strata]` block.
#: Detected as a plain substring (mirrors GITIGNORE_MARKER) so a user's own,
#: unrelated `mcp_servers` tables are never mistaken for ours.
CODEX_MCP_MARKER = "# Strata — managed by `strata register --harness codex`"

#: [verified] TOML shape — reproduced byte-for-byte by `codex mcp add` against
#: codex-cli 0.149.0 (CODEX-surface-2026-08.md #1). MCP `env` values are
#: literal TOML strings (no `${VAR}` interpolation is documented anywhere in
#: the MCP config reference) so the identity vars ship as empty placeholders:
#: fill them in by hand, or export them before launching `codex` and rely on
#: the *unverified* assumption that Codex's MCP subprocess inherits the
#: launching process's environment on top of these literal values.
CODEX_MCP_BLOCK = f"""\
{CODEX_MCP_MARKER}
[mcp_servers.strata]
command = "strata-mcp"

[mcp_servers.strata.env]
STRATA_AGENT_SCOPE = ""
STRATA_AGENT_SKILL = ""
STRATA_AGENT_SESSION_ID = ""
"""

#: Marker comment identifying Strata's managed `hooks.Stop` block.
CODEX_HOOK_MARKER = "# Strata freshness hook — managed by `strata register --harness codex`"

#: [schema-verified, live firing NOT verified] — `codex exec --strict-config`
#: accepted this exact `[[hooks.Stop]]` / `[[hooks.Stop.hooks]]` shape on
#: codex-cli 0.149.0 without rejecting it (CODEX-surface-2026-08.md #2), which
#: confirms the binary understands the schema. It does NOT confirm the hook
#: process actually runs at `Stop`, nor that it inherits STRATA_AGENT_* env —
#: the findings sandbox had no OpenAI credentials, so no turn ever completed.
#: `[features] hooks = true` is deliberately omitted: codex-cli 0.149.0 ships
#: hooks on by default, and re-declaring a plain `[features]` table here would
#: conflict with (TOML forbids redefining) a user's own `[features]` table if
#: one already exists in their config.
CODEX_HOOK_BLOCK = f"""\
{CODEX_HOOK_MARKER}
# Schema accepted by `codex exec --strict-config` (codex-cli 0.149.0). Live
# Stop-hook firing and STRATA_AGENT_* env inheritance are NOT verified — see
# README "Using Strata with Codex CLI" before relying on this in production.
[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "strata freshness-hook"
timeout = 30
"""


def codex_config_path() -> Path:
    """Resolve the Codex CLI config file: ``$CODEX_HOME/config.toml``.

    Defaults to ``~/.codex/config.toml`` when ``$CODEX_HOME`` is unset — the
    verified default confirmed hands-on against codex-cli 0.149.0
    (CODEX-surface-2026-08.md #1: ``codex mcp add`` writes here; CLI
    management "only manages the ``~/.codex/config.toml`` (global) server
    table by default in this version"). Project-scoped
    ``<repo>/.codex/config.toml`` is documented as also read for trusted
    projects, but is not what ``codex mcp add``/register targets here.
    """
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home) if codex_home else Path.home() / ".codex"
    return base / "config.toml"


def _append_block(config_text: str, block: str) -> str:
    """Append *block* to *config_text*, matching GITIGNORE_BLOCK's separator rule."""
    prefix = config_text
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix:
        prefix += "\n"
    return prefix + block


def codex_mcp_present(config_text: str) -> bool:
    """Return whether a Strata ``[mcp_servers.strata]`` table is already present.

    Tries a real TOML parse first (via stdlib ``tomllib``) so a user who ran
    `codex mcp add strata ...` by hand — with different ``command``/``env``
    values, no marker comment — is still recognised and left untouched.
    Falls back to the :data:`CODEX_MCP_MARKER` substring (mirrors
    :data:`GITIGNORE_MARKER`) when the text does not parse as TOML (e.g. a
    mid-edit file), so register still refuses to double-append.
    """
    import tomllib  # noqa: PLC0415

    try:
        data = tomllib.loads(config_text) if config_text.strip() else {}
        servers = data.get("mcp_servers")
        if isinstance(servers, dict) and "strata" in servers:
            return True
    except tomllib.TOMLDecodeError:
        pass
    return CODEX_MCP_MARKER in config_text


def merge_codex_mcp_server(config_text: str) -> tuple[str, bool]:
    """Additively append :data:`CODEX_MCP_BLOCK` to *config_text*.

    Strictly additive, text-level (ADR 0005 Decision 6, applied to a config
    format this project has no TOML writer for): every existing table,
    comment, and blank line in *config_text* is preserved byte-for-byte; the
    Strata block is appended only when :func:`codex_mcp_present` is false.

    Returns:
        ``(new_text, added)`` — *added* is ``True`` if the block was
        appended, ``False`` (with *new_text* == *config_text*) if a Strata
        (or user-written) ``[mcp_servers.strata]`` entry already existed.
    """
    if codex_mcp_present(config_text):
        return config_text, False
    return _append_block(config_text, CODEX_MCP_BLOCK), True


def codex_hook_present(config_text: str) -> bool:
    """Return whether Strata's managed ``hooks.Stop`` block is already present.

    Detected by the :data:`CODEX_HOOK_MARKER` substring — a text-level marker
    (like :func:`codex_mcp_present`'s fallback) because ``[[hooks.Stop]]`` is
    a TOML array-of-tables: a plain structural parse can't tell "ours" apart
    from a user's own Stop hook entries the way a single-occurrence table can.
    """
    return CODEX_HOOK_MARKER in config_text


def merge_codex_freshness_hook(config_text: str) -> tuple[str, bool]:
    """Additively append :data:`CODEX_HOOK_BLOCK` to *config_text*.

    Strictly additive: a user's own ``[[hooks.Stop]]`` entries are untouched —
    TOML array-of-tables let both coexist — and the Strata block is appended
    only once (idempotent re-merge).

    Returns:
        ``(new_text, added)`` — as :func:`merge_codex_mcp_server`.
    """
    if codex_hook_present(config_text):
        return config_text, False
    return _append_block(config_text, CODEX_HOOK_BLOCK), True


def _remove_block(config_text: str, blocks: tuple[str, ...], marker: str) -> tuple[str, str]:
    """Remove the first of *blocks* that still byte-matches from *config_text*.

    *blocks* is ordered current-shipped-block first, then any known
    historical shipped variants (round-4 unregister fix, bug B): a project
    registered under an older Strata release wrote an older block, and that
    is just as much "ours" as the current one — trying each in turn lets a
    historical, unmodified block be recognized and removed cleanly instead of
    misreported as user-edited. Mirrors :func:`remove_gitignore_block`'s
    verbatim-block-then-marker-only fallback, applied to Codex's
    ``config.toml`` text.

    Returns ``(new_text, status)`` where *status* is one of:

    - ``"removed"`` — a verbatim managed block (current or historical) was
      found and stripped, along with the blank-line separator
      :func:`_append_block` prepends, so surrounding lines stay
      byte-identical.
    - ``"edited"``  — the managed marker is present but no known block
      variant matches verbatim (the user edited inside it); *config_text* is
      returned unchanged.
    - ``"absent"``  — no managed marker at all — including the case where
      register found a *user's own* pre-existing entry and left it untouched
      without ever writing our marker, so there is nothing for us to remove.
    """
    for block in blocks:
        if block in config_text:
            sep_block = "\n" + block
            if sep_block in config_text:
                return config_text.replace(sep_block, "", 1), "removed"
            return config_text.replace(block, "", 1), "removed"
    if marker in config_text:
        return config_text, "edited"
    return config_text, "absent"


#: No Codex ``[mcp_servers.strata]`` block variant has shipped yet besides
#: the current one (CODEX_MCP_BLOCK has been byte-identical across every
#: release that has shipped Codex support) — kept as an explicit empty tuple,
#: not a special case, so :func:`remove_codex_mcp_server` goes through the
#: same current-or-historical mechanism every other managed block does
#: (round-4 unregister fix, bug B: "wire the mechanism uniformly").
CODEX_MCP_BLOCK_HISTORICAL: tuple[str, ...] = ()

#: See :data:`CODEX_MCP_BLOCK_HISTORICAL` — same story for the freshness
#: Stop-hook block.
CODEX_HOOK_BLOCK_HISTORICAL: tuple[str, ...] = ()


def remove_codex_mcp_server(config_text: str) -> tuple[str, str]:
    """Remove Strata's managed ``[mcp_servers.strata]`` block from *config_text*.

    The reverse of :func:`merge_codex_mcp_server`, honouring the
    strict-additive rule in reverse: removed only when it still byte-matches
    the current or a historical :data:`CODEX_MCP_BLOCK`. See
    :func:`_remove_block` for the status values.

    This removes only the canonical parent block. A third party (most
    notably the Codex CLI itself, writing per-tool approval state) may have
    since appended its own ``[mcp_servers.strata.*]`` subtables — those are
    meaningless without this parent table and are swept up separately by
    :func:`strip_orphaned_mcp_strata_tables`, which the caller runs only
    after this function reports ``"removed"`` (round-4 unregister fix, bug A).
    """
    return _remove_block(
        config_text, (CODEX_MCP_BLOCK, *CODEX_MCP_BLOCK_HISTORICAL), CODEX_MCP_MARKER
    )


def remove_codex_freshness_hook(config_text: str) -> tuple[str, str]:
    """Remove Strata's managed ``hooks.Stop`` block from *config_text*.

    The reverse of :func:`merge_codex_freshness_hook`; see :func:`_remove_block`
    for the status values.
    """
    return _remove_block(
        config_text, (CODEX_HOOK_BLOCK, *CODEX_HOOK_BLOCK_HISTORICAL), CODEX_HOOK_MARKER
    )


#: Matches a TOML table header line — either a plain table (``[name]``) or an
#: array-of-tables (``[[name]]``, the shape :data:`CODEX_HOOK_BLOCK` itself
#: uses for ``[[hooks.Stop]]``). Section boundaries in Codex's config.toml
#: text are defined by *either* form; matching only ``[name]`` would let an
#: adjacent ``[[...]]`` header get swallowed into a preceding orphaned
#: ``mcp_servers.strata.*`` table's span instead of ending it.
_TOML_TABLE_HEADER_RE = re.compile(r"^(\[\[?)([^\[\]]+)(\]\]?)[ \t]*$", re.MULTILINE)


def _table_header_end(text: str, start: int, end: int) -> int:
    """Shrink *end* leftward over trailing blank/comment-only lines.

    Used when computing an orphaned table's removal span: the raw span runs
    from one header up to the next header, but a trailing run of blank lines
    and ``#`` comments right before that next header usually belongs to it
    (e.g. a comment introducing the table that follows) rather than to the
    orphaned table being swept away. Always leaves at least the header line
    itself in the span.
    """
    lines = text[start:end].splitlines(keepends=True)
    while len(lines) > 1:
        stripped = lines[-1].strip()
        if stripped == "" or stripped.startswith("#"):
            lines.pop()
            continue
        break
    return start + sum(len(line) for line in lines)


def strip_orphaned_mcp_strata_tables(config_text: str) -> tuple[str, int]:
    """Remove every remaining ``[mcp_servers.strata]`` / ``[mcp_servers.strata.*]``
    table from *config_text*, plain or array-of-tables.

    Call this only after :func:`remove_codex_mcp_server` has reported
    ``"removed"`` for the canonical parent block — at that point the parent
    is confirmed to be ours (it byte-matched the current or a historical
    shipped block), so anything still named ``mcp_servers.strata`` or
    ``mcp_servers.strata.<...>`` afterwards is meaningless without it. In
    practice this is per-tool approval state the Codex CLI itself appends
    during a live session (``[mcp_servers.strata.tools.<tool>]``) — left
    behind by a plain-block-only removal, it orphans the ``strata`` MCP
    server entry and Codex then refuses to start ("invalid transport in
    mcp_servers.strata") — round-4 unregister fix, bug A.

    A table that is *not* named ``mcp_servers.strata`` or a dotted child of
    it — including a user's own hand-written ``[mcp_servers.strata]`` entry,
    which :func:`remove_codex_mcp_server` never reports ``"removed"`` for in
    the first place (no marker means nothing calls this function at all) —
    is never touched.

    Returns ``(new_text, removed_count)`` — *removed_count* is ``0`` (with
    *new_text* == *config_text*) when nothing orphaned is found.
    """

    def _is_orphan(m: re.Match[str]) -> bool:
        name = m.group(2).strip()
        return name == "mcp_servers.strata" or name.startswith("mcp_servers.strata.")

    headers = list(_TOML_TABLE_HEADER_RE.finditer(config_text))
    spans: list[tuple[int, int]] = []
    for i, m in enumerate(headers):
        if not _is_orphan(m):
            continue
        start = m.start()
        if i + 1 < len(headers):
            raw_end = headers[i + 1].start()
            next_is_orphan = _is_orphan(headers[i + 1])
        else:
            raw_end = len(config_text)
            next_is_orphan = False
        # Only trim trailing blank/comment lines back to the next KEPT
        # header — when the next header is itself orphaned, the text
        # between them is pure separator and belongs to neither side; leave
        # it in the span so it disappears along with both removed tables
        # instead of surviving as a dangling blank line.
        end = raw_end if next_is_orphan else _table_header_end(config_text, start, raw_end)
        spans.append((start, end))
    if not spans:
        return config_text, 0

    new_text = config_text
    for idx in range(len(spans) - 1, -1, -1):
        start, end = spans[idx]
        # Only the earliest span needs its own leading separator stripped —
        # later spans in the list are contiguous with (or already swept up
        # by) an earlier one, and removing this one first (reverse order)
        # never shifts the still-untouched text before it.
        if idx == 0 and start > 0 and new_text[start - 1] == "\n":
            start -= 1
        new_text = new_text[:start] + new_text[end:]
    return new_text, len(spans)


# ---------------------------------------------------------------------------
# AGENTS.md — additive marker-block merge (Task 6, harness parity: the
# Codex-harness analogue of the Claude Code skills — Codex has no skills
# mechanism, so guidance is seeded into the project's AGENTS.md instead).
#
# Same convention as GITIGNORE_BLOCK / CODEX_MCP_BLOCK above: the managed
# block is appended once, detected by a marker, so a re-run is a no-op and
# any of the user's own AGENTS.md content is never rewritten.
# ---------------------------------------------------------------------------

#: Marker identifying Strata's managed AGENTS.md block. An HTML comment so it
#: renders invisibly wherever AGENTS.md is displayed, and is unlikely to
#: collide with a user's own prose the way a bare "# Strata" heading might.
AGENTS_MD_MARKER = "<!-- strata:begin -->"

#: Closing marker of Strata's managed AGENTS.md block. Together with
#: :data:`AGENTS_MD_MARKER` this brackets the block so it can be located and
#: extracted even when its *content* has drifted from what's currently
#: shipped (:func:`classify_agents_md_drift`) — the plain-marker check
#: :func:`agents_md_present` only needs the begin marker, but locating the
#: block's full extent to diff or replace it needs both ends.
AGENTS_MD_END_MARKER = "<!-- strata:end -->"


def _shipped_agents_md_block() -> str:
    """Read the canonical AGENTS.md block from package data.

    Mirrors :func:`hook_matches_shipped`'s ``importlib.resources`` lookup —
    the block lives once, as package data under ``strata/_templates``,
    rather than duplicated as a Python string constant.
    """
    import importlib.resources  # noqa: PLC0415

    shipped = importlib.resources.files("strata") / "_templates" / "AGENTS-strata.md"
    return shipped.read_text(encoding="utf-8")


def agents_md_present(text: str) -> bool:
    """Return whether Strata's managed AGENTS.md block marker is present in *text*."""
    return AGENTS_MD_MARKER in text


def merge_agents_md(existing_text: str) -> tuple[str, bool]:
    """Additively append the canonical Strata block to *existing_text*.

    Strictly additive (ADR 0005 Decision 6): every existing line in
    *existing_text* is preserved byte-for-byte; the Strata block is appended
    only when :func:`agents_md_present` is false.

    Returns:
        ``(new_text, added)`` — *added* is ``True`` if the block was
        appended, ``False`` (with *new_text* == *existing_text*) if a Strata
        block was already present.
    """
    if agents_md_present(existing_text):
        return existing_text, False
    return _append_block(existing_text, _shipped_agents_md_block()), True


def remove_agents_md(existing_text: str) -> tuple[str, str]:
    """Remove Strata's managed AGENTS.md block from *existing_text*.

    The reverse of :func:`merge_agents_md`. Unlike the fixed-block matching
    :func:`_remove_block` does elsewhere, this locates the installed block by
    its begin/end markers (:func:`_extract_agents_md_block`) — so it finds
    the block regardless of which shipped release wrote its *content* — then
    classifies that exact content via :func:`classify_agents_md_drift`
    (current-or-historical-shipped vs. genuinely user-edited). A project
    registered under an older Strata release, whose block still matches what
    that release shipped, is removed cleanly rather than misreported as
    edited (round-4 unregister fix, bug B).

    Returns ``(new_text, status)``:

    - ``"removed"`` — the block matched the current or a historical shipped
      version and was stripped, along with the blank-line separator
      register prepends.
    - ``"edited"``  — the begin marker is present but the block doesn't
      classify as current-or-historical-shipped (user-edited, or the shipped
      reference couldn't be read to prove it); *existing_text* is returned
      unchanged.
    - ``"absent"``  — no managed marker at all; nothing to do.
    """
    block = _extract_agents_md_block(existing_text)
    if not block:
        if AGENTS_MD_MARKER in existing_text:
            return existing_text, "edited"
        return existing_text, "absent"
    status = classify_agents_md_drift(existing_text)
    if status not in ("match", "stale"):
        return existing_text, "edited"
    sep_block = "\n" + block
    if sep_block in existing_text:
        return existing_text.replace(sep_block, "", 1), "removed"
    return existing_text.replace(block, "", 1), "removed"


def _extract_agents_md_block(text: str) -> str:
    """Return the substring of *text* between Strata's begin/end markers
    (inclusive), or ``""`` if the markers aren't both present.

    Unlike the verbatim-block matching :func:`_remove_block` does, this finds
    the block's extent by its markers alone — so it still locates an
    installed block whose *content* has drifted from what's currently
    shipped (an older `strata register` wrote it, or the user edited it).
    """
    start = text.find(AGENTS_MD_MARKER)
    if start == -1:
        return ""
    end = text.find(AGENTS_MD_END_MARKER, start)
    if end == -1:
        return ""
    end += len(AGENTS_MD_END_MARKER)
    # The shipped block (as read from package data, and as merge_agents_md /
    # _append_block write it) ends with a trailing "\n" after the end
    # marker — that newline is part of the block's own content, not a
    # separator the caller added. Include it here too when it's actually
    # there, so a freshly-merged block's extent (and hash) matches the
    # shipped block's exactly, rather than falling one byte short every
    # time. A block whose trailing newline was stripped (a user edit) simply
    # has nothing to include here — bounds-checked, never indexes past len().
    if end < len(text) and text[end] == "\n":
        end += 1
    return text[start:end]


def classify_agents_md_drift(existing_text: str) -> str:
    """Classify AGENTS.md's managed block against shipped + historical hashes.

    Block-level, not file-level: only the fenced block (between
    :data:`AGENTS_MD_MARKER` and :data:`AGENTS_MD_END_MARKER`) is compared —
    everything else in *existing_text* is the user's own content and is
    never examined here.

    Returns:
        - ``"absent"``  — no managed block found (both markers missing, or
          the end marker is missing so the block can't be bounded).
        - ``"match"``   — the block is byte-identical to the currently
          shipped block.
        - ``"stale"``   — differs, but its hash is a known-historical
          shipped hash — safe to self-update.
        - ``"edited"``  — differs, and its hash isn't recognized —
          user-edited; never touched.
        - ``"unknown"`` — the shipped reference could not be read.
    """
    block = _extract_agents_md_block(existing_text)
    if not block:
        return "absent"
    try:
        shipped_block = _shipped_agents_md_block()
    except (OSError, ModuleNotFoundError):
        return "unknown"
    return _classify_drift(block, shipped_block, "agents-md")


def self_update_agents_md_block(existing_text: str) -> tuple[str, str]:
    """Self-update AGENTS.md's managed block in place when it's stale.

    Classifies via :func:`classify_agents_md_drift`. When the result is
    ``"stale"``, the block substring is replaced with the current shipped
    block — every byte of *existing_text* outside the fence (the user's own
    AGENTS.md content) is untouched, reusing the same bracket-and-splice
    approach as :func:`_remove_block` rather than rebuilding the file. Any
    other status returns *existing_text* unchanged.

    Returns:
        ``(new_text, status)`` — *status* is one of the
        :func:`classify_agents_md_drift` values.
    """
    status = classify_agents_md_drift(existing_text)
    if status != "stale":
        return existing_text, status
    block = _extract_agents_md_block(existing_text)
    shipped_block = _shipped_agents_md_block()
    return existing_text.replace(block, shipped_block, 1), status


# ---------------------------------------------------------------------------
# Harness detection
# ---------------------------------------------------------------------------

#: The harnesses Strata knows how to wire, in the order ``detect_harnesses``
#: and ``strata register``/``strata unregister`` report and act on them.
KNOWN_HARNESSES: tuple[str, ...] = ("claude-code", "codex")


def detect_harnesses(home: Path | None = None, path_env: str | None = None) -> list[str]:
    """Return the subset of :data:`KNOWN_HARNESSES` installed on this machine.

    A harness is detected if either its CLI binary is found on *path_env*
    (``claude`` / ``codex``) or its config directory exists under *home*
    (``~/.claude`` / ``~/.codex``). *home* defaults to ``Path.home()`` and
    *path_env* defaults to the real ``PATH`` — parameters exist so tests never
    depend on the real machine.

    Returns harnesses in :data:`KNOWN_HARNESSES` order, not detection order.
    """
    if home is None:
        home = Path.home()
    detected = []
    if shutil.which("claude", path=path_env) or (home / ".claude").exists():
        detected.append("claude-code")
    if shutil.which("codex", path=path_env) or (home / ".codex").exists():
        detected.append("codex")
    return detected


# ---------------------------------------------------------------------------
# .strata/config.toml — [launch] table (default harness, Task 4)
# ---------------------------------------------------------------------------

#: Regex matching a top-level ``[launch]`` table header line. The trailing
#: ``\r?`` matters: without it, a CRLF-authored ``config.toml`` (Windows is a
#: supported platform) leaves the header unmatched — ``$`` in MULTILINE mode
#: only matches immediately before ``\n``, and a bare ``[ \t]*`` does not
#: consume the ``\r`` that sits between ``[launch]`` and that ``\n`` — so the
#: whole function fell through to "no table found" and appended a *second*
#: ``[launch]`` table on every CRLF file (reported in review).
_LAUNCH_HEADER_RE = re.compile(r"(?m)^\[launch\][ \t]*\r?$")

#: Regex matching any top-level table (or array-of-tables) header line —
#: used to find where an existing ``[launch]`` table's body ends. Unaffected
#: by CRLF: ``^`` in MULTILINE mode matches right after a ``\n`` regardless
#: of what precedes it.
_TOP_LEVEL_HEADER_RE = re.compile(r"(?m)^\[")

#: Regex matching a ``default_harness = ...`` key line inside a table body,
#: stopping before any ``\r``/``\n`` rather than consuming them (``[^\r\n]*``
#: instead of ``.*$``): a trailing ``\r`` must NOT be swallowed into the
#: match, or replacing it turns that one line's CRLF into a bare LF and the
#: file's line-ending style stops being byte-preserved.
_DEFAULT_HARNESS_KEY_RE = re.compile(r"(?m)^default_harness[ \t]*=[^\r\n]*")


def _detect_newline(text: str) -> str:
    """Return the line-ending style already used in *text*.

    ``"\\r\\n"`` if any CRLF pair is present, else ``"\\n"``. Content this
    module *adds* (a fresh ``[launch]`` table, a fresh ``default_harness``
    key) uses this so a CRLF file stays CRLF throughout, not just on the
    lines it already had.
    """
    return "\r\n" if "\r\n" in text else "\n"


def set_default_harness(config_text: str, name: str) -> str:
    """Set ``default_harness = "<name>"`` under a ``[launch]`` table.

    Read-modify-write, textual (mirrors the Codex config mergers above —
    this project has no TOML writer): every existing table, comment, and
    blank line in *config_text* is preserved byte-for-byte outside the
    ``[launch]`` table's ``default_harness`` line — including its line-ending
    style (CRLF in, CRLF out; see :func:`_detect_newline`).

    - No ``[launch]`` table present: one is appended at the end (via the same
      blank-line separator rule as :func:`_append_block`).
    - ``[launch]`` present, no ``default_harness`` key: the key is appended
      inside the existing table body.
    - ``[launch]`` present with a ``default_harness`` key: that line is
      replaced in place — re-running never duplicates the table or the key.

    Args:
        config_text: The current ``.strata/config.toml`` contents.
        name: The harness name to record (validated by the caller against
            :data:`KNOWN_HARNESSES`).

    Returns:
        The new config text.
    """
    new_line = f'default_harness = "{name}"'
    nl = _detect_newline(config_text)

    header_match = _LAUNCH_HEADER_RE.search(config_text)
    if header_match is None:
        prefix = config_text
        if prefix and not prefix.endswith("\n"):
            prefix += nl
        if prefix:
            prefix += nl
        block = f"[launch]{nl}{new_line}{nl}"
        return prefix + block

    body_start = header_match.end()
    next_header = _TOP_LEVEL_HEADER_RE.search(config_text, body_start + 1)
    body_end = next_header.start() if next_header else len(config_text)
    body = config_text[body_start:body_end]

    key_match = _DEFAULT_HARNESS_KEY_RE.search(body)
    if key_match is not None:
        new_body = body[: key_match.start()] + new_line + body[key_match.end() :]
    else:
        new_body = body
        if new_body and not new_body.endswith("\n"):
            new_body += nl
        new_body += new_line + nl

    return config_text[:body_start] + new_body + config_text[body_end:]


def read_default_harness_from_text(config_text: str) -> str | None:
    """Return the ``[launch].default_harness`` value from *config_text*, or ``None``.

    Parses via ``tomllib`` (validation, not the write path — writes stay
    textual, see :func:`set_default_harness`). Returns ``None`` when the
    table/key is absent or the TOML fails to parse (e.g. mid-edit).

    Named ``*_from_text`` (not ``read_default_harness``, final fix wave item
    4) to disambiguate from :func:`strata.project_config.read_default_harness`
    — same verb, different signature (config text vs. a project root path);
    the two public functions shared one name across modules, which review
    flagged as confusing at call sites and in stack traces.
    """
    import tomllib  # noqa: PLC0415

    try:
        data = tomllib.loads(config_text) if config_text.strip() else {}
    except tomllib.TOMLDecodeError:
        return None
    launch = data.get("launch")
    if not isinstance(launch, dict):
        return None
    value = launch.get("default_harness")
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# .env — judge key capture (register's end-of-run prompt).
#
# GITIGNORE_BLOCK above already lists `.env` (safety gate: a key must never
# land in a project whose .gitignore does not cover it). This section is
# defense in depth for a project registered *before* that line existed, or
# one where a user's own .gitignore edit dropped it: the write still
# happens (skipping it would strand the operator worse than a warning
# would), but the caller is expected to check `gitignore_covers_dotenv`
# first and warn loudly when it returns False.
# ---------------------------------------------------------------------------

#: Matches a `JUDGE_API_KEY=` or `ANTHROPIC_API_KEY=` assignment line (bare or
#: STRATA_-prefixed), so an existing line under either spelling is replaced
#: in place rather than growing a duplicate. Anchored so a commented-out line
#: (`# JUDGE_API_KEY=...`) is left alone.
_ENV_JUDGE_KEY_LINE_RE = re.compile(r"^(?:STRATA_)?(?:JUDGE_API_KEY|ANTHROPIC_API_KEY)=")


def gitignore_covers_dotenv(gitignore_text: str) -> bool:
    """Return ``True`` if *gitignore_text* has a line that ignores ``.env`` exactly.

    A plain ``.env`` line (register's own GITIGNORE_BLOCK writes exactly
    this) is matched; a pattern that merely happens to contain the
    substring ``.env`` (e.g. a comment) is not — each line is matched
    whole, after stripping surrounding whitespace. A broader pattern that
    still covers ``.env`` (``*.env``, ``.env*``, a ``**``-glob) returns
    ``False`` here — a known, accepted false-negative (spurious warning,
    caller-side): the check is deliberately exact-line, not a glob matcher,
    so it never mistakes an unrelated pattern for coverage it doesn't
    actually provide.
    """
    return any(line.strip() == ".env" for line in gitignore_text.splitlines())


def write_env_judge_key(env_path: Path, key: str) -> str:
    """Write ``JUDGE_API_KEY=<key>`` into *env_path*, preserving everything else.

    - If *env_path* does not exist, it is created with just this one line.
    - If it exists and already has a line setting ``JUDGE_API_KEY`` or
      ``ANTHROPIC_API_KEY`` (either spelling), that line is replaced in
      place with the new ``JUDGE_API_KEY=<key>`` line — never duplicated.
    - Otherwise the new line is appended, adding a newline first only if
      the file doesn't already end with one.

    All other content is preserved byte-for-byte (read and written with
    explicit ``encoding="utf-8"`` and no newline translation on either
    end). Returns ``"created"``, ``"replaced"``, or ``"appended"``.
    """
    new_line = f"JUDGE_API_KEY={key}"

    if not env_path.exists():
        env_path.write_bytes((new_line + "\n").encode("utf-8"))
        # A freshly-created .env holds a secret — it must not inherit the
        # umask's default (commonly 0644, group/world-readable). Chmod
        # immediately after the write, before anything else can read it.
        # Only the create path touches permissions at all: append/replace
        # below operate on a file the user already owns, with whatever
        # mode they've already set — that's theirs to keep.
        os.chmod(env_path, 0o600)
        return "created"

    existing = env_path.read_bytes().decode("utf-8")
    lines = existing.split("\n")
    for i, line in enumerate(lines):
        if _ENV_JUDGE_KEY_LINE_RE.match(line.strip()):
            lines[i] = new_line
            env_path.write_bytes("\n".join(lines).encode("utf-8"))
            return "replaced"

    prefix = existing
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    env_path.write_bytes((prefix + new_line + "\n").encode("utf-8"))
    return "appended"
