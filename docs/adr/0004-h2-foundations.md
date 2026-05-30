# ADR 0004 — H2 Foundations: Embedded Mode, Manager Composition, Lazy Refresh

**Status:** Proposed
**Date:** 2026-05-30
**Related:** ADR 0001 (V1 architecture), ADR 0002 (fleet config source of
truth), ADR 0003 (`strata launch` as the binding point)

---

## Context

V1.2 ships fleet.yaml as the source of truth (ADR 0002) and skill-bound
sessions via `strata launch` (ADR 0003). Three gaps remain between V1.2
and what `docs/philosophy.md` describes as a working shared-memory
system:

1. **Server on the critical path.** The MCP server today proxies HTTP to
   the FastAPI backend (`mcp_server/strata_mcp.py:174`); the CLI relies
   on the backend implicitly. If the backend stops, agent contributions
   silently fail and the system effectively halts. The dogfooding
   wound.
2. **Perspective composition is stubbed.**
   `strata_read_perspective` (`mcp_server/strata_mcp.py:189`) returns
   the scope's own summary with a `_v1_limitation` note saying
   "perspective composition across ancestor scopes is post-V1." The
   API surface was stabilised in V1.1 so skill prompts wouldn't have
   to change — but the semantic is empty.
3. **Manager is single-scope blind.** The scope-manager LLM
   (`src/strata/scope_manager.py:162`) sees only its own scope's
   current summary + recent contributions. It cannot incorporate
   ancestor directives or context; its system prompt mentions
   binding-on-descendants but operationally it has no way to know
   what's bound from above.

The philosophy specifies the semantic shape: composition is read-time
(Concept 4), strict inheritance up the chain (Concept 2), directives
flow down / context flows up (Concept 5), record-vs-working-memory
separation (Concept 7). The engineering question is how to realise
this without inventing a cascade-refresh substrate, a write-time
fan-out, or an event bus.

---

## Decision

Five coupled decisions land together. They form one architectural
shape; splitting them produces partial designs that don't add up.

### 1. Embedded mode — the server is for the UI only

The system operates directly on the SQLite record store and on-disk
summary files. The FastAPI server (`src/strata/app.py`) is the UI
access layer and nothing more.

**Concrete:**

- **MCP server** (`mcp_server/strata_mcp.py`) — import the `strata`
  package and operate on `RecordStore`, `SummaryStore`, and
  `ScopeManager` in-process. Drop the `STRATA_BACKEND_URL` HTTP
  dependency. Rely on SQLite WAL mode for concurrent-write safety.
- **CLI subcommands** (`strata contribute`, `strata summary`,
  `strata launch`) — already operate directly on the stores. Verified,
  no change.
- **`strata start`** — keeps starting the FastAPI server. Running it
  gives you the Console UI; not running it does not interrupt the
  agent loop. Documented as optional.

The system is now resilient to server lifecycle: backend crash → only
the UI is unreachable; agents keep contributing, managers keep
summarising, perspectives keep reading.

### 2. Manager input expands to include parent's summary

The scope-manager at scope X reads its parent scope's current summary
as input when judging a new contribution, so it can write a summary
that is consistent with what is already bound from above.

**Concrete:**

- `src/strata/scope_manager.py:_build_user_message` gains a
  `parent_summary: ScopeSummary | None` parameter and renders it
  into the user message under a clearly-labelled "PARENT SCOPE
  SUMMARY (inherited)" section.
- `ScopeManager.judge` resolves the parent scope from the fleet
  config and fetches its current summary before the LLM call.
- L0 (root stratum) scopes have no parent; `parent_summary=None`.
  The render is omitted.

The manager remains a single LLM call per contribution. The user
message grows by one bounded summary's worth of tokens.

### 3. Perspective composition fills in the stub

`read_perspective` returns the layered stack: the scope's own summary
plus each ancestor scope's summary up to the root, with provenance
preserved per layer.

**Concrete:**

- `mcp_server/strata_mcp.py:strata_read_perspective` — drop the
  `_v1_limitation` note. Walk from the requested scope up the
  `edges` graph to the root, collecting each ancestor's
  `ScopeSummary`. Return `{layers: [{scope_id, summary, ...}], ...}`
  ordered root-first.
- The composition is read-time and lossless. The reader sees layers
  labelled by scope; precedence (Concept 5) is presented as
  structure, not pre-resolved.
- `read_perspective` becomes the agent's **default read**. Skill
  prompts (`.claude/skills/strata-worker/`) updated to call
  `read_perspective` (not `read_scope_summary`) as the first action
  in any session.

### 4. Lazy refresh via pre-session hook

