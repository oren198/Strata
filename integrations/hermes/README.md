# Hermes integration

`plugin/strata/` is the Strata memory provider for Hermes Agent, as a Hermes directory plugin.
**Its README (`plugin/strata/README.md`) is the user documentation**; the root README's section
"Using Strata with Hermes Agent" is the setup guide. There is one copy of the code: this directory.
The old `strata-hermes` pip package no longer exists and was never published.

## Tests

```
cd integrations/hermes
python -m pytest tests          # needs strata-mem and mcp; does not need Hermes
STRATA_MCP_VENVS=<dir> python -m pytest tests/test_mcp_client_versions.py
```

The unit tests run against a verbatim copy of Hermes 0.23.3's `MemoryProvider` base class
(`tests/fake_hermes`), load the plugin directory the way Hermes does (a synthetic module name,
relative imports only: `tests/plugin_loader.py`), and talk to a real `strata-mcp` with a scripted
judge. `test_mcp_client_versions.py` runs the client from venvs on mcp 2.3.0 and 1.29.x (what a
standard Hermes `[all]` install pins, and what the Strata engine pins) against a real engine; it
is skipped unless `STRATA_MCP_VENVS` names a directory holding `c<mcp version>/` client venvs and an
`eng/` venv with strata-mem installed. `hermes plugins validate plugin/strata` must pass with no
warning.
