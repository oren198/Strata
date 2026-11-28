"""Run by ``test_mcp_client_versions.py`` inside a CLIENT venv (mcp 2.3.0 or 1.29.x), with no
Strata installed there. argv: <tests dir> <project dir> <strata-mcp path>."""

from __future__ import annotations

import json
import os
import sys
from importlib.metadata import version

tests, project, server = sys.argv[1:4]
sys.path[:0] = [tests, os.path.join(tests, "fake_hermes")]
from plugin_loader import load_plugin  # noqa: E402

client_mod = load_plugin().client
client = client_mod.StrataClient(
    [server],
    {"PATH": os.environ["PATH"], "STRATA_AGENT_SCOPE": "g_probe", "STRATA_AGENT_SKILL": "probe"},
    cwd=project,
)
client.start()
out: dict = {"mcp": version("mcp"), "up": client.is_up()}
perspective = client.call_tool("strata_read_perspective", {})
out["scope_id"] = perspective.get("scope_id")
try:
    client.call_tool("strata_no_such_tool", {})
    out["unknown_tool"] = "no error"
except client_mod.StrataError as exc:
    out["unknown_tool"] = exc.code
client.close()
print("PROBE " + json.dumps(out))
