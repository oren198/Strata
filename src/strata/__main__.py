"""Entry point for ``strata`` and ``python -m strata``.

A small CLI that wraps the backend's common operations into a single
runnable. Subcommands:

* ``strata start``     — apply migrations, auto-seed fleet.yaml if absent,
                        run the FastAPI app via uvicorn.
* ``strata migrate``   — apply SQLite schema migrations.
* ``strata bootstrap`` — validate fleet.yaml and prepare the in-memory
                        FleetConfig mirror (no DB writes).
* ``strata scopes``    — terminal-friendly listing of the fleet.
* ``strata summary``   — print a scope's curated summary.
* ``strata record``    — print a scope's record (contributions + judgments).
* ``strata launch``       — validate scope, resolve skill, and exec ``claude``
                           with STRATA_AGENT_* env vars set (ADR 0003).
* ``strata export-fleet`` — read V1 fleet tables and write fleet.yaml for
                           the V1 → V1.2 upgrade path.
* ``strata doctor``       — diagnose a project's config/DB/fleet/install
                           wiring/agent binding in one offline pass.

All commands — including the inspection commands (``scopes``, ``summary``,
``record``) and ``launch`` — read ``fleet.yaml`` and the record/summary
stores directly (embedded mode, ADR 0004 D1). No backend needs to be
running; ``strata start`` is required only for the Console UI.

Vocabulary throughout follows ``CONTEXT.md``.
"""

# ---------------------------------------------------------------------------
# V1 → V1.2 upgrade guard
# ---------------------------------------------------------------------------
#
# ``strata start`` auto-applies migration 0002_drop_fleet_tables.sql, which
# drops the V1 fleet tables (strata, scopes, edges) that were the V1
# operational source of truth.  A V1 operator who runs ``strata start``
# before exporting their fleet config will silently lose it.
#
# ``_v1_upgrade_guard_should_refuse`` detects this situation by issuing
# read-only SELECTs against the source DB (same discipline as fleet_export.py)
# and returns True only when all four conditions hold:
#
#   1. The DB file exists.
#   2. Migration 0002_drop_fleet_tables.sql is pending (not in _migrations, or
#      the _migrations table itself doesn't exist yet).
#   3. The three V1 fleet tables (strata, scopes, edges) are present in
#      sqlite_master.
#   4. No fleet.yaml exists at the resolved path.
#
# ``cmd_start`` calls this before ``run_migrations``.  If it returns True,
# start exits non-zero with an actionable error message.  Pass
# ``--skip-upgrade-check`` to bypass.

from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strata.fleet_config import FleetConfig
    from strata.record_store import RecordStore
    from strata.scope_manager import ScopeManager
    from strata.summary_store import ScopeSummary, SummaryStore

from strata import DISTRIBUTION_NAME, __version__, install
from strata.install import (
    CONFIG_TOML as _CONFIG_TOML,
)
from strata.install import (
    GITIGNORE_BLOCK as _GITIGNORE_BLOCK,
)
from strata.install import (
    GITIGNORE_MARKER as _GITIGNORE_MARKER,
)
from strata.install import (
    HOOK_SCRIPT_NAME as _HOOK_SCRIPT_NAME,
)
from strata.install import (
    MCP_ENTRY as _MCP_ENTRY,
)
from strata.install import (
    MCP_ENTRY_HISTORICAL as _MCP_ENTRY_HISTORICAL,
)
from strata.install import (
    SKILL_NAMES,
    copy_hook,
    copy_skill,
    merge_mcp_server,
    merge_stop_hook,
    render_action_line,
)
from strata.install import (
    agents_md_present as _agents_md_present,
)
from strata.install import (
    classify_hook_drift as _classify_hook_drift,
)
from strata.install import (
    classify_skill_drift as _classify_skill_drift,
)
from strata.install import (
    codex_config_path as _codex_config_path,
)
from strata.install import (
    codex_hook_present as _codex_hook_present,
)
from strata.install import (
    codex_mcp_present as _codex_mcp_present,
)
from strata.install import (
    hook_matches_shipped as _hook_matches_shipped,
)
from strata.install import (
    is_bootstrap_venv_shape_mcp_entry as _is_bootstrap_venv_shape_mcp_entry,
)
from strata.install import (
    is_v1_2_shape_mcp_entry as _is_v1_2_shape_mcp_entry,
)
from strata.install import (
    mcp_server_present as _mcp_server_present,
)
from strata.install import (
    merge_agents_md as _merge_agents_md,
)
from strata.install import (
    merge_codex_freshness_hook as _merge_codex_freshness_hook,
)
from strata.install import (
    merge_codex_mcp_server as _merge_codex_mcp_server,
)
from strata.install import (
    remove_agents_md as _remove_agents_md,
)
from strata.install import (
    remove_codex_freshness_hook as _remove_codex_freshness_hook,
)
from strata.install import (
    remove_codex_mcp_server as _remove_codex_mcp_server,
)
from strata.install import (
    remove_gitignore_block as _remove_gitignore_block,
)
from strata.install import (
    remove_stop_hook as _remove_stop_hook,
)
from strata.install import (
    self_update_agents_md_block as _self_update_agents_md_block,
)
from strata.install import (
    self_update_hook as _self_update_hook,
)
from strata.install import (
    self_update_skill as _self_update_skill,
)
from strata.install import (
    skill_matches_shipped as _skill_matches_shipped,
)
from strata.install import (
    stop_hook_present as _stop_hook_present,
)
from strata.install import (
    strip_orphaned_mcp_strata_tables as _strip_orphaned_mcp_strata_tables,
)
from strata.launch import (
    SkillResolutionError,
    StrataRoleParseError,
    exec_claude,
    find_strata_role,
    is_interactive,
    make_session_id,
    parse_strata_role,
    prompt_scope,
    resolve_skill,
)
from strata.preflight import Check, run_launch_preflight, run_start_preflight

# Path to the bundled starter templates directory (package data, like _skills).
_TEMPLATES_DIR = Path(__file__).parent / "_templates"
_DEFAULT_TEMPLATE = _TEMPLATES_DIR / "dev-team.yaml"


def _storage_paths():
    """Resolve storage paths through the single source of truth (issue #44).

    ``.strata/config.toml`` (walk-up discovery) wins over env-var settings,
    exactly as the MCP server and the backend resolve them, so no two entry
    points can ever operate on different state.

    A pure resolver, deliberately: it is called eagerly by ``_build_parser``
    on EVERY ``main()`` invocation just to render ``--db``'s help text
    (``_db_path_default``), before any subcommand is chosen — so it must
    never have a side effect keyed to "the paths this run will actually
    use". :func:`strata.locks.configure_lock_dir` (issue #19, ADR 0012) used
    to be called here and that is exactly the bug it caused: this function
    runs against the real cwd on invocations that never touch a scope lock
    at all (``strata --help``, `pytest` collecting the CLI test module),
    silently pointing the process-global lock directory at whatever the
    caller's cwd happened to be. The actual lock-dir wiring for every CLI
    command that can take ``scope_lock`` — ``strata operator
    publish``/``supersede``/``retire``, ``strata publication bootstrap`` —
    lives in :func:`strata.stores.open_embedded_stores`, the store-init path
    those commands actually call; see its docstring.
    """
    from strata.project_config import resolve_storage_paths

    return resolve_storage_paths()


def _db_path_default() -> str:
    return _storage_paths().db_path


def _fleet_config_default() -> str:
    """Resolve the canonical fleet config path (ADR 0002 + ADR 0005 D2).

    Project config (``.strata/config.toml``) wins when present; otherwise
    ``STRATA_FLEET_CONFIG`` / the ``./fleet.yaml`` default via the same
    :class:`Settings` the backend uses, so the CLI and the running app never
    diverge on which file is canonical.
    """
    return _storage_paths().fleet_yaml_path


_UNRESOLVED_DEFAULT_HINT = "resolved from the project"


def _default_hint(resolver) -> str:
    """Render a default-value hint for argparse help text, never raising.

    ``_build_parser`` calls ``_db_path_default``/``_fleet_config_default``
    eagerly, before any subcommand is chosen, purely to render a couple of
    ``--db``/``--config`` help strings — so a failure in that resolution
    (e.g. ``Path.cwd()`` raising ``FileNotFoundError`` because the shell's
    cwd was deleted, such as a removed git worktree) must never take down
    parser construction itself. ``strata --version`` and ``strata --help``
    have to work from anywhere, even a dead cwd. On any failure here, fall
    back to a plain, non-specific hint instead of the resolved path.
    """
    try:
        return resolver()
    except Exception:
        return _UNRESOLVED_DEFAULT_HINT


def _resolve_fleet_config(explicit: str | None) -> str | None:
    """Pick the config path: explicit arg → Settings path.

    Returns ``None`` when neither resolves to an existing file — the caller
    reports "no fleet config found" rather than trying to load a path that
    doesn't exist. (There used to be a further fallback to a root-level
    ``fleet.example.yaml``; it was removed — ``src/strata/_templates/`` is
    the single starter-fleet source, issue #64.)
    """
    if explicit:
        return explicit
    settings_path = _fleet_config_default()
    if Path(settings_path).exists():
        return settings_path
    return None


_GUARD_MIGRATION = "0002_drop_fleet_tables.sql"
_V1_FLEET_TABLES = frozenset({"strata", "scopes", "edges"})


def _v1_upgrade_guard_should_refuse(
    db_path: str,
    fleet_yaml_path: str,
    *,
    skip: bool,
) -> bool:
    """Return True when ``strata start`` should refuse due to a risky V1→V1.2 upgrade.

    All DB access is read-only (SELECT only). The connection is opened, checked,
    and closed before any other action, following the same discipline as
    ``src/strata/fleet_export.py``.

    Refuse when all four conditions hold:
    1. The DB file exists (not a fresh install).
    2. Migration ``0002_drop_fleet_tables.sql`` is pending (absent from
       ``_migrations``, or the ``_migrations`` table doesn't exist yet).
    3. The three V1 fleet tables (``strata``, ``scopes``, ``edges``) are
       present in ``sqlite_master``.
    4. No ``fleet.yaml`` exists at the resolved path.

    Args:
        db_path:        Resolved path to the SQLite DB.
        fleet_yaml_path: Resolved path to fleet.yaml (from ``_fleet_config_default()``).
        skip:           When True, bypass the check and return False unconditionally.
    """
    if skip:
        return False

    # Condition 1: DB file must exist.
    if not Path(db_path).exists():
        return False

    # Condition 4: fleet.yaml must be absent.
    if Path(fleet_yaml_path).exists():
        return False

    # Open a read-only connection for conditions 2 and 3.
    conn = sqlite3.connect(db_path)
    try:
        # Condition 2: 0002_drop_fleet_tables.sql pending.
        try:
            applied = {row[0] for row in conn.execute("SELECT name FROM _migrations").fetchall()}
            migration_pending = _GUARD_MIGRATION not in applied
        except sqlite3.OperationalError:
            # _migrations table doesn't exist → migration definitely pending.
            migration_pending = True

        if not migration_pending:
            return False

        # Condition 3: All three V1 fleet tables present in sqlite_master.
        present = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='table' AND name IN ('strata','scopes','edges')"
            ).fetchall()
        }
        if present != _V1_FLEET_TABLES:
            return False
    finally:
        conn.close()

    return True


# ---------------------------------------------------------------------------
# Console glyphs — ASCII-safe on non-UTF8 terminals (issue #66)
# ---------------------------------------------------------------------------
#
# Status output uses Unicode markers (✓ ⚠ ✗). A non-UTF8 console — e.g. a
# Windows code page such as cp1255 — cannot encode them and raises
# UnicodeEncodeError mid-print. ``_glyph`` returns the Unicode marker when the
# console can encode it and an ASCII token otherwise, so output degrades
# gracefully instead of crashing.

_GLYPHS: dict[str, tuple[str, str]] = {
    "pass": ("✓", "OK"),
    "warn": ("⚠", "!"),
    "fail": ("✗", "x"),
}


def _glyph(status: str) -> str:
    """Return a status marker safe for the current console encoding.

    Maps a semantic *status* (``"pass"`` / ``"warn"`` / ``"fail"``) to its
    Unicode glyph, falling back to an ASCII token when either stdout or stderr
    cannot encode it — e.g. a cp1255 Windows console (issue #66), which would
    otherwise raise UnicodeEncodeError mid-print.
    """
    unicode_glyph, ascii_fallback = _GLYPHS[status]
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            continue
        try:
            unicode_glyph.encode(encoding)
        except (UnicodeError, LookupError):
            return ascii_fallback
    return unicode_glyph


# ---------------------------------------------------------------------------
# Preflight runner
# ---------------------------------------------------------------------------


