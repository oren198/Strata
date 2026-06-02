---
name: session-handoff
description: Compact the current session into a handoff document so a fresh agent (or a future you) can pick up where this one left off without re-deriving the context. Use when a session is getting long, you're about to run out of context, you're switching machines or roles, or the user says "hand this off", "write a handoff", "summarise so I can continue later". Produces a single markdown file written outside the repo, referencing existing artifacts rather than duplicating them.
---

# You are writing a session handoff

The goal is one self-contained markdown document that lets a brand-new
agent resume this work **cold** — with no access to this conversation —
and lose as little momentum as possible. Optimise for the next reader,
not for completeness. A handoff that restates everything is as useless as
one that restates nothing.

## Where this fits with Strata

Strata is the fleet's *durable* shared memory: decisions worth outliving
any session belong in a `strata_contribute` call, judged by a
scope-manager, not in a handoff file. **A handoff is the complement** — it
carries the *in-flight, not-yet-settled* state of one session: what you
were mid-way through, what you just learned but haven't ratified, the next
three moves. Two rules follow:

- **Before writing the handoff, sweep for anything that should be a
  contribution instead.** A decision the user made, a directive, a durable
  observation — push it to Strata (`strata-worker`) so it reaches the whole
  fleet, and then just *reference* it in the handoff. Don't let settled
  knowledge leak into a throwaway file.
- The handoff holds only what Strata shouldn't: half-finished reasoning,
  the current branch's uncommitted intent, the immediate plan.

## Where to write it

Write to the OS temp directory, **never** into the repo working tree —
this file is scratch, not a tracked artifact, and must not pollute
`git status` or get committed by accident.

```
${TMPDIR:-/tmp}/handoff-<short-slug>-<YYYYMMDD-HHMM>.md
```

Pick `<short-slug>` from the task (e.g. `perspective-composition`). After
writing, print the absolute path so the user can hand it to the next
session.

## What goes in it

Keep it tight. Reference, don't duplicate — if it already exists in a PRD,
ADR, plan, issue, commit, or diff, **link to it by path or URL** and
summarise in one line. Sections, in order:

1. **Task** — one or two sentences: what we're trying to achieve and why.
   If the user gave this skill an argument, treat it as the next session's
   intended focus and shape the whole document toward it.
2. **Current state** — where things actually stand right now. Branch name,
   what's committed vs. uncommitted, what's pushed, open PR (link it),
   CI/test status. Be honest: if tests are red or a step was skipped, say
   so plainly.
3. **What's been done** — the meaningful moves this session, as a short
   list. Each item one line, with a `path:line` or commit/PR reference
   where it helps. Skip the play-by-play.
4. **What's left / next moves** — the concrete next 1–5 actions, ordered.
   This is the most valuable section; make it actionable enough that the
   next agent can start without guessing.
5. **Gotchas & open questions** — landmines, dead ends already tried,
   decisions still pending the user, anything that would cost the next
   agent an hour to rediscover.
6. **Key references** — the canonical sources to read first
   (`CONTEXT.md`, the relevant ADR, the PR), each with a one-line "why".
7. **Suggested skills** — which skills the next session should invoke and
   when. For this repo that usually means one of: `architect` (design +
   review), `strata-worker` (do work bound to a scope), `strata-inspect`
   (read-only memory browse), `grill-with-docs` (stress-test a plan). Name
   the skill and the trigger.

## Rules

- **Redact secrets.** No API keys, tokens, passwords, or PII in the file —
  reference them by name/location only (e.g. "uses `ANTHROPIC_API_KEY` from
  the environment").
- **Match the vocabulary.** Use Strata's canonical terms exactly (`scope`,
  `stratum`, `contribution`, `directive`, `context`, `scope-manager`,
  `perspective`, `supersession`) — the next agent will read `CONTEXT.md`
  and the words must line up. No synonyms.
- **Reference over restate.** When in doubt, link and summarise in a line
  rather than paste. The diff is the record of *what changed*; the handoff
  is the record of *where we are and what's next*.
- **Don't commit it, don't push it.** It lives in temp. If the user wants
  it tracked, that's an explicit, separate request.
- **One file.** Don't spawn a directory of fragments.

## After writing

Print the path, then give the user a 3–4 line spoken summary of the
handoff so they can sanity-check it without opening the file. Offer to
push any still-uncontributed decisions into Strata via `strata-worker` if
the pre-write sweep turned any up.
