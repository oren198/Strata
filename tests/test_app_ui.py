"""Tests for the Strata Console UI static serving and root redirect.

Covers:
1. GET / returns 307 redirect to /ui/index.html.
2. GET /ui/index.html returns 200 with text/html content-type.
3. GET /ui/store.js returns 200 with JS content-type.
4. GET /ui/nonexistent.jsx returns 404.
5. Static mount survives an absolute-vs-relative cwd shift (monkeypatch.chdir).
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from strata.app import create_app
from strata.settings import Settings


@pytest.fixture()
def client(tmp_path: pathlib.Path) -> TestClient:
    """Return a TestClient wired to a temp-isolated Strata app."""
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        summaries_dir=str(tmp_path / "summaries"),
        anthropic_api_key="test-key",
    )
    app = create_app(settings=settings)
    return TestClient(app, follow_redirects=False)


def test_root_redirects_to_ui_index(client: TestClient) -> None:
    """GET / should return a 307 redirect to /ui/index.html."""
    response = client.get("/")
    assert response.status_code == 307
    assert response.headers["location"] == "/ui/index.html"


def test_ui_index_returns_html(client: TestClient) -> None:
    """GET /ui/index.html should return 200 with HTML content-type."""
    response = client.get("/ui/index.html")
    assert response.status_code == 200
    content_type = response.headers["content-type"]
    assert "text/html" in content_type


def test_ui_store_js_returns_js(client: TestClient) -> None:
    """GET /ui/store.js should return 200 with a JavaScript content-type."""
    response = client.get("/ui/store.js")
    assert response.status_code == 200
    content_type = response.headers["content-type"]
    # Content-type may be application/javascript or text/javascript.
    assert "javascript" in content_type


def test_ui_nonexistent_returns_404(client: TestClient) -> None:
    """GET /ui/nonexistent.jsx should return 404."""
    response = client.get("/ui/nonexistent.jsx")
    assert response.status_code == 404


def test_static_files_resolve_from_changed_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Static files must resolve correctly after changing the working directory.

    The app resolves ui/ relative to the package file (__file__), not the cwd,
    so a mid-test cwd change must not break static serving.
    """
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        summaries_dir=str(tmp_path / "summaries"),
        anthropic_api_key="test-key",
    )
    app = create_app(settings=settings)
    client = TestClient(app, follow_redirects=False)

    # Change the working directory to /tmp — far from the project root.
    monkeypatch.chdir("/tmp")

    response = client.get("/ui/index.html")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