def _run_preflight(checks: list[Check]) -> int:
    """Print structured preflight output and return non-zero on any hard failure.

    Output symbols:
      ✓  — check passed (hard or soft)
      ⚠  — soft check failed (warning; continues)
      ✗  — hard check failed (fatal; will exit 1)

    Returns 0 when all hard checks pass (soft failures are printed but
    do not affect the exit code).  Returns 1 when any hard check fails.
    """
    has_hard_failure = False
    for check in checks:
        if check.passed:
            print(f"  {_glyph('pass')} {check.name}: {check.message}")
        elif check.kind == "soft":
            print(f"  {_glyph('warn')} {check.name}: {check.message}", file=sys.stderr)
        else:
            print(f"  {_glyph('fail')} {check.name}: {check.message}", file=sys.stderr)
            has_hard_failure = True
    return 1 if has_hard_failure else 0


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def cmd_migrate(args: argparse.Namespace) -> int:
    """Apply pending SQLite migrations to the DB."""
    from strata.migrator import run_migrations

    db_path = args.db or _db_path_default()
    applied = run_migrations(db_path)
    if applied:
        print(f"Applied {len(applied)} migration(s) to {db_path}:")
        for name in applied:
            print(f"  · {name}")
    else:
        print(f"No pending migrations for {db_path}.")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Validate fleet.yaml and prepare the in-memory FleetConfig mirror.

    No DB writes are made.  The command validates all load-time invariants
    (the original 8 from ADR 0002, plus ADR 0004's and ADR 0008's) and
    reports success or the first error encountered.
    """
    from strata.bootstrap import load_fleet_config
    from strata.fleet_config import FleetConfigError

    config_path = _resolve_fleet_config(args.config)
    if config_path is None:
        print(
            "No fleet config found. Pass --config <path>, set STRATA_FLEET_CONFIG, "
            "or place fleet.yaml in the current directory.",
            file=sys.stderr,
        )
        return 1
    if not Path(config_path).exists():
        print(f"Fleet config not found: {config_path}", file=sys.stderr)
        return 1

    try:
        config = load_fleet_config(config_path)
    except FleetConfigError as exc:
        print(f"Fleet config invalid [{exc.kind}]: {exc.message}", file=sys.stderr)
        return 1

    print(f"Fleet config valid: {config_path}")
    print(f"  strata: {len(config.strata)}")
    print(f"  scopes: {len(config.scopes)}")
    print(f"  edges:  {len(config.edges)}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """Apply migrations, auto-seed fleet.yaml if absent, then run uvicorn."""
    from strata.migrator import run_migrations

    paths = _storage_paths()
    db_path = args.db or paths.db_path
    fleet_yaml_path = paths.fleet_yaml_path
    if paths.source == "project":
        print(f"using project config: {paths.project_root}/.strata/config.toml")
        if args.db and args.db != paths.db_path:
            print(
                f"--db {args.db} conflicts with .strata/config.toml (db = {paths.db_path}).\n"
                "The project config is the single source of truth for a registered "
                "project — edit .strata/config.toml instead of passing --db.",
                file=sys.stderr,
            )
            return 1
    elif args.db:
        # The served app resolves its own paths via Settings; export the
        # override so migrations, the upgrade guard, and the app all use the
        # SAME database (previously --db was migrated but ./strata.db served).
        os.environ["STRATA_DB_PATH"] = args.db
        from strata.settings import get_settings

        get_settings.cache_clear()

    # 1. Preflight — prerequisite hygiene checks before any DB or server work.
    if not args.skip_preflight:
        rc = _run_preflight(run_start_preflight(port=args.port, db_path=db_path))
        if rc != 0:
            return rc

    # 2. V1 → V1.2 upgrade guard: refuse if migration 0002 is pending but
    #    the V1 fleet tables are still present and no fleet.yaml exists.
    #    Must run before run_migrations so we catch the footgun before it fires.
    if _v1_upgrade_guard_should_refuse(
        db_path,
        fleet_yaml_path,
        skip=args.skip_upgrade_check,
    ):
        print(
            f"Detected a V1 fleet config in {db_path} and no fleet.yaml at {fleet_yaml_path}.\n"
            "Run `strata export-fleet` first to preserve it, then re-run `strata start`.\n"
            "(Pass --skip-upgrade-check to bypass this check.)",
            file=sys.stderr,
        )
        return 1

    # 3. Migrate.
    applied = run_migrations(db_path)
    if applied:
        print(f"Applied {len(applied)} migration(s).")

    # 4. Auto-seed fleet.yaml if absent. Only for the env-driven dev flow —
    #    in a registered project (source == "project") the fleet was seeded
    #    by `strata register`; a missing file there is a broken state the
    #    user should repair, not silently paper over with the dev template.
    fleet_path = Path(fleet_yaml_path)
    if not fleet_path.exists():
        if paths.source == "project":
            print(
                f"fleet.yaml missing at {fleet_path} (listed in .strata/config.toml).\n"
                "Re-run `strata register` from the project root to re-seed it.",
                file=sys.stderr,
            )
            return 1
        if _DEFAULT_TEMPLATE.exists():
            shutil.copy(_DEFAULT_TEMPLATE, fleet_path)
            print("seeded fleet.yaml from the default template; edit to suit")

    # 5. Serve.
    import uvicorn

    print()
    print(f"Strata backend → http://{args.host}:{args.port}")
    print(f"Strata Console → http://{args.host}:{args.port}/")
    print()
    uvicorn.run(
        "strata.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def cmd_scopes(args: argparse.Namespace) -> int:
    """List the fleet's active scopes (embedded read — fleet.yaml, ADR 0004 D1)."""
    from strata.stores import EmbeddedStoreError, open_embedded_stores

    try:
        stores = open_embedded_stores()
    except EmbeddedStoreError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    with stores:
        fleet = stores.fleet_config
        active = fleet.active_scopes()
        active_ids = {s.id for s in active}
        active_edges = [e for e in fleet.edges if e.from_ in active_ids and e.to in active_ids]

        print(f"Strata ({len(fleet.strata)}):")
        for s in fleet.strata:
            print(f"  [{s.ordinal}] {s.id:6s}  {s.name}")
        print()
        print(f"Scopes ({len(active)}):")
        for sc in active:
            print(f"  {sc.id:12s}  stratum={sc.stratum_id:4s}  {sc.name}")
        print()
        print(f"Edges ({len(active_edges)}):")
        for e in active_edges:
            print(f"  {e.from_:12s} → {e.to:12s}  {e.kind or ''}")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    """Print a scope's curated summary as markdown (embedded read, ADR 0004 D1)."""
    from datetime import UTC, datetime

    from strata.stores import EmbeddedStoreError, open_embedded_stores
    from strata.summary_store import ScopeSummary

    try:
        stores = open_embedded_stores()
    except EmbeddedStoreError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    with stores:
        scope = stores.fleet_config.get_scope(args.scope_id)
        if scope is None:
            print(f"Scope not found: {args.scope_id}", file=sys.stderr)
            return 1

        summary = stores.summary_store.read(args.scope_id)
        if summary is None:
            # Scope exists but has no summary yet — synthesize an empty one,
            # matching GET /scopes/{id}/summary (issue #59: version=0,
            # exists=False marks it as never actually written).
            summary = ScopeSummary(
                scope_id=args.scope_id,
                directives=[],
                context="",
                updated_at=datetime.now(tz=UTC).isoformat(),
                version=0,
                exists=False,
            )

        print(f"# Scope: {summary.scope_id}")
        print(f"_updated_at: {summary.updated_at}_")
        print()
        print("## Directives")
        if not summary.directives:
            print("_(none yet)_")
        for d in summary.directives:
            print()
            print(f"### [{d.id}] {d.content}")
            if d.subject:
                print(f"- subject: {d.subject}")
            # Skill is optional (issue #121): show the scope alone when absent,
            # never the literal "None".
            if d.source_skill:
                source = (
                    f"- source: scope={d.source_scope_id} · skill={d.source_skill}"
                    f" · at={d.created_at}"
                )
            else:
                source = f"- source: scope={d.source_scope_id} · at={d.created_at}"
            print(source)
        print()
        print("## Context")
        print(summary.context or "_(none yet)_")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Print one page of a scope's record (contributions + judgments) — embedded read.

    Bounded by default (issue #130), like every other door onto the record: the
    newest page, walked back with ``--before``.  The record only ever grows, so
    printing all of it was unbounded by construction.
    """
    from strata.settings import get_settings
    from strata.stores import EmbeddedStoreError, open_embedded_stores

    try:
        stores = open_embedded_stores()
    except EmbeddedStoreError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    with stores:
        scope = stores.fleet_config.get_scope(args.scope_id)
        if scope is None:
            print(f"Scope not found: {args.scope_id}", file=sys.stderr)
            return 1

        limit = args.limit if args.limit is not None else get_settings().record_page_size
        try:
            page = stores.record_store.page_record(
                scope_id=args.scope_id, limit=limit, before_id=args.before
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        contributions = page.contributions
        judgments = page.judgments
        states = {s.contribution_id: s for s in page.contribution_states}

        print(f"Scope: {args.scope_id}")
        print(f"Contributions: {len(contributions)} of {page.total} (newest first)")
        print(f"Judgments:     {len(judgments)}")
        print()
        for c in contributions:
            # The three states the library derives (issue #118): a verdict, the
            # mechanical judge-failed marker, or neither. "judge errored" is what
            # #118 exists for — a contribution the judge tried and failed to
            # judge must not read as one still in flight. A verdict is never
            # fabricated for it; it stays re-judgeable.
            #
            # Failed attempts recorded before the marker existed keep the #57
            # rendering — "(pending — N failed attempts)" — because nothing
            # observed that those runs ended, and the record is not rewritten to
            # pretend otherwise.
            state = states.get(c.id)
            if state is None or state.state == "pending":
                n = state.failed_attempts if state is not None else 0
                verdict = (
                    f"(pending — {n} failed attempt{'s' if n != 1 else ''})" if n else "(pending)"
                )
            elif state.state == "judge_failed":
                verdict = f"(judge errored — {state.error_class})"
            else:
                verdict = str(state.decision)
            print(f"  · {c.id}  [{c.proposed_classification:9s} → {verdict}]")
            contributor = c.contributor
            # Skill is optional (issue #121): render the scope alone when the
            # contribution carries no skill, never "None@scope".
            if contributor.skill:
                by = f"{contributor.skill}@{contributor.scope_id}"
            else:
                by = contributor.scope_id
            print(f"      by {by} at {contributor.ts}")
            if c.subject:
                print(f"      subject: {c.subject}")
            if c.supersedes:
                print(f"      supersedes: {c.supersedes}")
            if state is not None and state.state == "judge_failed":
                print(f"      judge failed at {state.failed_at}: {state.error_message}")
                print("      re-judge with the strata_rejudge MCP tool")
            # Indent multi-line content.
            for line in c.content.splitlines():
                print(f"      | {line}")
            print()

        # Name the exact next command rather than leaving the reader to work
        # the cursor out — the older pages are one keystroke away, not hidden.
        if page.next_before_id is not None:
            print(f"Older: strata record {args.scope_id} --before {page.next_before_id}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show the per-scope memory-freshness (staleness) metric — embedded read.

    Mechanical (issue #110): for each active scope, "N sessions read this scope's
    perspective since its last accepted contribution", over a recency window. A
    high count is the drift signal — memory being consumed but not updated. The
    metric never triggers or gates judgment; it only measures.
    """
    from strata.session_state import (
        DEFAULT_STALENESS_WINDOW_DAYS,
        SessionStateStore,
        compute_fleet_staleness,
        sessions_dir_for,
    )
    from strata.stores import EmbeddedStoreError, open_embedded_stores

    window_days = getattr(args, "window_days", None) or DEFAULT_STALENESS_WINDOW_DAYS

    try:
        stores = open_embedded_stores()
    except EmbeddedStoreError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    with stores:
        summaries_dir = str(stores.summary_store.summaries_dir)
        session_store = SessionStateStore(sessions_dir_for(summaries_dir))
        active = stores.fleet_config.active_scopes()
        metrics = compute_fleet_staleness(
            [s.id for s in active],
            record_store=stores.record_store,
            session_store=session_store,
            window_days=window_days,
        )

        print(
            f"Memory freshness — staleness over a {window_days}-day window "
            f"({len(active)} scope(s)):"
        )
        print()
        if not active:
            print("  _(no active scopes)_")
            return 0
        # Column width tracks the longest scope id so the metric column lines up.
        id_width = max((len(m.scope_id) for m in metrics), default=6)
        print(f"  {'scope':{id_width}}  reads-since-contrib  last accepted contribution")
        for m in metrics:
            last = m.last_accepted_contribution_at or "(none)"
            print(f"  {m.scope_id:{id_width}}  {m.reads_since_last_contribution:<19}  {last}")
    return 0


def _mcp_entry_is_migratable(entry: object, project_root: Path) -> bool:
    """Whether *entry* is something a previous `strata register` would have
    written into `.claude/settings.json`'s legacy `mcpServers.strata`
    location — byte-exact against the canonical/historical shape
    (:data:`_MCP_ENTRY` / :data:`_MCP_ENTRY_HISTORICAL`), or this exact
    project's `--bootstrap-venv` absolute-path shape
    (:func:`_is_bootstrap_venv_shape_mcp_entry`). Anything else — a
    hand-edited entry, or a different tool's — is not.

    Shared by `cmd_register`'s legacy-entry migration and `cmd_doctor`'s
    advice so the two never give conflicting guidance about the same
    on-disk entry: register only ever moves what doctor calls migratable,
    and doctor only ever tells the user to expect an automatic move for
    entries register will actually move.
    """
    if not isinstance(entry, dict):
        return False
    if entry in (_MCP_ENTRY, *_MCP_ENTRY_HISTORICAL):
        return True
    return _is_bootstrap_venv_shape_mcp_entry(entry, project_root)


# ---------------------------------------------------------------------------
# strata doctor — diagnose a registered project's wiring (Task 2.1,
# local-launch-bar plan).
#
# Entirely offline (ADR 0004 D1): reads files, opens the SQLite DB directly,
# and inspects fleet.yaml/env vars. Never makes an HTTP request — no backend
# needs to be running. One line per check with a pass/fail glyph; every
# failure line says how to fix it. Exits 0 when every check passes, 1
# otherwise (mirrors `strata start`'s preflight, see `strata.preflight`).
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose a project's Strata wiring: config, DB, fleet, install, binding.

    Checks (independent — one broken piece never masks the others):

    1. Project config (``.strata/config.toml``) resolvable.
    2. DB reachable and migrated.
    3. ``fleet.yaml`` valid.
    4. MCP server entry present in ``.mcp.json`` (flags a legacy, unread
       ``.claude/settings.json`` copy if that's all that's present).
    5. Stop hook script present and matching the shipped version.
    6. ``hooks.Stop`` entry present in ``.claude/settings.json``.
    7. Skills present in ``.claude/skills/``.
    8. Binding env vars (``STRATA_AGENT_SCOPE`` / ``_SKILL`` / ``_SESSION_ID``)
       set and valid against the fleet.
    9. Judge key (``JUDGE_API_KEY`` / ``ANTHROPIC_API_KEY``) resolvable —
       soft, like check 8's session-id half: never flips the exit code.
    """
    from strata.bootstrap import load_fleet_config
    from strata.fleet_config import FleetConfigError
    from strata.project_config import ProjectConfigError, resolve_storage_paths

    checks: list[Check] = []

    # -----------------------------------------------------------------------
    # 1. Project config resolvable.
    # -----------------------------------------------------------------------
    paths = None
    try:
        paths = resolve_storage_paths()
    except ProjectConfigError as exc:
        checks.append(
            Check(
                name="Project config",
                kind="hard",
                passed=False,
                message=(
                    f"{exc.message} — fix .strata/config.toml by hand, or remove it and "
                    "run 'strata register' to recreate it."
                ),
            )
        )
    else:
        checks.append(
            Check(
                name="Project config",
                kind="hard",
                passed=True,
                message=f"resolved via {paths.source} ({paths.fleet_yaml_path})",
            )
        )

    # -----------------------------------------------------------------------
    # 2. DB reachable and migrated. Read-only and diagnostic only — this must
    # never create or write to the DB it is inspecting. A doctor run in an
    # unregistered directory is exactly the scenario where a user wants to
    # find out what's wrong, not have ./strata.db silently materialize as a
    # side effect of asking. If the file is absent, report that and stop —
    # no connection is opened. If it exists, open it in SQLite's read-only
    # URI mode (never sqlite3.connect(path), which auto-creates) and compare
    # its applied-migrations table against the bundled migration files,
    # without applying anything.
    # -----------------------------------------------------------------------
    if paths is None:
        checks.append(
            Check(
                name="Database",
                kind="hard",
                passed=False,
                message="skipped — fix the project config check above first.",
            )
        )
    else:
        db_file = Path(paths.db_path)
        if not db_file.exists():
            checks.append(
                Check(
                    name="Database",
                    kind="hard",
                    passed=False,
                    message=(
                        f"no database at {db_file} — run 'strata start' or 'strata register' "
                        "to create and migrate it (doctor never creates it)."
                    ),
                )
            )
        else:
            from strata.migrator import _default_migrations_dir  # noqa: PLC0415

            conn: sqlite3.Connection | None = None
            try:
                conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
                try:
                    applied = {
                        row[0] for row in conn.execute("SELECT name FROM _migrations").fetchall()
                    }
                except sqlite3.OperationalError:
                    # No _migrations table yet — nothing has been applied.
                    applied = set()
                available = {p.name for p in _default_migrations_dir().glob("*.sql")}
                pending = sorted(available - applied)
            except sqlite3.Error as exc:
                checks.append(
                    Check(
                        name="Database",
                        kind="hard",
                        passed=False,
                        message=(
                            f"cannot open {db_file} read-only: {exc} — check it is a valid "
                            "Strata SQLite database."
                        ),
                    )
                )
            else:
                if pending:
                    checks.append(
                        Check(
                            name="Database",
                            kind="hard",
                            passed=False,
                            message=(
                                f"reachable but {len(pending)} migration(s) pending: "
                                f"{', '.join(pending)} — run 'strata migrate' or 'strata "
                                "start' to apply them."
                            ),
                        )
                    )
                else:
                    checks.append(
                        Check(
                            name="Database",
                            kind="hard",
                            passed=True,
                            message=f"reachable and migrated ({db_file})",
                        )
                    )
            finally:
                if conn is not None:
                    conn.close()

    # -----------------------------------------------------------------------
    # 3. fleet.yaml valid.
    # -----------------------------------------------------------------------
    fleet_config: FleetConfig | None = None
    if paths is None:
        checks.append(
            Check(
                name="Fleet config",
                kind="hard",
                passed=False,
                message="skipped — fix the project config check above first.",
            )
        )
    else:
        fleet_path = Path(paths.fleet_yaml_path)
        if not fleet_path.exists():
            checks.append(
                Check(
                    name="Fleet config",
                    kind="hard",
                    passed=False,
                    message=(
                        f"no fleet.yaml found at {fleet_path} — run 'strata register' to "
                        "seed one, or restore it from version control."
                    ),
                )
            )
        else:
            try:
                fleet_config = load_fleet_config(fleet_path)
            except (FleetConfigError, FileNotFoundError) as exc:
                checks.append(
                    Check(
                        name="Fleet config",
                        kind="hard",
                        passed=False,
                        message=f"invalid: {exc} — fix {fleet_path} and re-run.",
                    )
                )
            else:
                checks.append(
                    Check(
                        name="Fleet config",
                        kind="hard",
                        passed=True,
                        message=f"valid ({len(fleet_config.scopes)} scope(s))",
                    )
                )

    # -----------------------------------------------------------------------
    # Read .claude/settings.json once for checks 4 and 6.
    # -----------------------------------------------------------------------
    project_root = (
        paths.project_root if (paths is not None and paths.project_root is not None) else Path.cwd()
    )
    settings_json = project_root / ".claude" / "settings.json"
    settings_data: dict = {}
    settings_error: str | None = None
    if settings_json.exists():
        try:
            loaded_settings_json = json.loads(settings_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            settings_error = f"not valid JSON ({exc})"
        else:
            # Valid JSON but not an object (e.g. `[]`, `null`, a bare string)
            # — .get()/settings lookups below assume a dict; route this
            # through the same "fix the file" message rather than crashing.
            if isinstance(loaded_settings_json, dict):
                settings_data = loaded_settings_json
            else:
                settings_error = f"not a JSON object (got {type(loaded_settings_json).__name__})"

    # -----------------------------------------------------------------------
    # 4. MCP server entry present — checked in `.mcp.json`, the file Claude
    # Code actually reads for project-scoped MCP servers (not
    # `.claude/settings.json`, which has no `mcpServers` key in its schema).
    # -----------------------------------------------------------------------
    mcp_json = project_root / ".mcp.json"
    mcp_json_data: dict = {}
    mcp_json_error: str | None = None
    if mcp_json.exists():
        try:
            loaded_mcp_json = json.loads(mcp_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            mcp_json_error = f"not valid JSON ({exc})"
        else:
            # Valid JSON but not an object (e.g. `[]`, `null`, a bare string)
            # — .get()/mcpServers lookups below assume a dict; route this
            # through the same "fix the file" message rather than crashing.
            if isinstance(loaded_mcp_json, dict):
                mcp_json_data = loaded_mcp_json
            else:
                mcp_json_error = f"not a JSON object (got {type(loaded_mcp_json).__name__})"

    legacy_mcp_servers = settings_data.get("mcpServers") if settings_error is None else None
    legacy_mcp_entry = (
        legacy_mcp_servers.get("strata") if isinstance(legacy_mcp_servers, dict) else None
    )
    legacy_mcp_entry_present = legacy_mcp_entry is not None
    # Only entries `strata register` would actually move automatically get
    # told "run register to migrate/clean it up" — a hand-edited legacy
    # entry is register's business to leave alone (see
    # `_mcp_entry_is_migratable`'s docstring), so doctor must not promise a
    # move that will never happen (MEDIUM 4, live incident: doctor's advice
    # was a dead end for exactly this case).
    legacy_mcp_entry_migratable = _mcp_entry_is_migratable(legacy_mcp_entry, project_root)

    if mcp_json_error is not None:
        checks.append(
            Check(
                name="MCP server entry",
                kind="hard",
                passed=False,
                message=(
                    f".mcp.json is {mcp_json_error} — fix the file, then "
                    "run 'strata register' to add the strata entry."
                ),
            )
        )
    elif _mcp_server_present(mcp_json_data):
        message = "present in .mcp.json"
        if legacy_mcp_entry_present:
            # Both present — a half-migrated or re-registered-with-an-old-
            # release project (register itself sweeps this up as "stale
            # duplicate"; doctor surfaces it too rather than reporting a
            # silent clean pass).
            if legacy_mcp_entry_migratable:
                message += (
                    "; a stale duplicate is also in .claude/settings.json mcpServers — run "
                    "'strata register' to clean it up"
                )
            else:
                message += (
                    "; an edited mcpServers.strata entry is also in .claude/settings.json — "
                    "harmless (Claude Code doesn't read it), but left in place — remove it "
                    "by hand if you want it gone"
                )
        checks.append(
            Check(
                name="MCP server entry",
                kind="hard",
                passed=True,
                message=message,
            )
        )
    elif legacy_mcp_entry_present:
        if legacy_mcp_entry_migratable:
            message = (
                "found only in .claude/settings.json mcpServers — that file is not read "
                "by Claude Code for MCP servers; run 'strata register' to migrate it "
                "into .mcp.json."
            )
        else:
            message = (
                "found only in .claude/settings.json mcpServers, but it's been edited so "
                "'strata register' will not move it automatically — left in place; remove "
                "it by hand, then run 'strata register' to add the working entry to "
                ".mcp.json."
            )
        checks.append(
            Check(
                name="MCP server entry",
                kind="hard",
                passed=False,
                message=message,
            )
        )
    else:
        checks.append(
            Check(
                name="MCP server entry",
                kind="hard",
                passed=False,
                message="missing from .mcp.json mcpServers — run 'strata register' to add it.",
            )
        )

    # -----------------------------------------------------------------------
    # 5. Stop hook script present and matching shipped.
    # -----------------------------------------------------------------------
    hook_script = project_root / ".claude" / "hooks" / _HOOK_SCRIPT_NAME
    if not hook_script.exists():
        checks.append(
            Check(
                name="Stop hook",
                kind="hard",
                passed=False,
                message="script missing — run 'strata register' to restore it.",
            )
        )
    else:
        # Three-state drift, not just matches/doesn't: a "stale" script (an
        # older shipped version the user never touched) gets pointed at
        # `strata register` to self-update it; a genuinely "edited" one
        # keeps the "restore or keep" wording, since overwriting it is a
        # real, deliberate choice for the operator to make.
        hook_status = _classify_hook_drift(hook_script)
        if hook_status == "stale":
            checks.append(
                Check(
                    name="Stop hook",
                    kind="hard",
                    passed=False,
                    message=(
                        "script present but does not match the shipped version (an older "
                        "shipped version) — run 'strata register' to refresh it."
                    ),
                )
            )
        elif hook_status == "edited":
            checks.append(
                Check(
                    name="Stop hook",
                    kind="hard",
                    passed=False,
                    message=(
                        "script present but does not match the shipped version — run "
                        "'strata register' to restore it (or keep it if you edited it "
                        "intentionally)."
                    ),
                )
            )
        else:
            note = "" if hook_status == "match" else " (shipped version unavailable to compare)"
            checks.append(
                Check(
                    name="Stop hook",
                    kind="hard",
                    passed=True,
                    message=f"script present{note}",
                )
            )

    # -----------------------------------------------------------------------
    # 6. hooks.Stop entry present.
    # -----------------------------------------------------------------------
    if settings_error is not None:
        checks.append(
            Check(
                name="Stop hook entry",
                kind="hard",
                passed=False,
                message=(
                    f".claude/settings.json is {settings_error} — fix the "
                    "file, then run 'strata register' to add the Stop hook entry."
                ),
            )
        )
    elif _stop_hook_present(settings_data):
        checks.append(
            Check(
                name="Stop hook entry",
                kind="hard",
                passed=True,
                message="present in .claude/settings.json hooks.Stop",
            )
        )
    else:
        checks.append(
            Check(
                name="Stop hook entry",
                kind="hard",
                passed=False,
                message=(
                    "missing from .claude/settings.json hooks.Stop — run 'strata register' "
                    "to add it."
                ),
            )
        )

    # -----------------------------------------------------------------------
    # 7. Skills present AND matching the shipped version (drift detection,
    # same three-state semantics as the Stop-hook check above: a
    # stale-but-never-edited skill (an older shipped version) points at
    # `strata register` to self-update it; a genuinely edited one keeps the
    # "compare with --diff before deciding" wording, since register never
    # overwrites it on its own. classify_skill_drift returning "unknown"
    # means the shipped reference couldn't be read, so — same conservative
    # "leave it" rule as before — that skill counts as a pass.
    # -----------------------------------------------------------------------
    claude_skills_dir = project_root / ".claude" / "skills"
    missing_skills: list[str] = []
    stale_skills: list[str] = []
    mismatched_skills: list[str] = []
    for name in SKILL_NAMES:
        skill_md = claude_skills_dir / name / "Skill.md"
        if not skill_md.exists():
            missing_skills.append(name)
            continue
        skill_status = _classify_skill_drift(skill_md, name)
        if skill_status == "stale":
            stale_skills.append(name)
        elif skill_status == "edited":
            mismatched_skills.append(name)

    if missing_skills or stale_skills or mismatched_skills:
        problems = []
        if missing_skills:
            problems.append(f"missing: {', '.join(missing_skills)}")
        if stale_skills:
            problems.append(
                f"stale (an older shipped version): {', '.join(stale_skills)} — run "
                "'strata register' to refresh"
            )
        if mismatched_skills:
            problems.append(
                f"stale (does not match the shipped version): {', '.join(mismatched_skills)}"
            )
        checks.append(
            Check(
                name="Skills",
                kind="hard",
                passed=False,
                message=(
                    "; ".join(problems) + " — run 'strata register' to copy missing skills into "
                    ".claude/skills/, or 'strata register --diff' to see what changed in "
                    "the stale ones before deciding whether to restore them."
                ),
            )
        )
    else:
        checks.append(
            Check(
                name="Skills",
                kind="hard",
                passed=True,
                message=f"all {len(SKILL_NAMES)} present and match the shipped version",
            )
        )

    # -----------------------------------------------------------------------
    # 8. Binding env vars set and valid against the fleet.
    #
    # Single-scope auto-bind (mirrors strata.mcp.server._validate_binding):
    # an unset/empty STRATA_AGENT_SCOPE against a fleet with exactly one
    # active scope is not a failure here — it auto-binds, and the check
    # passes with a note saying so. Unset/empty against 2+ scopes (or no
    # fleet) keeps today's failure, naming the available scope IDs.
    # -----------------------------------------------------------------------
    scope = os.environ.get("STRATA_AGENT_SCOPE", "")
    skill = os.environ.get("STRATA_AGENT_SKILL", "")
    session_id = os.environ.get("STRATA_AGENT_SESSION_ID", "")
    binding_problems: list[str] = []
    auto_bind_note = ""

    if not scope and fleet_config is not None:
        sole = fleet_config.auto_bind_scope()
        if sole is not None:
            scope = sole.id
            auto_bind_note = f" (will auto-bind to {scope!r} — the fleet's only scope)"
            if not skill and sole.default_skill:
                skill = sole.default_skill

    if not scope:
        available = ""
        if fleet_config is not None:
            ids = ", ".join(s.id for s in fleet_config.active_scopes())
            if ids:
                available = f" (available: {ids})"
        binding_problems.append(f"STRATA_AGENT_SCOPE is not set{available}")

    scope_obj = None
    if fleet_config is not None and scope:
        scope_obj = fleet_config.get_scope(scope)
        if scope_obj is None:
            available = ", ".join(s.id for s in fleet_config.active_scopes())
            binding_problems.append(
                f"scope {scope!r} not found in fleet.yaml (available: {available or '(none)'})"
            )

    # A scope that declares no skills (no default_skill, no permitted_skills)
    # may bind skill-less — mirrors strata.mcp.server._validate_binding.
    scope_waives_skill = scope_obj is not None and not (
        scope_obj.default_skill or scope_obj.permitted_skills
    )
    if not skill and not scope_waives_skill:
        binding_problems.append("STRATA_AGENT_SKILL is not set")

    if scope_obj is not None and skill:
        permitted = scope_obj.permitted_skills or []
        if permitted and skill not in permitted:
            binding_problems.append(
                f"skill {skill!r} is not permitted for scope {scope!r} "
                f"(permitted: {', '.join(permitted)})"
            )

    if binding_problems:
        checks.append(
            Check(
                name="Agent binding",
                kind="hard",
                passed=False,
                message=(
                    "; ".join(binding_problems)
                    + " — export STRATA_AGENT_SCOPE / STRATA_AGENT_SKILL before launching, "
                    "matching fleet.yaml."
                ),
            )
        )
    else:
        skill_note = f", STRATA_AGENT_SKILL={skill!r}" if skill else ""
        checks.append(
            Check(
                name="Agent binding",
                kind="hard",
                passed=True,
                message=f"STRATA_AGENT_SCOPE={scope!r} valid{skill_note}{auto_bind_note}",
            )
        )

    # STRATA_AGENT_SESSION_ID is auto-generated when absent (mirrors
    # strata.mcp.server: `os.environ.get("STRATA_AGENT_SESSION_ID", f"sess_{uuid4()...}")`)
    # — an operator's shell will almost never have it exported, so its absence is
    # informational, not a setup problem. Soft check: warns, never fails the run.
    if session_id:
        checks.append(
            Check(
                name="Agent session ID",
                kind="soft",
                passed=True,
                message=f"STRATA_AGENT_SESSION_ID={session_id!r}",
            )
        )
    else:
        checks.append(
            Check(
                name="Agent session ID",
                kind="soft",
                passed=False,
                message=(
                    "STRATA_AGENT_SESSION_ID is not set — fine for `strata doctor` itself "
                    "(one is auto-generated per session); set it explicitly only if you "
                    "want stable session tracking across restarts."
                ),
            )
        )

    # -----------------------------------------------------------------------
    # 9. Judge key resolvable. Soft — like the session-id check above: a
    # project can be fully wired and still have no judge key yet (the first
    # contribution just sits unjudged until one is set). Never flips the
    # exit code.
    # -----------------------------------------------------------------------
    if _judge_key_visible(project_root):
        checks.append(
            Check(
                name="Judge key",
                kind="soft",
                passed=True,
                message="resolved (JUDGE_API_KEY or ANTHROPIC_API_KEY)",
            )
        )
    else:
        checks.append(
            Check(
                name="Judge key",
                kind="soft",
                passed=False,
                message=(
                    "no judge key found — add JUDGE_API_KEY=... to .env in this "
                    "project (or export it); contributions wait unjudged until "
                    "then. `strata register` offers to capture it."
                ),
            )
        )

    return _run_preflight(checks)


# ---------------------------------------------------------------------------
# strata operator — the operator stratum's vanilla-Strata entry surface
# (ADR 0008 D1: "Strata must work fully locally"). Embedded reads/writes,
# same discipline as scopes/summary/record above.
# ---------------------------------------------------------------------------

# Set by _build_parser() so `strata operator` with no subcommand can print
# its own help (mirrors main()'s bare-`strata` behaviour, one level down).
_operator_parser: argparse.ArgumentParser | None = None


def cmd_operator_root(args: argparse.Namespace) -> int:
    """``strata operator`` with no subcommand — print the group's help."""
    if _operator_parser is not None:
        _operator_parser.print_help()
    return 0


def cmd_operator_publish(args: argparse.Namespace) -> int:
    """``strata operator publish`` — publish a new operator memory item (ADR 0008 D1)."""
    from strata.operator import operator_publish
    from strata.stores import EmbeddedStoreError, open_embedded_stores

    try:
        stores = open_embedded_stores()
    except EmbeddedStoreError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    with stores:
        scope = stores.fleet_config.get_scope(args.scope_id)
        if scope is None:
            print(f"Scope not found: {args.scope_id}", file=sys.stderr)
            return 1

        item = operator_publish(
            args.scope_id,
            args.content,
            args.kind,
            args.subject,
            record_store=stores.record_store,
            summaries_dir=stores.summary_store.summaries_dir,
        )
        print(f"Published operator {item.kind} [{item.id}] attached at {args.scope_id!r}.")
    return 0


def cmd_operator_supersede(args: argparse.Namespace) -> int:
    """``strata operator supersede`` — routes by id prefix (ADR 0008 D1 vs. D4).

    ``op_`` ids supersede an operator-stratum item (capacity 1, not judged).
    ``c_`` ids correct a scope's own native directive in person (capacity 2,
    a judgment made by the operator).
    """
    from strata.operator import operator_supersede, operator_supersede_item
    from strata.stores import EmbeddedStoreError, open_embedded_stores

    try:
        stores = open_embedded_stores()
    except EmbeddedStoreError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    with stores:
        scope = stores.fleet_config.get_scope(args.scope_id)
        if scope is None:
            print(f"Scope not found: {args.scope_id}", file=sys.stderr)
            return 1

        item_id: str = args.id
        if item_id.startswith("op_"):
            try:
                item = operator_supersede_item(
                    args.scope_id,
                    item_id,
                    args.content,
                    args.subject,
                    record_store=stores.record_store,
                    summaries_dir=stores.summary_store.summaries_dir,
                )
            except KeyError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(
                f"Superseded operator item {item_id} -> [{item.id}] attached at {args.scope_id!r}."
            )
        elif item_id.startswith("c_"):
            try:
                new_directive = operator_supersede(
                    args.scope_id,
                    item_id,
                    args.content,
                    args.subject,
                    fleet=stores.fleet_config,
                    record_store=stores.record_store,
                    summary_store=stores.summary_store,
                )
            except (ValueError, KeyError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(
                f"Superseded directive {item_id} -> [{new_directive.id}] "
                f"in scope {args.scope_id!r} (operator correction)."
            )
        else:
            print(
                f"Unrecognized id {item_id!r} — expected an 'op_' operator item id "
                "(operator stratum) or a 'c_' directive id (scope correction).",
                file=sys.stderr,
            )
            return 1
    return 0


def cmd_operator_retire(args: argparse.Namespace) -> int:
    """``strata operator retire`` — routes by id prefix (ADR 0008 D1 vs. D4).

    ``op_`` ids retire an operator-stratum item (capacity 1, not judged).
    ``c_`` ids retire a scope's own native directive in person without
    replacement (capacity 2 — appends a ``retirements`` event, never a
    fabricated contribution).
    """
    from strata.operator import operator_retire, operator_retire_item
    from strata.stores import EmbeddedStoreError, open_embedded_stores

    try:
        stores = open_embedded_stores()
    except EmbeddedStoreError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    with stores:
        scope = stores.fleet_config.get_scope(args.scope_id)
        if scope is None:
            print(f"Scope not found: {args.scope_id}", file=sys.stderr)
            return 1

        item_id: str = args.id
        if item_id.startswith("op_"):
            try:
                act = operator_retire_item(
                    args.scope_id,
                    item_id,
                    record_store=stores.record_store,
                    summaries_dir=stores.summary_store.summaries_dir,
                )
            except KeyError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"Retired operator item {item_id} (act {act.id}) attached at {args.scope_id!r}.")
        elif item_id.startswith("c_"):
            try:
                retirement = operator_retire(
                    args.scope_id,
                    item_id,
                    args.reason,
                    fleet=stores.fleet_config,
                    record_store=stores.record_store,
                    summary_store=stores.summary_store,
                )
            except (ValueError, KeyError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(
                f"Retired directive {item_id} from scope {args.scope_id!r} "
                f"(retirement {retirement.id}, operator correction)."
            )
        else:
            print(
                f"Unrecognized id {item_id!r} — expected an 'op_' operator item id "
                "(operator stratum) or a 'c_' directive id (scope correction).",
                file=sys.stderr,
            )
            return 1
    return 0


def cmd_operator_show(args: argparse.Namespace) -> int:
    """``strata operator show`` — print operator layer(s) verbatim + the health signal.

    Without ``scope_id``: every attachment scope's operator memory plus
    fleet-wide totals. With ``scope_id``: that scope's operator layer plus
    its own item/word counts (ADR 0008 D6 — constitutional, not operational).
    """
    from strata.operator import operator_health, read_operator_layer
    from strata.stores import EmbeddedStoreError, open_embedded_stores

    try:
        stores = open_embedded_stores()
    except EmbeddedStoreError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    with stores:
        summaries_dir = stores.summary_store.summaries_dir
        health = operator_health(record_store=stores.record_store, summaries_dir=summaries_dir)

        if args.scope_id:
            scope = stores.fleet_config.get_scope(args.scope_id)
            if scope is None:
                print(f"Scope not found: {args.scope_id}", file=sys.stderr)
                return 1

            items = read_operator_layer(args.scope_id, summaries_dir=summaries_dir)
            print(f"Operator memory attached at {args.scope_id!r} ({len(items)} item(s)):")
            if not items:
                print("  _(none yet)_")
            for item in items:
                subject_part = f" — {item.subject}" if item.subject else ""
                print(f"  [{item.id}] {item.kind}{subject_part}  (at {item.created_at})")
                for line in item.content.splitlines():
                    print(f"      | {line}")
            print()
            scope_health = health["per_scope"].get(args.scope_id, {"items": 0, "words": 0})
            print(f"Health: {scope_health['items']} item(s), {scope_health['words']} word(s).")
        else:
            print(
                f"Operator memory — {health['total_items']} item(s), "
                f"{health['total_words']} word(s) across "
                f"{len(health['per_scope'])} attachment scope(s):"
            )
            if not health["per_scope"]:
                print("  _(none yet)_")
            for scope_id, counts in sorted(health["per_scope"].items()):
                print(f"  {scope_id}: {counts['items']} item(s), {counts['words']} word(s)")

        print()
        print(
            f"Operator acts: {health['total_acts']} total, "
            f"{health['acts_last_N_days']} in the last {health['churn_window_days']} days."
        )
        print(
            "Doctrine (ADR 0008 D6): operator memory is constitutional, not operational — "
            "small, rare, and mostly stable."
        )
    return 0


# ---------------------------------------------------------------------------
# strata publication — ADR 0007's local entry surface: show a scope's (or
# every scope's) publication artifact verbatim, or bootstrap a scope's
# initial publication (ADR 0007 D4). Embedded reads/writes, same discipline
# as the operator group above.
# ---------------------------------------------------------------------------

# Set by _build_parser() so `strata publication` with no subcommand can print
# its own help (mirrors the operator group's pattern).
_publication_parser: argparse.ArgumentParser | None = None


def cmd_publication_root(args: argparse.Namespace) -> int:
    """``strata publication`` with no subcommand — print the group's help."""
    if _publication_parser is not None:
        _publication_parser.print_help()
    return 0


def cmd_publication_show(args: argparse.Namespace) -> int:
    """``strata publication show [scope_id]`` — print publication artifact(s) verbatim.

    Without ``scope_id``: every scope that has published anything. With
    ``scope_id``: that scope's artifact (or a "published nothing yet"
    message when it has no publication file).
    """
    from strata.publication import list_scopes_with_publications, read_publication_text
    from strata.stores import EmbeddedStoreError, open_embedded_stores

    try:
        stores = open_embedded_stores()
    except EmbeddedStoreError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    with stores:
        summaries_dir = str(stores.summary_store.summaries_dir)

        if args.scope_id:
            scope = stores.fleet_config.get_scope(args.scope_id)
            if scope is None:
                print(f"Scope not found: {args.scope_id}", file=sys.stderr)
                return 1
            text = read_publication_text(args.scope_id, summaries_dir=summaries_dir)
            if text is None:
                print(f"Scope {args.scope_id!r} has published nothing yet.")
            else:
                print(text, end="" if text.endswith("\n") else "\n")
            return 0

        scope_ids = list_scopes_with_publications(summaries_dir)
        if not scope_ids:
            print("No scope has published anything yet.")
            return 0
        for sid in scope_ids:
            text = read_publication_text(sid, summaries_dir=summaries_dir) or ""
            print(f"=== {sid} ===")
            print(text, end="" if text.endswith("\n") else "\n")
            print()
    return 0


def cmd_publication_bootstrap(args: argparse.Namespace) -> int:
    """``strata publication bootstrap <scope_id>`` — bootstrap an initial publication (ADR 0007 D4).

    A one-shot, operator-initiated migration step: the scope-manager
    proposes an initial publication distilled from the scope's CURRENT
    summary, judged through the normal publication path. Requires
    ``JUDGE_API_KEY`` (or the deprecated ``ANTHROPIC_API_KEY`` /
    ``STRATA_ANTHROPIC_API_KEY`` names) — this is an LLM judgment, not a
    mechanical copy.
    """
    from strata.publication import bootstrap_publication
    from strata.scope_manager import ScopeManager
    from strata.settings import get_settings
    from strata.stores import EmbeddedStoreError, open_embedded_stores

    try:
        stores = open_embedded_stores()
    except EmbeddedStoreError as exc:
        print(exc.message, file=sys.stderr)
        return 1

    with stores:
        scope = stores.fleet_config.get_scope(args.scope_id)
        if scope is None:
            print(f"Scope not found: {args.scope_id}", file=sys.stderr)
            return 1

        settings = get_settings()
        manager = ScopeManager(
            client=settings.build_judge_client(),
            model=settings.manager_model,
        )

        try:
            outcome = bootstrap_publication(
                args.scope_id,
                fleet=stores.fleet_config,
                record_store=stores.record_store,
                summary_store=stores.summary_store,
                scope_manager=manager,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        if outcome.decision == "decline" or not outcome.items:
            print(
                f"Scope-manager declined to bootstrap a publication for {args.scope_id!r}: "
                f"{outcome.reasoning}"
            )
            return 0

        print(
            f"Bootstrapped {len(outcome.items)} published item(s) for {args.scope_id!r}: "
            f"{outcome.reasoning}"
        )
        for item in outcome.items:
            print(f"  [{item.id}] {item.kind} anchors={item.anchors}")
    return 0


def cmd_export_fleet(args: argparse.Namespace) -> int:
    """Read V1 fleet tables and write fleet.yaml for V1.2.

    Reads ``strata``, ``scopes``, and ``edges`` from the V1 DB without running
    migrations, converts to the V1.2 schema, and writes a ``fleet.yaml`` that
    round-trips through :class:`FleetConfig.load`.
    """
    from strata.fleet_config import FleetConfigError
    from strata.fleet_export import ExportResult, TablesAbsentError, export_fleet

    db_path = args.db or _db_path_default()
    out_path_str = args.out or _fleet_config_default()
    out_path = Path(out_path_str)

    try:
        result: ExportResult = export_fleet(db_path, out_path, force=args.force)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except TablesAbsentError:
        print(
            f"No V1 fleet tables found in {db_path} — nothing to export "
            "(already migrated to V1.2?)",
            file=sys.stderr,
        )
        return 1
    except FileExistsError:
        print(
            f"{out_path} already exists. Pass --force to overwrite, or choose a "
            "different path with --out.",
            file=sys.stderr,
        )
        return 1
    except FleetConfigError as exc:
        print(
            f"Exported data failed validation [{exc.kind}]: {exc.message}",
            file=sys.stderr,
        )
        return 1

    print(
        f"Exported {result.strata_count} strata, {result.scopes_count} scopes, "
        f"{result.edges_count} edges → {result.out_path}"
    )
    print("Now run `strata start` to apply migration 0002 and load the exported config.")
    return 0


def _is_stale(summary: ScopeSummary, parent_summary: ScopeSummary) -> bool:
    """Return True when *summary* was built from an older parent version.

    A summary is stale when its ``parent_version`` stamp is less than the
    parent scope's current ``version`` stamp.  A missing ``parent_version``
    (``None``) is treated as stale so that legacy summaries without stamps
    get refreshed on the next launch.
    """
    if summary.parent_version is None:
        return True
    return summary.parent_version < parent_summary.version


def _refresh_scope(
    scope_id: str,
    *,
    fleet_config: FleetConfig,
    record_store: RecordStore,
    summary_store: SummaryStore,
    manager: ScopeManager,
    summary_max_words: int,
    window_verbatim_tail: int | None = None,
    recency_window_size: int | None = None,
    _visited: set[str] | None = None,
) -> None:
    """Refresh the summary for *scope_id*: mechanical splice, then one LLM call.

    Recursively refreshes stale ancestors first (root-first order).  Uses a
    ``_visited`` set to guard against cycles (which validation prevents, but
    this is defensive).

    ADR 0011 D4 splits the refresh in two: the parent's directives are
    incorporated MECHANICALLY (:func:`~strata.summary_store.splice_parent_directives`
    — byte-exact, ids and provenance preserved, no LLM), and the judge call
    that follows reconciles the context digest with that refreshed state. Its
    amendment may carry only ``new_context`` and lifecycle ops. The refresh
    contribution and its judgment are recorded exactly as before: the summary
    never moves without a record trail.

    ADR 0004 Decision 4 — last-write-wins; no lock.
    """
    from datetime import UTC, datetime

    from strata.record_store import RECENCY_WINDOW_SIZE, ContributorRef
    from strata.scope_manager import WINDOW_VERBATIM_TAIL
    from strata.summary_store import ScopeSummary, splice_parent_directives

    # The engine defaults when the caller has no settings in hand (ADR 0011 D2).
    # Resolved here rather than in the signature: scope_manager owns the
    # verbatim-tail constant, and importing it eagerly would pull the Anthropic
    # SDK into every CLI start.
    if window_verbatim_tail is None:
        window_verbatim_tail = WINDOW_VERBATIM_TAIL
    if recency_window_size is None:
        recency_window_size = RECENCY_WINDOW_SIZE

    if _visited is None:
        _visited = set()
    if scope_id in _visited:
        return
    _visited.add(scope_id)

    scope = fleet_config.get_scope(scope_id)
    if scope is None:
        print(
            f"  [refresh] scope {scope_id!r} not found in fleet config — skipping",
            file=sys.stderr,
        )
        return

    stratum_map = {s.id: s for s in fleet_config.strata}
    stratum = stratum_map.get(scope.stratum_id)
    if stratum is None:
        return

    # Resolve inter-stratum parent
    parent_scope = fleet_config.inter_stratum_parent(scope_id)

    # If there is a parent, ensure it is fresh first (recursive bottom-out at L0)
    if parent_scope is not None:
        parent_summary = summary_store.read(parent_scope.id)
        my_summary = summary_store.read(scope_id)

        # `parent_summary is not None` MUST come first — `_is_stale`'s signature
        # requires a non-None parent_summary. Without the short-circuit, a child
        # whose parent_version stamp is non-None but whose parent summary has
        # been deleted from disk would crash with AttributeError.
        already_fresh = (
            parent_summary is not None
            and my_summary is not None
            and not _is_stale(my_summary, parent_summary)
        )
        if parent_summary is None or already_fresh:
            # Either parent has no on-disk summary yet, or my summary is already
            # fresh against the parent's current version → no need to recurse.
            pass
        else:
            # Parent is missing or my summary is stale → refresh parent first
            _refresh_scope(
                parent_scope.id,
                fleet_config=fleet_config,
                record_store=record_store,
                summary_store=summary_store,
                manager=manager,
                summary_max_words=summary_max_words,
                window_verbatim_tail=window_verbatim_tail,
                recency_window_size=recency_window_size,
                _visited=_visited,
            )

        # Re-read parent summary after potential refresh
        parent_summary = summary_store.read(parent_scope.id)
    else:
        parent_summary = None

    # Now refresh this scope
    current_summary = summary_store.read(scope_id)
    # ADR 0011 D2: the mechanical recency digest, same windowed read the
    # contribute path uses.
    recent_contributions = record_store.list_recent_contributions(
        scope_id=scope_id, limit=recency_window_size
    )

    ts = datetime.now(tz=UTC).isoformat()

    # ADR 0011 D4: incorporate the parent's directives mechanically, before
    # the judge sees anything. A child with no summary of its own still gets
    # them — that is what makes a fresh child inherit on its first launch.
    if parent_summary is not None and parent_summary.directives:
        base_summary = (
            current_summary
            if current_summary is not None
            else ScopeSummary(scope_id=scope_id, directives=[], context="", updated_at=ts)
        )
        current_summary = splice_parent_directives(base_summary, parent_summary)

    # The refresh request is itself a contribution: it is appended to the
    # record BEFORE judgment and its judgment is recorded after, so the
    # summary never changes without a record trail ("the record is sacred" —
    # ROADMAP principle 4; CONTEXT.md § Contribution).
    refresh_contribution = record_store.append_contribution(
        scope_id=scope_id,
        content=(
            "[Manager refresh triggered by strata launch"
            " — parent directives already incorporated mechanically;"
            " reconcile the context digest with the refreshed state.]"
        ),
        proposed_classification="context",
        subject="manager-refresh",
        supersedes=None,
        contributor=ContributorRef(
            scope_id=scope_id,
            skill="scope-manager",
            session_id="refresh",
            ts=ts,
        ),
    )

    from strata.operator import operator_memory_binding

    print(f"  [refresh] refreshing scope {scope_id!r}...", file=sys.stderr)
    judgment = manager.judge(
        scope=scope,
        stratum=stratum,
        parent_summary=parent_summary,
        current_summary=current_summary,
        recent_contributions=recent_contributions,
        new_contribution=refresh_contribution,
        summary_max_words=summary_max_words,
        window_verbatim_tail=window_verbatim_tail,
        entitlement=fleet_config.entitlement_view(scope_id),
        operator_memory=operator_memory_binding(
            scope_id, fleet=fleet_config, summaries_dir=summary_store.summaries_dir
        ),
        # ADR 0011 D4 — the refresh amendment is context + lifecycle ops only.
        amendment_context_only=True,
    )

    record_store.record_judgment(
        contribution_id=refresh_contribution.id,
        decision=judgment.decision,
        judged_by="scope-manager",
        notes=judgment.record_notes,
    )

    if judgment.new_summary is not None:
        # Stamp the parent_version before writing
        parent_ver = parent_summary.version if parent_summary is not None else None
        to_write = judgment.new_summary.model_copy(update={"parent_version": parent_ver})
        summary_store.write(scope_id, to_write)
        # A retire op removes a directive with no replacement, so the record
        # carries a retirement event for it (ADR 0011 D1).
        for directive_id in judgment.retired_directive_ids:
            record_store.append_retirement(
                scope_id=scope_id,
                directive_id=directive_id,
                retired_by="scope-manager",
                reason=judgment.reasoning,
            )
        print(f"  [refresh] scope {scope_id!r} summary updated", file=sys.stderr)


def _run_manager_refresh(scope_id: str, *, skip: bool = False) -> None:
    """Run the pre-session manager-refresh step for *scope_id*.

    Walks the inter-stratum ancestor chain (root-first), refreshes any stale
    ancestors, then refreshes *scope_id* itself.  Skipped when:

    - ``skip`` is True (``--skip-refresh`` flag).
    - No judge API key is available (soft — prints a warning).
    - Any ancestor/scope is missing from the fleet config (non-fatal warning).

    ADR 0004 Decision 4 — last-write-wins, no lock.
    """
    from strata.fleet_config import FleetConfig
    from strata.record_store import RecordStore
    from strata.scope_manager import ScopeManager
    from strata.settings import get_settings
    from strata.summary_store import SummaryStore

    if skip:
        return

    settings = get_settings()

    if not (settings.judge_api_key or settings.anthropic_api_key):
        print(
            "  [refresh] JUDGE_API_KEY not set — skipping manager refresh",
            file=sys.stderr,
        )
        return

    paths = _storage_paths()
    fleet_yaml = paths.fleet_yaml_path
    if not Path(fleet_yaml).exists():
        print(
            f"  [refresh] fleet config not found at {fleet_yaml!r} — skipping manager refresh",
            file=sys.stderr,
        )
        return

    try:
        fleet_config = FleetConfig.load(Path(fleet_yaml))
    except Exception as exc:
        print(
            f"  [refresh] cannot load fleet config: {exc} — skipping manager refresh",
            file=sys.stderr,
        )
        return

    db_path = paths.db_path
    summaries_dir = paths.summaries_dir

    client = settings.build_judge_client()
    manager = ScopeManager(client=client, model=settings.manager_model)

    with RecordStore(db_path) as record_store:
        summary_store = SummaryStore(summaries_dir)

        # Walk ancestors root-first; refresh stale ones first, then the target scope.
        ancestors = fleet_config.inter_stratum_ancestors(scope_id)

        visited: set[str] = set()
        # Refresh stale ancestors (root-first)
        for ancestor in ancestors:
            ancestor_summary = summary_store.read(ancestor.id)
            if ancestor_summary is None:
                # No existing summary — refresh it
                _refresh_scope(
                    ancestor.id,
                    fleet_config=fleet_config,
                    record_store=record_store,
                    summary_store=summary_store,
                    manager=manager,
                    summary_max_words=settings.summary_max_words,
                    window_verbatim_tail=settings.window_verbatim_tail,
                    recency_window_size=settings.recency_window_size,
                    _visited=visited,
                )
            else:
                # Check if this ancestor is stale relative to its own parent
                ancestor_parent = fleet_config.inter_stratum_parent(ancestor.id)
                if ancestor_parent is not None:
                    ap_summary = summary_store.read(ancestor_parent.id)
                    if ap_summary is not None and _is_stale(ancestor_summary, ap_summary):
                        _refresh_scope(
                            ancestor.id,
                            fleet_config=fleet_config,
                            record_store=record_store,
                            summary_store=summary_store,
                            manager=manager,
                            summary_max_words=settings.summary_max_words,
                            window_verbatim_tail=settings.window_verbatim_tail,
                            _visited=visited,
                        )

        # Refresh the target scope itself
        my_summary = summary_store.read(scope_id)
        parent_scope = fleet_config.inter_stratum_parent(scope_id)
        parent_summary = summary_store.read(parent_scope.id) if parent_scope is not None else None

        needs_refresh = my_summary is None or (
            parent_summary is not None and _is_stale(my_summary, parent_summary)
        )
        if needs_refresh:
            _refresh_scope(
                scope_id,
                fleet_config=fleet_config,
                record_store=record_store,
                summary_store=summary_store,
                manager=manager,
                summary_max_words=settings.summary_max_words,
                window_verbatim_tail=settings.window_verbatim_tail,
                recency_window_size=settings.recency_window_size,
                _visited=visited,
            )


#: Codex launch is schema-verified but not live-verified (README, "Using
#: Strata with Codex CLI") — `strata launch` refuses honestly rather than
#: handing off to a binding that has never been confirmed to actually work.
_CODEX_LAUNCH_NOT_WIRED_MESSAGE = (
    "Codex launch is not wired yet: Codex's MCP env delivery is still being "
    "verified live (see README, 'Using Strata with Codex CLI'). Start codex "
    "manually after filling in the [mcp_servers.strata.env] values."
)


def _resolve_launch_harness(args: argparse.Namespace, project_root: Path | None) -> str:
    """Resolve which harness ``strata launch`` should start (Task 5).

    Resolution order:

    1. An explicit ``--harness`` flag wins outright.
    2. Else the project's recorded default (``strata set-default-harness``),
       read via :func:`strata.project_config.read_default_harness` — only
       when it names a harness Strata actually knows (see below).
    3. Else, when exactly one harness is WIRED in this project (see
       :func:`_wired_harnesses` — the same marker check as ``strata
       unregister``'s default), that one.
    4. Else ``"claude-code"`` — today's behavior, unchanged.

    *project_root* is ``None`` when the project isn't registered via
    ``.strata/config.toml`` (e.g. env-var-driven ``STRATA_FLEET_CONFIG``
    dev usage) — falls back to the current working directory so steps 2–3
    still have somewhere to look; both degrade gracefully to "not found"
    when nothing is there, landing on claude-code exactly as before.

    A recorded default that names a harness outside
    :data:`install.KNOWN_HARNESSES` (a hand-edited ``config.toml``, or one
    written by a future Strata version this one predates) is not silently
    treated as claude-code — it prints a one-line notice to stderr naming
    the bad value and falls back to claude-code explicitly, so a typo in
    ``default_harness`` is visible instead of quietly launching the wrong
    thing with no explanation.
    """
    explicit: str | None = getattr(args, "harness", None)
    if explicit is not None:
        return explicit

    from strata.project_config import read_default_harness

    effective_root = project_root if project_root is not None else Path.cwd().resolve()

    default = read_default_harness(effective_root)
    if default is not None:
        if default in install.KNOWN_HARNESSES:
            return default
        print(
            f"default harness {default!r} in .strata/config.toml is not one of: "
            f"{', '.join(install.KNOWN_HARNESSES)} — launching claude-code",
            file=sys.stderr,
        )
        return "claude-code"

    wired = _wired_harnesses(effective_root)
    if len(wired) == 1:
        return wired[0]

    return "claude-code"


def cmd_launch(args: argparse.Namespace) -> int:
    """Validate scope, resolve skill, and exec ``claude`` with STRATA_AGENT_* set.

    Steps (per ADR 0003 + ADR 0004 D1/D4 + issue #45; harness resolution —
    Task 5 — added as Step 0):
    0. Resolve which harness to start (see :func:`_resolve_launch_harness`).
       ``codex`` refuses honestly (exit 1) rather than handing off to a
       binding that isn't live-verified; ``claude-code`` continues below,
       unchanged.
    1. Preflight — prerequisite hygiene checks, reported in the same pass as
       fleet-resolution failures (all problems in one run, matching the MCP
       server's refuse-to-start style).
    2. Load active scopes from fleet.yaml directly (embedded mode — the
       backend is the Console UI only and is NOT required to launch).
    3. Determine target scope: positional arg > .strata-role discovery > picker.
    4. Resolve skill from scope declaration (ADR 0002 resolution table).
    5. Build session ID (auto-generated or --session override).
    5a. Manager-refresh step (ADR 0004 D4): refresh stale ancestor summaries,
        then refresh the scope itself.  Skipped with --skip-refresh.
    6. execvp("claude", ...) with STRATA_AGENT_* env vars.
    """
    from strata.fleet_config import FleetConfig, FleetConfigError

    # -----------------------------------------------------------------------
    # Step 0: Resolve harness (Task 5). Computed before preflight/fleet load
    # so an unwired codex refusal never depends on — or trips over — either.
    # -----------------------------------------------------------------------
    paths = _storage_paths()
    harness = _resolve_launch_harness(args, paths.project_root)
    if harness == "codex":
        print(_CODEX_LAUNCH_NOT_WIRED_MESSAGE, file=sys.stderr)
        return 1

    # -----------------------------------------------------------------------
    # Step 1: Preflight + fleet resolution — one pass, all failures reported.
    # -----------------------------------------------------------------------
    preflight_rc = 0
    if not args.skip_preflight:
        preflight_rc = _run_preflight(run_launch_preflight())

    # -----------------------------------------------------------------------
    # Step 2: Load active scopes from fleet.yaml (no backend required).
    # -----------------------------------------------------------------------
    fleet_path = Path(paths.fleet_yaml_path)
    fleet_error: str | None = None
    active_scopes: list[dict] = []
    if not fleet_path.exists():
        fleet_error = (
            f"No fleet config found at {fleet_path}.\n"
            "  In a registered project: run `strata register` from the project root.\n"
            "  In the Strata repo: run `strata register` from the repo root, "
            "or set STRATA_FLEET_CONFIG."
        )
    else:
        try:
            fleet = FleetConfig.load(fleet_path)
            active_scopes = [sc.model_dump() for sc in fleet.active_scopes()]
        except FleetConfigError as exc:
            fleet_error = f"Fleet config invalid [{exc.kind}]: {exc.message}"

    if fleet_error is not None:
        print(fleet_error, file=sys.stderr)
    if preflight_rc != 0 or fleet_error is not None:
        return 1

    interactive = is_interactive()
    valid_ids = [sc["id"] for sc in active_scopes]

    # -----------------------------------------------------------------------
    # Step 3: Determine target scope.
    # -----------------------------------------------------------------------
    scope_id_arg: str | None = args.scope_id  # may be None
    skill_from_role: str | None = None

    if scope_id_arg is not None:
        # Explicit positional arg — validate it.
        scope_data = next((sc for sc in active_scopes if sc["id"] == scope_id_arg), None)
        if scope_data is None:
            print(
                f"Unknown scope {scope_id_arg!r}. Valid scope IDs: {valid_ids}",
                file=sys.stderr,
            )
            return 1
    else:
        # Try .strata-role first.
        role_file = find_strata_role(Path.cwd())
        if role_file is not None:
            try:
                scope_id_from_role, skill_from_role = parse_strata_role(role_file)
            except StrataRoleParseError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            scope_data = next((sc for sc in active_scopes if sc["id"] == scope_id_from_role), None)
            if scope_data is None:
                print(
                    f"Scope {scope_id_from_role!r} (from {role_file}) is not an active scope. "
                    f"Valid scope IDs: {valid_ids}",
                    file=sys.stderr,
                )
                return 1
        elif (sole := fleet.auto_bind_scope()) is not None:
            # Single-scope fleet auto-bind (operator directive): with no
            # positional arg and no .strata-role, a fleet with exactly one
            # scope needs no picker — interactive or not — bind to it and
            # say so, mirroring the MCP server's own auto-bind notice. Routes
            # through FleetConfig.auto_bind_scope() — the single source of
            # truth for this rule — rather than re-deriving it from
            # active_scopes here.
            scope_data = sole.model_dump()
            print(f"Only one scope in the fleet — binding to {scope_data['id']!r}.")
        else:
            # No positional arg, no .strata-role, 2+ scopes — need the
            # interactive picker or fail.
            if not interactive:
                print(
                    f"No scope specified and no .strata-role found. Valid scope IDs: {valid_ids}",
                    file=sys.stderr,
                )
                return 1
            try:
                scope_data = prompt_scope(active_scopes)
            except SystemExit as exc:
                print(str(exc), file=sys.stderr)
                return 1

    # -----------------------------------------------------------------------
    # Step 4: Resolve skill.
    # -----------------------------------------------------------------------
    # --skill flag takes precedence over .strata-role skill (which in turn
    # falls through to the resolution table).
    requested_skill = args.skill if args.skill is not None else skill_from_role

    try:
        skill = resolve_skill(scope_data, requested_skill, interactive=interactive)
    except SkillResolutionError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # -----------------------------------------------------------------------
    # Step 5: Build session ID.
    # -----------------------------------------------------------------------
    session_id: str = args.session if args.session else make_session_id(scope_data["id"], skill)

    # -----------------------------------------------------------------------
    # Step 5a: Manager-refresh (ADR 0004 D4) — before execvp. A refresh
    # failure (API outage, bad key, malformed model output) must not abort
    # the launch: the session can still run on the existing summaries.
    # -----------------------------------------------------------------------
    try:
        _run_manager_refresh(
            scope_data["id"],
            skip=getattr(args, "skip_refresh", False),
        )
    except Exception as exc:  # noqa: BLE001 — deliberate: refresh is best-effort
        print(
            f"  [refresh] failed: {exc} — continuing with existing summaries "
            "(use --skip-refresh to skip this step entirely)",
            file=sys.stderr,
        )

    # -----------------------------------------------------------------------
    # Step 6: hand off to claude with STRATA_AGENT_* set.
    #
    # POSIX replaces this process image (execvpe); Windows spawns a
    # console-sharing child and propagates its exit code. Both raise
    # FileNotFoundError when claude is not on PATH — one message either way.
    # -----------------------------------------------------------------------
    env = os.environ.copy()
    env["STRATA_AGENT_SCOPE"] = scope_data["id"]
    # Skill is optional (issue #121): when the scope declares none and none was
    # requested, resolve_skill returns None — leave STRATA_AGENT_SKILL unset
    # rather than exporting an empty value, so the binding carries no skill.
    if skill is not None:
        env["STRATA_AGENT_SKILL"] = skill
    else:
        env.pop("STRATA_AGENT_SKILL", None)
    env["STRATA_AGENT_SESSION_ID"] = session_id

    try:
        return exec_claude(env)
    except FileNotFoundError:
        print(
            "Cannot find 'claude' on PATH. Install Claude Code and ensure it is on your PATH.",
            file=sys.stderr,
        )
        return 1


# ---------------------------------------------------------------------------
# strata register — brownfield install helper (ADR 0005 Decision 4)
# ---------------------------------------------------------------------------

#: Project root marker files — at least one must be present.
_PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")

# The additive-merge constants and machinery (_MCP_ENTRY, _GITIGNORE_MARKER,
# _GITIGNORE_BLOCK, _CONFIG_TOML, the settings merge, skill copy, and --diff
# rendering) live in strata.install — the documented public import surface
# (ADR 0009 D3). Imported at the top of this
# module; strata register below is built on them so the rules exist once.


def _self_install_spec() -> str | None:
    """Return a pip-installable spec for the *currently running* strata.

    Uses the PEP 610 ``direct_url.json`` metadata pip records for installs
    from a path or VCS URL. Returns None when no safe source can be
    determined (e.g. a hypothetical index install) — the caller must fail
    actionably rather than ``pip install strata``, which resolves to an
    unrelated PyPI package; this project publishes as ``strata-mem``
    (ADR 0009 D1; issue #49).
    """
    import importlib.metadata  # noqa: PLC0415

    try:
        dist = importlib.metadata.distribution(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None
    try:
        raw = dist.read_text("direct_url.json")
    except OSError:
        raw = None
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return None
    url = info.get("url", "")
    vcs_info = info.get("vcs_info")
    if vcs_info and url:
        vcs = vcs_info.get("vcs", "git")
        commit = vcs_info.get("commit_id", "")
        return f"{vcs}+{url}@{commit}" if commit else f"{vcs}+{url}"
    if url.startswith("file://"):
        return url.removeprefix("file://")
    return None


#: Printed instead of the interactive prompt whenever the judge key isn't
#: visible yet and `strata register` can't (or shouldn't) ask for it:
#: --yes, non-interactive, or the operator pressed Enter to skip. Kept as one
#: constant so every one of those paths — and their tests — say exactly the
#: same thing.
_JUDGE_KEY_LATER_NOTE = (
    "judge key: not set — add JUDGE_API_KEY=... to .env in this project "
    "(or export it) when ready; contributions wait unjudged until then."
)


def _judge_key_visible(project_root: Path) -> bool:
    """Return ``True`` if a judge/Anthropic API key already resolves for *project_root*.

    Builds a throwaway :class:`~strata.settings.Settings` scoped to this
    project's own ``.env`` rather than calling the cached
    ``get_settings()`` singleton directly: that singleton is constructed
    once per process and resolves its ``.env`` file relative to the
    process's cwd, which is wrong whenever `register --path` targets a
    directory other than cwd (and unworkable in tests that don't chdir).
    The precedence itself — ``JUDGE_API_KEY`` wins, ``ANTHROPIC_API_KEY`` is
    the fallback, either spelling, env or .env — is not reimplemented here;
    it stays exactly what ``Settings`` already resolves.
    """
    from strata.settings import Settings  # noqa: PLC0415

    settings = Settings(_env_file=project_root / ".env")
    return bool(settings.judge_api_key or settings.anthropic_api_key)


def _offer_judge_key_capture(project_root: Path, *, skip_prompt: bool) -> None:
    """End-of-register step: offer to capture the judge key (operator-directed).

    Motivated by a live failure — a project registered without a judge key,
    so its first contribution sat unjudged with no obvious next step. Runs
    only from the very end of a successful, non-diff `strata register`:

    - A key is already visible (env or this project's ``.env``) → print
      ``judge key: found`` and stop; nothing to ask.
    - Otherwise, when both ends of the terminal are interactive and
      ``--yes`` wasn't passed → prompt for the key with hidden input
      (:func:`getpass.getpass`, mirroring the markerless-register prompt's
      TTY check). A non-empty answer is written to this project's
      ``.env`` (creating, appending, or replacing an existing
      JUDGE_API_KEY/ANTHROPIC_API_KEY line — see
      :func:`strata.install.write_env_judge_key`); an empty answer, EOF
      (e.g. stdin closed mid-session), or Ctrl-C falls through to the
      how-to-add-it-later note below.
    - Non-interactive, or ``--yes`` → never prompts; prints the same note.

    Before any write, checks that this project's ``.gitignore`` actually
    covers ``.env`` (register's own GITIGNORE_BLOCK has covered it since
    this feature shipped — see :data:`strata.install.GITIGNORE_BLOCK` — but
    an already-registered project keeps whatever block it was seeded with,
    and a user can always edit their .gitignore by hand). If it doesn't,
    this warns loudly on stderr and still writes the key — skipping the
    write would strand the operator worse than the warning does.
    """
    if _judge_key_visible(project_root):
        print("judge key: found")
        return

    if skip_prompt or not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(_JUDGE_KEY_LATER_NOTE)
        return

    try:
        value = getpass.getpass(
            "Judge key — the LLM that reviews contributions (Anthropic, or any "
            "Messages-API endpoint). Paste to store it in .env, or press Enter "
            "to skip: "
        )
    except (EOFError, OSError, KeyboardInterrupt):
        value = ""

    value = value.strip()
    if not value:
        print(_JUDGE_KEY_LATER_NOTE)
        return

    gitignore_path = project_root / ".gitignore"
    gitignore_text = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    if not install.gitignore_covers_dotenv(gitignore_text):
        print(
            f"  {_glyph('fail')} WARNING: {gitignore_path} does not ignore `.env` — "
            "the judge key just written there could be committed to version "
            "control. Add a `.env` line to .gitignore.",
            file=sys.stderr,
        )

    env_path = project_root / ".env"
    action = install.write_env_judge_key(env_path, value)
    verb = {"created": "wrote", "replaced": "updated", "appended": "appended to"}[action]
    print(f"judge key: {verb} {env_path.relative_to(project_root)}")


def cmd_register(args: argparse.Namespace) -> int:
    """Idempotent brownfield installer — per ADR 0005 Decision 4.

    Walks the registration steps in order:
    1. Detect project root (require a project marker).
    2. Create .strata/ directory.
    3. Write .strata/config.toml (skip if exists).
    4. Update .gitignore (idempotent block with # Strata marker).
    5. Seed .strata/fleet.yaml from templates/minimal.yaml (skip if exists).
    6. Copy strata skills to .claude/skills/ (skip each if exists).
    7. Merge strata into .mcp.json mcpServers (skip if exists; migrates a
       legacy .claude/settings.json mcpServers.strata entry if found).
    8. Print next-steps or diff report.

    All writes are strictly additive (never overwrite existing user state).
    With --diff: read-only mode, prints what would differ.
    With --bootstrap-venv: creates .strata/.venv/ with strata installed.
    """
    import importlib.resources
    import json
    import subprocess
    import venv

    path_arg: str | None = getattr(args, "path", None)
    project_root = Path(path_arg).resolve() if path_arg else Path.cwd().resolve()
    diff_mode: bool = getattr(args, "diff", False)
    bootstrap_venv: bool = getattr(args, "bootstrap_venv", False)

    # -----------------------------------------------------------------------
    # Resolve which harness(es) to wire (Task 2, multi-harness parity):
    # explicit --harness flags win outright; otherwise detect what's
    # installed on this machine; if detection finds nothing, fall back to
    # claude-code (today's default) with a one-line notice so a bare CI
    # machine keeps working exactly as before.
    # -----------------------------------------------------------------------
    harness_flags: list[str] | None = getattr(args, "harness", None)
    no_harness_notice = False
    if harness_flags:
        resolved_harnesses: list[str] = list(harness_flags)
    else:
        resolved_harnesses = install.detect_harnesses()
        if not resolved_harnesses:
            resolved_harnesses = ["claude-code"]
            no_harness_notice = True

    # -----------------------------------------------------------------------
    # Step 1: Require a project marker.
    # -----------------------------------------------------------------------
    if not any((project_root / m).exists() for m in _PROJECT_MARKERS):
        markers_str = ", ".join(_PROJECT_MARKERS)
        markers_prose = ", ".join(_PROJECT_MARKERS[:-1]) + f", or {_PROJECT_MARKERS[-1]}"
        refuse_message = (
            f"Not a project root — register from a directory containing one of: {markers_str}\n"
            f"(checked: {project_root})\n"
            f"Starting fresh? Run `git init` first — a project root is anything with "
            f"{markers_prose}."
        )
        skip_prompt: bool = getattr(args, "yes", False)
        if not skip_prompt:
            # Only ask when both ends of the terminal are interactive — a
            # script piping stdout (or driven by a non-tty stdin) gets the
            # unchanged refusal + hint instead of hanging on input().
            if sys.stdin.isatty() and sys.stdout.isatty():
                try:
                    answer = (
                        input(
                            f"This directory has no project marker ({markers_str}). "
                            f"Register here anyway? [y/N] "
                        )
                        .strip()
                        .lower()
                    )
                except EOFError:
                    answer = ""
                if answer not in ("y", "yes"):
                    print(refuse_message, file=sys.stderr)
                    return 1
                # else: fall through and register exactly as normal.
            else:
                print(refuse_message, file=sys.stderr)
                return 1

    # -----------------------------------------------------------------------
    # Step 1b: .strata/ sanity check (ADR 0005 Decision 4).
    #
    # Before any action, if .strata/ exists but lacks config.toml, refuse to
    # proceed.  Prevents silently writing into a foreign tool's directory and
    # prevents register from running against a half-initialised state from
    # an interrupted prior register.
    # -----------------------------------------------------------------------
    candidate_strata = project_root / ".strata"
    if candidate_strata.exists() and not (candidate_strata / "config.toml").exists():
        print(
            f"Existing .strata/ directory at {candidate_strata} does not look like a Strata "
            f"workspace (no config.toml).\n"
            f"Please remove or rename it before running `strata register`.",
            file=sys.stderr,
        )
        return 1

    # -----------------------------------------------------------------------
    # Helper: print action or diff line.
    # -----------------------------------------------------------------------
    def _rel(path: str | Path) -> Path:
        return Path(path).relative_to(project_root) if Path(path).is_absolute() else Path(path)

    def _act(action: str, path: str | Path, *, skipped: bool = False) -> None:
        print(render_action_line(action, _rel(path), diff_mode=diff_mode, skipped=skipped))

    def _report_self_update(path: str | Path, status: str) -> None:
        """Print the register line for a managed artifact's self-update status.

        *status* is one of the three-state values ``strata.install``'s
        ``classify_*_drift``/``self_update_*`` functions return, restricted
        to the two register ever needs to report here (``"match"`` is
        handled by the ordinary ``_act("skip", ...)`` line, same as today):

        - ``"stale"``  — the installed copy matched an older shipped version
          and was never hand-edited; self-updated to current shipped content
          (or, in ``--diff`` mode, would be).
        - ``"edited"`` / ``"unknown"`` — left in place, same as a plain skip,
          plus a one-line note pointing at ``--diff`` so the operator can see
          what shipped content it's now behind.
        """
        rel = Path(path).relative_to(project_root) if Path(path).is_absolute() else Path(path)
        if status == "stale":
            if diff_mode:
                print(f"  [would update]  {rel} (shipped content changed)")
            else:
                print(f"  updated: {rel} (shipped content changed)")
        elif status == "edited":
            _act("kept", path, skipped=True)
            print("    (differs from shipped — see strata register --diff)")
        else:  # "unknown" — shipped reference unreadable; can't prove anything
            _act("skip", path, skipped=True)

    if diff_mode:
        print(f"strata register --diff  (dry-run, no writes)\nProject root: {project_root}")
    else:
        print(f"strata register\nProject root: {project_root}")
    if no_harness_notice:
        print("no harness detected on this machine — wiring claude-code (the default)")

    # -----------------------------------------------------------------------
    # Step 2: Create .strata/ directory.
    # -----------------------------------------------------------------------
    strata_dir = project_root / ".strata"
    if not strata_dir.exists():
        if not diff_mode:
            strata_dir.mkdir(parents=True)
        _act("created", strata_dir)

    # -----------------------------------------------------------------------
    # Step 3: Write .strata/config.toml.
    # -----------------------------------------------------------------------
    config_toml = strata_dir / "config.toml"
    if config_toml.exists():
        _act("skip", config_toml, skipped=True)
    else:
        if not diff_mode:
            config_toml.write_text(_CONFIG_TOML, encoding="utf-8")
        _act("wrote", config_toml)

    # -----------------------------------------------------------------------
    # Step 4: Update .gitignore.
    # -----------------------------------------------------------------------
    gitignore = project_root / ".gitignore"
    existing_gitignore = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if _GITIGNORE_MARKER in existing_gitignore:
        _act("skip", gitignore, skipped=True)
    else:
        if not diff_mode:
            with gitignore.open("a", encoding="utf-8") as f:
                if existing_gitignore and not existing_gitignore.endswith("\n"):
                    f.write("\n")
                f.write("\n")
                f.write(_GITIGNORE_BLOCK)
        _act("appended Strata block to", gitignore)

    # -----------------------------------------------------------------------
    # Step 5: Seed .strata/fleet.yaml from templates/minimal.yaml.
    # -----------------------------------------------------------------------
    fleet_yaml = strata_dir / "fleet.yaml"
    minimal_template = _TEMPLATES_DIR / "minimal.yaml"
    if fleet_yaml.exists():
        _act("skip", fleet_yaml, skipped=True)
    else:
        if not diff_mode:
            if minimal_template.exists():
                shutil.copy(minimal_template, fleet_yaml)
            else:
                # Fallback: write a minimal inline template.
                fleet_yaml.write_text(
                    "# TODO: replace with your team's structure\n"
                    "strata:\n  - id: L0\n    name: root\n    ordinal: 0\n"
                    "scopes:\n  - id: g_root\n    name: Root\n    stratum_id: L0\n"
                    "edges: []\n",
                    encoding="utf-8",
                )
        _act("seeded", fleet_yaml)

    def _seeded_binding_hint() -> tuple[bool, str]:
        """Return ``(single_scope, first_scope_id)`` for *fleet_yaml*.

        Loads via ``FleetConfig.load`` and routes the single/multi decision
        through ``FleetConfig.auto_bind_scope()`` — the same load path and
        the same source of truth as every other auto-bind call site (MCP
        server validation, the freshness evaluator, `strata doctor`, `strata
        launch`), so this can never drift from them by re-deriving its own
        active/archived status rule.

        Falls back to ``(True, "g_root")`` only when there is nothing to
        load: no file on disk (e.g. --diff on a project without a
        pre-existing fleet.yaml — nothing was actually written) or a load
        failure (a hand-edited fleet.yaml that fails validation must not
        crash the next-steps printing) — both the copied template and the
        inline fallback above seed exactly one scope, ``g_root``, so the
        fallback matches what would land on disk on a real (non-diff) run.
        """
        if not fleet_yaml.exists():
            return True, "g_root"
        try:
            from strata.fleet_config import FleetConfig  # noqa: PLC0415

            loaded = FleetConfig.load(fleet_yaml)
            sole = loaded.auto_bind_scope()
            if sole is not None:
                return True, sole.id
            active = loaded.active_scopes()
            return False, (active[0].id if active else "g_root")
        except Exception:  # noqa: BLE001
            return True, "g_root"

    settings_unreadable = False
    mcp_json_unreadable = False

    def _register_claude_code() -> None:
        nonlocal settings_unreadable, mcp_json_unreadable

        # -------------------------------------------------------------------
        # Step 6: Copy canonical skills to .claude/skills/ (skip each if
        # exists — unless it's stale-but-never-edited, in which case it's
        # self-updated to the current shipped content; see
        # strata.install._HISTORICAL_ARTIFACT_HASHES).
        # -------------------------------------------------------------------
        claude_skills_dir = project_root / ".claude" / "skills"
        skills_root = importlib.resources.files("strata") / "_skills"
        for skill_name in SKILL_NAMES:
            dest_skill_dir = claude_skills_dir / skill_name
            skill_md = dest_skill_dir / "Skill.md"
            if not dest_skill_dir.exists():
                copied = copy_skill(skills_root, skill_name, claude_skills_dir, dry_run=diff_mode)
                _act("copied" if copied else "skip", dest_skill_dir, skipped=not copied)
                continue
            status = _self_update_skill(skill_md, skill_name, dry_run=diff_mode)
            if status == "match":
                _act("skip", dest_skill_dir, skipped=True)
            else:
                _report_self_update(skill_md, status)

        # -------------------------------------------------------------------
        # Step 6b: Copy the freshness Stop-hook script to .claude/hooks/
        # (issue #112). Additive like the skills — an existing, unmodified
        # script is self-updated the same way; a hand-edited one is kept.
        # -------------------------------------------------------------------
        claude_hooks_dir = project_root / ".claude" / "hooks"
        hooks_root = importlib.resources.files("strata") / "_hooks"
        dest_hook = claude_hooks_dir / _HOOK_SCRIPT_NAME
        if not dest_hook.exists():
            hook_copied = copy_hook(hooks_root, claude_hooks_dir, dry_run=diff_mode)
            _act("copied" if hook_copied else "skip", dest_hook, skipped=not hook_copied)
        else:
            hook_status = _self_update_hook(dest_hook, dry_run=diff_mode)
            if hook_status == "match":
                _act("skip", dest_hook, skipped=True)
            else:
                _report_self_update(dest_hook, hook_status)

        # -------------------------------------------------------------------
        # Step 7: Merge strata into the project's `.mcp.json` — the file
        # Claude Code actually reads for project-scoped MCP servers
        # (`.claude/settings.json` has no `mcpServers` key in its schema;
        # its `enabledMcpjsonServers` setting explicitly refers to servers
        # "from .mcp.json"). `.claude/settings.json` still gets the
        # freshness `hooks.Stop` entry below (issue #112) — that location
        # IS correct for hooks. Both merges are strictly additive.
        #
        # Earlier Strata releases wrote the `mcpServers.strata` entry into
        # `.claude/settings.json` — a location Claude Code never reads for
        # MCP servers, shipping a memory-blind session. A legacy entry that
        # still byte/shape-matches exactly what a previous register wrote
        # is migrated here: removed from settings.json, written into
        # `.mcp.json`, with a "moved" line printed. Anything else there
        # (hand-edited, V1.2-shape, or simply other keys alongside it) is
        # left untouched with a note — never deleted speculatively.
        # -------------------------------------------------------------------
        settings_json = project_root / ".claude" / "settings.json"
        if settings_json.exists():
            try:
                loaded_settings_json = json.loads(settings_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                # NEVER fall through to a write here: writing with an empty dict
                # would replace the user's entire settings file with just the
                # Stop hook entry. Skip the merge outright and fail the run so
                # the user notices ("never overwrite user state").
                print(
                    f"  {_glyph('fail')} .claude/settings.json exists but is not valid JSON "
                    f"({exc}).\n"
                    "    Fix the file, then re-run `strata register` to add the freshness "
                    "Stop hook entry.",
                    file=sys.stderr,
                )
                settings_unreadable = True
                settings_data: dict = {}
            else:
                # Valid JSON but not an object (`[]`, `null`, ...) — never
                # fall through to a write here either; the merge below
                # assumes a dict.
                if isinstance(loaded_settings_json, dict):
                    settings_data = loaded_settings_json
                else:
                    print(
                        f"  {_glyph('fail')} .claude/settings.json exists but is not a JSON "
                        f"object (got {type(loaded_settings_json).__name__}).\n"
                        "    Fix the file, then re-run `strata register` to add the freshness "
                        "Stop hook entry.",
                        file=sys.stderr,
                    )
                    settings_unreadable = True
                    settings_data = {}
        else:
            settings_data = {}

        mcp_json = project_root / ".mcp.json"
        mcp_json_data: dict = {}
        if mcp_json.exists():
            try:
                loaded_mcp_json = json.loads(mcp_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                print(
                    f"  {_glyph('fail')} .mcp.json exists but is not valid JSON ({exc}).\n"
                    "    Fix the file, then re-run `strata register` to add the strata "
                    "mcpServers entry.",
                    file=sys.stderr,
                )
                mcp_json_unreadable = True
            else:
                # Valid JSON but not an object (`[]`, `null`, ...) — never
                # fall through to a write here either; the merge below
                # assumes a dict.
                if isinstance(loaded_mcp_json, dict):
                    mcp_json_data = loaded_mcp_json
                else:
                    print(
                        f"  {_glyph('fail')} .mcp.json exists but is not a JSON object "
                        f"(got {type(loaded_mcp_json).__name__}).\n"
                        "    Fix the file, then re-run `strata register` to add the strata "
                        "mcpServers entry.",
                        file=sys.stderr,
                    )
                    mcp_json_unreadable = True

        settings_changed = False
        mcp_json_changed = False

        legacy_mcp_servers = settings_data.get("mcpServers") if not settings_unreadable else None
        legacy_entry = (
            legacy_mcp_servers.get("strata") if isinstance(legacy_mcp_servers, dict) else None
        )

        def _migratable_legacy_entry() -> dict | None:
            """Return a deep copy of *legacy_entry* if it's something a
            previous release of `strata register` would have written —
            see :func:`_mcp_entry_is_migratable`. Anything else —
            hand-edited or simply a different tool's entry — returns
            ``None`` and is left untouched.
            """
            if _mcp_entry_is_migratable(legacy_entry, project_root):
                return copy.deepcopy(legacy_entry)
            return None

        def _drop_legacy_entry() -> None:
            nonlocal settings_changed
            del legacy_mcp_servers["strata"]
            if not legacy_mcp_servers:
                del settings_data["mcpServers"]
            settings_changed = True

        migratable_entry = _migratable_legacy_entry()

        if mcp_json_unreadable:
            pass  # merge skipped — reported above; register exits non-zero below
        elif _mcp_server_present(mcp_json_data):
            _act("skip", mcp_json, skipped=True)
            if migratable_entry is not None:
                # .mcp.json already has it (e.g. a previous migration run) but
                # the stale duplicate in settings.json was never cleaned up —
                # sweep it now so re-running register finishes the migration.
                # In --diff mode nothing is actually written below (the
                # settings.json write is gated on `not diff_mode`), so the
                # wording must stay a preview, not a past-tense claim.
                _drop_legacy_entry()
                verb = "would remove" if diff_mode else "removed"
                print(
                    f"  {verb} stale duplicate mcpServers.strata entry from "
                    f".claude/settings.json ({_rel(mcp_json)} already has it)"
                )
        elif migratable_entry is not None:
            # Something a previous register wrote (byte-exact canonical/
            # historical shape, or the --bootstrap-venv absolute-path shape)
            # — migrate it rather than leaving Claude Code memory-blind.
            # The venv shape's project-specific command is carried over
            # verbatim rather than replaced with the plain-register default.
            _drop_legacy_entry()
            mcp_json_data.setdefault("mcpServers", {})["strata"] = migratable_entry
            mcp_json_changed = True
            _act("moved strata mcpServers entry from .claude/settings.json into", mcp_json)
            if diff_mode:
                # _act's diff-mode wording only names the .mcp.json side
                # ("[would create/update]  .mcp.json") — call out the other
                # half of the move explicitly so --diff doesn't undersell
                # what a real run would do.
                print("  [would remove]  mcpServers.strata entry from .claude/settings.json")
        else:
            if isinstance(legacy_entry, dict):
                if _is_v1_2_shape_mcp_entry(legacy_entry):
                    print(
                        f"  {_glyph('warn')} WARNING: your existing strata mcpServer entry in "
                        ".claude/settings.json is V1.2-shape\n"
                        "    and will silently fail on V1.3 (the `mcp_server` Python module "
                        "no longer exists;\n"
                        "    `STRATA_BACKEND_URL` is unused). It is also in a location Claude "
                        "Code never reads for MCP\n"
                        f"    servers — strata register is adding the working entry to "
                        f"{_rel(mcp_json)} instead; run\n"
                        "    `strata register --diff` to see the canonical entry, then remove "
                        "the stale one from\n"
                        "    .claude/settings.json by hand.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"  {_glyph('warn')} NOTE: .claude/settings.json has an "
                        "mcpServers.strata entry that differs from the canonical\n"
                        "    one — Claude Code never reads mcpServers from settings.json, so "
                        "it was never doing\n"
                        f"    anything; left in place. strata register is adding the working "
                        f"entry to {_rel(mcp_json)} instead.",
                        file=sys.stderr,
                    )
            merge_mcp_server(mcp_json_data)
            mcp_json_changed = True
            _act("merged strata into", mcp_json)

        if mcp_json_changed and not diff_mode:
            mcp_json.write_text(json.dumps(mcp_json_data, indent=2) + "\n", encoding="utf-8")

        # Step 7b: additively merge the freshness hooks.Stop entry (issue #112)
        # into .claude/settings.json — that location IS correct for hooks. A
        # user's own Stop hooks — and every other settings key — are preserved;
        # the Strata group is appended only when absent.
        if not settings_unreadable:
            if _stop_hook_present(settings_data):
                _act("skip Stop hook in", settings_json, skipped=True)
            else:
                merge_stop_hook(settings_data)
                settings_changed = True
                _act("merged Stop hook into", settings_json)

        # One write for the settings.json changes (legacy-entry removal and/or
        # the Stop hook merge) — so bootstrap-venv (step 8) reads the
        # up-to-date file back.
        if settings_changed and not diff_mode:
            (project_root / ".claude").mkdir(parents=True, exist_ok=True)
            settings_json.write_text(json.dumps(settings_data, indent=2) + "\n", encoding="utf-8")

    def _register_codex() -> None:
        # ---------------------------------------------------------------
        # Step 6/7 (codex): merge the [mcp_servers.strata] table and the
        # freshness hooks.Stop block into Codex's own config.toml — never
        # into .claude/settings.json (out of scope for this harness; a
        # broken .claude/settings.json in the same repo must not block a
        # codex-only registration — see test_register_codex.py).
        #
        # Only claims what docs/marketing/CODEX-surface-2026-08.md marks
        # [verified]: the MCP table shape and location are verified
        # hands-on against codex-cli 0.149.0; the Stop-hook block is
        # schema-verified only (accepted by `codex exec --strict-config`)
        # — live firing and STRATA_AGENT_* env inheritance are pending
        # live verification, and the merged block says so on its face.
        # ---------------------------------------------------------------
        # codex_config lives under $CODEX_HOME (default ~/.codex), NOT under
        # project_root — unlike every other artifact register touches, so it
        # cannot go through the project-relative _act() helper above
        # (Path.relative_to would raise). Render its lines directly, against
        # the absolute path, via the same render_action_line used everywhere
        # else for consistent [would create/update]/kept-user's wording.
        codex_config = _codex_config_path()
        existing_codex_text = (
            codex_config.read_text(encoding="utf-8") if codex_config.exists() else ""
        )

        def _act_codex(action: str, *, skipped: bool) -> None:
            print(render_action_line(action, codex_config, diff_mode=diff_mode, skipped=skipped))

        mcp_already = _codex_mcp_present(existing_codex_text)
        merged_text, mcp_added = _merge_codex_mcp_server(existing_codex_text)
        _act_codex("merged strata into" if mcp_added else "skip", skipped=not mcp_added)
        if mcp_already and not mcp_added:
            print(
                "    (an [mcp_servers.strata] table already exists in Codex's config — "
                "left untouched)"
            )

        hook_already = _codex_hook_present(merged_text)
        merged_text, hook_added = _merge_codex_freshness_hook(merged_text)
        _act_codex(
            "merged freshness Stop hook into" if hook_added else "skip Stop hook in",
            skipped=not hook_added,
        )
        if hook_already and not hook_added:
            print("    (a Strata Stop-hook block already exists in Codex's config — left as-is)")

        if not diff_mode and (mcp_added or hook_added):
            codex_config.parent.mkdir(parents=True, exist_ok=True)
            codex_config.write_text(merged_text, encoding="utf-8")

        # Codex has no skills mechanism (unlike Claude Code's
        # .claude/skills/) — seed the same memory-move guidance into the
        # project's own AGENTS.md instead, additively (Task 6, harness
        # parity).
        agents_md = project_root / "AGENTS.md"
        # read_bytes, not read_text: text-mode I/O strips \r via universal
        # newline translation before it ever reaches the CRLF-preserving
        # merge logic (final fix wave, item 2).
        existing_agents_text = agents_md.read_bytes().decode("utf-8") if agents_md.exists() else ""
        if _agents_md_present(existing_agents_text):
            # The marker's already there — this is a self-update check, not
            # a fresh merge. Block-level (see classify_agents_md_drift):
            # only the fenced <!-- strata:begin -->...<!-- strata:end -->
            # block is compared/replaced, so user content outside the fence
            # is never touched.
            updated_text, agents_status = _self_update_agents_md_block(existing_agents_text)
            if agents_status == "match":
                _act("skip", agents_md, skipped=True)
            else:
                _report_self_update(agents_md, agents_status)
                if agents_status == "stale" and not diff_mode:
                    agents_md.write_bytes(updated_text.encode("utf-8"))
        else:
            new_agents_text, agents_added = _merge_agents_md(existing_agents_text)
            _act("merged strata into", agents_md, skipped=False)
            if not diff_mode and agents_added:
                agents_md.write_bytes(new_agents_text.encode("utf-8"))

        single_scope, first_scope = _seeded_binding_hint()
        if single_scope:
            # Single-scope auto-bind (operator directive): the engine
            # auto-binds an empty STRATA_AGENT_SCOPE to the fleet's only
            # scope, so Codex's env values can stay exactly as seeded
            # (empty) — nothing to fill in for a fresh, single-scope fleet.
            print(
                f"\n  Codex config: the env values under [mcp_servers.strata.env] in "
                f"{codex_config}\n"
                f"  can stay empty — the fleet has one scope ({first_scope!r}) and the "
                "engine auto-binds to\n"
                "  it. Fill them in only once the fleet grows past one scope. The "
                "Stop-hook block is schema-\n"
                '  verified only; see README "Using Strata with Codex CLI" for exactly '
                "what is and isn't proven to work."
            )
        else:
            print(
                "\n  Codex config: fill in STRATA_AGENT_SCOPE / STRATA_AGENT_SKILL / "
                "STRATA_AGENT_SESSION_ID under\n"
                f"  [mcp_servers.strata.env] in {codex_config} before running `codex` "
                "(MCP env values are literal\n"
                "  TOML strings — Codex does not interpolate them). The Stop-hook block "
                "is schema-verified only;\n"
                '  see README "Using Strata with Codex CLI" for exactly what is and '
                "isn't proven to work."
            )

    # -----------------------------------------------------------------------
    # Per-harness wiring loop (Task 2, multi-harness parity): runs the
    # extracted wiring block for every resolved harness, each announced with
    # a `== NAME ==` header so --diff / register output stays legible when
    # more than one harness is wired in a single run.
    # -----------------------------------------------------------------------
    for _harness in resolved_harnesses:
        print(f"\n== {_harness} ==")
        if _harness == "claude-code":
            _register_claude_code()
        elif _harness == "codex":
            _register_codex()

    # -----------------------------------------------------------------------
    # Step 8: bootstrap-venv (if requested; Claude-Code-only — updates
    # .claude/settings.json to point at the venv's strata-mcp). Codex has no
    # equivalent step (its config.toml `command` is resolved on PATH like
    # Claude Code's default, not pointed at a project-local venv), so make
    # the skip explicit rather than silently doing nothing.
    # -----------------------------------------------------------------------
    if bootstrap_venv and "codex" in resolved_harnesses:
        # Worded to stay accurate on a both-harness machine: --bootstrap-venv
        # as a whole is NOT skipped there — it still runs, just below, for
        # claude-code. Only the codex-specific step (there is no
        # codex-harness venv equivalent) is skipped (final fix wave, item 3:
        # the old "--bootstrap-venv skipped" phrasing read as if the whole
        # flag were a no-op even while the claude-code venv was created).
        print(
            "\n  --bootstrap-venv: codex has no venv-wiring equivalent yet — skipping "
            "that step for codex only (not the rest of --bootstrap-venv). It only "
            "wires .mcp.json for\n"
            "  Claude Code; install strata normally (pipx/pip) so `strata-mcp` and "
            "`strata` are on PATH for\n"
            "  the Codex config register wrote."
        )
    if bootstrap_venv and "claude-code" in resolved_harnesses:
        venv_dir = strata_dir / ".venv"
        venv_strata_mcp = venv_dir / "bin" / "strata-mcp"
        if diff_mode:
            print("  [would create] .strata/.venv/ and pip install strata into it")
        else:
            if venv_dir.exists():
                print("  .strata/.venv/ already exists — skipping venv creation")
            else:
                # Python discovery (ADR 0005 Decision 7).
                #
                # `python -m venv` itself requires a Python ≥ 3.11 interpreter.
                # Strata's own runtime is already ≥ 3.11 (per pyproject.toml's
                # requires-python), so sys.executable is the right default.
                # --python is the escape hatch for the rare case where the user
                # wants to seed the venv with a different interpreter than the
                # one running register.
                python_arg: str | None = getattr(args, "python", None)
                venv_python = python_arg if python_arg else sys.executable

                install_spec = _self_install_spec()
                if install_spec is None:
                    print(
                        f"  {_glyph('fail')} cannot determine a safe install source for "
                        f"strata: this process was not\n"
                        "    installed from a local path or VCS URL (PEP 610 direct_url.json "
                        "not found for the\n"
                        f"    `{DISTRIBUTION_NAME}` distribution). Install strata into\n"
                        f"    .strata/.venv/ manually (e.g. `pip install {DISTRIBUTION_NAME}`), "
                        "or re-run --bootstrap-venv\n"
                        "    from a path or VCS install (editable install, git clone, etc).",
                        file=sys.stderr,
                    )
                    return 1

                print(f"  creating .strata/.venv/ using {venv_python} ...")
                # Use the chosen Python to drive `venv` if it isn't us.
                if venv_python == sys.executable:
                    venv.create(str(venv_dir), with_pip=True, clear=False)
                else:
                    subprocess.check_call(
                        [venv_python, "-m", "venv", str(venv_dir)],
                    )
                pip = venv_dir / "bin" / "pip"
                subprocess.check_call(
                    [str(pip), "install", "--quiet", install_spec],
                )
                print("  installed strata into .strata/.venv/")

            if not venv_strata_mcp.exists():
                print(
                    f"  {_glyph('fail')} .strata/.venv/ exists but bin/strata-mcp is missing "
                    "— the venv looks half-built\n"
                    "    (interrupted install?). Remove .strata/.venv/ and re-run "
                    "`strata register --bootstrap-venv`.",
                    file=sys.stderr,
                )
                return 1

            # Update .mcp.json to point at the venv binary — that's the file
            # Claude Code actually reads for project-scoped MCP servers, same
            # as the plain-register merge above. Runs on every bootstrap-venv
            # invocation (not only when the venv was just created) so an
            # earlier interrupted run can be repaired by re-running. Merge,
            # never replace: a user-customised env block on the strata entry
            # is preserved.
            if mcp_json_unreadable:
                print(
                    f"  {_glyph('fail')} skipping .mcp.json venv update — fix the JSON "
                    "first (see above).",
                    file=sys.stderr,
                )
            else:
                mcp_json = project_root / ".mcp.json"
                mcp_json_data_venv: dict
                if mcp_json.exists():
                    try:
                        mcp_json_data_venv = json.loads(mcp_json.read_text(encoding="utf-8"))
                    except json.JSONDecodeError as exc:
                        print(
                            f"  {_glyph('fail')} .mcp.json is not valid JSON "
                            f"({exc}) — fix it, then re-run.",
                            file=sys.stderr,
                        )
                        return 1
                else:
                    mcp_json_data_venv = {}
                mcp_venv = mcp_json_data_venv.get("mcpServers", {})
                existing_entry = mcp_venv.get("strata")
                preserved_env = (
                    existing_entry.get("env", {}) if isinstance(existing_entry, dict) else {}
                )
                mcp_venv["strata"] = {
                    "command": str(venv_strata_mcp),
                    "env": preserved_env,
                }
                mcp_json_data_venv["mcpServers"] = mcp_venv
                mcp_json.write_text(
                    json.dumps(mcp_json_data_venv, indent=2) + "\n", encoding="utf-8"
                )
                print(f"  updated .mcp.json to use {venv_strata_mcp}")

    # -----------------------------------------------------------------------
    # Print next steps.
    # -----------------------------------------------------------------------
    if not diff_mode:
        single_scope, first_scope = _seeded_binding_hint()

        print()
        print("Done. Next steps:")
        print(f"  1. Edit {fleet_yaml.relative_to(project_root)} for your team's structure")
        print()
        if single_scope:
            # Single-scope auto-bind (operator directive): a fresh install
            # must work with minimum friction — the seeded fleet has one
            # scope, so binding needs no export at all.
            print(
                f"  2. You're ready — open your harness; this session binds to "
                f"{first_scope!r} automatically."
            )
            print("     Export STRATA_AGENT_SCOPE only when the fleet grows past one scope.")
        else:
            print("  2. Bind your session — every agent works as one scope of the fleet:")
            print(f"       export STRATA_AGENT_SCOPE={first_scope}")
            print("       export STRATA_AGENT_SKILL=<your-skill>  # optional")
            print("     or run `strata launch` to be prompted interactively")
        print()
        next_step = 3
        if "claude-code" in resolved_harnesses:
            print(f"  {next_step}. Open Claude Code in this directory: claude")
            next_step += 1
        if "codex" in resolved_harnesses:
            if single_scope:
                print(
                    f"  {next_step}. Open Codex CLI in this directory: codex "
                    f"(env values in {_codex_config_path()} can stay empty for now)"
                )
            else:
                print(
                    f"  {next_step}. Fill in the env values in {_codex_config_path()}, "
                    "then open Codex CLI in this directory: codex"
                )
            next_step += 1

    # -----------------------------------------------------------------------
    # Step 9: offer to capture the judge key. Only at the very end of a
    # *successful* interactive register — --diff makes no writes, and a
    # settings.json merge failure below means this run didn't fully
    # succeed, so neither is "the end of a successful register".
    # -----------------------------------------------------------------------
    if not diff_mode and not settings_unreadable and not mcp_json_unreadable:
        print()
        _offer_judge_key_capture(project_root, skip_prompt=getattr(args, "yes", False))

    if settings_unreadable or mcp_json_unreadable:
        broken_paths = (
            (".claude/settings.json", settings_unreadable),
            (".mcp.json", mcp_json_unreadable),
        )
        broken = ", ".join(p for p, bad in broken_paths if bad)
        print(
            f"\nCompleted with 1 problem: {broken} could not be merged (invalid JSON — see above).",
            file=sys.stderr,
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# strata unregister — reverse the brownfield wiring (issue #53)
# ---------------------------------------------------------------------------
#
# Undoes exactly what `strata register` wired, honouring ADR 0005 Decision 6
# ("never delete or override user state") in reverse: every artifact is
# removed ONLY when it still byte-matches what register would have written.
# Anything the user has since edited is reported and left in place, and the
# run exits 1 so scripts can detect the partial case.
#
#   1. `.gitignore` managed block  — removed verbatim, other lines byte-stable.
#   2. `mcpServers.strata` entry    — removed from `.mcp.json` only if
#                                     identical to _MCP_ENTRY; a legacy
#                                     `.claude/settings.json` copy is cleaned
#                                     up the same way if still present.
#   3. the three vendored skills     — removed only if byte-identical to the
#                                     currently-shipped src/strata/_skills copy.
#   4. `.strata/` data               — memory, not wiring: left alone unless
#                                     --purge-data is passed.
#
# --dry-run prints every action with the same _glyph format and touches
# nothing.  Running against an already-clean project reports "nothing to do"
# per item and exits 0 (idempotent).


def _wired_harnesses(project_root: Path) -> list[str]:
    """Return every harness in :data:`install.KNOWN_HARNESSES` order that is
    currently WIRED in *project_root* — i.e. its markers are present in the
    files ``strata register`` writes, not merely installed on this machine.

    Shared by ``strata unregister``'s default harness resolution (Task 3) and
    ``strata launch``'s "exactly one harness wired" fallback (Task 5) — one
    definition of "wired" only.

    Codex is machine-scoped (``$CODEX_HOME/config.toml`` is shared by every
    project on the box), so its config alone is not project evidence: codex
    counts as WIRED here only when the machine config tables are present
    *and* this project's ``AGENTS.md`` carries the strata marker block
    (controller ruling, harness-parity final fix wave — see
    ``_codex_wired`` below).
    """
    import json  # noqa: PLC0415

    def _claude_code_wired() -> bool:
        mcp_json = project_root / ".mcp.json"
        if mcp_json.exists():
            try:
                mcp_data = json.loads(mcp_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # Corrupt JSON is not proof there's nothing wired — treat it as
                # conservatively "possibly wired".
                return True
            if not isinstance(mcp_data, dict):
                # Valid JSON but not an object (`[]`, `null`, ...) — same
                # "possibly wired" conservatism as corrupt JSON; the actual
                # unregister step below reports this properly instead of
                # crashing.
                return True
            if _mcp_server_present(mcp_data):
                return True

        settings_json = project_root / ".claude" / "settings.json"
        if not settings_json.exists():
            return False
        try:
            data = json.loads(settings_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Corrupt JSON is not proof there's nothing wired — treat it as
            # conservatively "possibly wired".
            return True
        if not isinstance(data, dict):
            # Valid JSON but not an object (`[]`, `null`, ...) — same
            # "possibly wired" conservatism as corrupt JSON; the actual
            # unregister step below reports this properly instead of
            # crashing.
            return True
        # `_mcp_server_present` here also catches a not-yet-migrated legacy
        # entry (earlier releases wrote mcpServers.strata into settings.json).
        return _mcp_server_present(data) or _stop_hook_present(data)

    def _codex_wired() -> bool:
        # Codex "wiredness" must be project-scoped, not machine-scoped
        # (controller ruling, harness-parity final fix wave): config.toml
        # lives under $CODEX_HOME — a machine-level file shared by every
        # project on the box — so its presence alone is not evidence THIS
        # project registered codex. Require the project-local AGENTS.md
        # marker block too, the project-side evidence register wrote here.
        # This gate applies only to the no-flags default resolution shared
        # by unregister and launch's single-wired fallback; an explicit
        # `--harness codex` bypasses _wired_harnesses entirely and keeps
        # its unconditional-cleanup behavior (Task 3).
        codex_config = _codex_config_path()
        if not codex_config.exists():
            return False
        text = codex_config.read_text(encoding="utf-8")
        if not (_codex_mcp_present(text) or _codex_hook_present(text)):
            return False
        agents_md = project_root / "AGENTS.md"
        if not agents_md.exists():
            return False
        return _agents_md_present(agents_md.read_text(encoding="utf-8"))

    checks = {"claude-code": _claude_code_wired, "codex": _codex_wired}
    return [h for h in install.KNOWN_HARNESSES if checks[h]()]


def cmd_unregister(args: argparse.Namespace) -> int:
    """Reverse `strata register`'s wiring — issue #53; harness-symmetric (Task 3).

    Removes each artifact register wired ONLY when it still byte-matches what
    the CURRENT or a known HISTORICAL release of register would have written
    (round-4 unregister fix, bug B — the same current-or-historical mechanism
    register's own self-update uses, so a project registered under an older
    Strata release is never misreported as "edited" just because a newer
    release ships different shipped content); genuinely user-edited
    artifacts are reported and left in place (ADR 0005 Decision 6, applied in
    reverse). Steps:

    1. `.gitignore` managed block     (removed verbatim, other lines untouched).
    2. per resolved harness: claude-code's `mcpServers.strata` entry + Stop
       hook, or codex's `[mcp_servers.strata]` table + Stop-hook block
       (each removed only if == the canonical entry it was merged with; for
       codex, removing the canonical `[mcp_servers.strata]` table also sweeps
       up any `[mcp_servers.strata.*]` subtables a third party — most notably
       the Codex CLI itself, writing per-tool approval state — appended
       after it, which are meaningless without the parent and otherwise
       orphan the entry and break Codex startup — round-4 unregister fix,
       bug A).
    3. the three vendored skills        (claude-code only; removed only if
       byte-identical to the current or a historical shipped version).
    4. `.strata/` data                  (left alone unless --purge-data).

    Harness resolution (symmetric with, but not identical to, register's):
    explicit `--harness` flags -> exactly those; no flags -> every harness
    whose wiring markers are currently present (what's WIRED, not what's
    installed/detected). A harness named explicitly but not wired prints a
    skip line and does not fail the run.

    --dry-run prints every action and touches nothing. Idempotent: an
    already-clean project reports "nothing to do" per item and exits 0.

    Exit code: 0 on success (including nothing-to-do); 1 when something the
    user asked to remove was left in place because it had been edited, so
    scripts can detect the partial case.
    """
    import json  # noqa: PLC0415

    path_arg: str | None = getattr(args, "path", None)
    project_root = Path(path_arg).resolve() if path_arg else Path.cwd().resolve()
    dry_run: bool = getattr(args, "dry_run", False)
    purge_data: bool = getattr(args, "purge_data", False)

    # -----------------------------------------------------------------------
    # Resolve which harness(es) to reverse (Task 3, multi-harness parity).
    # Unlike register (which defaults to everything *detected*), unregister
    # defaults to everything *wired* — what register's markers show is
    # actually present in this project, not what's installed on the machine.
    # Explicit --harness flags always win outright, even naming a harness
    # with nothing wired (that just prints a skip line below).
    # -----------------------------------------------------------------------
    harness_flags: list[str] | None = getattr(args, "harness", None)
    if harness_flags:
        resolved_harnesses: list[str] = list(harness_flags)
    else:
        resolved_harnesses = _wired_harnesses(project_root)

    # Tracks whether any artifact the user asked to remove was left in place
    # (edited/modified) — drives the exit-1 partial-completion signal.
    left_in_place = False

    def _ok(message: str) -> None:
        print(f"  {_glyph('pass')} {message}")

    def _left(message: str) -> None:
        nonlocal left_in_place
        left_in_place = True
        print(f"  {_glyph('warn')} {message}", file=sys.stderr)

    def _would(present: str, past: str) -> str:
        return f"would {present}" if dry_run else past

    header = "strata unregister --dry-run  (no writes)" if dry_run else "strata unregister"
    print(f"{header}\nProject root: {project_root}")
    print()

    # -----------------------------------------------------------------------
    # Step 1: `.gitignore` managed block.
    # -----------------------------------------------------------------------
    gitignore = project_root / ".gitignore"
    if not gitignore.exists():
        _ok(".gitignore: nothing to do (no .gitignore)")
    else:
        original = gitignore.read_text(encoding="utf-8")
        new_text, status = _remove_gitignore_block(original)
        if status == "removed":
            if new_text.strip() == "":
                # The file is now empty. Register creates `.gitignore` when it
                # is absent, but that origin is not detectable from content
                # alone, so we leave the (now-empty) file rather than risk
                # deleting a file the user created. (design item 1)
                if not dry_run:
                    gitignore.write_text(new_text, encoding="utf-8")
                _ok(
                    f".gitignore: {_would('remove', 'removed')} managed Strata block "
                    "(file now empty — left in place; register's authorship is not detectable)"
                )
            else:
                if not dry_run:
                    gitignore.write_text(new_text, encoding="utf-8")
                _ok(f".gitignore: {_would('remove', 'removed')} managed Strata block")
        elif status == "edited":
            _left(
                ".gitignore: managed Strata block was edited — left in place "
                "(remove it by hand if you meant to)"
            )
        else:  # absent
            _ok(".gitignore: nothing to do (no managed Strata block)")

    def _unregister_claude_code() -> None:
        # -----------------------------------------------------------------------
        # Step 2a: `.mcp.json` — the `mcpServers.strata` entry. That file is
        # what Claude Code actually reads for project-scoped MCP servers, so
        # this — not `.claude/settings.json` — is where register's live
        # entry lives now. Removed only when it still byte-matches what
        # register wrote.
        # -----------------------------------------------------------------------
        mcp_json = project_root / ".mcp.json"
        if not mcp_json.exists():
            _ok(".mcp.json: nothing to do (no .mcp.json)")
        else:
            mcp_json_data: dict | None
            try:
                loaded_mcp_json = json.loads(mcp_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                _left(f".mcp.json: not valid JSON ({exc}) — left untouched (fix it, then re-run)")
                mcp_json_data = None
            else:
                if isinstance(loaded_mcp_json, dict):
                    mcp_json_data = loaded_mcp_json
                else:
                    _left(
                        f".mcp.json: must be a JSON object, got "
                        f"{type(loaded_mcp_json).__name__} — left untouched (fix it, then "
                        "re-run)"
                    )
                    mcp_json_data = None

            if mcp_json_data is not None:
                mcp_json_changed = False
                mcp_servers = mcp_json_data.get("mcpServers")
                entry = mcp_servers.get("strata") if isinstance(mcp_servers, dict) else None
                removable = isinstance(entry, dict) and (
                    entry in (_MCP_ENTRY, *_MCP_ENTRY_HISTORICAL)
                    or _is_bootstrap_venv_shape_mcp_entry(entry, project_root)
                )
                if entry is None:
                    _ok(".mcp.json: nothing to do (no mcpServers.strata entry)")
                elif removable:
                    del mcp_servers["strata"]
                    if not mcp_servers:
                        del mcp_json_data["mcpServers"]
                    mcp_json_changed = True
                    _ok(f".mcp.json: {_would('remove', 'removed')} mcpServers.strata entry")
                else:
                    _left(
                        ".mcp.json: mcpServers.strata entry was edited "
                        "(differs from the canonical entry) — left in place"
                    )

                if mcp_json_changed:
                    if not mcp_json_data:
                        # register only ever creates `.mcp.json` to carry this
                        # one entry, so an empty result has no standalone
                        # reason to exist — delete it for a clean round-trip
                        # (unlike settings.json/.gitignore, where the file
                        # predates register far more often and an emptied
                        # file's authorship isn't detectable).
                        if not dry_run:
                            mcp_json.unlink()
                        _ok(f".mcp.json: {_would('remove', 'removed')} (empty after removal)")
                    elif not dry_run:
                        mcp_json.write_text(
                            json.dumps(mcp_json_data, indent=2) + "\n", encoding="utf-8"
                        )

        # -----------------------------------------------------------------------
        # Step 2b: `.claude/settings.json` — a legacy `mcpServers.strata`
        # entry (what a pre-fix `strata register` wrote there; Claude Code
        # never reads mcpServers from settings.json) and the freshness
        # `hooks.Stop` entry (issue #112). Both removed only when they still
        # byte-match what register wrote; one write persists both removals.
        # -----------------------------------------------------------------------
        settings_json = project_root / ".claude" / "settings.json"
        if not settings_json.exists():
            _ok(".claude/settings.json: nothing to do (no settings.json)")
        else:
            settings_data: dict | None
            try:
                loaded_settings_json = json.loads(settings_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                _left(
                    f".claude/settings.json: not valid JSON ({exc}) — left untouched "
                    "(fix it, then re-run)"
                )
                settings_data = None
            else:
                if isinstance(loaded_settings_json, dict):
                    settings_data = loaded_settings_json
                else:
                    _left(
                        f".claude/settings.json: must be a JSON object, got "
                        f"{type(loaded_settings_json).__name__} — left untouched (fix it, "
                        "then re-run)"
                    )
                    settings_data = None

            if settings_data is not None:
                settings_changed = False

                # Legacy mcpServers.strata entry (pre-fix `strata register`).
                legacy_mcp_servers = settings_data.get("mcpServers")
                legacy_entry = (
                    legacy_mcp_servers.get("strata")
                    if isinstance(legacy_mcp_servers, dict)
                    else None
                )
                legacy_removable = isinstance(legacy_entry, dict) and (
                    legacy_entry in (_MCP_ENTRY, *_MCP_ENTRY_HISTORICAL)
                    or _is_bootstrap_venv_shape_mcp_entry(legacy_entry, project_root)
                )
                if legacy_entry is None:
                    _ok(".claude/settings.json: nothing to do (no legacy mcpServers.strata entry)")
                elif legacy_removable:
                    del legacy_mcp_servers["strata"]
                    # Register creates the mcpServers block when absent; if strata
                    # was its only key, drop the now-empty block so a project that
                    # had no mcpServers before register round-trips byte-for-byte.
                    if not legacy_mcp_servers:
                        del settings_data["mcpServers"]
                    settings_changed = True
                    _ok(
                        f".claude/settings.json: {_would('remove', 'removed')} legacy "
                        "mcpServers.strata entry"
                    )
                else:
                    _left(
                        ".claude/settings.json: legacy mcpServers.strata entry was edited "
                        "(differs from the canonical entry) — left in place"
                    )

                # Freshness hooks.Stop entry (issue #112) — same byte-identity rule;
                # remove_stop_hook drops emptied Stop/hooks containers so a project
                # with no hooks before register round-trips byte-for-byte.
                hook_status = _remove_stop_hook(settings_data)
                if hook_status == "removed":
                    settings_changed = True
                    _ok(f".claude/settings.json: {_would('remove', 'removed')} freshness Stop hook")
                elif hook_status == "edited":
                    _left(
                        ".claude/settings.json: freshness Stop hook was edited "
                        "(differs from the canonical entry) — left in place"
                    )
                else:  # absent
                    _ok(".claude/settings.json: nothing to do (no freshness Stop hook)")

                if settings_changed and not dry_run:
                    settings_json.write_text(
                        json.dumps(settings_data, indent=2) + "\n", encoding="utf-8"
                    )
                if settings_changed and not settings_data:
                    # The file is now an empty object. Register creates settings.json
                    # when absent, but that origin is not detectable from content, so
                    # the empty file is left rather than risk deleting one the user
                    # created — mirroring the empty-.gitignore treatment.
                    _ok(
                        ".claude/settings.json: now an empty object — left in place "
                        "(register's authorship is not detectable)"
                    )

        # -----------------------------------------------------------------------
        # Step 3: the three vendored skills.
        # -----------------------------------------------------------------------
        claude_skills_dir = project_root / ".claude" / "skills"
        for skill_name in ["strata", "strata-worker", "strata-inspect"]:
            skill_dir = claude_skills_dir / skill_name
            skill_md = skill_dir / "Skill.md"
            if not skill_dir.exists():
                _ok(f"skill {skill_name}: nothing to do (not installed)")
                continue
            match = _skill_matches_shipped(skill_md, skill_name) if skill_md.exists() else False
            if match is True:
                if not dry_run:
                    skill_md.unlink()
                    # Remove the skill directory only if register's Skill.md was
                    # its sole content; never delete other files the user added.
                    _rmdir_if_empty(skill_dir)
                _ok(f"skill {skill_name}: {_would('remove', 'removed')} (matched shipped version)")
            elif match is None:
                _left(
                    f"skill {skill_name}: could not read the shipped reference to compare "
                    "— left in place"
                )
            else:  # False — differs
                _left(
                    f"skill {skill_name}: modified or from an older Strata version "
                    "(differs from shipped) — left in place"
                )

        # -----------------------------------------------------------------------
        # Step 3b: the vendored freshness Stop-hook script (issue #112).
        # -----------------------------------------------------------------------
        claude_hooks_dir = project_root / ".claude" / "hooks"
        hook_script = claude_hooks_dir / _HOOK_SCRIPT_NAME
        if not hook_script.exists():
            _ok(f"hook {_HOOK_SCRIPT_NAME}: nothing to do (not installed)")
        else:
            hook_match = _hook_matches_shipped(hook_script)
            if hook_match is True:
                if not dry_run:
                    hook_script.unlink()
                _ok(
                    f"hook {_HOOK_SCRIPT_NAME}: {_would('remove', 'removed')} "
                    "(matched shipped version)"
                )
            elif hook_match is None:
                _left(
                    f"hook {_HOOK_SCRIPT_NAME}: could not read the shipped reference to compare "
                    "— left in place"
                )
            else:  # False — differs
                _left(
                    f"hook {_HOOK_SCRIPT_NAME}: modified or from an older Strata version "
                    "(differs from shipped) — left in place"
                )

        # Tidy up register-created empty parent dirs so a clean unregister restores
        # the tree exactly. Only ever removes directories that are already empty.
        if not dry_run:
            _rmdir_if_empty(claude_skills_dir)
            _rmdir_if_empty(claude_hooks_dir)
            _rmdir_if_empty(project_root / ".claude")

    def _unregister_codex() -> None:
        # ---------------------------------------------------------------
        # Step 2 (codex): reverse of `strata register --harness codex` —
        # the [mcp_servers.strata] table and freshness hooks.Stop block in
        # $CODEX_HOME/config.toml, each removed only when it still
        # byte-matches what register wrote (same rule as the Claude-Code
        # settings.json entries above). .claude/settings.json is untouched
        # by this harness, by design (see cmd_register).
        # ---------------------------------------------------------------
        codex_config = _codex_config_path()
        if not codex_config.exists():
            _ok(f"{codex_config}: nothing to do (no Codex config.toml)")
        else:
            codex_text = codex_config.read_text(encoding="utf-8")
            codex_changed = False

            new_text, mcp_status = _remove_codex_mcp_server(codex_text)
            if mcp_status == "removed":
                codex_text = new_text
                codex_changed = True
                # Sweep up any [mcp_servers.strata.*] subtables a third party
                # (most notably the Codex CLI itself, writing per-tool
                # approval state) appended after the canonical block — those
                # are meaningless without the parent table we just confirmed
                # is ours, and left behind they orphan the strata MCP server
                # entry and break Codex startup (round-4 unregister fix, bug
                # A). Only swept when the parent itself matched — a manual,
                # unmarked [mcp_servers.strata] entry (and its subtables) is
                # never touched, because mcp_status is "absent" for that case.
                codex_text, orphaned = _strip_orphaned_mcp_strata_tables(codex_text)
                if orphaned:
                    _ok(
                        f"{codex_config}: {_would('remove', 'removed')} [mcp_servers.strata] "
                        f"and {orphaned} related table{'s' if orphaned != 1 else ''}"
                    )
                else:
                    _ok(f"{codex_config}: {_would('remove', 'removed')} [mcp_servers.strata] table")
            elif mcp_status == "edited":
                _left(
                    f"{codex_config}: [mcp_servers.strata] table was edited "
                    "(differs from the canonical entry) — left in place"
                )
            else:  # absent
                _ok(f"{codex_config}: nothing to do (no [mcp_servers.strata] table)")

            new_text, hook_status = _remove_codex_freshness_hook(codex_text)
            if hook_status == "removed":
                codex_text = new_text
                codex_changed = True
                _ok(f"{codex_config}: {_would('remove', 'removed')} freshness Stop-hook block")
            elif hook_status == "edited":
                _left(
                    f"{codex_config}: freshness Stop-hook block was edited "
                    "(differs from the canonical entry) — left in place"
                )
            else:  # absent
                _ok(f"{codex_config}: nothing to do (no freshness Stop-hook block)")

            if codex_changed and not dry_run:
                codex_config.write_text(codex_text, encoding="utf-8")

        # Reverse of the AGENTS.md seed in `strata register --harness codex`
        # (Task 6, harness parity). AGENTS.md lives at the project root, not
        # under $CODEX_HOME, unlike config.toml above.
        agents_md = project_root / "AGENTS.md"
        if not agents_md.exists():
            _ok(f"{agents_md}: nothing to do (no AGENTS.md)")
        else:
            # read_bytes/write_bytes, not read_text/write_text — see the
            # matching comment in _register_codex (final fix wave, item 2).
            agents_text = agents_md.read_bytes().decode("utf-8")
            new_agents_text, agents_status = _remove_agents_md(agents_text)
            if agents_status == "removed":
                _ok(f"{agents_md}: {_would('remove', 'removed')} the Strata block")
                if not dry_run:
                    agents_md.write_bytes(new_agents_text.encode("utf-8"))
            elif agents_status == "edited":
                _left(
                    f"{agents_md}: Strata block was edited "
                    "(differs from the canonical block) — left in place"
                )
            else:  # absent
                _ok(f"{agents_md}: nothing to do (no Strata block)")

    # -----------------------------------------------------------------------
    # Per-harness reversal loop (Task 3, multi-harness parity): mirrors
    # register's per-harness loop, one `== NAME ==` header per resolved
    # harness. Every resolved harness's helper runs unconditionally — the
    # marker gate (_WIRED_CHECK, above) only decides *resolution* on the
    # no-flags path (which harnesses to include at all); it is deliberately
    # NOT re-checked here. An explicitly-named harness must still be
    # reversible even when its settings.json/config.toml entries were
    # hand-deleted but its skills/hook script remain — the per-harness
    # helpers already print their own per-artifact "nothing to do" lines,
    # which is what satisfies a named-but-unwired harness's "skip line".
    # -----------------------------------------------------------------------
    _UNREGISTER_STEP = {
        "claude-code": _unregister_claude_code,
        "codex": _unregister_codex,
    }
    for _harness in resolved_harnesses:
        print(f"\n== {_harness} ==")
        _UNREGISTER_STEP[_harness]()

    # -----------------------------------------------------------------------
    # Step 4: `.strata/` data — memory, not wiring.
    # -----------------------------------------------------------------------
    strata_dir = project_root / ".strata"
    if not strata_dir.exists():
        _ok(".strata/: nothing to do (no workspace)")
    elif purge_data:
        if not dry_run:
            shutil.rmtree(strata_dir)
        verb = _would("purge", "purged")
        _ok(f".strata/: {verb} project memory (config, fleet.yaml, DB, summaries)")
    else:
        _ok(
            ".strata/: left in place — memory, not wiring "
            "(config, fleet.yaml, DB, summaries; pass --purge-data to remove)"
        )

    print()
    if left_in_place:
        print(
            "Completed with items left in place (edited artifacts were not removed — see above). "
            "Exit code 1.",
            file=sys.stderr,
        )
        return 1
    print("Done." if not dry_run else "Dry run complete — nothing was changed.")
    return 0


def _rmdir_if_empty(directory: Path) -> None:
    """Remove *directory* only when it exists and is empty. Never touches files."""
    try:
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# strata set-default-harness (Task 4) — records what `strata launch` starts.
# ---------------------------------------------------------------------------


def cmd_set_default_harness(args: argparse.Namespace) -> int:
    """Write ``default_harness`` under ``[launch]`` in ``.strata/config.toml``.

    Validates *args.harness_name* against :data:`install.KNOWN_HARNESSES`
    (exit 2 + the valid list otherwise) and requires a registered workspace
    (exit 1 + "run 'strata register' first" otherwise). The write is a
    textual read-modify-write (:func:`install.set_default_harness`): every
    other line in ``config.toml`` — including a pre-existing ``[launch]``
    table's other keys — survives byte-for-byte; a re-run replaces the value
    in place rather than duplicating the table.
    """
    name: str = args.harness_name
    if name not in install.KNOWN_HARNESSES:
        print(
            f"Unknown harness {name!r}. Valid harnesses: {', '.join(install.KNOWN_HARNESSES)}",
            file=sys.stderr,
        )
        return 2

    path_arg: str | None = getattr(args, "path", None)
    project_root = Path(path_arg).resolve() if path_arg else Path.cwd().resolve()
    config_path = project_root / ".strata" / "config.toml"
    if not config_path.exists():
        print(
            f"No .strata/config.toml found at {project_root} — run 'strata register' first.",
            file=sys.stderr,
        )
        return 1

    # read_bytes/write_bytes, not read_text/write_text: text-mode I/O does
    # universal-newline translation, which strips \r out of a CRLF file
    # before set_default_harness's CRLF-aware regexes ever see it — silently
    # neutering the CRLF preservation those regexes are built for (final fix
    # wave, item 2).
    current_text = config_path.read_bytes().decode("utf-8")
    new_text = install.set_default_harness(current_text, name)
    config_path.write_bytes(new_text.encode("utf-8"))

    print(f"default harness: {name} (used by 'strata launch')")
    return 0


# ---------------------------------------------------------------------------
# strata freshness-hook / freshness-evaluator — the turn-boundary contribution
# Stop-hook and its detached background evaluator (issue #112, WP3). Hidden
# subcommands: they are the engine the installed `.claude/hooks/strata-stop-hook`
# wrapper and its spawned child invoke, not commands a user runs by hand.
# ---------------------------------------------------------------------------


def cmd_freshness_hook(args: argparse.Namespace) -> int:
    """Run the Stop hook from Claude Code's stdin payload (issue #112).

    Reads the hook JSON on stdin, consults the session's #110 asymmetry
    counters, and either spawns a detached background evaluator (default) or
    emits the strict-mode block JSON on stdout. Always exits 0 — a broken hook
    must never break the user's session — so the return code is fixed at 0 and
    any strict-mode decision rides on stdout.
    """
    from strata.freshness import run_stop_hook  # noqa: PLC0415

    stdin_text = sys.stdin.read() if not sys.stdin.isatty() else ""
    run_stop_hook(stdin_text, out=sys.stdout)
    return 0


def cmd_freshness_evaluator(args: argparse.Namespace) -> int:
    """Run the detached background evaluator for one stale session (issue #112).

    Reads the session transcript tail, drafts a possible contribution via the
    evaluator model, and either submits it through the judged contribute path or
    records a mechanical decline — resetting the session's asymmetry counters
    either way. Spawned detached by the Stop hook; always exits 0.
    """
    from strata.freshness import run_evaluator  # noqa: PLC0415

    run_evaluator(session_id=args.session_id, transcript_path=args.transcript_path)
    return 0


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strata",
        description="Strata — shared memory for agent fleets.",
    )
    parser.add_argument("--version", action="version", version=f"strata {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_start = sub.add_parser("start", help="Migrate, auto-seed fleet.yaml, and run the backend.")
    p_start.add_argument("--host", default="127.0.0.1")
    p_start.add_argument("--port", type=int, default=8000)
    p_start.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload.")
    p_start.add_argument("--db", help=f"DB path (default: {_default_hint(_db_path_default)}).")
    p_start.add_argument(
        "--skip-upgrade-check",
        action="store_true",
        help=(
            "Bypass the V1→V1.2 upgrade guard. Use only after you have already "
            "run `strata export-fleet`, or on a fresh install."
        ),
    )
    p_start.add_argument(
        "--skip-preflight",
        action="store_true",
        dest="skip_preflight",
        help="Bypass all preflight prerequisite checks.",
    )
    p_start.set_defaults(func=cmd_start)

    p_migrate = sub.add_parser("migrate", help="Apply pending SQLite migrations.")
    p_migrate.add_argument("--db", help=f"DB path (default: {_default_hint(_db_path_default)}).")
    p_migrate.set_defaults(func=cmd_migrate)

    p_bootstrap = sub.add_parser("bootstrap", help="Validate fleet.yaml (no DB writes).")
    p_bootstrap.add_argument(
        "--config", help=f"Config path (default: {_default_hint(_fleet_config_default)})."
    )
    p_bootstrap.add_argument("--db", help="Ignored (kept for backward compatibility).")
    p_bootstrap.set_defaults(func=cmd_bootstrap)

    p_scopes = sub.add_parser("scopes", help="List the fleet's active scopes.")
    p_scopes.set_defaults(func=cmd_scopes)

    p_summary = sub.add_parser("summary", help="Print a scope's curated summary.")
    p_summary.add_argument("scope_id")
    p_summary.set_defaults(func=cmd_summary)

    p_record = sub.add_parser(
        "record", help="Print a page of a scope's record (contributions + judgments)."
    )
    p_record.add_argument("scope_id")
    p_record.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Contributions per page (default: STRATA_RECORD_PAGE_SIZE).",
    )
    p_record.add_argument(
        "--before",
        default=None,
        help="Cursor: show contributions older than this contribution id.",
    )
    p_record.set_defaults(func=cmd_record)

    p_status = sub.add_parser(
        "status",
        help="Show per-scope memory-freshness (read-vs-contribute staleness).",
    )
    p_status.add_argument(
        "--window-days",
        type=int,
        default=None,
        dest="window_days",
        help="Recency window in days for the staleness metric (default: 30).",
    )
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser(
        "doctor",
        help="Diagnose a project's Strata wiring: config, DB, fleet, install, binding.",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    # -------------------------------------------------------------------
    # strata operator — ADR 0008 D1's local entry surface: publish/supersede/
    # retire operator memory, or (via a 'c_' id) correct a scope's native
    # memory in person.
    # -------------------------------------------------------------------
    global _operator_parser
    p_operator = sub.add_parser(
        "operator",
        help=(
            "Operator stratum: publish/supersede/retire operator memory, or "
            "correct a scope's native memory in person."
        ),
    )
    _operator_parser = p_operator
    p_operator.set_defaults(func=cmd_operator_root)
    operator_sub = p_operator.add_subparsers(dest="operator_command", metavar="<operator-command>")

    p_op_publish = operator_sub.add_parser(
        "publish", help="Publish a new operator memory item attached at a scope."
    )
    p_op_publish.add_argument("scope_id")
    p_op_publish.add_argument(
        "--kind", choices=["directive", "context"], required=True, help="Operator memory kind."
    )
    p_op_publish.add_argument("--content", required=True, help="Verbatim operator memory text.")
    p_op_publish.add_argument("--subject", default=None, help="Optional short subject line.")
    p_op_publish.set_defaults(func=cmd_operator_publish)

    p_op_supersede = operator_sub.add_parser(
        "supersede",
        help=(
            "Supersede an operator item ('op_' id) or a scope's native "
            "directive ('c_' id) in person."
        ),
    )
    p_op_supersede.add_argument("scope_id")
    p_op_supersede.add_argument(
        "id", help="An 'op_...' operator item id or a 'c_...' directive id."
    )
    p_op_supersede.add_argument("--content", required=True, help="Verbatim replacement content.")
    p_op_supersede.add_argument("--subject", default=None, help="Optional short subject line.")
    p_op_supersede.set_defaults(func=cmd_operator_supersede)

    p_op_retire = operator_sub.add_parser(
        "retire",
        help=(
            "Retire an operator item ('op_' id) or a scope's native directive "
            "('c_' id) in person, without replacement."
        ),
    )
    p_op_retire.add_argument("scope_id")
    p_op_retire.add_argument("id", help="An 'op_...' operator item id or a 'c_...' directive id.")
    p_op_retire.add_argument("--reason", default=None, help="Optional free-text rationale.")
    p_op_retire.set_defaults(func=cmd_operator_retire)

    p_op_show = operator_sub.add_parser(
        "show", help="Print operator memory verbatim, plus the health signal."
    )
    p_op_show.add_argument(
        "scope_id",
        nargs="?",
        default=None,
        help="Attachment scope to show. Omit to show every attachment scope + totals.",
    )
    p_op_show.set_defaults(func=cmd_operator_show)

    # -------------------------------------------------------------------
    # strata publication — ADR 0007's local entry surface: show a scope's
    # (or every scope's) publication artifact verbatim, or bootstrap a
    # scope's initial publication (ADR 0007 D4).
    # -------------------------------------------------------------------
    global _publication_parser
    p_publication = sub.add_parser(
        "publication",
        help=("Show a scope's publication artifact, or bootstrap its initial publication."),
    )
    _publication_parser = p_publication
    p_publication.set_defaults(func=cmd_publication_root)
    publication_sub = p_publication.add_subparsers(
        dest="publication_command", metavar="<publication-command>"
    )

    p_pub_show = publication_sub.add_parser(
        "show",
        help="Print a scope's publication artifact verbatim (or every scope that publishes).",
    )
    p_pub_show.add_argument(
        "scope_id",
        nargs="?",
        default=None,
        help="Scope whose publication to show. Omit to show every scope that publishes.",
    )
    p_pub_show.set_defaults(func=cmd_publication_show)

    p_pub_bootstrap = publication_sub.add_parser(
        "bootstrap",
        help="Bootstrap a scope's initial publication from its current summary.",
    )
    p_pub_bootstrap.add_argument("scope_id")
    p_pub_bootstrap.set_defaults(func=cmd_publication_bootstrap)

    p_launch = sub.add_parser(
        "launch",
        help="Resolve scope/skill binding and exec claude with STRATA_AGENT_* set.",
    )
    p_launch.add_argument(
        "scope_id",
        nargs="?",
        help="Target scope ID. Omit to use .strata-role or interactive picker.",
    )
    p_launch.add_argument(
        "--skill",
        help="Override resolved skill (must be in permitted_skills when that list is set).",
    )
    p_launch.add_argument(
        "--session",
        help="Override auto-generated session ID.",
    )
    p_launch.add_argument(
        "--skip-refresh",
        action="store_true",
        dest="skip_refresh",
        help=(
            "Skip the pre-session manager-refresh step. "
            "Use when the API key is unavailable or for debugging."
        ),
    )
    p_launch.add_argument(
        "--skip-preflight",
        action="store_true",
        dest="skip_preflight",
        help="Bypass all preflight prerequisite checks.",
    )
    p_launch.add_argument(
        "--harness",
        dest="harness",
        choices=install.KNOWN_HARNESSES,
        default=None,
        help=(
            "Harness to start. Default: the project's recorded default "
            "(`strata set-default-harness`); else the single harness wired in "
            "this project, if exactly one is; else claude-code. 'codex' "
            "currently exits 1 — Codex launch is schema-verified but not "
            "live-verified (see README 'Using Strata with Codex CLI')."
        ),
    )
    p_launch.set_defaults(func=cmd_launch)

    p_export = sub.add_parser(
        "export-fleet",
        help="Export V1 fleet tables to fleet.yaml for V1.2 upgrade.",
    )
    p_export.add_argument("--db", help=f"V1 DB path (default: {_default_hint(_db_path_default)}).")
    p_export.add_argument(
        "--out",
        help=f"Output fleet.yaml path (default: {_default_hint(_fleet_config_default)}).",
    )
    p_export.add_argument(
        "--force",
        action="store_true",
        help="Overwrite --out if it already exists.",
    )
    p_export.set_defaults(func=cmd_export_fleet)

    p_register = sub.add_parser(
        "register",
        help=(
            "Idempotent brownfield installer — create .strata/config.toml, "
            "seed fleet.yaml, copy skills, merge MCP entry."
        ),
    )
    p_register.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Project root directory (default: current working directory).",
    )
    p_register.add_argument(
        "--yes",
        dest="yes",
        action="store_true",
        help=(
            "Skip the confirmation prompt when the directory has no project marker "
            "(.git, pyproject.toml, package.json, Cargo.toml, go.mod) and register anyway. "
            "Required for scripts/CI registering a markerless directory on purpose; "
            "has no effect when a marker is already present."
        ),
    )
    p_register.add_argument(
        "--diff",
        action="store_true",
        help=(
            "Read-only mode: show what would differ between current state and canonical "
            "without writing anything. Useful after `pipx upgrade strata`."
        ),
    )
    p_register.add_argument(
        "--bootstrap-venv",
        action="store_true",
        dest="bootstrap_venv",
        help=(
            "Create .strata/.venv/ with strata installed and update "
            ".mcp.json to use the absolute venv path. "
            "Use when pipx is not available or strata-mcp is not on PATH."
        ),
    )
    p_register.add_argument(
        "--python",
        dest="python",
        default=None,
        help=(
            "Path to a Python ≥ 3.11 interpreter used to seed the bootstrap venv. "
            "Only relevant with --bootstrap-venv. Defaults to the running "
            "interpreter when it is itself ≥ 3.11; otherwise the user must "
            "supply this flag explicitly. Strata cannot create a 3.11 venv from a "
            "3.10 interpreter."
        ),
    )
    p_register.add_argument(
        "--harness",
        dest="harness",
        action="append",
        choices=install.KNOWN_HARNESSES,
        default=None,
        help=(
            "Harness to wire (repeatable). Default: every harness detected on "
            "this machine (claude-code and/or codex); if none are detected, "
            "claude-code is wired anyway with a one-line notice. 'codex' merges "
            "Strata's [mcp_servers.strata] table and freshness Stop-hook block "
            "into the Codex CLI's own config.toml instead of "
            ".mcp.json; the Stop-hook wiring is schema-verified "
            "only (see README 'Using Strata with Codex CLI')."
        ),
    )
    p_register.set_defaults(func=cmd_register)

    p_unregister = sub.add_parser(
        "unregister",
        help=(
            "Reverse `strata register` — remove the .gitignore block, the "
            "mcpServers.strata entry, and the vendored skills (only when "
            "unmodified). Leaves .strata/ data unless --purge-data."
        ),
        description=(
            "Reverse the wiring `strata register` added, honouring the "
            "strict-additive contract in reverse: each artifact is removed only "
            "when it still byte-matches what register wrote. Artifacts you have "
            "since edited (the .gitignore block, the mcpServers.strata entry, a "
            "vendored skill) are reported and left in place. Your project's "
            "memory under .strata/ (fleet.yaml, DB, summaries, config.toml) is "
            "left untouched unless you pass --purge-data. With no --harness flag, "
            "every harness that is actually wired is reversed (see --harness); "
            "a harness named explicitly but not wired just prints a skip line. "
            "Exit code: 0 on success including nothing-to-do; 1 when something "
            "you asked to remove was left in place because it had been edited, "
            "so scripts can detect the partial case."
        ),
    )
    p_unregister.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Project root directory (default: current working directory).",
    )
    p_unregister.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Show every action in the same format without writing anything.",
    )
    p_unregister.add_argument(
        "--purge-data",
        action="store_true",
        dest="purge_data",
        help=(
            "Also delete the .strata/ workspace (fleet.yaml, DB, summaries, "
            "config.toml). Off by default — that data is memory, not wiring. "
            "Combine with --dry-run to preview what would be purged."
        ),
    )
    p_unregister.add_argument(
        "--harness",
        dest="harness",
        action="append",
        choices=install.KNOWN_HARNESSES,
        default=None,
        help=(
            "Harness to reverse (repeatable). Default: every harness that is "
            "actually WIRED in this project (not every harness installed on "
            "this machine — see `strata register`'s default) — a harness "
            "named explicitly but not wired just prints a skip line. 'codex' "
            "removes the [mcp_servers.strata] table and freshness Stop-hook "
            "block from the Codex CLI's config.toml instead of "
            ".mcp.json — only when they still byte-match what "
            "`strata register --harness codex` wrote."
        ),
    )
    p_unregister.set_defaults(func=cmd_unregister)

    p_set_default_harness = sub.add_parser(
        "set-default-harness",
        help="Record which harness `strata launch` starts by default.",
        description=(
            "Write default_harness under [launch] in .strata/config.toml, so "
            "`strata launch` knows which harness to start without a --harness "
            "flag every time. Requires a registered workspace "
            "(`strata register` first)."
        ),
    )
    p_set_default_harness.add_argument(
        "harness_name",
        metavar="NAME",
        help=f"Harness to make the default. One of: {', '.join(install.KNOWN_HARNESSES)}.",
    )
    p_set_default_harness.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Project root directory (default: current working directory).",
    )
    p_set_default_harness.set_defaults(func=cmd_set_default_harness)

    # -------------------------------------------------------------------
    # Hidden engine subcommands for the freshness Stop-hook (issue #112).
    # help=SUPPRESS: invoked by the installed hook wrapper and its spawned
    # evaluator, not by users. Kept on the `strata` CLI so PATH resolution
    # matches the installed engine (`strata-mcp`) and there is one shipped
    # implementation for every installer to point at.
    # -------------------------------------------------------------------
    p_fresh_hook = sub.add_parser("freshness-hook", help=argparse.SUPPRESS)
    p_fresh_hook.set_defaults(func=cmd_freshness_hook)

    p_fresh_eval = sub.add_parser("freshness-evaluator", help=argparse.SUPPRESS)
    p_fresh_eval.add_argument("--session-id", dest="session_id", required=True)
    p_fresh_eval.add_argument("--transcript-path", dest="transcript_path", required=True)
    p_fresh_eval.set_defaults(func=cmd_freshness_evaluator)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
