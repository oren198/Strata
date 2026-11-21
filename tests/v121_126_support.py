"""Shared world for the #126 (human review levels) acceptance tests. ADR 0018.

The fleet: g_org (L0) owns c_suite and c_tokens; g_web and g_api (L1) read them. ``human_review``
is set per scope through fleet.yaml, nothing else. A judge answer is scripted through the real
``ScopeManager`` with a mock client, so the answer goes through the real judge, every engine
guard and the R2 guard before the hold seam in ``app._judge_and_record``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import yaml

from strata.app import run_contribution
from strata.fleet_config import FleetConfig
from strata.migrator import run_migrations
from strata.perspective import compose_perspective
from strata.record_store import ContributorRef, RecordStore
from strata.scope_manager import ScopeManager
from strata.summary_store import Directive, EndCondition, ScopeSummary, SummaryStore
from tests.test_scope_manager import _fake_response

STAMP = "2026-10-01T00:00:00+00:00"
SUITE = "Run the full test suite before publishing a release."
TOKENS = "Access tokens expire after at most 15 minutes."
NEW_RULE = "Every release needs two approvals."


def write_fleet(tmp_path: Path, levels: dict[str, Any] | None = None) -> Path:
    """fleet.yaml for the world; ``levels`` maps a scope id to its raw ``human_review`` value."""
    scopes = []
    for scope_id, name, stratum in (
        ("g_org", "Org", "L0"),
        ("g_web", "Web", "L1"),
        ("g_api", "Api", "L1"),
    ):
        entry: dict[str, Any] = {"id": scope_id, "name": name, "stratum_id": stratum}
        if levels and scope_id in levels:
            entry["human_review"] = levels[scope_id]
        scopes.append(entry)
    raw = {
        "strata": [
            {"id": "L0", "name": "org", "ordinal": 0},
            {"id": "L1", "name": "team", "ordinal": 1},
        ],
        "scopes": scopes,
        "edges": [{"from": "g_web", "to": "g_org"}, {"from": "g_api", "to": "g_org"}],
    }
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.dump(raw), encoding="utf-8")
    return path


class World:
    """The stores and the fleet, plus the helpers the tests share."""

    def __init__(self, tmp_path: Path, levels: dict[str, Any] | None = None) -> None:
        self.tmp_path = tmp_path
        self.fleet = FleetConfig.load(write_fleet(tmp_path, levels))
        db = str(tmp_path / "strata.db")
        run_migrations(db)
        self.store = RecordStore(db)
        self.summaries = SummaryStore(str(tmp_path / "summaries"))
        self.summaries.write(
            "g_org",
            ScopeSummary(
                scope_id="g_org",
                directives=[
                    Directive(
                        id="c_suite", content=SUITE, source_scope_id="g_org", created_at=STAMP
                    ),
                    Directive(
                        id="c_tokens", content=TOKENS, source_scope_id="g_org", created_at=STAMP
                    ),
                ],
                context="Org context.",
                updated_at=STAMP,
            ),
        )

    def kw(self) -> dict[str, Any]:
        return {"fleet": self.fleet, "record_store": self.store, "summary_store": self.summaries}

    def org_ids(self) -> list[str]:
        return [d.id for d in self.summaries.read("g_org").directives]

    def set_judged_end(self, directive_id: str, condition: str = "the audit closes") -> None:
        summary = self.summaries.read("g_org")
        self.summaries.write(
            "g_org",
            summary.model_copy(
                update={
                    "directives": [
                        d.model_copy(
                            update={
                                "end_condition": EndCondition(kind="judged", condition=condition),
                                "set_by": directive_id,
                            }
                        )
                        if d.id == directive_id
                        else d
                        for d in summary.directives
                    ]
                }
            ),
        )

    def replace_directive(self, old_id: str, new_id: str, content: str) -> None:
        """What a supersession leaves in the summary: the old row gone, a new one in its place."""
        summary = self.summaries.read("g_org")
        self.summaries.write(
            "g_org",
            summary.model_copy(
                update={
                    "directives": [
                        Directive(
                            id=new_id, content=content, source_scope_id="g_org", created_at=STAMP
                        )
                        if d.id == old_id
                        else d
                        for d in summary.directives
                    ]
                }
            ),
        )

    def perspective(self, scope_id: str) -> dict:
        return compose_perspective(
            scope_id,
            fleet=self.fleet,
            summary_store=self.summaries,
            change_event_reader=lambda s: self.store.list_change_events(scope_id=s),
        )

    def binding_contents(self, scope_id: str) -> list[str]:
        out: list[str] = []
        for layer in self.perspective(scope_id)["layers"]:
            if layer.get("binding"):
                for d in layer.get("directives") or []:
                    out.append(d["content"] if isinstance(d, dict) else d.content)
        return out

    def propose(
        self,
        answer: dict,
        *,
        scope_id: str = "g_org",
        content: str = NEW_RULE,
        classification: str = "directive",
        fulfils: str | None = None,
        session: str | None = None,
    ):  # noqa: ANN201
        client = MagicMock()
        client.messages.create.return_value = _fake_response(answer)
        scope = self.fleet.get_scope(scope_id)
        return run_contribution(
            scope=scope,
            stratum=next(s for s in self.fleet.strata if s.id == scope.stratum_id),
            content=content,
            proposed_classification=classification,  # type: ignore[arg-type]
            subject=None,
            supersedes=None,
            contributor=ContributorRef(
                scope_id=scope_id,
                skill=None,
                session_id=session or f"s_{scope_id}",
                ts="2026-11-21T09:00:00Z",
            ),
            fleet=self.fleet,
            record_store=self.store,
            summary_store=self.summaries,
            scope_manager=ScopeManager(client=client),
            summary_max_words=500,
            fulfils=fulfils,
        )

    def close(self) -> None:
        self.store.close()


def directive_answer(ops: list[dict], reasoning: str = "A binding rule.") -> dict:
    return {
        "decision": "accept_as_directive",
        "reasoning": reasoning,
        "directive_ops": ops,
        "new_context": None,
    }
