"""#224: optional engine-side OpenRouter provider pinning (``JUDGE_PROVIDER``).

Covers:

1. The choke point is ENFORCED, not conventional — an AST scan of every
   ``.py`` file under ``src/strata/`` fails on any ``<something>.messages.create(``
   call outside a short allowlist of functions
   (``_messages_create``, ``_default_draft_fn``, ``_probe_judge_live``),
   and a second scan asserts each of those allowlisted functions actually
   calls ``apply_provider_pin`` somewhere in its own body — being exempt
   from the raw-call violation is not enough; it must actually pin.
   Whichever branch merges a new judge call site second (#219 C's
   `check_claim_carriers`, at this writing, still unmerged) must route
   through one of these, and this test makes skipping that impossible to
   miss.
2. Input identity: with no provider configured, the kwargs a mocked client
   receives are IDENTICAL to a pre-#224 call (no ``extra_body`` key at
   all) — on both an ordinary accept and a declined judgment. With a
   provider configured against an OpenRouter-shaped client, ONLY
   ``extra_body.provider`` is added; every other kwarg is untouched, and a
   caller's own ``extra_body`` keys (if any) survive alongside it. Against
   a non-OpenRouter-shaped client, the setting is ignored outright — no
   ``extra_body`` at all, same as unset.
3. ``Settings``/``resolve_judge`` resolve ``judge_provider`` from both env
   spellings, default ``None`` (unpinned).
4. The doctor line shows the pin state: pinned-and-applied, or
   configured-but-ignored on a non-OpenRouter endpoint — never silent.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from strata.scope_manager import ScopeManager
from strata.settings import (
    ResolvedJudge,
    _is_openrouter_client,
    resolve_judge,
    resolve_judge_from_env,
)

from .test_scope_manager import (
    CURRENT_SUMMARY,
    NEW_CONTRIBUTION,
    RECENT_ROW,
    SCOPE,
    STRATUM,
    _accept_context_input,
    _decline_input,
    _fake_response,
)

_REPO_ROOT = Path(__file__).parent.parent
_SRC = _REPO_ROOT / "src" / "strata"


# ---------------------------------------------------------------------------
# 1. The choke point is enforced.
# ---------------------------------------------------------------------------


_ALLOWLISTED_CALL_SITES = frozenset({"_messages_create", "_default_draft_fn", "_probe_judge_live"})


def _raw_messages_create_calls(source: str, filename: str) -> list[tuple[str, int]]:
    """Every ``<expr>.messages.create(`` call in *source*, as
    ``(filename, lineno)``, EXCEPT inside a function whose name is on
    ``_ALLOWLISTED_CALL_SITES`` (the only places allowed to call the raw
    client -- each of those is checked separately for actually calling
    ``apply_provider_pin``, see ``test_allowlisted_call_sites_apply_the_pin``)."""
    tree = ast.parse(source, filename=filename)
    violations: list[tuple[str, int]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.func_stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self.func_stack.append(node.name)
            self.generic_visit(node)
            self.func_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "create"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "messages"
                and not (set(self.func_stack) & _ALLOWLISTED_CALL_SITES)
            ):
                violations.append((filename, node.lineno))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return violations


def test_every_messages_create_call_routes_through_the_choke_point() -> None:
    violations: list[tuple[str, int]] = []
    for path in _SRC.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "messages.create" not in source:
            continue
        violations.extend(_raw_messages_create_calls(source, str(path.relative_to(_REPO_ROOT))))
    assert violations == [], (
        "every judge API call must go through one of "
        f"{sorted(_ALLOWLISTED_CALL_SITES)} (#224) -- found a raw "
        f"<client>.messages.create(...) call outside them at: {violations}"
    )


def _function_source_calls_name(tree: ast.AST, func_name: str, call_name: str) -> bool:
    """True if some function literally named *func_name* in *tree* contains,
    anywhere in its own body, a call whose callee's final attribute/name is
    *call_name*."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    func = inner.func
                    if isinstance(func, ast.Name) and func.id == call_name:
                        return True
                    if isinstance(func, ast.Attribute) and func.attr == call_name:
                        return True
    return False


def test_allowlisted_call_sites_apply_the_pin() -> None:
    """Being exempt from the raw-call scan is not enough on its own -- each
    allowlisted function must actually call ``apply_provider_pin`` itself."""
    found: dict[str, bool] = dict.fromkeys(_ALLOWLISTED_CALL_SITES, False)
    defined: set[str] = set()
    for path in _SRC.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if not any(name in source for name in _ALLOWLISTED_CALL_SITES):
            continue
        tree = ast.parse(source, filename=str(path))
        for func_name in _ALLOWLISTED_CALL_SITES:
            if f"def {func_name}(" not in source:
                continue
            defined.add(func_name)
            if _function_source_calls_name(tree, func_name, "apply_provider_pin"):
                found[func_name] = True
    assert defined == _ALLOWLISTED_CALL_SITES, (
        f"expected to find definitions for all of {sorted(_ALLOWLISTED_CALL_SITES)}, "
        f"only found {sorted(defined)} -- update the allowlist or the scan"
    )
    missing = [name for name, ok in found.items() if not ok]
    assert missing == [], f"these allowlisted call sites never call apply_provider_pin: {missing}"


# ---------------------------------------------------------------------------
# 2. Input identity.
# ---------------------------------------------------------------------------


def _judge_with(manager: ScopeManager) -> None:
    manager.judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[RECENT_ROW],
        new_contribution=NEW_CONTRIBUTION,
    )


