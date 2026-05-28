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
* ``strata launch``    — validate scope, resolve skill, and exec ``claude``
                        with STRATA_AGENT_* env vars set (ADR 0003).

The inspection commands (``scopes``, ``summary``, ``record``, ``launch``)
talk to a running backend over HTTP — start the backend first with
``strata start`` in another terminal. All other commands work directly
against the DB on disk.

Vocabulary throughout follows ``CONTEXT.md``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

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

# Path to the bundled starter templates directory.
_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"
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

    # 1. Migrate.
    applied = run_migrations(db_path)
    if applied:
        print(f"Applied {len(applied)} migration(s).")

    # 2. Auto-seed fleet.yaml if absent.
    fleet_path = Path(_fleet_config_default())
    if not fleet_path.exists() and _DEFAULT_TEMPLATE.exists():
        shutil.copy(_DEFAULT_TEMPLATE, fleet_path)
        print("seeded fleet.yaml from the default template; edit to suit")

    # 3. Serve.
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


def cmd_launch(args: argparse.Namespace) -> int:
    """Validate scope, resolve skill, and exec ``claude`` with STRATA_AGENT_* set.

    Steps (per ADR 0003):
    1. Fetch active scopes from GET /scopes; fail fast if backend unreachable.
    2. Determine target scope: positional arg > .strata-role discovery > picker.
    3. Resolve skill from scope declaration (ADR 0002 resolution table).
    4. Build session ID (auto-generated or --session override).
    5. execvp("claude", ...) with STRATA_AGENT_* env vars.
    """
    import httpx

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
    p_launch.set_defaults(func=cmd_launch)

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
