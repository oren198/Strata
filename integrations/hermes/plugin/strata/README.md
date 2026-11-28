# Strata memory provider for Hermes Agent

Gives a Hermes profile the shared, judged memory of a Strata scope. Each turn reads the scope's
perspective from a cache; the agent writes back only through the `strata_contribute` tool, which a
Strata judge reviews and may decline. Nothing from the transcript is copied into memory
automatically, and Hermes's built-in memory (`MEMORY.md`, `USER.md`) is never touched.

**Tested on:** Hermes Agent v0.23.3 (installed with Hermes's official installer), Linux/WSL2, a
named profile, Strata 1.22.0, an existing Strata project. Other combinations are untested.

## Install

You need a registered Strata project first (`strata register`, in a folder that is not the Strata
repository) whose fleet has the scope. Then:

    hermes -p <profile> plugins install https://github.com/oren198/Strata#integrations/hermes/plugin/strata

(This installs from the repository's default branch.)

Answer the prompts (scope, project folder, optional skill, optional judge key) and `y` to "Use
'strata' as the memory provider now?". The answers go into the profile's own `.env`.

The Strata engine (`strata-mem` 1.22.0, pinned exactly) is downloaded from PyPI the first time the
profile starts a chat, by Hermes's own package manager into a side environment under the profile's
Hermes home. The plugin declares no Python dependencies and
installs nothing into Hermes's environment.

If your Hermes has `security.allow_lazy_installs: false`, `hermes -p <profile> memory status` says
"engine: not installed, and lazy installs are off; run `hermes memory setup strata`", and that one
command (`hermes -p <profile> memory setup strata`) installs the engine and activates the provider.
It never prompts.

## Settings (the profile's own `.env`)

| Variable | Meaning |
|---|---|
| `STRATA_AGENT_SCOPE` | The Strata scope this profile acts as. Required. |
| `STRATA_PROJECT_DIR` | The registered project (the folder holding `.strata/config.toml`). `strata-mcp` runs there and reads the fleet and the stores from it. |
| `STRATA_AGENT_SKILL` | Optional: the skill the profile acts as. |
| `STRATA_JUDGE_API_KEY` | Optional: the judge key. Passed to the engine process only, as `JUDGE_API_KEY`. Leave it blank to keep the key in the project's `.env`. |
| `STRATA_MCP_COMMAND` | Advanced: use this `strata-mcp` instead of the Hermes-managed engine. |
| `STRATA_ENGINE_REQUIREMENT` | Advanced: a different exact engine pin (a mirror or a local build). |

The provider reads these through Hermes's secret scope (`agent.secret_scope.get_secret`), never
from a file and, for a named profile, never from the launch environment. It never sets
`STRATA_AGENT_SESSION_ID`: the engine derives the session from the Hermes process.

## What it does on your machine (disclosure)

- **A subprocess:** the engine, `strata-mcp`, started by this plugin when a chat starts and stopped
  when it ends. Its environment is the launch environment without any `STRATA_*` or judge-key
  variable, plus only this profile's scope, skill and (if given) judge key.
- **Network:** the engine sends each contribution to the judge provider named in the Strata
  project's settings (a model API). The first chat downloads `strata-mem` through Hermes's package
  manager. This plugin makes no other network calls.
- **Stored credentials:** the optional judge key in the profile's `.env`, or the key in the Strata
  project's `.env` that only the engine reads. The plugin never reads a `.env` file.
- **Files:** the engine keeps its store in the project folder you chose, never in Hermes's home.
  The engine environment lives under `<profile Hermes home>/environments/strata-mem`.
- No telemetry.

## Tools the agent gets

`strata_read_perspective`, `strata_contribute`, `strata_session_closeout`, `strata_rejudge`, and a
read-only `strata_status`. There is no bind tool: scope and skill come from the profile.

## When something is wrong

Tool calls never raise into Hermes. They return JSON with an `error` and a stable `code`; the
message says Hermes's built-in memory is untouched.

| `code` | Cause | What to do |
|---|---|---|
| `strata_unreachable` | The engine is not installed, did not start, or stopped answering. | `hermes -p <profile> memory setup strata`, or check `STRATA_MCP_COMMAND` / `STRATA_PROJECT_DIR`. |
| `strata_unbound` | No scope is set, or Strata does not know the scope (or the skill is not permitted for it). | Set `STRATA_AGENT_SCOPE` (and `STRATA_AGENT_SKILL`) to a scope in the project's `fleet.yaml`. |
| `strata_error` | Strata answered with an error. | Read the message; it is Strata's own. |

If the judge is unavailable when the agent contributes, Strata records the contribution unjudged:
it is not memory until it is judged, and `strata_rejudge` asks again.

## Known limits

- **No turn-end write-back nudge.** In Claude Code, Strata can stop a session that has read memory
  and written nothing and ask it to contribute. Hermes has no equivalent, so a Hermes agent
  contributes when it chooses to; the system prompt block says how.
- **Chats served through one gateway are one Strata session.** Two chats on one Hermes
  gateway were recorded under the same Strata session id (measured 2026-11-27). Whether that skews the write-back
  counters has not been tested.
- **The engine needs a registered project.** Without `STRATA_PROJECT_DIR` pointing at one it starts
  unbound.
- **Tested once.** Hermes v0.23.3 (standard install), Linux/WSL2, 2026-11-28; `hermes pm repair`
  and `plugins.isolation: host` were tried and worked. `hermes update` was not run; Hermes's own
  guide says the plugins directory survives it.
