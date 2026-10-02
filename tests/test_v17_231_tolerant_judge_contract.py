"""#231: the tolerant judge contract.

`ScopeManager.judge`/`judge_batch`/`judge_publication`/`judge_bootstrap_publication`
each gained a trailing ``**_extra`` so a FUTURE optional keyword the engine starts
passing is a no-op there, never a `TypeError` — three kwarg-naming incidents in two
cycles (#202, the P5 parse/prompt-gate split, #229) all took this shape.

This is enforced from two directions, both DERIVED from the real call sites —
`strata.app`/`strata.publication` — never hand-typed, so the test itself cannot drift
out of sync with what the engine actually passes:

1. On the REAL `ScopeManager`, every keyword the engine passes TODAY must be a named
   parameter — never merely swallowed by `**_extra`. A typo'd kwarg on the caller side
   must still surface (as a missing-argument `TypeError`, not silent data loss) rather
   than disappearing into the catch-all — the `**_extra` hole #231 is guarding against
   is a FUTURE unknown keyword, not today's known ones.
2. Every in-repo class standing in for `ScopeManager` (every test fake defining one of
   these four methods, anywhere under `tests/`) must itself declare a `**kwargs`
   catch-all — so the NEXT new keyword does not silently stop reaching it the way #229
   found batching does, and does not fail its own test with an unrelated `TypeError`.
   A signature-binding check (`inspect.signature(...).bind`) on each fake, using the
   real call shape, catches a positional/keyword-only mismatch a bare name check would
   miss.

Scope: this derives from and checks call sites and fakes IN THIS REPO. An out-of-repo
judge (the only thing a Python-level check here cannot reach) is what the strata-evals
mechanical suite (releasing.md item 12) and strata-evals #32's fakes sweep cover.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

from strata.scope_manager import ScopeManager

_REPO_ROOT = Path(__file__).parent.parent
_SRC = _REPO_ROOT / "src" / "strata"
_TESTS = _REPO_ROOT / "tests"

_JUDGE_METHODS = ("judge", "judge_batch", "judge_publication", "judge_bootstrap_publication")


def _derive_call_kwargs(source: str, method_name: str) -> set[str]:
    """Every keyword `scope_manager.<method_name>(...)` is called with, in *source*.

    Handles both a literal `name=value` keyword AND a `**some_dict` spread by
    tracing, within the SAME enclosing function, every `some_dict["key"] = ...`
    assignment — the pattern `strata.app` uses for `acted_on_target` and
    `examined_context` (conditionally built, then spread in).
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        dict_keys: dict[str, set[str]] = {}
        for node in ast.walk(func):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)
                    ):
                        dict_keys.setdefault(target.value.id, set()).add(target.slice.value)
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == method_name
            ):
                for kw in node.keywords:
                    if kw.arg is not None:
                        found.add(kw.arg)
                    elif isinstance(kw.value, ast.Name):
                        found |= dict_keys.get(kw.value.id, set())
    return found


def _real_call_site_kwargs() -> dict[str, set[str]]:
    app_src = (_SRC / "app.py").read_text()
    publication_src = (_SRC / "publication.py").read_text()
    return {
        "judge": _derive_call_kwargs(app_src, "judge"),
        "judge_batch": _derive_call_kwargs(app_src, "judge_batch"),
        "judge_publication": _derive_call_kwargs(publication_src, "judge_publication"),
        "judge_bootstrap_publication": _derive_call_kwargs(
            publication_src, "judge_bootstrap_publication"
        ),
    }


_CALL_SITE_KWARGS = _real_call_site_kwargs()


def test_every_method_actually_has_call_sites() -> None:
    # A guard on the deriver itself: if a method stops being called anywhere (a
    # refactor), an empty set would make every check below vacuously pass.
    for method, kwargs in _CALL_SITE_KWARGS.items():
        assert kwargs, f"no call-site keywords derived for {method} — deriver or call site moved"


@pytest.mark.parametrize("method", _JUDGE_METHODS)
def test_real_scope_manager_names_every_call_site_kwarg(method: str) -> None:
    """Every kwarg the engine passes today is a NAMED parameter on the real
    ScopeManager — never merely absorbed by **_extra. A typo on the caller side
    must still raise (missing-argument), not vanish silently."""
    sig = inspect.signature(getattr(ScopeManager, method))
    for name in _CALL_SITE_KWARGS[method]:
        assert name in sig.parameters, (
            f"ScopeManager.{method} has no parameter named {name!r}, only {sorted(sig.parameters)}"
        )
        assert sig.parameters[name].kind != inspect.Parameter.VAR_KEYWORD


def test_real_scope_manager_methods_ignore_one_unknown_future_kwarg() -> None:
    """**_extra actually swallows a name the engine does not pass yet."""
    for method in _JUDGE_METHODS:
        sig = inspect.signature(getattr(ScopeManager, method))
        var_keyword = [
            p for p in sig.parameters.values() if p.kind == inspect.Parameter.VAR_KEYWORD
        ]
        assert var_keyword, f"ScopeManager.{method} has no **_extra catch-all"


# --- every in-repo fake standing in for ScopeManager -----------------------------------


def _discover_fakes() -> list[tuple[str, str, str]]:
    """(module_path, class_name, method_name) for every class under tests/ that
    defines one of the four judge methods — scanned, not hand-typed, so a new
    test file adding a fake is picked up automatically."""
    found: list[tuple[str, str, str]] = []
    for path in sorted(_TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text())
        # Top-level classes only (tree.body, not ast.walk): a class defined
        # inside a function is not reachable as a module attribute, so it is
        # never a drop-in ScopeManager replacement any production code could
        # actually receive.
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (
                        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name in _JUDGE_METHODS
                    ):
                        found.append((f"tests.{path.stem}", node.name, item.name))
    return found


_FAKES = _discover_fakes()


def test_fakes_were_discovered() -> None:
    # A guard on the discoverer: tests/ is known (from the #231 design note) to
    # carry 9 files' worth of fakes across ~20 classes.
    assert len(_FAKES) >= 20, f"only found {len(_FAKES)} fake judge methods — discoverer broke"


@pytest.mark.parametrize("module_path,class_name,method_name", _FAKES)
def test_fake_has_a_var_keyword_catch_all(
    module_path: str, class_name: str, method_name: str
) -> None:
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    method = getattr(cls, method_name)
    sig = inspect.signature(method)
    var_keyword = [p for p in sig.parameters.values() if p.kind == inspect.Parameter.VAR_KEYWORD]
    assert var_keyword, (
        f"{module_path}.{class_name}.{method_name} has no **kwargs catch-all — "
        "the next new judge keyword will break it exactly like #229 found batching"
    )


@pytest.mark.parametrize("module_path,class_name,method_name", _FAKES)
def test_fake_binds_the_real_call_shape(
    module_path: str, class_name: str, method_name: str
) -> None:
    """Signature-binding (not calling) the fake with the REAL method's required
    parameters plus every derived call-site kwarg — dummy values throughout,
    since `bind` only checks parameter names/kinds, never types. Catches a
    positional/keyword-only mismatch a bare name check would miss."""
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    method = getattr(cls, method_name)

    real_sig = inspect.signature(getattr(ScopeManager, method_name))
    required = {
        name
        for name, param in real_sig.parameters.items()
        if name != "self"
        and param.default is inspect.Parameter.empty
        and param.kind != inspect.Parameter.VAR_KEYWORD
    }
    dummy_kwargs = {name: None for name in required | _CALL_SITE_KWARGS[method_name]}

    fake_sig = inspect.signature(method)
    fake_sig.bind(object(), **dummy_kwargs)
