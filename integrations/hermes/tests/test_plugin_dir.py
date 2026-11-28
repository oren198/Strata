"""The directory plugin (``integrations/hermes/plugin/strata``) as Hermes 0.23.3 loads it.

Offline: the Hermes ABC is the verbatim double in ``fake_hermes``; the loader mimics
``plugins/memory/__init__.py`` (synthetic module name, relative imports only).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from plugin_loader import PLUGIN_DIR, USER_NAMESPACE, load_plugin

pytest_plugins: list[str] = []


def _manifest() -> dict:
    import yaml  # noqa: PLC0415

    return yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))


# -- loading ------------------------------------------------------------------------------------


def test_the_directory_is_named_strata_and_has_the_files_a_catalog_entry_needs() -> None:
    assert PLUGIN_DIR.name == "strata"
    for name in ("__init__.py", "plugin.yaml", "README.md"):
        assert (PLUGIN_DIR / name).is_file(), name


def test_it_loads_under_the_synthetic_module_name_with_relative_imports() -> None:
    module = load_plugin()
    assert module.__name__.startswith(f"{USER_NAMESPACE}.strata__source_")
    assert "strata_hermes" not in sys.modules or module.__name__ != "strata_hermes"
    provider_cls = module.StrataMemoryProvider
    assert provider_cls.__module__.startswith(f"{USER_NAMESPACE}.strata__source_")


def test_register_hands_hermes_a_provider_named_strata() -> None:
    module = load_plugin()
    got: list[object] = []
    module.register(SimpleNamespace(register_memory_provider=got.append))
    assert len(got) == 1 and got[0].name == "strata"


def test_no_module_imports_strata_or_uses_an_absolute_self_import() -> None:
    for path in PLUGIN_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for name in names:
                top = name.split(".")[0]
                assert top not in {"strata", "strata_hermes"}, f"{path.name} imports {name}"


# -- plugin.yaml --------------------------------------------------------------------------------


def test_manifest_name_floor_and_no_python_dependencies() -> None:
    manifest = _manifest()
    assert manifest["name"] == "strata"
    assert manifest["requires_hermes"] == ">=0.23.3"
    assert not manifest.get("python_dependencies")
    assert not manifest.get("external_dependencies")
    assert not (PLUGIN_DIR / "pyproject.toml").exists()
    assert not (PLUGIN_DIR / "requirements.txt").exists()


def test_manifest_requires_env_declares_scope_project_skill_and_the_optional_judge_key() -> None:
    specs = {spec["name"]: spec for spec in _manifest()["requires_env"]}
    assert set(specs) == {
        "STRATA_AGENT_SCOPE",
        "STRATA_PROJECT_DIR",
        "STRATA_AGENT_SKILL",
        "STRATA_JUDGE_API_KEY",
    }
    assert specs["STRATA_JUDGE_API_KEY"].get("secret") is True
    for name in ("STRATA_AGENT_SCOPE", "STRATA_PROJECT_DIR"):
        assert specs[name].get("secret", False) is False


def test_manifest_pins_the_engine_exactly_and_the_constant_matches() -> None:
    module = load_plugin()
    assert re.fullmatch(r"strata-mem==\d+\.\d+\.\d+", module.engine.ENGINE_REQUIREMENT)


# -- config schema and save_config --------------------------------------------------------------


def test_schema_covers_scope_skill_and_project_dir_and_nothing_secret() -> None:
    provider = load_plugin().StrataMemoryProvider()
    schema = {field["key"]: field for field in provider.get_config_schema()}
    assert set(schema) == {"scope", "skill", "project_dir"}
    assert schema["scope"]["required"] is True
    assert schema["skill"]["required"] is False
    assert [f["env_var"] for f in schema.values()] == [
        "STRATA_AGENT_SCOPE",
        "STRATA_AGENT_SKILL",
        "STRATA_PROJECT_DIR",
    ]
    assert not any(field.get("secret") for field in schema.values())


def test_save_config_bootstraps_the_engine_explicitly_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_plugin()
    calls: list[dict] = []

    def fake_ensure(requirement: str, *, explicit: bool) -> Path:
        calls.append({"requirement": requirement, "explicit": explicit})
        return tmp_path / "bin" / "strata-mcp"

    monkeypatch.setattr(module.engine, "ensure_engine", fake_ensure)
    module.StrataMemoryProvider().save_config({"scope": "g_x"}, str(tmp_path))
    assert calls == [{"requirement": module.engine.ENGINE_REQUIREMENT, "explicit": True}]
    assert str(tmp_path / "bin" / "strata-mcp") in capsys.readouterr().out

    def boom(requirement: str, *, explicit: bool) -> Path:
        raise module.engine.EngineUnavailable("no network")

    monkeypatch.setattr(module.engine, "ensure_engine", boom)
    module.StrataMemoryProvider().save_config({"scope": "g_x"}, str(tmp_path))  # must not raise
    assert "no network" in capsys.readouterr().out


def test_get_status_config_reports_the_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_x")
    monkeypatch.setenv("STRATA_AGENT_SKILL", "s")
    status = load_plugin().StrataMemoryProvider().get_status_config({})
    assert status["scope"] == "g_x" and status["skill"] == "s"


# -- engine resolution --------------------------------------------------------------------------


@pytest.fixture
def plugin(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    for name in ("STRATA_MCP_COMMAND", "STRATA_AGENT_SCOPE", "STRATA_ENGINE_REQUIREMENT"):
        monkeypatch.delenv(name, raising=False)
    return load_plugin()


def test_an_explicit_command_wins_over_the_pm_tool(plugin, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    exe = tmp_path / "my-strata-mcp"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("STRATA_MCP_COMMAND", str(exe))
    monkeypatch.setattr(plugin.engine, "installed_engine", lambda: tmp_path / "pm" / "strata-mcp")
    provider = plugin.StrataMemoryProvider()
    assert provider._resolve_command({"command": str(exe)}) == [str(exe)]  # noqa: SLF001


def test_the_pm_tool_path_is_used_when_no_command_is_set(plugin, monkeypatch, tmp_path) -> None:  # noqa: ANN001
    pm_tool = tmp_path / "pm" / "strata-mcp"
    monkeypatch.setattr(plugin.engine, "installed_engine", lambda: pm_tool)
    provider = plugin.StrataMemoryProvider()
    assert provider._resolve_command({"command": ""}) == [str(pm_tool)]  # noqa: SLF001


def test_initialize_installs_lazily_non_explicitly_when_the_engine_is_missing(
    plugin, monkeypatch, tmp_path, hermes_home
) -> None:  # noqa: ANN001
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_x")
    monkeypatch.setattr(plugin.engine, "installed_engine", lambda: None)
    seen: list[bool] = []

    def lazy(requirement: str, *, explicit: bool) -> Path:
        seen.append(explicit)
        raise plugin.engine.EngineUnavailable("lazy installs are off")

    monkeypatch.setattr(plugin.engine, "ensure_engine", lazy)
    provider = plugin.StrataMemoryProvider()
    provider.initialize("sess", hermes_home=str(hermes_home))
    assert seen == [False]
    assert provider._init_error is not None  # noqa: SLF001
    assert "hermes memory setup strata" in provider._init_error.message  # noqa: SLF001


def test_is_available_never_installs_and_never_hangs(plugin, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_x")
    monkeypatch.setattr(plugin.engine, "installed_engine", lambda: None)
    monkeypatch.setattr(
        plugin.engine,
        "ensure_engine",
        lambda *a, **k: pytest.fail("is_available must not install"),
    )
    monkeypatch.setattr(plugin.engine, "lazy_install_allowed", lambda: True)
    assert plugin.StrataMemoryProvider().is_available() is True
    monkeypatch.setattr(plugin.engine, "lazy_install_allowed", lambda: False)
    provider = plugin.StrataMemoryProvider()
    assert provider.is_available() is False
    assert "hermes memory setup strata" in provider.unavailable_reason()


def test_unbound_is_unavailable_with_the_scope_reason(plugin) -> None:  # noqa: ANN001
    provider = plugin.StrataMemoryProvider()
    assert provider.is_available() is False
    assert "STRATA_AGENT_SCOPE" in provider.unavailable_reason()


def test_the_judge_key_goes_only_into_the_engine_subprocess_env(
    plugin, monkeypatch, tmp_path, hermes_home
) -> None:  # noqa: ANN001
    exe = tmp_path / "strata-mcp"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_x")
    monkeypatch.setenv("STRATA_MCP_COMMAND", str(exe))
    monkeypatch.setenv("STRATA_JUDGE_API_KEY", "sk-test-judge")
    captured: dict = {}

    class FakeClient:
        def __init__(self, command, env, *, cwd=None, **_):  # noqa: ANN001, ANN003
            captured.update(command=command, env=env, cwd=cwd)

        def start(self) -> None: ...
        def close(self) -> None: ...
        def is_up(self) -> bool:
            return True

        def call_tool(self, name, args=None, **_):  # noqa: ANN001, ANN003, ANN202
            return {"scope_id": "g_x", "layers": []}

    monkeypatch.setattr(plugin.provider_module, "StrataClient", FakeClient)
    provider = plugin.StrataMemoryProvider()
    provider.initialize("sess", hermes_home=str(hermes_home))
    assert captured["env"]["JUDGE_API_KEY"] == "sk-test-judge"
    assert "sk-test-judge" not in repr(provider.identity_signature())
    assert "sk-test-judge" not in provider.handle_tool_call("strata_status", {})


def test_the_engine_pin_equals_the_release_version() -> None:
    import tomllib  # noqa: PLC0415

    root = Path(__file__).parents[3] / "pyproject.toml"
    version = tomllib.loads(root.read_text(encoding="utf-8"))["project"]["version"]
    assert f"strata-mem=={version}" == load_plugin().engine.ENGINE_REQUIREMENT


def test_status_config_tells_the_user_what_to_run_when_the_engine_is_missing(
    plugin, monkeypatch, tmp_path
) -> None:  # noqa: ANN001
    """Hermes never calls ``unavailable_reason``; ``memory status`` prints this block instead."""
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_x")
    monkeypatch.setattr(plugin.engine, "installed_engine", lambda: None)
    monkeypatch.setattr(plugin.engine, "lazy_install_allowed", lambda: False)
    status = plugin.StrataMemoryProvider().get_status_config({})
    assert "hermes memory setup strata" in status["engine"]
    monkeypatch.setattr(plugin.engine, "installed_engine", lambda: tmp_path / "strata-mcp")
    assert (
        str(tmp_path / "strata-mcp")
        in plugin.StrataMemoryProvider().get_status_config({})["engine"]
    )
    monkeypatch.setattr(plugin.engine, "installed_engine", lambda: None)
    monkeypatch.setattr(plugin.engine, "lazy_install_allowed", lambda: True)
    assert "first chat" in plugin.StrataMemoryProvider().get_status_config({})["engine"]


_POLLUTION = {
    "STRATA_AGENT_SKILL": "another-profiles-skill",
    "STRATA_AGENT_SESSION_ID": "sess_other",
    "STRATA_PROJECT_DIR": "/elsewhere",
    "STRATA_JUDGE_API_KEY": "sk-launch-strata-judge",
    "JUDGE_API_KEY": "sk-launch-judge",
    "ANTHROPIC_API_KEY": "sk-launch-anthropic",
    "STRATA_ANTHROPIC_API_KEY": "sk-launch-strata-anthropic",
}


def test_the_engine_env_does_not_inherit_the_launch_profiles_strata_or_judge_settings(
    plugin, monkeypatch, tmp_path, hermes_home
) -> None:  # noqa: ANN001
    exe = tmp_path / "strata-mcp"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    for name, value in _POLLUTION.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_x")
    monkeypatch.setenv("STRATA_MCP_COMMAND", str(exe))
    monkeypatch.setenv("PATH", "/usr/bin")
    # this profile binds a scope but NO skill, no project and no judge key of its own
    monkeypatch.setattr(
        plugin.provider_module,
        "_read",
        lambda n, home=None: {"STRATA_AGENT_SCOPE": "g_x", "STRATA_MCP_COMMAND": str(exe)}.get(
            n, ""
        ),
    )
    captured: dict = {}

    class FakeClient:
        def __init__(self, command, env, *, cwd=None, **_):  # noqa: ANN001, ANN003
            captured.update(env=env, cwd=cwd)

        def start(self) -> None: ...
        def close(self) -> None: ...
        def is_up(self) -> bool:
            return True

        def call_tool(self, name, args=None, **_):  # noqa: ANN001, ANN003, ANN202
            return {"scope_id": "g_x", "layers": []}

    monkeypatch.setattr(plugin.provider_module, "StrataClient", FakeClient)
    plugin.StrataMemoryProvider().initialize("sess", hermes_home=str(hermes_home))
    env = captured["env"]
    assert env["STRATA_AGENT_SCOPE"] == "g_x"
    for name in _POLLUTION:
        assert name not in env, f"{name} leaked from the launch environ into the engine"
    assert env["PATH"] == "/usr/bin"
    assert captured["cwd"] is None


def test_post_setup_bootstraps_activates_and_never_prompts(
    plugin, monkeypatch, tmp_path, capsys
) -> None:  # noqa: ANN001
    import builtins  # noqa: PLC0415
    import types  # noqa: PLC0415

    monkeypatch.setattr(builtins, "input", lambda *a: pytest.fail("post_setup must not prompt"))
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_x")
    monkeypatch.delenv("STRATA_PROJECT_DIR", raising=False)
    saved: list[dict] = []
    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.save_config = lambda cfg, *a, **k: saved.append(cfg)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)
    seen: list[bool] = []
    monkeypatch.setattr(
        plugin.engine,
        "ensure_engine",
        lambda requirement, *, explicit: seen.append(explicit) or tmp_path / "strata-mcp",
    )
    config: dict = {"memory": {}}
    plugin.StrataMemoryProvider().post_setup(str(tmp_path), config)
    out = capsys.readouterr().out
    assert seen == [True]
    assert config["memory"]["provider"] == "strata" and saved == [config]
    assert str(tmp_path / "strata-mcp") in out
    assert "STRATA_PROJECT_DIR" in out and "STRATA_AGENT_SCOPE" not in out.split("Missing")[-1]


def test_post_setup_reports_a_failed_bootstrap_and_still_activates(
    plugin, monkeypatch, tmp_path, capsys
) -> None:  # noqa: ANN001
    import types  # noqa: PLC0415

    saved: list[dict] = []
    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.save_config = lambda cfg, *a, **k: saved.append(cfg)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)

    def refuse(requirement: str, *, explicit: bool) -> Path:
        raise plugin.engine.EngineUnavailable("offline")

    monkeypatch.setattr(plugin.engine, "ensure_engine", refuse)
    config: dict = {}
    plugin.StrataMemoryProvider().post_setup(str(tmp_path), config)
    assert "offline" in capsys.readouterr().out
    assert config["memory"]["provider"] == "strata" and saved


_PYTHON_ENV = {
    "PYTHONPATH": "/hermes/checkout:/hermes/venv/lib/python3.14/site-packages",
    "PYTHONHOME": "/hermes/python",
    "VIRTUAL_ENV": "/hermes/venv",
    "__HERMES_ACTIVATED": "1",
}


def test_the_engine_env_drops_the_launch_interpreters_python_environment(
    plugin, monkeypatch, tmp_path, hermes_home
) -> None:  # noqa: ANN001
    """Hermes sets PYTHONPATH to its own site-packages at startup (pm/environments.py); an engine
    that inherits it imports Hermes's mcp 2.3.0 instead of its own and dies on import."""
    exe = tmp_path / "strata-mcp"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    for name, value in _PYTHON_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("STRATA_AGENT_SCOPE", "g_x")
    monkeypatch.setenv("STRATA_MCP_COMMAND", str(exe))
    captured: dict = {}

    class FakeClient:
        def __init__(self, command, env, *, cwd=None, **_):  # noqa: ANN001, ANN003
            captured["env"] = env

        def start(self) -> None: ...
        def close(self) -> None: ...
        def is_up(self) -> bool:
            return True

        def call_tool(self, name, args=None, **_):  # noqa: ANN001, ANN003, ANN202
            return {"scope_id": "g_x", "layers": []}

    monkeypatch.setattr(plugin.provider_module, "StrataClient", FakeClient)
    plugin.StrataMemoryProvider().initialize("sess", hermes_home=str(hermes_home))
    for name in _PYTHON_ENV:
        assert name not in captured["env"], f"{name} reached the engine"
    assert "PATH" in captured["env"]  # the rest of the environment is kept
