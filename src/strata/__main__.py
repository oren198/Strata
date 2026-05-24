"""Entry point for ``strata`` and ``python -m strata``.

A small CLI that wraps the backend's common operations into a single
runnable. Subcommands:

* ``strata start``     — apply migrations, bootstrap fleet if empty,
                        run the FastAPI app via uvicorn.
* ``strata migrate``   — apply SQLite schema migrations.
* ``strata bootstrap`` — apply a YAML fleet config to the DB.
* ``strata scopes``    — terminal-friendly listing of the fleet.
* ``strata summary``   — print a scope's curated summary.
* ``strata record``    — print a scope's record (contributions + judgments).

The inspection commands (``scopes``, ``summary``, ``record``) talk to a
running backend over HTTP — start the backend first with ``strata start``
in another terminal. All other commands work directly against the DB on
disk.

Vocabulary throughout follows ``CONTEXT.md``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from strata import __version__


def _backend_url() -> str:
    return os.environ.get("STRATA_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")


def _db_path_default() -> str:
    return os.environ.get("STRATA_DB_PATH", "./strata.db")


def _fleet_config_default() -> str:
    return os.environ.get("STRATA_FLEET_CONFIG", "fleet.yaml")


def _resolve_fleet_config(explicit: str | None) -> str | None:
    """Pick the config path: explicit arg → env var → fleet.yaml → fleet.example.yaml."""
    if explicit:
        return explicit
    env_path = os.environ.get("STRATA_FLEET_CONFIG")
    if env_path:
        return env_path
    if Path("fleet.yaml").exists():
        return "fleet.yaml"
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
    """Apply a YAML fleet config to the DB."""
    from strata.bootstrap import apply_fleet_config, load_fleet_config
    from strata.record_store import RecordStore

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

    db_path = args.db or _db_path_default()
    config = load_fleet_config(config_path)
    with RecordStore(db_path) as store:
        result = apply_fleet_config(store, config)
    print(f"Fleet bootstrapped from {config_path}:")
    print(f"  strata: {len(result.strata_created)} created, {len(result.strata_existing)} existing")
    print(f"  scopes: {len(result.scopes_created)} created, {len(result.scopes_existing)} existing")
    print(f"  edges:  {len(result.edges_created)} created, {len(result.edges_existing)} existing")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """Apply migrations, optionally auto-bootstrap, then run uvicorn.

    The auto-bootstrap step runs only when the scopes table is empty AND a
    fleet config is discoverable. Override with ``--no-bootstrap``.
    """
    from strata.migrator import run_migrations
    from strata.record_store import RecordStore

    db_path = args.db or _db_path_default()

    # 1. Migrate.
    applied = run_migrations(db_path)
    if applied:
        print(f"Applied {len(applied)} migration(s).")

    # 2. Auto-bootstrap if scopes table is empty.
    if not args.no_bootstrap:
        with RecordStore(db_path) as store:
            scopes_present = len(store.list_scopes()) > 0
        if not scopes_present:
            config_path = _resolve_fleet_config(None)
            if config_path is not None and Path(config_path).exists():
                rc = cmd_bootstrap(argparse.Namespace(config=config_path, db=db_path))
                if rc != 0:
                    return rc
            else:
                print(
                    "No scopes in DB and no fleet config found — starting empty. "
                    "Run `strata bootstrap --config <path>` later to seed.",
                )

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
    """List the fleet's strata, scopes, and edges (via the backend API)."""
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

    p_start = sub.add_parser("start", help="Migrate, auto-bootstrap, and run the backend.")
    p_start.add_argument("--host", default="127.0.0.1")
    p_start.add_argument("--port", type=int, default=8000)
    p_start.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload.")
    p_start.add_argument(
        "--no-bootstrap", action="store_true", help="Skip the auto-bootstrap step."
    )
    p_start.add_argument("--db", help=f"DB path (default: {_db_path_default()}).")
    p_start.set_defaults(func=cmd_start)

    p_migrate = sub.add_parser("migrate", help="Apply pending SQLite migrations.")
    p_migrate.add_argument("--db", help=f"DB path (default: {_db_path_default()}).")
    p_migrate.set_defaults(func=cmd_migrate)

    p_bootstrap = sub.add_parser("bootstrap", help="Apply a YAML fleet config.")
    p_bootstrap.add_argument("--config", help=f"Config path (default: {_fleet_config_default()}).")
    p_bootstrap.add_argument("--db", help=f"DB path (default: {_db_path_default()}).")
    p_bootstrap.set_defaults(func=cmd_bootstrap)

    p_scopes = sub.add_parser("scopes", help="List the fleet's strata, scopes, and edges.")
    p_scopes.set_defaults(func=cmd_scopes)

    p_summary = sub.add_parser("summary", help="Print a scope's curated summary.")
    p_summary.add_argument("scope_id")
    p_summary.set_defaults(func=cmd_summary)

    p_record = sub.add_parser("record", help="Print a scope's record (contributions + judgments).")
    p_record.add_argument("scope_id")
    p_record.set_defaults(func=cmd_record)

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