The manager's summary regeneration is triggered by `strata launch
<scope>`. There is no cascade refresh, no event bus, no scheduler.

**Concrete:**

- `cmd_launch` (`src/strata/__main__.py`) — after preflight, before
  `execvp`, run a manager-refresh step for the launched scope:
  1. If my parent's summary is older than the parent-summary version
     I last incorporated, refresh parent first (recursion bottoms out
     at L0).
  2. Fetch raw contributions at my scope since my last refresh.
  3. Call `ScopeManager.judge` once, with parent's current summary as
     input.
  4. Write the new summary to disk.
- Concurrent launches at the same scope: **last-write-wins**, no
  lock. The manager is approximately-deterministic given the same
  inputs; two near-simultaneous refreshes are wasteful but not
  incorrect.
- Each summary records the parent-summary version it was built from
  (an integer per scope), so staleness is detectable without
  re-running the LLM.

Cost: one LLM call latency at session start per stale ancestor. A
warm chain refreshes in milliseconds (no LLM call); a cold chain
costs ~5–10s per stale scope. Acceptable as a one-time briefing per
session.

### 5. Bounded summary via prompt parameter

The manager prompt instructs the LLM to produce a summary of at most
N words, configurable via `STRATA_SUMMARY_MAX_WORDS` (default 500).
The LLM enforces the budget as part of summarisation. No post-hoc
trimming, no token-budget walker.

**Concrete:**

- `src/strata/settings.py` — add `summary_max_words: int = 500`.
- `src/strata/scope_manager.py:_build_user_message` — render
  "BUDGET: your rewritten summary must be at most {N} words." into
  the user message.
- The implicit rule: when the manager must trim, directives never
  get cut below visibility; the context section absorbs the squeeze.
  Stated in the system prompt, not enforced post-hoc.

---

## Alternatives Considered

- **Cascade refresh on ancestor changes** — when scope X's parent
  updates, immediately re-summarise X and all its descendants.
  Rejected: cascade introduces ordering, idempotence, and
  in-flight-conflict semantics with no operational payoff (mid-session
  ancestor updates are rare; pre-session refresh catches them on next
  launch).
- **Pre-emptive perspective materialisation** — fan out contributions
  to every visible scope at write time. Rejected: contradicts
  philosophy ("composed at the moment of reading," Concept 4) and
  requires a re-materialisation pipeline for every topology change.
- **Read-time perspective resolution (resolve precedence to a flat
  view)** — present one merged document instead of layers. Rejected:
  contradicts philosophy ("merging destroys the information about
  where each piece came from," Concept 4). Layered with provenance is
  what the agent gets.
- **Server-mediated MCP** — keep the HTTP proxy in MCP. Rejected:
  one network hop too many for a sidecar; introduces the failure
  class this ADR's Decision 1 closes.
- **Token-budget walker enforcing the summary cap post-hoc** —
  rejected: trusts the model less but adds a whole subsystem.
  Prompt-parameter is simpler and the LLM is already good at length
  constraints.

---

## Consequences

**Positive:**

- The server-down failure class is closed. The system runs without
  the backend; only the UI requires it.
- `read_perspective` becomes real; agents see their full inherited
  context as one tool call.
- Reads remain O(ancestor-depth) summary fetches with no graph logic.
- Composition complexity lives entirely inside the manager's
  summarisation loop and the read-time stitch — no event bus, no
  scheduler, no cascade.
- The path is incremental: each decision lands as a small feature
  branch.

**Negative:**

- The MCP server now requires the `strata` package importable in the
  same Python environment as the CC session's MCP runtime. The
  `strata init` follow-up (V1.2.1) configures this.
- Manager LLM cost grows per judgment (user message includes
  parent's summary). Bounded by `STRATA_SUMMARY_MAX_WORDS`.
- Mid-session ancestor updates are invisible to the running agent
  until they restart. Acceptable — directives are not high-frequency
  signals.
- First read of a long-idle scope chain pays sequential LLM latency
  to refresh stale ancestors. Worst case ~N × 10s for an N-deep
  cold chain.

---

## Out of scope (deferred)

- **Cross-tree visibility.** Strict inheritance only (philosophy
  Concept 2). Cross-tree knowledge flow happens via manager
  ratification through a common ancestor — no peer-visibility
  primitive.
- **Trust scores / earned-trust tracking** (philosophy Concept 6).
  Provenance is captured at contribute-time; trust weighting is a
  later layer.
- **Forgetting mechanisms beyond what the manager does at write-time
  squeeze** (philosophy Concept 7 — supersession, decay, retirement
  as first-class). The manager's bounded summary already exercises
  supersession implicitly; explicit retirement APIs are deferred.
- **Pre-session UX in environments other than `strata launch`** (e.g.
  agents that re-attach to an existing session). The current decision
  binds the refresh trigger to launch; other surfaces require a
  follow-up.

---

## Execution order (infrastructure first)

The feature branches land in this order, each as its own PR off
`dev`:

1. **`feature/embedded-mode`** — Decision 1. MCP server in-process,
   `strata start` documented as UI-only. Smallest blast radius,
   removes the dogfooding wound, unblocks the next steps.
2. **`feature/perspective-composition`** — Decisions 2 + 3. Manager
   reads parent's summary; `read_perspective` walks the ancestor
   chain. Default read swaps to perspective. Skill prompts updated.
3. **`feature/manager-refresh`** — Decisions 4 + 5. Pre-session hook
   in `cmd_launch`, version stamps on summaries, prompt-parameter
   budget.

After these three land, the V1.2.1 follow-ups (`feature/strata-init`,
`feature/preflight`) resume from the paused plan and inherit the new
infrastructure — init configures MCP for in-process operation; the
pre-session hook benefits from preflight.
