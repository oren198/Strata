# strata-hermes

A Strata memory provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

**Status: measured on Hermes Agent v0.23.3 (`caafe53`) with one layout: a named profile under an
isolated `HERMES_HOME`, with its settings in the profile's own `.env`.** A Claude Code session and a
Hermes profile shared one scope; each admitted contribution was visible to the other harness
verbatim, with provenance. Other Hermes versions and layouts are not claimed. This package is not
published to PyPI. The root README's "Using Strata with Hermes Agent" section is the setup guide;
this file describes what the code does. The unit tests run against a verbatim copy of Hermes's
`MemoryProvider` base class (v0.23.3) and a real `strata-mcp` server with a scripted judge.

## What it does

- **Reads** your scope's Strata perspective (the directives that bind you, and your scope's
  context) and gives it to Hermes through the provider's recall hooks (`queue_prefetch` /
  `prefetch`). Recall is served from a cache that a background thread refreshes, so a turn never
  waits on the Strata server.
- **Writes only through tools the agent calls.** The agent gets `strata_read_perspective`,
  `strata_contribute`, `strata_session_closeout`, `strata_rejudge` and a read-only `strata_status`.
  Every contribution goes through Strata's judge, which may decline it.
- **Does not copy the transcript into memory.** `sync_turn`, `on_session_end`, `on_memory_write`
  and the other lifecycle hooks are Hermes's own no-ops here, on purpose: automatic transcript sync
  would bypass the judge. Hermes's built-in memory (`MEMORY.md`, `USER.md`) is never touched.
- **Has no bind tool.** The scope and skill come from the profile's environment (below), never from
  the agent.

## Install and set up

Tested only in a Hermes environment built with `uv sync` (no `[all]` extra, mcp 1.29.1). A
standard Hermes install (`[all]`) pins mcp 2.3.0, which conflicts with strata-mem's mcp<1.30, so
the install command below fails there. A one-step install for standard Hermes is planned.

Install `strata-hermes` into the environment Hermes runs in. It depends on `strata-mem>=1.20`
(which ships `strata-mcp`). The tested command, from a checkout of the Strata repository, is
`uv pip install --python <hermes venv>/bin/python -e . -e integrations/hermes`. Hermes finds a pip-installed provider through the
`hermes_agent.memory_providers` entry point; the provider's name is `strata`.

Then select it, with `hermes memory setup` or in the profile's config:

```yaml
memory:
  provider: strata
```

Set these in **each profile's own `.env`** (one Hermes profile is one Strata agent). For a named
profile Hermes keeps that file in the profile's secret scope, and the provider reads it from there
(`agent.secret_scope.get_secret`); `os.environ` is the launch profile's and is not consulted for a
named profile:

| Variable | Meaning |
|---|---|
| `STRATA_AGENT_SCOPE` | The Strata scope this profile acts as. Required. |
| `STRATA_AGENT_SKILL` | The skill it acts as, when the scope has permitted skills. |
| `STRATA_PROJECT_DIR` | A registered Strata project: the folder that holds `.strata/config.toml` (create it with `strata register`). `strata-mcp` is started there and reads the fleet and the stores from it. |
| `STRATA_MCP_COMMAND` | How to start the server. Default `strata-mcp`. |

Leave `STRATA_AGENT_SESSION_ID` unset: the server derives the session from the Hermes process.
The provider never sets it, and removes it from the server's environment if it is present.

## When something is wrong

Tool calls never raise into Hermes. They return JSON with an `error` and a stable `code`, and the
message always says that Hermes's built-in memory is untouched.

| `code` | Cause | What to do |
|---|---|---|
| `strata_unreachable` | The `strata-mcp` command was not found, did not start, or stopped answering. | Install `strata-mem`, or set `STRATA_MCP_COMMAND` and `STRATA_PROJECT_DIR`. |
| `strata_unbound` | No scope is set, or Strata does not know the scope (or the skill is not permitted for it). | Set `STRATA_AGENT_SCOPE` (and `STRATA_AGENT_SKILL`) to a scope in the project's `fleet.yaml`. |
| `strata_error` | Strata answered with an error. | Read the message; it is Strata's own. |

While the provider is unavailable, `is_available()` is false and `unavailable_reason()` says why.
If the judge is unavailable when the agent contributes, Strata records the contribution unjudged:
it is not memory until it is judged, and `strata_rejudge` asks again.

## Known limits

- **No turn-end write-back nudge.** In Claude Code, Strata can stop a session that has read memory
  and written nothing and ask it to contribute. Hermes has no equivalent: its `on_session_end`
  fires after every turn and cannot block, and only `pre_verify` can block, and only on turns where
  the agent edited code. So a Hermes agent contributes when it chooses to, and the system prompt
  block says how.
- **Provider isolation.** Hermes refuses a pip-installed provider when `plugins.isolation` is
  `host` (third-party plugin code never runs inside the Hermes process). Set it to `in_process`
  to load this one.
- **Pip entry points are for owner-managed installs.** Hermes's own guide says entry-point
  discovery does not install packages and recommends a directory provider on package-manager-managed
  installs. This package is the entry-point form.
- **Chats served through one gateway are one Strata session.** Measured: two chats on one Hermes
  gateway were recorded under the same Strata session id. Whether that skews the write-back
  counters has not been tested.
- **The server needs a registered project.** `strata-mcp` reads `.strata/config.toml`; without
  `STRATA_PROJECT_DIR` pointing at one it starts unbound.

## Tests

```
cd integrations/hermes
PYTHONPATH=src:tests/fake_hermes pytest tests
```

They need `strata-mem` and `mcp` installed and do not need Hermes.