@pytest.mark.parametrize("tool_input", [_accept_context_input(), _decline_input()])
def test_unset_provider_leaves_kwargs_byte_identical(tool_input: dict) -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(tool_input)
    manager = ScopeManager(client=mock_client)  # judge_provider defaults to None

    _judge_with(manager)

    kwargs = mock_client.messages.create.call_args.kwargs
    assert "extra_body" not in kwargs


def test_a_provider_against_an_openrouter_client_adds_only_extra_body_provider() -> None:
    mock_client = MagicMock()
    mock_client.base_url = MagicMock(host="openrouter.ai")
    mock_client.messages.create.return_value = _fake_response(_accept_context_input())
    manager = ScopeManager(client=mock_client, judge_provider="Alibaba")

    _judge_with(manager)

    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["extra_body"] == {"provider": {"order": ["Alibaba"], "allow_fallbacks": False}}
    # Nothing else in the call changed -- rebuild the unpinned call and diff.
    unpinned_client = MagicMock()
    unpinned_client.messages.create.return_value = _fake_response(_accept_context_input())
    ScopeManager(client=unpinned_client).judge(
        scope=SCOPE,
        stratum=STRATUM,
        current_summary=CURRENT_SUMMARY,
        recent_contributions=[RECENT_ROW],
        new_contribution=NEW_CONTRIBUTION,
    )
    unpinned_kwargs = unpinned_client.messages.create.call_args.kwargs
    pinned_kwargs_minus_extra_body = {k: v for k, v in kwargs.items() if k != "extra_body"}
    assert pinned_kwargs_minus_extra_body == unpinned_kwargs


def test_a_providers_own_extra_body_key_survives_alongside_the_pin() -> None:
    mock_client = MagicMock()
    mock_client.base_url = MagicMock(host="openrouter.ai")
    manager = ScopeManager(client=mock_client, judge_provider="Alibaba")
    mock_client.messages.create.return_value = _fake_response(_accept_context_input())

    manager._messages_create(extra_body={"some_other_key": "value"})

    kwargs = mock_client.messages.create.call_args.kwargs
    assert kwargs["extra_body"] == {
        "some_other_key": "value",
        "provider": {"order": ["Alibaba"], "allow_fallbacks": False},
    }


