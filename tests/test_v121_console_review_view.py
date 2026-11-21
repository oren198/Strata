"""The Console's held-for-the-operator view (ADR 0018 D4): read-only, over GET /review/held."""

from __future__ import annotations

import importlib.resources
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from strata import review
from strata.app import create_app
from strata.settings import Settings
from tests.v121_126_support import NEW_RULE, World, directive_answer

_UI = importlib.resources.files("strata") / "_ui"


@pytest.fixture
def world(tmp_path: Path):  # noqa: ANN201
    w = World(tmp_path, {"g_org": 1})
    yield w
    w.close()


def _client(w: World) -> TestClient:
    settings = Settings(
        db_path=str(w.tmp_path / "strata.db"),
        summaries_dir=str(w.tmp_path / "summaries"),
        fleet_yaml_path=str(w.tmp_path / "fleet.yaml"),
        manager_model="claude-haiku-4-5",
        anthropic_api_key="k",
    )
    return TestClient(create_app(settings=settings))


# -- the data the view shows ---------------------------------------------------------------------


def test_nothing_held_says_so(world: World) -> None:
    with _client(world) as client:
        data = client.get("/review/held").json()
    assert data == {"count": 0, "oldest_age_seconds": None, "items": []}


def test_the_route_lists_the_count_the_oldest_age_and_each_item(world: World) -> None:
    held = world.propose(directive_answer([{"op": "append"}]))
    request = review.request_retirement(
        "g_org", "c_tokens", "currency", "Not needed.", actor_session_id="s_requester", **world.kw()
    )
    with _client(world) as client:
        data = client.get("/review/held").json()

    assert data["count"] == 2
    assert data["oldest_age_seconds"] is not None and data["oldest_age_seconds"] >= 0
    by_id = {item["id"]: item for item in data["items"]}
    judge_item, request_item = by_id[held.contribution_id], by_id[request.id]
    assert judge_item["scope_id"] == "g_org" and NEW_RULE in judge_item["content"]
    assert judge_item["label"] == "held for the operator" and judge_item["requester"] is None
    assert request_item["kind"] == "retirement_request"
    assert "c_tokens" in request_item["content"] and "Not needed." in request_item["content"]
    assert request_item["requester"] == "s_requester"  # the REQUESTER of a session retirement
    assert request_item["label"] == "held for the operator"


def test_an_approved_item_leaves_the_list(world: World) -> None:
    held = world.propose(directive_answer([{"op": "append"}]))
    review.approve(held.contribution_id, operator="oren", **world.kw())
    with _client(world) as client:
        assert client.get("/review/held").json()["count"] == 0


def test_the_route_can_be_narrowed_to_a_scope(world: World) -> None:
    world.propose(directive_answer([{"op": "append"}]))
    with _client(world) as client:
        assert client.get("/review/held", params={"scope_id": "g_org"}).json()["count"] == 1
        assert client.get("/review/held", params={"scope_id": "g_web"}).json()["count"] == 0


# -- read-only: nothing in the Console can approve, reject or write ------------------------------


def test_no_review_route_accepts_anything_but_a_read(world: World) -> None:
    app = _client(world).app
    review_routes = [r for r in app.routes if "review" in getattr(r, "path", "")]
    assert [r.path for r in review_routes] == ["/review/held"]
    for route in review_routes:
        assert route.methods <= {"GET", "HEAD"}, route.methods
    with _client(world) as client:
        for method in ("post", "put", "patch", "delete"):
            for path in ("/review/held", "/review/approve", "/review/reject"):
                assert getattr(client, method)(path).status_code in (404, 405), (method, path)


def test_the_view_has_no_control_and_makes_no_write_call() -> None:
    source = (_UI / "review.jsx").read_text(encoding="utf-8")
    assert "<button" not in source and "<input" not in source and "<form" not in source
    assert "onClick" not in source and "onSubmit" not in source
    assert not re.search(r"method\s*:", source) and "POST" not in source
    # the only store call it makes is the read
    assert re.findall(r"STRATA_STORE\.(\w+)", source) == ["fetchReviewHeld"]


def test_the_store_reaches_review_only_through_one_get() -> None:
    store = (_UI / "store.js").read_text(encoding="utf-8")
    assert re.findall(r"fetch\(`\$\{base\}(/review[^`?]*)", store) == ["/review/held"]
    body = store[store.index("async function fetchReviewHeld") :].split("}\n", 2)
    assert "method" not in body[0] + body[1] and "POST" not in body[0] + body[1]
    functions = re.findall(r"async function (\w+)", store)
    assert [name for name in functions if "review" in name.lower()] == ["fetchReviewHeld"]


def test_the_view_is_wired_into_the_page() -> None:
    assert (_UI / "review.jsx").is_file()
    assert 'src="review.jsx"' in (_UI / "index.html").read_text(encoding="utf-8")
    app = (_UI / "app.jsx").read_text(encoding="utf-8")
    assert "<ReviewView />" in app and 'onTab("review")' in app


# -- the same offline Babel checks the other Console files get ------------------------------------


def _babel_node_path() -> str | None:
    if shutil.which("node") is None:
        return None
    for candidate in (os.environ.get("STRATA_TEST_BABEL_NODE_PATH"), "/tmp/node_modules"):
        if candidate and (Path(candidate) / "@babel" / "core").is_dir():
            return candidate
    return None


_NODE_PATH = _babel_node_path()


@pytest.mark.skipif(_NODE_PATH is None, reason="node + @babel/core not available")
def test_review_jsx_parses_and_formats_the_age() -> None:
    source = (_UI / "review.jsx").read_text(encoding="utf-8")
    script = (
        "const babel = require('@babel/core');"
        "let code = '';"
        "process.stdin.setEncoding('utf8');"
        "process.stdin.on('data', d => code += d);"
        "process.stdin.on('end', () => {"
        "  const { code: out } = babel.transform(code, {"
        "    presets: ['@babel/preset-react'], filename: 'review.jsx' });"
        "  global.React = {}; global.window = {};"
        "  (0, eval)(out + ';global.__age = reviewAge;');"
        "  const a = global.__age;"
        "  console.log(JSON.stringify([a(null), a(0), a(125), a(7200), a(3 * 86400)]));"
        "});"
    )
    result = subprocess.run(
        ["node", "-e", script],
        input=source,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "NODE_PATH": _NODE_PATH},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '["none","0m","2m","2h 0m","3d"]'
