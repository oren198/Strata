# ADR 0009 — Packaging: `mem-strata` Is the Engine, `memfleet` Is the Cloud Client

**Status:** Accepted (owner-confirmed 2026-07-17, in session; no grill — a
packaging/distribution decision, no memory-model semantics touched).
**Date:** 2026-07-17
**Related:** ADR 0005 (brownfield install — the additive-merge rules the
client reuses), #49 (the mem-strata → memfleet rename this partially
reverses), #116 (engine-side implementation), #109 (memory-freshness design
this tooling serves); strata-web#52 (platform API), strata-web#53 (client
implementation), strata-web `docs/MEMFLEET_CLIENT_PLAN.md` (implementation
plan).

---

## Context

Strata now exists in two products with different lifecycles:

- the **local memory engine** — `import strata`, the `strata` CLI, eval-gated
  releases, deliberately slow cadence;
- the **fleet cloud** (memfleet.com / strata-web) and the tooling that
  connects a developer's terminal to it — a client that must iterate in
  lockstep with the platform API and much faster than the engine.

Until now one PyPI distribution (`memfleet`, releases 1.6.0/1.6.1) shipped the
engine, and the in-terminal cloud workflow (#115) was headed into it as a
`strata register --remote` flag. Owner review sized that workflow as a full
product surface (device-flow login, session-scoped agent profiles, in-terminal
fleet management) and raised the naming question directly: the name `memfleet`
reads as the cloud, yet it shipped the local engine.

Constraints found on inspection (2026-07-17):

- PyPI `strata` is **squatted** (dormant third-party 0.0.0dev) — the engine
  cannot take the symmetric name.
- PyPI `mem-strata` is **free** — the pre-1.6 name was renamed away (#49)
  before anything was published under it.
- External adoption of `memfleet` ≤ 1.6.1 is effectively zero (first publish
  2026-07-13), so repurposing the name will never be cheaper.

## Decision

**D1 — The engine's distribution becomes `mem-strata`.** Import package
`strata`, the `strata` CLI, version numbering, and all behavior are unchanged;
only the `pip install` name moves. Next engine release (natural candidate:
the #113 judge-parse fix as 1.6.2) publishes as `mem-strata`.

**D2 — PyPI `memfleet` is repurposed as the cloud client.** Import package
`memfleet`, console script `memfleet` (`connect`, `login`, `profiles`,
management verbs). It is implemented in the **strata-web repo** (`client/`),
co-located with the platform API it speaks so client, server, and contract
tests change together. First client release is **2.0.0** — it must exceed
1.6.1 so the repurposed package's "latest" never resolves to an engine
release; the client README opens by saying releases ≤ 1.6.1 were the engine.

**D3 — The client may depend on `mem-strata`, never import `strata.*` memory
paths.** The engine exposes its additive install machinery (settings merge,
skill copy, `--diff` — the ADR 0005 rules) as a documented module boundary
(#116, e.g. `strata.install`) so there is exactly one implementation of
additive-merge semantics. The client consumes that module and the platform's
HTTP API; memory semantics stay behind the server's adapter firewall.

**D4 — Three credential planes, never crossed** (restated here because the
packaging boundary is where confusion would start): the **owner token**
(device-flow issued, manages the fleet), the **agent key** (what a session
presents; one agent ⇄ one scope, fixed at registration), and the
**enrollment code** (delegation to non-owners). Sessions bind, not machines:
locally there are only named agent profiles; the binding lives server-side.

## Alternatives rejected

- **One distribution, two namespaces** (`pipx install memfleet` ships both
  CLIs): avoids all PyPI churn, but chains every client fix to an eval-gated
  engine release and leaves "memfleet" meaning two things. Rejected for
  cadence coupling.
- **PEP 541 reclaim of PyPI `strata`**: slow, uncertain, and unnecessary —
  revisit only if the name frees up; not a dependency of anything here.
- **A third repo for the client**: maximal separation, but the client's
  contract partner is strata-web's API — a fourth repo adds coordination
  cost and removes the co-located contract tests that motivate the split.

## Consequences

- Owner one-time steps on pypi.org: register a trusted publisher for
  `mem-strata` → oren198/Strata `publish.yml` (env `pypi`); repoint the
  existing `memfleet` publisher → oren198/strata-web's client publish
  workflow.
- `memfleet` 1.6.x remains visible in the package history as engine
  releases; the 2.0 README note is the permanent explanation.
- strata-web's engine pin is a git SHA and is unaffected.
- Docs sweep in both repos (#116, strata-web#53).

---

## Amendment 2026-07-18 — engine distribution name is `strata-mem`, not `mem-strata`

PyPI rejected the `mem-strata` trusted-publisher registration with "this
project name is too similar to an existing project" — the existing project
being the `memstrata` squat (already recorded in the 2026-07-10 plan);
PyPI's similarity check treats the hyphen-only difference as confusable.
Owner picked **`strata-mem`** (verified absent on PyPI 2026-07-18) as the
replacement. Everything else in this ADR stands: import package `strata`,
`strata` CLI, `memfleet` as the cloud client, D2/D3 unchanged. Every
occurrence of `mem-strata` above reads as `strata-mem`.
