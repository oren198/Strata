"""Load the plugin directory the way Hermes 0.23.3 loads a user-installed memory provider.

Hermes imports a directory provider under a synthetic module name
(``plugins/memory/__init__.py:_module_name``: ``<user namespace>.<name>__source_<digest>``) with the
directory as the package's search path, so the plugin may only use RELATIVE imports.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PLUGIN_DIR = Path(__file__).parents[1] / "plugin" / "strata"
USER_NAMESPACE = "_hermes_user_memory"


def synthetic_name(path: Path, name: str = "strata") -> str:
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    return f"{USER_NAMESPACE}.{name}__source_{digest}"


def load_plugin(path: Path = PLUGIN_DIR) -> ModuleType:
    module_name = synthetic_name(path)
    if module_name in sys.modules:
        return sys.modules[module_name]
    if USER_NAMESPACE not in sys.modules:
        sys.modules[USER_NAMESPACE] = ModuleType(USER_NAMESPACE)
        sys.modules[USER_NAMESPACE].__path__ = []  # a namespace package, as Hermes makes it
    spec = importlib.util.spec_from_file_location(
        module_name, path / "__init__.py", submodule_search_locations=[str(path)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