def test_a_provider_against_a_non_openrouter_client_is_ignored() -> None:
    mock_client = MagicMock()
    mock_client.base_url = MagicMock(host="api.anthropic.com")
    mock_client.messages.create.return_value = _fake_response(_accept_context_input())
    manager = ScopeManager(client=mock_client, judge_provider="Alibaba")

    _judge_with(manager)

    kwargs = mock_client.messages.create.call_args.kwargs
    assert "extra_body" not in kwargs


def test_a_provider_against_a_client_with_no_base_url_is_ignored() -> None:
    mock_client = MagicMock()
    mock_client.base_url = None
    mock_client.messages.create.return_value = _fake_response(_accept_context_input())
    manager = ScopeManager(client=mock_client, judge_provider="Alibaba")

    _judge_with(manager)

    kwargs = mock_client.messages.create.call_args.kwargs
    assert "extra_body" not in kwargs


def test_is_openrouter_client_checks_base_url_host_only() -> None:
    client = MagicMock()
    client.base_url = MagicMock(host="openrouter.ai")
    assert _is_openrouter_client(client) is True

    client.base_url = MagicMock(host="api.anthropic.com")
    assert _is_openrouter_client(client) is False

    assert _is_openrouter_client(MagicMock(spec=["messages"])) is False


# ---------------------------------------------------------------------------
# 3. Settings / resolve_judge resolve judge_provider.
# ---------------------------------------------------------------------------


def test_resolve_judge_defaults_judge_provider_to_none() -> None:
    resolved = resolve_judge(model=None, base_url=None, judge_api_key=None, anthropic_api_key=None)
    assert resolved.provider is None


def test_resolve_judge_carries_judge_provider_through() -> None:
    resolved = resolve_judge(
        model=None,
        base_url=None,
        judge_api_key="k",
        anthropic_api_key=None,
        judge_provider="Alibaba",
    )
    assert resolved.provider == "Alibaba"


def test_resolve_judge_from_env_reads_both_spellings() -> None:
    assert resolve_judge_from_env({"STRATA_JUDGE_PROVIDER": "Alibaba"}).provider == "Alibaba"
    assert resolve_judge_from_env({"JUDGE_PROVIDER": "Alibaba"}).provider == "Alibaba"
    assert resolve_judge_from_env({}).provider is None


def test_settings_judge_provider_field_resolves_from_both_env_vars() -> None:
    from strata.settings import Settings

    assert Settings(_env_file=None, JUDGE_API_KEY="k").judge_provider is None
    assert (
        Settings(_env_file=None, JUDGE_API_KEY="k", STRATA_JUDGE_PROVIDER="Alibaba").judge_provider
        == "Alibaba"
    )
    assert (
        Settings(_env_file=None, JUDGE_API_KEY="k", JUDGE_PROVIDER="Alibaba").judge_provider
        == "Alibaba"
    )


# ---------------------------------------------------------------------------
# 4. The doctor line.
# ---------------------------------------------------------------------------


def test_doctor_line_shows_pin_when_openrouter() -> None:
    from strata.__main__ import _judge_line

    resolved = ResolvedJudge(
        model="qwen/qwen3-235b-a22b-2507",
        base_url="https://openrouter.ai/api",
        api_key="k",
        reason="default",
        provider="Alibaba",
    )
    line = _judge_line(resolved)
    assert "[pinned to Alibaba]" in line


def test_doctor_line_shows_ignored_when_not_openrouter() -> None:
    from strata.__main__ import _judge_line

    resolved = ResolvedJudge(
        model="claude-haiku-4-5",
        base_url=None,
        api_key="k",
        reason="kept_anthropic",
        provider="Alibaba",
    )
    line = _judge_line(resolved)
    assert "configured, ignored: not an OpenRouter endpoint" in line


def test_doctor_line_shows_nothing_extra_when_unset() -> None:
    from strata.__main__ import _judge_line

    resolved = ResolvedJudge(
        model="qwen/qwen3-235b-a22b-2507",
        base_url="https://openrouter.ai/api",
        api_key="k",
        reason="default",
        provider=None,
    )
    assert "pinned" not in _judge_line(resolved)
    assert "JUDGE_PROVIDER" not in _judge_line(resolved)
