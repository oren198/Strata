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

The inspection commands (``scopes``, ``summary``, ``record``, ``launch``)
talk to a running backend over HTTP — start the backend first with
``strata start`` in another terminal. All other commands work directly
against the DB on disk.

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

from strata import __version__
from strata.launch import (
    SkillResolutionError,
    StrataRoleParseError,
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


def _backend_url() -> str:
    return os.environ.get("STRATA_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")


def _db_path_default() -> str:
    return os.environ.get("STRATA_DB_PATH", "./strata.db")


def _fleet_config_default() -> str:
    """Resolve the canonical fleet config path through Settings.

    Reads ``STRATA_FLEET_CONFIG`` (or the ``./fleet.yaml`` default) via the
    same :class:`Settings` the backend uses, so the CLI and the running app
    never diverge on which file is canonical (ADR 0002).
    """
    from strata.settings import get_settings

    return get_settings().fleet_yaml_path


def _resolve_fleet_config(explicit: str | None) -> str | None:
    """Pick the config path: explicit arg → Settings path → fleet.example.yaml."""
    if explicit:
        return explicit
    settings_path = _fleet_config_default()
    if Path(settings_path).exists():
        return settings_path
    if Path("fleet.example.yaml").exists():
        return "fleet.example.yaml"
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
            print(f"  ✓ {check.name}: {check.message}")
        elif check.kind == "soft":
            print(f"  ⚠ {check.name}: {check.message}", file=sys.stderr)
        else:
            print(f"  ✗ {check.name}: {check.message}", file=sys.stderr)
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

    No DB writes are made.  The command validates all 8 load-time invariants
    from ADR 0002 and reports success or the first error encountered.
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

    db_path = args.db or _db_path_default()
    fleet_yaml_path = _fleet_config_default()

    # 0. Preflight — prerequisite hygiene checks before any DB or server work.
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

    # 4. Auto-seed fleet.yaml if absent.
    fleet_path = Path(_fleet_config_default())
    if not fleet_path.exists() and _DEFAULT_TEMPLATE.exists():
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
    """List the fleet's active scopes (via the backend API)."""
    import httpx

    url = f"{_backend_url()}/scopes"
    try:
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"Backend error: {e}", file=sys.stderr)
        return 2
    data = resp.json()

    print(f"Strata ({len(data['strata'])}):")
    for s in data["strata"]:
        print(f"  [{s['ordinal']}] {s['id']:6s}  {s['name']}")
    print()
    print(f"Scopes ({len(data['scopes'])}):")
    for sc in data["scopes"]:
        print(f"  {sc['id']:12s}  stratum={sc['stratum_id']:4s}  {sc['name']}")
    print()
    print(f"Edges ({len(data['edges'])}):")
    for e in data["edges"]:
        print(f"  {e['from_scope_id']:12s} → {e['to_scope_id']}")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    """Print a scope's curated summary as markdown."""
    import httpx

    url = f"{_backend_url()}/scopes/{args.scope_id}/summary"
    try:
        resp = httpx.get(url, timeout=10)
    except httpx.HTTPError as e:
        print(f"Backend error: {e}", file=sys.stderr)
        return 2
    if resp.status_code == 404:
        print(f"Scope not found: {args.scope_id}", file=sys.stderr)
        return 1
    resp.raise_for_status()
    summary = resp.json()

    print(f"# Scope: {summary['scope_id']}")
    print(f"_updated_at: {summary['updated_at']}_")
    print()
    print("## Directives")
    if not summary["directives"]:
        print("_(none yet)_")
    for d in summary["directives"]:
        print()
        print(f"### [{d['id']}] {d['content']}")
        if d.get("subject"):
            print(f"- subject: {d['subject']}")
        print(
            f"- source: scope={d['source_scope_id']} · "
            f"skill={d['source_skill']} · at={d['created_at']}"
        )
    print()
    print("## Context")
    print(summary["context"] or "_(none yet)_")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Print a scope's record (all contributions + their judgments)."""
    import httpx

    url = f"{_backend_url()}/scopes/{args.scope_id}/record"
    try:
        resp = httpx.get(url, timeout=10)
    except httpx.HTTPError as e:
        print(f"Backend error: {e}", file=sys.stderr)
        return 2
    if resp.status_code == 404:
        print(f"Scope not found: {args.scope_id}", file=sys.stderr)
        return 1
    resp.raise_for_status()
    data = resp.json()

    print(f"Scope: {args.scope_id}")
    print(f"Contributions: {len(data['contributions'])}")
    print(f"Judgments:     {len(data['judgments'])}")
    print()
    judgments_by_contrib = {j["contribution_id"]: j for j in data["judgments"]}
    for c in data["contributions"]:
        verdict = judgments_by_contrib.get(c["id"], {}).get("decision", "(pending)")
        print(f"  · {c['id']}  [{c['proposed_classification']:9s} → {verdict}]")
        contributor = c["contributor"]
        print(f"      by {contributor['skill']}@{contributor['scope_id']} at {contributor['ts']}")
        if c.get("subject"):
            print(f"      subject: {c['subject']}")
        if c.get("supersedes"):
            print(f"      supersedes: {c['supersedes']}")
        # Indent multi-line content.
        for line in c["content"].splitlines():
            print(f"      | {line}")
        print()
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
    _visited: set[str] | None = None,
) -> None:
    """Refresh the summary for *scope_id* via one scope-manager LLM call.

    Recursively refreshes stale ancestors first (root-first order).  Uses a
    ``_visited`` set to guard against cycles (which validation prevents, but
    this is defensive).

    ADR 0004 Decision 4 — last-write-wins; no lock.
    """
    from datetime import UTC, datetime

    from strata.record_store import Contribution, ContributorRef

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
                _visited=_visited,
            )

        # Re-read parent summary after potential refresh
        parent_summary = summary_store.read(parent_scope.id)
    else:
        parent_summary = None

    # Now refresh this scope
    current_summary = summary_store.read(scope_id)
    recent_contributions = record_store.list_contributions(scope_id=scope_id, limit=20)

    # Build a synthetic "refresh" contribution that requests a summary rewrite.
    # We use a ContributorRef representing the manager itself.
    ts = datetime.now(tz=UTC).isoformat()
    refresh_contribution = Contribution(
        id=f"refresh_{scope_id}_{ts}",
        scope_id=scope_id,
        content=(
            "[Manager refresh triggered by strata launch"
            " — rewrite summary incorporating current state.]"
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
        created_at=ts,
    )

    print(f"  [refresh] refreshing scope {scope_id!r}...", file=sys.stderr)
    judgment = manager.judge(
        scope=scope,
        stratum=stratum,
        parent_summary=parent_summary,
        current_summary=current_summary,
        recent_contributions=recent_contributions,
        new_contribution=refresh_contribution,
        summary_max_words=summary_max_words,
    )

    if judgment.new_summary is not None:
        # Stamp the parent_version before writing
        parent_ver = parent_summary.version if parent_summary is not None else None
        to_write = judgment.new_summary.model_copy(update={"parent_version": parent_ver})
        summary_store.write(scope_id, to_write)
        print(f"  [refresh] scope {scope_id!r} summary updated", file=sys.stderr)


def _run_manager_refresh(scope_id: str, *, skip: bool = False) -> None:
    """Run the pre-session manager-refresh step for *scope_id*.

    Walks the inter-stratum ancestor chain (root-first), refreshes any stale
    ancestors, then refreshes *scope_id* itself.  Skipped when:

    - ``skip`` is True (``--skip-refresh`` flag).
    - No ``ANTHROPIC_API_KEY`` is available (soft — prints a warning).
    - Any ancestor/scope is missing from the fleet config (non-fatal warning).

    ADR 0004 Decision 4 — last-write-wins, no lock.
    """
    import anthropic

    from strata.fleet_config import FleetConfig
    from strata.record_store import RecordStore
    from strata.scope_manager import ScopeManager
    from strata.settings import get_settings
    from strata.summary_store import SummaryStore

    if skip:
        return

    settings = get_settings()

    if not settings.anthropic_api_key:
        print(
            "  [refresh] ANTHROPIC_API_KEY not set — skipping manager refresh",
            file=sys.stderr,
        )
        return

    fleet_yaml = _fleet_config_default()
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

    db_path = settings.db_path
    summaries_dir = settings.summaries_dir

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
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
                _visited=visited,
            )


