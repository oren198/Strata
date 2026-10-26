"""v1.20 forgetting peripherals: MCP, operator CLI, Console, and metrics.

The position gate itself is Bob's
``test_a_non_owner_or_the_engine_actor_is_refused_and_nothing_changes`` in
``tests/test_v120_forgetting_core.py``. These tests assert the surfaces take
no scope or actor argument, and that a refusal is surfaced verbatim.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from strata.app import ContributeRequest, create_app
from strata.forgetting_surface import (
    OPERATOR_ITEMS_NOTE,
    PROMPT_ONLY,
    format_forgetting_metrics,
    present_candidate,
    reject_dismiss_days,
)
from strata.record_store import RecordStore, RetirementCandidate
from strata.summary_store import Directive, ScopeSummary, SummaryStore
from tests.test_mcp_server import _load_mcp_module, _make_db, _make_fleet_yaml

_UNTIL = "2027-06-01"
_NOTICE = "The tide table changed, so this no longer binds."
_ROOT = Path(__file__).resolve().parents[1]


def _directive(scope_id: str, directive_id: str = "c_keep") -> Directive:
    return Directive(
        id=directive_id,
        content="The quay gate stays shut after dark.",
        subject="gate",
        source_scope_id=scope_id,
        source_skill=None,
        created_at="2026-01-01T00:00:00+00:00",
    )


def _write_summary(summaries_dir: str, scope_id: str, directive_id: str = "c_keep") -> None:
    SummaryStore(summaries_dir).write(
        scope_id,
        ScopeSummary(
            scope_id=scope_id,
            directives=[_directive(scope_id, directive_id)],
            context="",
            updated_at="2026-01-01T00:00:00+00:00",
        ),
    )


def _bound(mod, scope_id: str, session_id: str = "sess_owner"):  # noqa: ANN001
    from unittest.mock import patch

    return patch.multiple(
        mod,
        _AGENT_SCOPE=scope_id,
        _AGENT_SKILL=None,
        _AGENT_SESSION_ID=session_id,
    )


def _seed_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(
        """\
strata:
  - id: L0
    name: Executive
    ordinal: 0
scopes:
  - id: g_ceo
    name: CEO
    stratum_id: L0
edges: []
""",
        encoding="utf-8",
    )
    db_path = tmp_path / "test.db"
    summaries = tmp_path / "summaries"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRATA_FLEET_CONFIG", str(fleet))
    monkeypatch.setenv("STRATA_DB_PATH", str(db_path))
    monkeypatch.setenv("STRATA_SUMMARIES_DIR", str(summaries))
    from strata.migrator import run_migrations
    from strata.settings import get_settings

    get_settings.cache_clear()
    run_migrations(str(db_path))
    return str(db_path), str(summaries)


def _client(tmp_path: Path) -> TestClient:
    from strata.migrator import run_migrations
    from strata.settings import Settings

    db_path = tmp_path / "test.db"
    summaries = tmp_path / "summaries"
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(
        """\
strata:
  - id: L0
    name: Executive
    ordinal: 0
scopes:
  - id: g_ceo
    name: CEO
    stratum_id: L0
