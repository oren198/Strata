"""``mcp`` 1.29 names a tool result's fields ``isError`` / ``structuredContent``; mcp 2.3.0 (what a
standard Hermes ``[all]`` install pins) names them ``is_error`` / ``structured_content``. The
client must read both."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from plugin_loader import load_plugin


def _block(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text)


def _v1(**kw) -> SimpleNamespace:  # noqa: ANN003
    return SimpleNamespace(
        content=[_block(kw.get("text", ""))],
        isError=kw.get("error", False),
        structuredContent=kw.get("structured"),
    )


def _v2(**kw) -> SimpleNamespace:  # noqa: ANN003
    return SimpleNamespace(
        content=[_block(kw.get("text", ""))],
        is_error=kw.get("error", False),
        structured_content=kw.get("structured"),
    )


@pytest.mark.parametrize("make", [_v1, _v2], ids=["mcp-1.29", "mcp-2.3"])
def test_structured_content_is_read_under_either_naming(make) -> None:  # noqa: ANN001
    client = load_plugin().client
    assert client.parse_result(make(structured={"scope_id": "g"}), "t") == {"scope_id": "g"}
    assert client.parse_result(make(structured={"result": {"a": 1}}), "t") == {"a": 1}
    assert client.parse_result(make(text='{"b": 2}'), "t") == {"b": 2}
    assert client.parse_result(make(text="plain"), "t") == {"text": "plain"}


@pytest.mark.parametrize("make", [_v1, _v2], ids=["mcp-1.29", "mcp-2.3"])
def test_an_error_result_raises_with_the_stable_codes(make) -> None:  # noqa: ANN001
    client = load_plugin().client
    with pytest.raises(client.StrataError) as failed:
        client.parse_result(make(error=True, text="boom"), "t")
    assert failed.value.code == client.FAILED
    with pytest.raises(client.StrataError) as unbound:
        client.parse_result(make(error=True, text="session is not bound"), "t")
    assert unbound.value.code == client.UNBOUND