def cmd_launch(args: argparse.Namespace) -> int:
    """Validate scope, resolve skill, and exec ``claude`` with STRATA_AGENT_* set.

    Steps (per ADR 0003 + ADR 0004 D4):
    0. Preflight — prerequisite hygiene checks.
    1. Fetch active scopes from GET /scopes; fail fast if backend unreachable.
    2. Determine target scope: positional arg > .strata-role discovery > picker.
    3. Resolve skill from scope declaration (ADR 0002 resolution table).
    4. Build session ID (auto-generated or --session override).
    4a. Manager-refresh step (ADR 0004 D4): refresh stale ancestor summaries,
        then refresh the scope itself.  Skipped with --skip-refresh.
    5. execvp("claude", ...) with STRATA_AGENT_* env vars.
    """
    import httpx

    # -----------------------------------------------------------------------
    # Step 0: Preflight.
    # -----------------------------------------------------------------------
    if not args.skip_preflight:
        rc = _run_preflight(run_launch_preflight())
        if rc != 0:
            return rc

    interactive = is_interactive()

    # -----------------------------------------------------------------------
    # Step 1: Fetch active scopes from the backend.
    # -----------------------------------------------------------------------
    url = f"{_backend_url()}/scopes"
    try:
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(
            f"Cannot reach backend ({e}). Start the backend with: strata start",
            file=sys.stderr,
        )
        return 1

    data = resp.json()
    active_scopes: list[dict] = data.get("scopes", [])
    valid_ids = [sc["id"] for sc in active_scopes]

    # -----------------------------------------------------------------------
    # Step 2: Determine target scope.
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
        else:
            # No positional arg, no .strata-role — need interactive picker or fail.
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
    # Step 3: Resolve skill.
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
    # Step 4: Build session ID.
    # -----------------------------------------------------------------------
    session_id: str = args.session if args.session else make_session_id(scope_data["id"], skill)

    # -----------------------------------------------------------------------
    # Step 4a: Manager-refresh (ADR 0004 D4) — before execvp.
    # -----------------------------------------------------------------------
    _run_manager_refresh(
        scope_data["id"],
        skip=getattr(args, "skip_refresh", False),
    )

    # -----------------------------------------------------------------------
    # Step 5: exec claude.
    # -----------------------------------------------------------------------
    env = os.environ.copy()
    env["STRATA_AGENT_SCOPE"] = scope_data["id"]
    env["STRATA_AGENT_SKILL"] = skill
    env["STRATA_AGENT_SESSION_ID"] = session_id

    claude_bin = "claude"
    try:
        os.execvpe(claude_bin, [claude_bin], env)
    except FileNotFoundError:
        print(
            "Cannot find 'claude' on PATH. Install Claude Code and ensure it is on your PATH.",
            file=sys.stderr,
        )
        return 1
    # execvpe does not return on success; the line below is unreachable.
    return 0  # pragma: no cover


