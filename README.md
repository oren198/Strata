# Shared Memory for Agent Fleets — Theoretical Foundations

This document lays out the theory behind a shared memory system for a fleet of agents: the problem it solves, why that problem is hard, and the conceptual solution. It is deliberately implementation-agnostic. It describes ideas and concepts, not a particular system; the same foundations could be built many different ways.

---

## The problem

A fleet of agents working in isolation is wasteful. Each agent rediscovers what another already learned, repeats mistakes others already made, and acts without knowledge of decisions made elsewhere in the system. Nothing compounds.

The goal is the opposite: agents that share memory. When one agent learns something useful, others should be able to benefit from it. When a decision is made somewhere in the fleet, the agents it affects should act on it. The performance of the whole system should grow as a function of every agent's contribution, not just its own.

Two distinct capabilities sit inside that goal:

- **Shared reading** — an agent can draw on memory produced by others.
- **Shared contribution** — an agent can add to the memory others read, so its learning influences the fleet.

Shared contribution is the harder and more valuable half. It is what turns a fleet from a set of independent workers into a system that improves itself.

---

## Why it is hard

If sharing memory were free, the design would be trivial: one pool, everyone reads and writes. It is not free, because naive sharing degrades the system in predictable ways.

- **Contamination.** A single wrong contribution does not stay local — it spreads to everyone who reads it. The blast radius of an error scales with how widely memory is shared.
- **Echo chambers.** Agents read shared memory, converge toward it, and write back conclusions that merely restate what they read. The fleet loses diversity and reinforces its own errors.
- **Authority confusion.** Not every agent should be able to assert anything to everyone. A directive from a position of authority and an offhand observation from a single worker cannot carry equal weight, or the system has no stable notion of what it "knows."
- **Relevance collapse.** What is signal to one agent is noise to another. A single undifferentiated pool drowns each reader in things that do not concern it.
- **Unbounded growth.** Memory that only accumulates becomes slower to search, harder to trust, and full of claims that were true once and no longer are.

So the real problem is not *how to share memory* — it is **how to let every agent influence shared memory without letting any agent corrupt it.** Every concept that follows exists to resolve that tension.

---

## The solution, in one idea

Shared memory should be **scoped**, **contributed under controlled authority**, and **interpreted by precedence rather than merged**.

- *Scoped* means memory has reach: some memory is meant for the whole fleet, some only for a part of it. Reach is not global by default.
- *Contributed under controlled authority* means agents can add to shared memory, but the breadth of what they can assert is bounded — influence is real but not unchecked.
- *Interpreted by precedence* means that when memory from different sources or different reaches meets, conflicts resolve by rule, not by overwriting.

The rest of this document develops the concepts that make this work.

---

## Core concept 1: Scope

