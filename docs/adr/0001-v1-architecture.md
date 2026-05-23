# ADR 0001 — V1 Architecture

**Status:** Accepted
**Date:** 2026-05-23

---

## Context

The conceptual model is locked (see `CONTEXT.md`): a scope hierarchy structured
by strata, agents as `(session, skill, scope)` triples, contributions
gate-kept by scope-managers, and reads as provenance-preserving
**perspectives**. We now need a V1 software shape that exercises that model
end-to-end with minimal ceremony.

Requirements that shape V1:

- **Multi-session is first-class.** The user must be able to run multiple
  Claude Code sessions concurrently (CEO, Architect, Developer) against a
  single shared Strata instance — this is the minimum demo, not a future
  feature.
- **Claude Code is the agent runtime.** Strata does not bring its own agent
  application; it integrates with Claude Code via plugins (tools) and skills
  (specializations).
- **A console UI exists** as a viewer prototype, intended to grow into a
  command-and-control surface. It must not couple to storage; it is just
  another client.

## Decision

V1 has four parts:

### 1. Python backend (local, single-process)

A Python service running on the user's machine, owning all storage. Single
writer; single source of truth.

- **Storage.** SQLite for the **record** (append-only contributions, all
  judgments, scope/edge config) plus markdown-on-disk for **scope summaries**
  (one file per scope — human-readable, diff-friendly, debuggable).
- **API.** HTTP (REST). Surface is small in V1: contribute, read perspective,
  read scope summary, list scopes, read record (for debugging).

### 2. Claude Code plugin + skills

Strata ships as a Claude Code plugin providing:

- **Tools** the agent (CC session) calls: `strata.contribute`,
  `strata.read_perspective`, `strata.read_scope_summary`, ... — thin RPC
  wrappers over the backend API.
- **Skills** that specialize a CC session for a role: e.g. `architect`,
  `developer`, `ceo`. Each skill specifies its `(skill, scope)` binding at
  session start; the session declares this to the backend on first call.

### 3. Scope-manager runtime — backend-spawned

When a contribution arrives at the backend, the backend invokes the Anthropic
API directly with:

- the `scope-manager` prompt (a Python-side template),
- the scope's current state (summary + relevant slice of record),
- the new contribution.

The model classifies/declines and (if accepted) emits the updated summary.
The backend persists both the contribution and the resulting summary.

The scope-manager is conceptually still an **agent** — its session is the
API call, its skill is the prompt template, its scope is fixed by which
scope's contribution it's processing. It just isn't hosted in Claude Code.

### 4. Strata Console UI

A separate frontend (the prototype in `Strata Console.html` + JSX modules).
**Never talks to storage directly** — it is a client of the backend API,
same as the CC plugin.

V1 scope:

- **Read:** scope tree, scope summaries, memory items, record (for forensic
  views).
- **Write:** fleet configuration only (add scope, add edge, etc.). **No
  direct memory writes.** Any UI-driven contribution flows through
  `strata.contribute`, which is gated by the scope-manager — bypassing it
  would defeat the authority model.

The C&C ambitions (drive sessions, ratify, retire, manager-override) are V2.

## Alternatives Considered

- **Library embedded in each agent application.** Rejected: doesn't
  naturally support multiple concurrent agents reading the same state
  without a separate synchronization layer. The local backend gives us
  multi-session for free.
- **Production-shaped service from day one** (auth, multi-tenant,
  deployment story). Rejected: enormous surface area for V1. Local backend
  is the right granularity to prove the model; promotion to multi-host is
  mostly a deployment exercise once the API and storage are separated
  (which they already are).
- **Scope-manager as a CC sub-agent spawned by the contributing session.**
  Rejected: works for contributions originating in CC, but UI-initiated
  contributions have no CC session to spawn from, requiring a backend
  scope-manager *anyway*. Two manager runtimes is the asymmetry we
  specifically want to avoid.
- **Pure spec / protocol without reference implementation.** Rejected: V1
  must demonstrate the model in motion. Paper is not enough.

## Consequences

- The "agent" abstraction has **two concrete runtimes** in V1: user-driven
  CC sessions (workers) and backend-driven Anthropic API calls
  (scope-managers). The conceptual model is preserved; the hosting differs.
- All durable state lives in **one process**. CC plugin and UI are both
  clients. Promoting to multi-host (V2+) is bounded — the seam already
  exists at the API.
- LLM-calling code lives at **one site** in V1: the scope-manager runtime
  inside the backend. CC plugin tools are thin RPC wrappers; they don't
  call models themselves.
- The UI's existing vocabulary (`group`) diverges from the glossary
  (`scope`). The implementation port should reconcile this — V1 work, not a
  separate ADR.
- Direct memory edits in the current UI prototype (`add_memory`,
  `update_memory`, `remove_memory`) **cannot survive** the backend port
  unchanged; any UI write that affects scope memory must go through
  `strata.contribute`. UI-side CRUD on fleet config (scopes, edges) is fine
  to keep.
- Human-in-the-loop curation — a CC session taking over a scope's
  manager role — is explicitly out of scope for V1; the hybrid model will
  get its own ADR when we build it.