# ---------------------------------------------------------------------------
# strata register — brownfield install helper (ADR 0005 Decision 4)
# ---------------------------------------------------------------------------

#: Project root marker files — at least one must be present.
_PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")

#: Gitignore block appended by strata register (idempotent — detected by header).
_GITIGNORE_MARKER = "# Strata"
_GITIGNORE_BLOCK = """\
# Strata — managed by `strata register` — do not remove this line
.strata/.venv/
.strata/strata.db*
.strata/summaries/
# fleet.yaml is intentionally NOT listed above — commit it (it is your team's org chart).
"""

#: Minimal settings.json merge entry.
_MCP_ENTRY: dict = {"command": "strata-mcp", "env": {}}


def _is_v1_2_shape_mcp_entry(entry: dict) -> bool:
    """Return True if *entry* matches a known-stale V1.2 mcpServer shape.

    V1.2 settings shipped:
      command: python
      args: ["-m", "mcp_server.strata_mcp"]
      env: { "STRATA_BACKEND_URL": "...", ... }

    All three of those break on V1.3:
    - mcp_server is no longer a top-level module (folded into strata.mcp).
    - STRATA_BACKEND_URL is no longer consumed (embedded mode, ADR 0004 D1).

    We only need to recognise *any* of these signals to warn.
    """
    if entry.get("command") == "python":
        args = entry.get("args") or []
        if isinstance(args, list) and "-m" in args:
            tail = args[args.index("-m") + 1 :]
            if tail and "mcp_server" in tail[0]:
                return True
    env = entry.get("env") or {}
    return isinstance(env, dict) and "STRATA_BACKEND_URL" in env


#: Default .strata/config.toml content.
_CONFIG_TOML = """\
# Strata per-project configuration — managed by `strata register`.
# Paths are relative to this project's root.
db = ".strata/strata.db"
fleet_yaml = ".strata/fleet.yaml"
summaries_dir = ".strata/summaries"
"""