Memory belongs to a **scope** — a bounded region of the fleet for which a piece of memory is relevant and authoritative. Scopes nest: broader scopes contain narrower ones, forming a hierarchy from the widest reach (the whole fleet) down to the narrowest (a single agent's working context).

Scope is the answer to **reach**: how far does a given memory extend? A decision meant for everyone lives in a broad scope; a fact relevant only to one team lives in a narrow one. An agent belongs to a position in this hierarchy, and that position determines what is relevant to it.

The power of scope is that it makes reach explicit and structural instead of leaving it to convention. Sharing is no longer all-or-nothing.

---

## Core concept 2: Visibility through inheritance

An agent does not see the whole store. It sees memory from its own scope and from every scope that contains it — it **inherits** the broader memory above it while remaining blind to memory in scopes that do not contain it.

This is the mechanism of shared *reading*. A fleet-wide decision is visible to every agent because every agent sits within the broadest scope. A team-specific fact is visible only within that team. Visibility falls directly out of the scope hierarchy: an agent sees what reaches it.

Crucially, visibility is about *content and reach*, not about identity. An agent does not address another agent and ask for its memory. It draws on whatever memory reaches its position, and the original author is incidental to that — relevant for trust and accountability, but not for discovery. This decoupling is what lets agents come and go without the rest of the fleet having to know about them.

---

## Core concept 3: Authority and controlled contribution

Shared contribution is only safe if it is bounded. The concept that bounds it is **authority**: the right to assert memory at a given scope.

Authority is tied to position in the scope hierarchy, not granted ad hoc. An agent responsible for a broad scope may assert memory that reaches everyone within it; an agent responsible for a narrow scope may only assert within that narrow region. A worker with no scope of its own cannot unilaterally write into shared memory at all.

This does not silence the worker — it changes *how* its influence travels. An agent without authority over a scope influences that scope **indirectly**: by producing evidence. Repeated, corroborated contributions accumulate weight, and an agent that does hold authority can ratify them into broader reach. Influence flows upward through evidence and ratification, never through unilateral assertion.

The result is the resolution of the central tension. Every agent can influence shared memory — that is the whole point of the system — but the breadth of influence is governed by authority, so no single agent can corrupt what the fleet collectively holds to be true.

---

## Core concept 4: Composition over merging

Because memory is scoped and an agent inherits several scopes at once, a reader faces memory from multiple reaches simultaneously. The naive response is to merge it all into one flat pool. That is a mistake: merging destroys the information about where each piece came from and how far it reaches, which is exactly the information needed to resolve conflicts well.

Instead, an agent's memory is **composed**: presented as the layered set of scopes it inherits, each retaining its identity and reach, assembled into one coherent view at the moment of reading. Composition is a *mechanism* in service of sharing — it is how a scoped, hierarchical store presents itself to a single reader — not the purpose of the system.

A helpful analogy is variable scope in programming: a local context, an enclosing context, and a global context are all visible at once as a single namespace, with inner contexts able to shadow outer ones, yet no one "merges" them into a single undifferentiated bag. Shared fleet memory composes the same way.

---

## Core concept 5: Precedence — directives versus context

Once composition keeps multiple reaches side by side, conflicts between them must resolve by rule. The key insight is that the right rule **depends on the kind of memory**, and there are two kinds that obey opposite rules.

- **Directives** are binding decisions — what the fleet (or a part of it) has resolved to do or to treat as true. For directives, **broader authority wins.** A narrower scope inherits a directive and may refine within it, but may not contradict it. A worker's local belief cannot repeal a fleet-wide decision.
- **Context** is observation and working state — what is happening, what was just learned. For context, **the closest scope wins.** The most specific, most recent context is the most relevant when two pieces conflict.

These pull in opposite directions — authority flows *down* for directives, relevance flows *up* for context — and that is precisely why they must be distinguished. They rarely truly collide, because they govern different kinds of content; and a piece of context never overrides a directive, no matter how close or how recent. Distinguishing the two is what makes the composed view coherent rather than contradictory.

---

## Core concept 6: Provenance and earned trust

Because contributions vary in reliability, every piece of shared memory must carry its **provenance** — where it came from. Provenance is what makes accountability and recovery possible: if a source proves unreliable, its contributions can be identified and removed wholesale. It is also what lets retrieval weigh a claim from an authoritative source more heavily than the same claim from elsewhere.

Trust, unlike provenance, is **earned and revised.** Acting on a memory that leads to good outcomes should raise its standing; acting on one that leads to bad outcomes should lower it. Over time the store should self-correct, with reliable memory rising in influence and unreliable memory falling. This is a primary defense against contamination: a wrong contribution does not carry equal weight forever simply because it was written down.

---

## Core concept 7: Forgetting

Memory that only grows eventually defeats the purpose of sharing it — it becomes slow, noisy, and full of claims that have expired. A shared memory system must forget on purpose.

There are several kinds of forgetting, and they are conceptually distinct:

- **Supersession** — newer memory replaces older memory on the same subject. The old version leaves active use but can be retained for accountability.
- **Decay** — memory that is rarely used or poorly trusted fades from relevance over time.
- **Retirement** — an authority deliberately withdraws a piece of shared memory.

A useful principle is to separate the **record** from the **working memory**: the raw history of what was contributed can be kept immutably for accountability and recovery, while the curated memory that agents actually read is allowed to forget. Forgetting then operates on the working view without destroying the system's ability to look back.

---

## The system as a living equilibrium

Putting the concepts together, shared fleet memory is not a static store that fills up. It is a **moving equilibrium.**

Knowledge flows **upward**: an observation made in a narrow scope, once corroborated and ratified, consolidates into broader reach — narrow becomes wide, transient becomes durable. Knowledge fades **downward and out**: stale or distrusted memory is superseded, decays, or is retired. At any moment, what an agent reads is the current balance of these flows, scoped to its position and resolved by precedence.

This is the deeper answer to the original goal. The fleet improves not because memory accumulates, but because useful contributions propagate to where they are relevant, gain trust as they prove out, and displace what they supersede — all while authority and scope keep any single contribution from corrupting the whole. Shared memory, contributed safely, is what lets a fleet's performance compound.

---

## Summary of the core ideas

| Concept | The question it answers |
| --- | --- |
| Scope | How far does a piece of memory reach? |
| Visibility through inheritance | What memory can a given agent see? |
| Authority and controlled contribution | What may an agent assert, and how far? |
| Composition over merging | How are multiple reaches presented to one reader? |
| Precedence (directives vs. context) | When sources conflict, which wins? |
| Provenance and earned trust | How reliable is a piece of memory, and who is accountable? |
| Forgetting | How does memory stay relevant over time? |

The throughline: **a fleet should share memory so its performance compounds, and the entire difficulty — and therefore the entire design — is letting every agent contribute to that shared memory without letting any agent corrupt it.**

---

## Development

### Prerequisites

Python 3.11+. No other system dependencies for the backend.

### Setup

```bash
make install    # pip install -e ".[dev]"
```

### Common tasks

```bash
make test       # run pytest
make lint       # ruff check + ruff format --check
make format     # ruff format (auto-fix style)
make run        # uvicorn strata.app:app --reload --port 8000  (requires feature/app-server)
make migrate    # apply SQLite schema migrations (requires feature/record-store)
```

### Project layout

```
src/strata/         # Python package — the backend service
tests/              # pytest test suite
migrations/         # SQLite schema migrations for the record store
scripts/            # Utility scripts (run_migrations.py, etc.)
docs/adr/           # Architecture Decision Records
```

### Vocabulary

All code and comments use the canonical glossary from `CONTEXT.md`. Key terms:
`scope`, `stratum`, `agent`, `contribution`, `scope-manager`, `directive`,
`context`, `perspective`, `record`, `provenance`, `trust`.
