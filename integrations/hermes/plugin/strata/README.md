# Strata memory provider for Hermes Agent

Gives a Hermes profile the shared, judged memory of a Strata scope: each turn reads the scope's
perspective from a cache, and the agent writes back only through the `strata_contribute` tool, which
a Strata judge reviews. Nothing from the transcript is copied into memory automatically.

Tested on: Hermes Agent v0.23.3, Linux/WSL2, a named profile, Strata 1.22.0. Other combinations are
untested.

## Install

You need a registered Strata project first (`strata register` in the project folder).

    hermes -p <profile> plugins install <this plugin's address>

Answer the prompts (scope, project folder, optional skill, optional judge key) and `y` to "Use
'strata' as the memory provider now?". The Strata engine (`strata-mem`, pinned to the exact version
this plugin was tested with) is installed the first time the profile starts a chat, by Hermes's own
package manager into a side environment; nothing is added to Hermes's environment. If your Hermes has
`security.allow_lazy_installs: false`, the provider says so and `hermes -p <profile> memory setup
strata` installs the engine (a second command).

## What it does on your machine (disclosure)

- **A subprocess:** the engine, `strata-mcp`, started by this plugin when a chat starts, stopped when
  it ends. The engine keeps its store in the Strata project folder you chose, never in Hermes's home.
- **Network:** the engine sends each contribution to the judge provider named in the Strata project's
  settings (a model API). The first chat downloads `strata-mem` from the package index Hermes's
  package manager uses. This plugin makes no other network calls.
- **The judge key** lives either in the Strata project's `.env` (only `strata-mcp` reads it), or, if
  you gave one at install, in the profile's `.env` (read through Hermes's secret scope and passed only
  into the engine process). The plugin never reads `.env` files itself.
- **Reads outside the plugin's own data:** none beyond the Strata project folder the engine opens.
- No telemetry.

## Limits

- No write-back nudge: the agent contributes only when it decides to.
- One gateway serves one Strata session.
- `plugins.isolation: host` is untested here until reported otherwise.