def cmd_register(args: argparse.Namespace) -> int:
    """Idempotent brownfield installer — per ADR 0005 Decision 4.

    Walks the registration steps in order:
    1. Detect project root (require a project marker).
    2. Create .strata/ directory.
    3. Write .strata/config.toml (skip if exists).
    4. Update .gitignore (idempotent block with # Strata marker).
    5. Seed .strata/fleet.yaml from templates/minimal.yaml (skip if exists).
    6. Copy strata skills to .claude/skills/ (skip each if exists).
    7. Merge strata into .claude/settings.json mcpServers (skip if exists).
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
    # Step 1: Require a project marker.
    # -----------------------------------------------------------------------
    if not any((project_root / m).exists() for m in _PROJECT_MARKERS):
        markers_str = ", ".join(_PROJECT_MARKERS)
        print(
            f"Not a project root — register from a directory containing one of: {markers_str}\n"
            f"(checked: {project_root})",
            file=sys.stderr,
        )
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
    def _act(action: str, path: str | Path, *, skipped: bool = False) -> None:
        rel = Path(path).relative_to(project_root) if Path(path).is_absolute() else Path(path)
        if diff_mode:
            if skipped:
                print(f"  [unchanged]  {rel}")
            else:
                print(f"  [would create/update]  {rel}")
        else:
            if skipped:
                print(f"  kept user's {rel}")
            else:
                print(f"  {action}: {rel}")

    if diff_mode:
        print(f"strata register --diff  (dry-run, no writes)\nProject root: {project_root}")
    else:
        print(f"strata register\nProject root: {project_root}")

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

    # -----------------------------------------------------------------------
    # Step 6: Copy canonical skills to .claude/skills/ (skip each if exists).
    # -----------------------------------------------------------------------
    claude_skills_dir = project_root / ".claude" / "skills"
    skills_root = importlib.resources.files("strata") / "_skills"
    for skill_name in ["strata", "strata-worker", "strata-inspect"]:
        dest_skill_dir = claude_skills_dir / skill_name
        if dest_skill_dir.exists():
            _act("skip", dest_skill_dir, skipped=True)
        else:
            skill_src = skills_root / skill_name / "Skill.md"
            if not diff_mode:
                dest_skill_dir.mkdir(parents=True, exist_ok=True)
                dest_skill_md = dest_skill_dir / "Skill.md"
                dest_skill_md.write_text(skill_src.read_text(encoding="utf-8"), encoding="utf-8")
            _act("copied", dest_skill_dir)

    # -----------------------------------------------------------------------
    # Step 7: Merge strata into .claude/settings.json mcpServers block.
    # -----------------------------------------------------------------------
    settings_json = project_root / ".claude" / "settings.json"
    if settings_json.exists():
        try:
            settings_data: dict = json.loads(settings_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(
                f"Warning: .claude/settings.json is not valid JSON ({exc}). Skipping MCP merge.",
                file=sys.stderr,
            )
            settings_data = {}
    else:
        settings_data = {}

    mcp_servers: dict = settings_data.get("mcpServers", {})
    if "strata" in mcp_servers:
        _act("skip", settings_json, skipped=True)
        # Stale-shape detection (ADR 0005 Decision 6): warn if the existing
        # strata mcpServer entry is V1.2-shape (broken on V1.3). Keeps the
        # strict-additive contract — we never overwrite — but surfaces the
        # upgrade-path issue at register time, when the user is in fix-mind.
        existing = mcp_servers["strata"]
        if isinstance(existing, dict) and _is_v1_2_shape_mcp_entry(existing):
            print(
                "  ⚠ WARNING: your existing strata mcpServer entry is V1.2-shape and will silently",
                file=sys.stderr,
            )
            print(
                "    fail on V1.3 (the `mcp_server` Python module no longer exists; "
                "`STRATA_BACKEND_URL` is unused).",
                file=sys.stderr,
            )
            print(
                f"    The canonical V1.3 entry is: {json.dumps(_MCP_ENTRY)}",
                file=sys.stderr,
            )
            print(
                "    Strata never overwrites your settings — run `strata register --diff` to "
                "see the canonical,",
                file=sys.stderr,
            )
            print(
                "    then update .claude/settings.json by hand.",
                file=sys.stderr,
            )
    else:
        mcp_servers["strata"] = _MCP_ENTRY
        settings_data["mcpServers"] = mcp_servers
        if not diff_mode:
            (project_root / ".claude").mkdir(parents=True, exist_ok=True)
            settings_json.write_text(json.dumps(settings_data, indent=2) + "\n", encoding="utf-8")
        _act("merged strata into", settings_json)

    # -----------------------------------------------------------------------
    # Step 8: bootstrap-venv (if requested).
    # -----------------------------------------------------------------------
    if bootstrap_venv:
        venv_dir = strata_dir / ".venv"
        venv_strata_mcp = venv_dir / "bin" / "strata-mcp"
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

            if not diff_mode:
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
                    [str(pip), "install", "--quiet", "strata[cc-plugin]"],
                )
                print("  installed strata into .strata/.venv/")

                # Update settings.json to use absolute path.
                # (Outer `if not diff_mode:` at L1048 guarantees we're in
                # write-mode here — no inner check needed.)
                settings_data_venv: dict
                if settings_json.exists():
                    settings_data_venv = json.loads(settings_json.read_text(encoding="utf-8"))
                else:
                    settings_data_venv = {}
                mcp_venv = settings_data_venv.get("mcpServers", {})
                mcp_venv["strata"] = {
                    "command": str(venv_strata_mcp),
                    "env": {},
                }
                settings_data_venv["mcpServers"] = mcp_venv
                (project_root / ".claude").mkdir(parents=True, exist_ok=True)
                settings_json.write_text(
                    json.dumps(settings_data_venv, indent=2) + "\n", encoding="utf-8"
                )
                print(f"  updated .claude/settings.json to use {venv_strata_mcp}")
            else:
                print("  [would create] .strata/.venv/ and pip install strata")

    # -----------------------------------------------------------------------
    # Print next steps.
    # -----------------------------------------------------------------------
    if not diff_mode:
        # Determine the first scope ID from the seeded fleet.yaml.
        first_scope = "g_root"
        if fleet_yaml.exists():
            try:
                import yaml  # noqa: PLC0415

                fleet_data = yaml.safe_load(fleet_yaml.read_text(encoding="utf-8"))
                scopes = fleet_data.get("scopes", [])
                if scopes:
                    first_scope = scopes[0].get("id", "g_root")
            except Exception:  # noqa: BLE001
                pass

        print()
        print("Done. Next steps:")
        print(f"  1. Edit {fleet_yaml.relative_to(project_root)} for your team's structure")
        print(f"  2. export STRATA_AGENT_SCOPE={first_scope}")
        print("     export STRATA_AGENT_SKILL=<your-skill>")
        print("  3. Open Claude Code in this directory: claude")

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
    p_start.add_argument("--db", help=f"DB path (default: {_db_path_default()}).")
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
    p_migrate.add_argument("--db", help=f"DB path (default: {_db_path_default()}).")
    p_migrate.set_defaults(func=cmd_migrate)

    p_bootstrap = sub.add_parser("bootstrap", help="Validate fleet.yaml (no DB writes).")
    p_bootstrap.add_argument("--config", help=f"Config path (default: {_fleet_config_default()}).")
    p_bootstrap.add_argument("--db", help="Ignored (kept for backward compatibility).")
    p_bootstrap.set_defaults(func=cmd_bootstrap)

    p_scopes = sub.add_parser("scopes", help="List the fleet's active scopes.")
    p_scopes.set_defaults(func=cmd_scopes)

    p_summary = sub.add_parser("summary", help="Print a scope's curated summary.")
    p_summary.add_argument("scope_id")
    p_summary.set_defaults(func=cmd_summary)

    p_record = sub.add_parser("record", help="Print a scope's record (contributions + judgments).")
    p_record.add_argument("scope_id")
    p_record.set_defaults(func=cmd_record)

    p_launch = sub.add_parser(
        "launch",
        help="Resolve scope/skill binding and exec claude with STRATA_AGENT_* set (ADR 0003).",
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
            "Skip the pre-session manager-refresh step (ADR 0004 D4). "
            "Use when the API key is unavailable or for debugging."
        ),
    )
    p_launch.add_argument(
        "--skip-preflight",
        action="store_true",
        dest="skip_preflight",
        help="Bypass all preflight prerequisite checks.",
    )
    p_launch.set_defaults(func=cmd_launch)

    p_export = sub.add_parser(
        "export-fleet",
        help="Export V1 fleet tables to fleet.yaml for V1.2 upgrade.",
    )
    p_export.add_argument("--db", help=f"V1 DB path (default: {_db_path_default()}).")
    p_export.add_argument(
        "--out",
        help=f"Output fleet.yaml path (default: {_fleet_config_default()}).",
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
            "seed fleet.yaml, copy skills, merge MCP entry (ADR 0005)."
        ),
    )
    p_register.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Project root directory (default: current working directory).",
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
            ".claude/settings.json to use the absolute venv path. "
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
    p_register.set_defaults(func=cmd_register)

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
