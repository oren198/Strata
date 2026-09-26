#!/usr/bin/env python3
"""Seed a reproducible demo DB for the P5 Console screenshot (ADR 0017 P5).

Gives one scope-held directive at g_root and one operator directive attached
at g_root, with two operator_evidence rows against the operator directive —
one already marked seen, one unseen — reported by g_child (a descendant of
g_root). This is exactly what the "View as" tab's operator-memory layer shows
for g_root once the Console piece lands: an inline "2 evidence reports
(1 unseen)" marker on the operator directive.

Usage:
    python scripts/seed_p5_console_demo.py [target_dir]

Writes fleet.yaml, migrates a fresh strata.db, and seeds it under
*target_dir* (default: ./.strata-p5-console-demo, relative to cwd). Safe to
re-run: it wipes and recreates *target_dir* first.

Then start the Console against this demo data:

    STRATA_FLEET_CONFIG=<target_dir>/fleet.yaml \\
    STRATA_DB_PATH=<target_dir>/strata.db \\
    STRATA_SUMMARIES_DIR=<target_dir>/summaries \\
        strata start --skip-preflight

Open http://127.0.0.1:8000/ , go to the "View as" tab, and select g_root.
Expand the "Set by you, the operator" layer — the "Never deploy on a Friday"
directive shows the evidence marker there.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

_FLEET_YAML = """\
strata:
  - id: L0
    name: Executive
    ordinal: 0
  - id: L1
    name: Function
    ordinal: 1

scopes:
  - id: g_root
    name: Root Scope
    stratum_id: L0
    status: active
  - id: g_child
    name: Child Scope
    stratum_id: L1
    status: active

edges:
  - from: g_child
    to: g_root
"""


def main(target_dir: Path) -> None:
    from strata.fleet_config import FleetConfig
    from strata.migrator import run_migrations
    from strata.operator import operator_publish
    from strata.record_store import ContributorRef, OperatorEvidenceInput, RecordStore

    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True)

    fleet_yaml_path = target_dir / "fleet.yaml"
    fleet_yaml_path.write_text(_FLEET_YAML, encoding="utf-8")
    db_path = target_dir / "strata.db"
    summaries_dir = target_dir / "summaries"

    run_migrations(str(db_path))
    fleet = FleetConfig.load(fleet_yaml_path)

    contributor = ContributorRef(
        scope_id="g_child",
        skill="engineer",
        session_id="sess_demo",
        ts="2026-09-26T09:00:00Z",
    )

    with RecordStore(str(db_path)) as rs:
        # One ordinary, scope-held directive at g_root — the contrast case:
        # never gets an evidence marker, since scope-held directive failures
        # raise as an ordinary contribution at the issuer, not operator_evidence.
        directive_id = rs.append_contribution(
            scope_id="g_root",
            content="All services must use TLS 1.3 or later.",
            proposed_classification="directive",
            subject="tls",
            supersedes=None,
            contributor=ContributorRef(
                scope_id="g_root",
                skill="architect",
                session_id="sess_demo_root",
                ts="2026-09-26T09:00:00Z",
            ),
        ).id
        rs.record_judgment(
            contribution_id=directive_id,
            decision="accept_as_directive",
            judged_by="scope-manager",
        )

        # One operator directive attached at g_root.
        item = operator_publish(
            "g_root",
            "Never deploy on a Friday.",
            "deploy-window",
            record_store=rs,
            summaries_dir=str(summaries_dir),
            fleet=fleet,
        )

        # Evidence row 1: reported, then marked seen.
        outcome_1 = rs.append_contribution(
            scope_id="g_child",
            content="Deployed on Friday afternoon; the release broke within the hour.",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=contributor,
        ).id
        rs.record_judgment_and_raise(
            contribution_id=outcome_1,
            decision="accept_as_context",
            judged_by="scope-manager",
            raise_operator_evidence=OperatorEvidenceInput(
                operator_item_id=item.id,
                raised_from=outcome_1,
                reporter=contributor,
                content=(
                    f"evidence from g_child: following {item.id} went wrong: "
                    "deployed on Friday afternoon; the release broke within the hour."
                ),
            ),
        )
        (evidence_1,) = [
            e
            for e in rs.list_operator_evidence(operator_item_id=item.id)
            if e.raised_from == outcome_1
        ]
        rs.mark_operator_evidence_seen(evidence_1.id, seen_at="2026-09-25T08:00:00Z")

        # Evidence row 2: reported, left unseen.
        outcome_2 = rs.append_contribution(
            scope_id="g_child",
            content="Deployed on a different Friday; a hotfix was needed within the day.",
            proposed_classification="context",
            subject=None,
            supersedes=None,
            contributor=contributor,
        ).id
        rs.record_judgment_and_raise(
            contribution_id=outcome_2,
            decision="accept_as_context",
            judged_by="scope-manager",
            raise_operator_evidence=OperatorEvidenceInput(
                operator_item_id=item.id,
                raised_from=outcome_2,
                reporter=contributor,
                content=(
                    f"evidence from g_child: following {item.id} went wrong: "
                    "deployed on a different Friday; a hotfix was needed within the day."
                ),
            ),
        )

    print(f"Seeded demo data under {target_dir}")
    print()
    print("Start the Console against it:")
    print()
    print(
        f"    STRATA_FLEET_CONFIG={fleet_yaml_path} \\\n"
        f"    STRATA_DB_PATH={db_path} \\\n"
        f"    STRATA_SUMMARIES_DIR={summaries_dir} \\\n"
        "        strata start --skip-preflight"
    )
    print()
    print('Open http://127.0.0.1:8000/ , go to the "View as" tab, select g_root, and')
    print('expand the "Set by you, the operator" layer.')


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / ".strata-p5-console-demo"
    main(target)
