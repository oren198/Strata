"""#126 / ADR 0018 D5: at level 1 a bound session's own ``strata_retire`` is a held request.

The directive STILL BINDS for everyone, including the requester. Nothing is noticed until the
operator enacts it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml

from strata.summary_store import Directive, ScopeSummary, SummaryStore
from tests.test_mcp_server import _load_mcp_module, _make_db

NOTICE = "The tide table changed, so this no longer binds."
RULE = "The quay gate stays shut after dark."


def _fleet(tmp_path: Path, level: int) -> Path:
    raw = {
        "strata": [{"id": "L0", "name": "executive", "ordinal": 0}],
        "scopes": [{"id": "g_arch", "name": "Arch", "stratum_id": "L0", "human_review": level}],
        "edges": [],
    }
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.dump(raw), encoding="utf-8")
    return path


def _bound(mod, scope_id: str, session_id: str):  # noqa: ANN001, ANN202
    return patch.multiple(
        mod, _AGENT_SCOPE=scope_id, _AGENT_SKILL=None, _AGENT_SESSION_ID=session_id
    )


def _mod(tmp_path: Path, level: int):  # noqa: ANN202
    summaries = str(tmp_path / "summaries")
    SummaryStore(summaries).write(
        "g_arch",
        ScopeSummary(
            scope_id="g_arch",
            directives=[
                Directive(
                    id="c_keep",
                    content=RULE,
                    subject="gate",
                    source_scope_id="g_arch",
                    created_at="2026-10-01T00:00:00+00:00",
                )
            ],
            context="",
            updated_at="2026-10-01T00:00:00+00:00",
        ),
    )
    return _load_mcp_module(_make_db(tmp_path), summaries, str(_fleet(tmp_path, level))), summaries


async def test_a_level_zero_retire_is_unchanged(  # regression: passes on the base by design
    tmp_path: Path,
) -> None:
    mod, summaries = _mod(tmp_path, 0)
    with _bound(mod, "g_arch", "sess_a"):
        result = await mod.strata_retire("c_keep", "currency", NOTICE)
    assert result["label"] == "retired" and result["notice"] == NOTICE
    assert SummaryStore(summaries).read("g_arch").directives == []


async def test_a_level_one_retire_is_a_held_request_and_the_directive_still_binds(
    tmp_path: Path,
) -> None:
    mod, summaries = _mod(tmp_path, 1)
    with _bound(mod, "g_arch", "sess_a"):
        result = await mod.strata_retire("c_keep", "currency", NOTICE)
        text = str(result)
        assert "held for the operator" in text
        assert "c_keep still binds" in text
    assert [d.id for d in SummaryStore(summaries).read("g_arch").directives] == ["c_keep"]
    # no retirement and no notice yet
    assert mod._record_store.list_retirements(scope_id="g_arch") == []
    assert mod._record_store.list_change_events(scope_id="g_arch") == []
    # the request is recorded, with the actor session, kind, notice and time
    (request,) = mod._record_store.list_retirement_requests(scope_id="g_arch")
    assert request.id.startswith("rr_") and request.directive_id == "c_keep"
    assert request.actor_session_id == "sess_a" and request.kind == "currency"
    assert request.notice == NOTICE and request.disposition == "held"


async def test_the_requester_next_perspective_still_binds_and_shows_the_request(
    tmp_path: Path,
) -> None:
    from strata.perspective import compose_perspective  # noqa: PLC0415

    mod, summaries = _mod(tmp_path, 1)
    with _bound(mod, "g_arch", "sess_a"):
        await mod.strata_retire("c_keep", "correction", NOTICE)
    perspective = compose_perspective(
        "g_arch", fleet=mod._load_fleet(), summary_store=SummaryStore(summaries)
    )
    own = next(layer for layer in perspective["layers"] if layer.get("relation") == "self")
    assert RULE in str(own["summary"]["directives"])
    assert "held for the operator" in str(perspective)
