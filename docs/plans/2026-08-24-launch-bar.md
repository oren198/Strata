# The all-local launch bar

_2026-08-24. What must be true before Strata's first public push, in order,
with what's done and what remains. Scope: this engine and its measurement
repo (strata-evals). No launch date implied — the bar is about robustness._

## The bar

1. **Claude Code integration, excellent** — including reliable session
   write-back: a free "nothing to record" close-out decline plus the
   turn-boundary nudge. Agents reading and never contributing is the top
   observed failure.
2. **Multiple concurrent agents on one machine** — different terminals,
   different scopes, per-session binding, concurrent write safety, no
   machine-level agent identity. The fleet demo on one laptop.
3. **A local UI for the engine.** Priority inside it: declined
   contributions visible with reasons; staleness surfaced; a browsable
   record trail; view-as with per-layer token weight; one-click operator
   supersede/retire with judgment staying automatic. The first three are
   the proof surfaces — kept screenshot-able.
4. **A quickstart that survives a hostile first run** on a clean machine.
5. **strata-evals goes open source** — a README a stranger can run to the
   gate numbers, documented thresholds, a contribution path.

Codex CLI is the second harness target after Claude Code. Rule: verify
what its hook/event surface actually supports before building or claiming
anything.

## Status

| Bar item | State |
|---|---|
| 1. Write-back | Shipped in 1.9.0 (decline valve, Stop hook, nudge). `strata doctor` (merged) catches half-wired projects. Live end-to-end check rides the hostile quickstart run below. |
| 2. Multi-agent, one machine | Done — cross-process per-scope file locks (ADR 0012), wired through the MCP server, the CLI, the freshness evaluator, and the Console backend. Two-process race test in `tests/test_interprocess_locks.py`. |
| 3. Local UI | Built — all five surfaces plus `docs/console.md` (PR #155). Merge condition: one manual browser click-through (no JS test harness exists, so the suite cannot prove the JSX renders). |
| 4. Quickstart | Pending — run after the next PyPI release, since the quickstart installs from PyPI. Every stumble becomes a code fix (preferred) or a README fix. |
| 5. strata-evals | Portability fixed and stranger docs merged (evals #24, #25). Before flipping public: rotate the leaked local bearer token, delete the two remote branches carrying infra references, then the operator flips visibility. |
| Codex | Surface verified hands-on (MCP: solid; hooks: schema-verified). `strata register/unregister --harness codex` built (PR #153). Open: the live checklist in the README's Codex section — needs real credentials. |

## Remaining work, in order

1. Merge the green PRs: #153 (Codex harness), #154 (environment/project
   separation), #155 (Console) — #155 after the browser click-through.
2. Release to PyPI: bump the version (dev now carries the locks, doctor,
   Console, and Codex harness beyond 1.9.0), merge dev → main, publish a
   GitHub release; the publish workflow does the rest. Verify the PyPI
   page renders the README.
3. Hostile quickstart run on a clean machine against the published
   package, including one real session that triggers the nudge and
   records a close-out decline.
4. Codex live verification — the five-item checklist in the README's
   Codex section, with real credentials. What passes gets claimed;
   what fails gets documented.
5. strata-evals: token rotation + branch cleanup, then public.

## Standing rules

- Everything except the UI works with no backend running; the backend
  serves the Console only. No contribution, read, or summary flow may
  come to depend on it.
- This repo's public surfaces name no consumer of the engine.
- Plain language everywhere a stranger reads.
