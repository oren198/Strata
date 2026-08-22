# ADR 0009 — Packaging: the engine's distribution is `strata-mem`

**Status:** Accepted (owner-confirmed 2026-07-17, in session; no grill — a
packaging/distribution decision, no memory-model semantics touched).
**Date:** 2026-07-17
**Related:** ADR 0005 (brownfield install — the additive-merge rules this
exposes), #49 (the rename this partially reverses), #116 (implementation),
#109 (memory-freshness design the install surface serves).

---

## Context

Strata's PyPI distribution name has moved twice, and this ADR settles it.

Releases 1.6.0 and 1.6.1 were published under a distribution name that is no
longer the engine's — it was reassigned to a separate project outside this
repository. Anyone holding one of those two releases has the engine; from
1.6.2 onward the engine publishes under its own name and nothing else does.

Constraints found on inspection (2026-07-17):

- PyPI `strata` is **squatted** (dormant third-party 0.0.0dev) — the engine
  cannot take the symmetric name.
- PyPI `mem-strata` was **free** — the pre-1.6 name was renamed away (#49)
  before anything was published under it.
- External adoption of the 1.6.0/1.6.1 releases is effectively zero (first
  publish 2026-07-13), so settling the name will never be cheaper.

A second question rode along: `strata register --remote` (#115) was headed
into this distribution as a flag. Owner review sized that workflow as a full
product surface — device-flow login, session-scoped agent profiles,
in-terminal fleet management — that must iterate far faster than an
eval-gated engine release. It does not belong in the engine's release
cadence, and therefore not in the engine's distribution.

## Decision

**D1 — The engine's distribution is `strata-mem`.** Import package `strata`,
the `strata` CLI, version numbering, and all behavior are unchanged; only the
`pip install` name moves. Next engine release (natural candidate: the #113
judge-parse fix as 1.6.2) publishes under it.

**D2 — Remote-server tooling ships outside this distribution.** A client that
connects a terminal to a remote Strata server iterates in lockstep with that
server's API, not with the engine's eval gate. Chaining every client fix to an
eval-gated engine release is the coupling this avoids. Such a client depends
on `strata-mem`; the engine does not depend on it, know its name, or track its
releases.

**D3 — The install machinery is a public import surface; memory paths are
not.** The engine exposes its additive install machinery (settings merge,
skill copy, `--diff` — the ADR 0005 rules) as a documented module boundary
(#116, `strata.install`) so there is exactly one implementation of
additive-merge semantics for every tool that installs Strata wiring into a
project. Consumers use that module and, where a server is involved, that
server's HTTP API. Memory semantics stay behind the engine's own primitives.

**D4 — Three credential planes, never crossed** (restated here because the
packaging boundary is where confusion would start): the **owner token**
(device-flow issued, manages the fleet), the **agent key** (what a session
presents; one agent ⇄ one scope, fixed at registration), and the **enrollment
code** (delegation to non-owners). Sessions bind, not machines: locally there
are only named agent profiles; the binding lives server-side.

## Alternatives rejected

- **One distribution, two namespaces** (one install ships both CLIs): avoids
  all PyPI churn, but chains every client fix to an eval-gated engine release
  and leaves one name meaning two things. Rejected for cadence coupling.
- **PEP 541 reclaim of PyPI `strata`**: slow, uncertain, and unnecessary —
  revisit only if the name frees up; not a dependency of anything here.

## Consequences

- Owner one-time step on pypi.org: register a trusted publisher for
  `strata-mem` → oren198/Strata `publish.yml` (env `pypi`).
- The 1.6.0/1.6.1 releases remain visible in PyPI history under the old name;
  this ADR is the permanent explanation.
- Docs sweep for the distribution name (#116).

---

## Amendment 2026-07-18 — the name is `strata-mem`, not `mem-strata`

PyPI rejected the `mem-strata` trusted-publisher registration with "this
project name is too similar to an existing project" — the existing project
being the `memstrata` squat; PyPI's similarity check treats the hyphen-only
difference as confusable. Owner picked **`strata-mem`** (verified absent on
PyPI 2026-07-18) as the replacement. Everything else in this ADR stands.