edges: []
""",
        encoding="utf-8",
    )
    run_migrations(str(db_path))
    settings = Settings(
        db_path=str(db_path),
        summaries_dir=str(summaries),
        fleet_yaml_path=str(fleet),
        manager_model="claude-haiku-4-5",
        anthropic_api_key="k",
    )
    return TestClient(create_app(settings=settings))


def test_reject_dismiss_days_stops_at_the_surface() -> None:
    assert reject_dismiss_days(1) == 1
    assert reject_dismiss_days(365) == 365
    for bad in (0, 366, -1, True, "30"):
        with pytest.raises(ValueError, match="1 to 365"):
            reject_dismiss_days(bad)


def test_age_and_disuse_append_the_phrase_on_display_only() -> None:
    for ground in ("age", "disuse"):
        candidate = RetirementCandidate(
            id="rc_1",
            directive_id="c_keep",
            scope_id="g_ceo",
            ground=ground,
            evidence_ids="[]",
            opened_at="t",
            state="open",
            dismissed_until=None,
            note=None,
        )
        shown = present_candidate(candidate)
        assert PROMPT_ONLY in shown["display"]
        assert shown["note"] is None
        assert candidate.note is None
    other = present_candidate(
        RetirementCandidate(
            id="rc_2",
            directive_id="c_keep",
            scope_id="g_ceo",
            ground="date_near",
            evidence_ids="[]",
            opened_at="t",
            state="open",
            dismissed_until=None,
            note="soon",
        )
    )
    assert other["display"] == "soon"
    assert PROMPT_ONLY not in (other["display"] or "")


def test_position_gate_stays_in_bobs_core_test() -> None:
    """Cite Bob: the refusal of a non-owner and of the engine actor is
    ``test_a_non_owner_or_the_engine_actor_is_refused_and_nothing_changes``.
    """
    source = (_ROOT / "tests" / "test_v120_forgetting_core.py").read_text(encoding="utf-8")
    name = "test_a_non_owner_or_the_engine_actor_is_refused_and_nothing_changes"
    assert f"def {name}" in source


async def test_mcp_tools_take_no_scope_or_actor(tmp_path: Path) -> None:
    mod = _load_mcp_module(
        _make_db(tmp_path), str(tmp_path / "summaries"), str(_make_fleet_yaml(tmp_path))
    )
    for name in (
        "strata_retire",
        "strata_retirement_candidates",
        "strata_dismiss_candidate",
    ):
        params = inspect.signature(getattr(mod, name)).parameters
        assert "scope_id" not in params
        assert "actor" not in params
    with pytest.raises(TypeError):
        await mod.strata_retire("c_keep", "currency", _NOTICE, scope_id="g_other")
    with pytest.raises(TypeError):
        await mod.strata_retirement_candidates(scope_id="g_other")
    with pytest.raises(TypeError):
        await mod.strata_dismiss_candidate("rc_1", "later", 3, actor="forged")


async def test_unbound_session_gets_the_not_bound_error(tmp_path: Path) -> None:
    from unittest.mock import patch

    mod = _load_mcp_module(
        _make_db(tmp_path), str(tmp_path / "summaries"), str(_make_fleet_yaml(tmp_path))
    )
    with (
        patch.object(mod, "_UNRESOLVED", True),
        patch.object(mod, "_STARTUP_ERRORS_BINDING", ["this session is not bound"]),
    ):
        with pytest.raises(RuntimeError, match="not bound"):
            await mod.strata_retire("c_keep", "currency", _NOTICE)
        with pytest.raises(RuntimeError, match="not bound"):
            await mod.strata_retirement_candidates()
        with pytest.raises(RuntimeError, match="not bound"):
            await mod.strata_dismiss_candidate("rc_1", "later", 3)


async def test_retire_returns_the_notice_and_drops_the_directive(tmp_path: Path) -> None:
    summaries = str(tmp_path / "summaries")
    mod = _load_mcp_module(_make_db(tmp_path), summaries, str(_make_fleet_yaml(tmp_path)))
    _write_summary(summaries, "g_arch")
    with _bound(mod, "g_arch"):
        result = await mod.strata_retire("c_keep", "currency", _NOTICE)
    assert result["directive_id"] == "c_keep"
    assert result["label"] == "retired"
    assert result["kind"] == "currency"
    assert result["notice"] == _NOTICE
    assert mod._summary_store.read("g_arch").directives == []


async def test_engine_session_surfaces_the_refusal_and_changes_nothing(tmp_path: Path) -> None:
    summaries = str(tmp_path / "summaries")
    mod = _load_mcp_module(_make_db(tmp_path), summaries, str(_make_fleet_yaml(tmp_path)))
    _write_summary(summaries, "g_arch")
    with (
        _bound(mod, "g_arch", session_id="engine"),
        pytest.raises(RuntimeError, match="Only a session bound"),
    ):
        await mod.strata_retire("c_keep", "correction", _NOTICE)
    assert [item.id for item in mod._summary_store.read("g_arch").directives] == ["c_keep"]
    assert mod._record_store.list_retirements(scope_id="g_arch") == []


async def test_candidates_show_the_phrase_and_leave_the_note(tmp_path: Path) -> None:
    summaries = str(tmp_path / "summaries")
    mod = _load_mcp_module(_make_db(tmp_path), summaries, str(_make_fleet_yaml(tmp_path)))
    store = mod._record_store
    age = store.open_retirement_candidate(directive_id="c_keep", scope_id="g_arch", ground="age")
    disuse = store.open_retirement_candidate(
        directive_id="c_keep", scope_id="g_arch", ground="disuse"
    )
    store.open_retirement_candidate(directive_id="op_hidden", scope_id="g_arch", ground="age")
    with _bound(mod, "g_arch"):
        result = await mod.strata_retirement_candidates()
    by_ground = {row["ground"]: row for row in result["candidates"]}
    assert set(by_ground) == {"age", "disuse"}
    for row in by_ground.values():
        assert PROMPT_ONLY in row["display"]
        assert row["note"] is None
    assert store.get_retirement_candidate(age.id).note is None
    assert store.get_retirement_candidate(disuse.id).note is None


async def test_dismiss_days_outside_1_to_365_never_reach_the_engine(tmp_path: Path) -> None:
    summaries = str(tmp_path / "summaries")
    mod = _load_mcp_module(_make_db(tmp_path), summaries, str(_make_fleet_yaml(tmp_path)))
    opened = mod._record_store.open_retirement_candidate(
        directive_id="c_keep", scope_id="g_arch", ground="date_near"
    )
    with _bound(mod, "g_arch"):
        for days in (0, 366):
            with pytest.raises(RuntimeError, match="1 to 365"):
                await mod.strata_dismiss_candidate(opened.id, "not yet", days)
            assert mod._record_store.get_retirement_candidate(opened.id).state == "open"
        await mod.strata_dismiss_candidate(opened.id, "ask next quarter", 30)
    dismissed = mod._record_store.get_retirement_candidate(opened.id)
    assert dismissed.state == "dismissed"
    assert dismissed.note == "ask next quarter"


async def test_contribute_docstring_ends_and_fulfils(tmp_path: Path) -> None:
    mod = _load_mcp_module(
        _make_db(tmp_path), str(tmp_path / "summaries"), str(_make_fleet_yaml(tmp_path))
    )
    text = " ".join((mod.strata_contribute.__doc__ or "").split())
    assert "a release-only contribution with supersedes is answered `retire`" in text
    assert "use `strata_retire` to retire your own directive" in text
    fulfils = (
        "`fulfils` is evidence, and only the owning scope's judge or a session "
        "bound to the owner can confirm it."
    )
    assert fulfils in text
    params = inspect.signature(mod.strata_contribute).parameters
    assert "ends" in params and "fulfils" in params
    assert "issue #" not in (mod.strata_retire.__doc__ or "").lower()


async def test_ends_on_a_context_proposal_writes_nothing(tmp_path: Path) -> None:
    from unittest.mock import patch

    summaries = str(tmp_path / "summaries")
    mod = _load_mcp_module(_make_db(tmp_path), summaries, str(_make_fleet_yaml(tmp_path)))
    with (
        _bound(mod, "g_backend"),
        patch("strata.scope_manager.ScopeManager.judge", side_effect=AssertionError("judge")),
        pytest.raises(ValueError, match="ends is only meaningful"),
    ):
        await mod.strata_contribute(
            scope_id="g_backend",
            content="A note, not a directive.",
            proposed_classification="context",
            ends={"until": _UNTIL},
        )
    assert mod._record_store.list_contributions(scope_id="g_backend") == []


async def test_fulfils_of_an_unknown_directive_writes_nothing(tmp_path: Path) -> None:
    from unittest.mock import patch

    summaries = str(tmp_path / "summaries")
    mod = _load_mcp_module(_make_db(tmp_path), summaries, str(_make_fleet_yaml(tmp_path)))
    with (
        _bound(mod, "g_backend"),
        patch("strata.scope_manager.ScopeManager.judge", side_effect=AssertionError("judge")),
        pytest.raises(ValueError),
    ):
        await mod.strata_contribute(
            scope_id="g_backend",
            content="The condition was met.",
            proposed_classification="directive",
            fulfils="c_missing",
        )
    assert mod._record_store.list_contributions(scope_id="g_backend") == []


def test_there_is_no_scope_session_cli(capsys: pytest.CaptureFixture[str]) -> None:
    from strata.__main__ import main

    for argv in (
        ["retire", "c_keep", "--kind", "currency", "--notice", _NOTICE],
        ["candidates"],
        ["candidates", "--scope", "g_ceo"],
        ["dismiss-candidate", "rc_1", "--reason", "later", "--days", "3"],
        ["operator", "retire", "g_ceo", "op_x", "--kind", "currency"],
    ):
        with pytest.raises(SystemExit) as caught:
            main(argv)
        assert caught.value.code == 2
    capsys.readouterr()


def test_operator_publish_until_and_supersede_when(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from strata.__main__ import main

    _db, summaries = _seed_cli(tmp_path, monkeypatch)
    rc = main(["operator", "publish", "g_ceo", "--content", "Shut the gate.", "--until", _UNTIL])
    assert rc == 0
    layer = Path(summaries) / "operator" / "g_ceo.md"
    text = layer.read_text(encoding="utf-8")
    assert f"until={_UNTIL}" in text
    op_id = text.split("[", 1)[-1].split("]", 1)[0] if "[" in text else ""
    if not op_id.startswith("op_"):
        op_id = next(token for token in text.split() if token.startswith("op_"))
    capsys.readouterr()
    rc = main(
        [
            "operator",
            "supersede",
            "g_ceo",
            op_id,
            "--content",
            "Shut the gate at dusk.",
            "--when",
            "the quay closes",
        ]
    )
    assert rc == 0
    replaced = layer.read_text(encoding="utf-8")
    assert "the quay closes" in replaced
    assert "when=" in replaced


def test_c_id_supersede_with_an_end_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from strata.__main__ import main

    db_path, summaries = _seed_cli(tmp_path, monkeypatch)
    _write_summary(summaries, "g_ceo")
    summary_path = Path(summaries) / "g_ceo.md"
    before = summary_path.read_bytes()
    rc = main(
        [
            "operator",
            "supersede",
            "g_ceo",
            "c_keep",
            "--content",
            "A replacement.",
            "--until",
            _UNTIL,
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "Nothing was written." in captured.err
    assert summary_path.read_bytes() == before
    assert RecordStore(db_path).list_contributions(scope_id="g_ceo") == []


def test_retire_item_needs_kind_and_notice_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from strata.__main__ import main

    _db, summaries = _seed_cli(tmp_path, monkeypatch)
    assert main(["operator", "publish", "g_ceo", "--content", "Keep the gate."]) == 0
    published = capsys.readouterr().out
    op_id = published.split("[", 1)[1].split("]", 1)[0]
    layer = Path(summaries) / "operator" / "g_ceo.md"
    before = layer.read_bytes()
    rc = main(["operator", "retire-item", "g_ceo", op_id, "--kind", "currency"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Nothing was written." in captured.err
    assert layer.read_bytes() == before
    rc = main(
        [
            "operator",
            "retire-item",
            "g_ceo",
            op_id,
            "--kind",
            "correction",
            "--notice",
            "It should not have bound.",
        ]
    )
    assert rc == 0
    assert op_id not in layer.read_text(encoding="utf-8")


def test_operator_candidates_print_the_phrase_and_dismiss_rejects_366(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from strata.__main__ import main

    db_path, _summaries = _seed_cli(tmp_path, monkeypatch)
    store = RecordStore(db_path)
    opened = store.open_retirement_candidate(
        directive_id="op_gate", scope_id="g_ceo", ground="disuse"
    )
    rc = main(["operator", "candidates", "--scope", "g_ceo"])
    captured = capsys.readouterr()
    assert rc == 0
    assert PROMPT_ONLY in captured.out
    assert store.get_retirement_candidate(opened.id).note is None
    rc = main(["operator", "dismiss-candidate", opened.id, "--reason", "later", "--days", "366"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Nothing was written." in captured.err
    assert store.get_retirement_candidate(opened.id).state == "open"
    rc = main(
        ["operator", "dismiss-candidate", opened.id, "--reason", "next season", "--days", "14"]
    )
    assert rc == 0
    dismissed = store.get_retirement_candidate(opened.id)
    assert dismissed.state == "dismissed"
    assert dismissed.note == "next season"
    assert PROMPT_ONLY not in (dismissed.note or "")


def test_stats_forgetting_leaves_operator_items_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from strata.__main__ import main

    db_path, _summaries = _seed_cli(tmp_path, monkeypatch)
    store = RecordStore(db_path)
    store.append_retirement(
        scope_id="g_ceo",
        directive_id="c_old",
        retired_by="scope-session",
        kind="currency",
        notice="The date passed.",
        cause="date_lapse",
    )
    store.append_retirement(
        scope_id="g_ceo",
        directive_id="c_bad",
        retired_by="scope-session",
        kind="correction",
        notice="It was wrong.",
        cause="act",
    )
    store.open_retirement_candidate(directive_id="c_old", scope_id="g_ceo", ground="age")
    store.open_retirement_candidate(directive_id="op_old", scope_id="g_ceo", ground="age")
    rc = main(["stats", "forgetting", "--scope", "g_ceo"])
    text = capsys.readouterr().out
    assert rc == 0
    assert text.strip().splitlines()[-1] == OPERATOR_ITEMS_NOTE
    assert "lapses: 1" in text
    assert "releases: 1" in text
    assert "retractions: 1" in text
    assert "age: open 1, dismissed 0, acted 0" in text
    assert "condensation measured:" in text
    assert "condensation unmeasured:" in text
    assert "condensation unmeasured_by_reason:" in text
    assert "condensation inversions:" in text
    rc = main(["stats", "forgetting", "--scope", "g_ceo", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["operator_items"] == OPERATOR_ITEMS_NOTE
    assert report["candidates"]["age"]["open"] == 1
    assert "unmeasured" in report["condensation"]
    assert "unmeasured_by_reason" in report["condensation"]
    rendered = format_forgetting_metrics(report)
    assert rendered.strip().splitlines()[-1] == OPERATOR_ITEMS_NOTE


def test_http_candidates_dismiss_and_metrics(tmp_path: Path) -> None:
    from strata.settings import get_settings

    db_path = tmp_path / "test.db"
    with _client(tmp_path) as client:
        store = RecordStore(str(db_path))
        scope_row = store.open_retirement_candidate(
            directive_id="c_keep", scope_id="g_ceo", ground="age"
        )
        op_row = store.open_retirement_candidate(
            directive_id="op_keep", scope_id="g_ceo", ground="disuse"
        )
        missing = client.get("/scopes/g_missing/retirement-candidates")
        assert missing.status_code == 404
        scope_body = client.get("/scopes/g_ceo/retirement-candidates").json()
        assert [row["directive_id"] for row in scope_body["candidates"]] == ["c_keep"]
        assert PROMPT_ONLY in scope_body["candidates"][0]["display"]
        assert scope_body["candidates"][0]["note"] is None
        operator_body = client.get("/operator/retirement-candidates", params={"scope_id": "g_ceo"})
        assert operator_body.status_code == 200
        assert [row["directive_id"] for row in operator_body.json()["candidates"]] == ["op_keep"]
        refused = client.post(
            f"/operator/retirement-candidates/{op_row.id}/dismiss",
            json={"reason": "later", "days": 366},
        )
        assert refused.status_code == 422
        assert store.get_retirement_candidate(op_row.id).state == "open"
        gone = client.post(
            f"/scopes/g_ceo/retirement-candidates/{scope_row.id}/dismiss",
            json={"reason": "later", "days": 3},
        )
        assert gone.status_code == 404
        assert store.get_retirement_candidate(scope_row.id).state == "open"
        ok = client.post(
            f"/operator/retirement-candidates/{op_row.id}/dismiss",
            json={"reason": "next season", "days": 10},
        )
        assert ok.status_code == 200
        assert store.get_retirement_candidate(op_row.id).note == "next season"
        metrics = client.get("/forgetting-metrics", params={"scope_id": "g_ceo"}).json()
        assert metrics["operator_items"] == OPERATOR_ITEMS_NOTE
        assert metrics["candidates"]["age"]["open"] == 1
        assert metrics["candidates"]["disuse"]["open"] == 0
        assert "unmeasured" in metrics["condensation"]
    get_settings.cache_clear()


def test_post_contribute_body_has_no_ends_or_fulfils() -> None:
    assert "ends" not in ContributeRequest.model_fields
    assert "fulfils" not in ContributeRequest.model_fields


def test_console_renders_the_label_the_notice_and_the_phrase() -> None:
    ui = _ROOT / "src" / "strata" / "_ui"
    scope = (ui / "scope-detail.jsx").read_text(encoding="utf-8")
    view = (ui / "view-as.jsx").read_text(encoding="utf-8")
    assert "binds until" in scope
    assert PROMPT_ONLY in scope
    assert "operator items not counted" in scope
    assert 'label="Retire"' in scope
    assert "changed circumstance:" in scope and "r.changed_circumstance" in scope
    start = scope.index("function ScopeCandidateList")
    body = scope[start : scope.index("function ", start + 10)]
    assert "dismiss" not in body.lower()
    assert "c.retirement.label" in view
    assert "c.retirement.notice" in view


# operator_retire*, operator_publish, operator_supersede*, and an operator
# dismiss or candidates tool. forgetting_surface defines none of these; the
# scan still includes that module so a function added there is covered.
_OPERATOR_ACT = re.compile(
    r"(?<![\w])(?:"
    r"operator_retire\w*"
    r"|operator_publish\w*"
    r"|operator_supersede\w*"
    r"|operator_dismiss\w*"
    r"|operator_candidates\w*"
    r"|dismiss_operator\w*"
    r")(?![\w])"
)


def _operator_act_names() -> set[str]:
    """Operator acts an MCP tool must not call.

    ``forgetting_surface`` is included because that is the peripherals module.
    It defines none of these today. The acts themselves live in
    ``strata.forgetting`` and ``strata.operator``.
    """
    import strata.forgetting as forgetting
    import strata.forgetting_surface as surface
    import strata.operator as operator

    names: set[str] = set()
    for module in (surface, forgetting, operator):
        for name, value in vars(module).items():
            if callable(value) and _OPERATOR_ACT.search(name):
                names.add(name)
    return names


def _tool_source(fn: object) -> str:
    """Source of a registered tool, including the function under the wrappers."""
    chunks: list[str] = []
    seen: list[object] = []
    current = fn
    while current is not None and current not in seen:
        seen.append(current)
        try:
            chunks.append(inspect.getsource(current))
        except (OSError, TypeError):
            chunks.append("")
        current = getattr(current, "__wrapped__", None)
    source = "\n".join(chunks)
    if not source.strip():
        raise AssertionError("registered tool has no readable source")
    return source


async def test_mcp_exposes_no_operator_tool_and_retire_takes_no_scope(tmp_path: Path) -> None:
    """Every exposed MCP tool, by name and by what it calls.

    ``strata_retire``'s input schema has no ``scope_id`` and no ``actor``.
    """
    mod = _load_mcp_module(
        _make_db(tmp_path),
        str(tmp_path / "summaries"),
        str(_make_fleet_yaml(tmp_path)),
    )
    listed = await mod.mcp.list_tools()
    assert listed, "the MCP server exposed no tools"
    listed_names = {tool.name for tool in listed}
    registered = set(mod.mcp._tool_manager._tools)
    assert listed_names == registered

    operator_named = sorted(name for name in listed_names if _OPERATOR_ACT.search(name))
    assert operator_named == [], operator_named

    acts = _operator_act_names()
    assert {
        "dismiss_operator_candidate",
        "operator_candidates",
        "operator_publish",
        "operator_retire",
        "operator_retire_item",
        "operator_supersede",
        "operator_supersede_item",
    } <= acts
    dispatched: list[str] = []
    for name in sorted(listed_names):
        source = _tool_source(mod.mcp._tool_manager._tools[name].fn)
        used = sorted(
            act for act in acts if re.search(rf"(?<![\w]){re.escape(act)}(?![\w])", source)
        )
        if used:
            dispatched.append(f"{name}: {', '.join(used)}")
    assert dispatched == [], dispatched

    schema = {tool.name: tool for tool in listed}["strata_retire"].inputSchema
    if not isinstance(schema, dict):
        schema = schema.model_dump()
    properties = schema.get("properties") or {}
    assert "scope_id" not in properties
    assert "actor" not in properties
