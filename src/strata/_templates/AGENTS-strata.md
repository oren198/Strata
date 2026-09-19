<!-- strata:begin -->
## Strata memory

Strata is a shared memory layer this project's agents read from and write to
across sessions.

- **Read before working.** At the start of a session, pull your scope's
  perspective before you act on anything.
- **Contribute what the next agent needs.** A decision, a finding, a gap —
  write it back. Nothing you don't contribute survives past this session.
- **Expect the judge's verdict.** Every contribution is reviewed by that
  scope's manager before it counts as memory — propose freely, but the
  scope-manager decides what sticks.
- **Never end silent.**
  Before you finish, either contribute what you learned or call `strata_session_closeout` with a reason — never end silent.

Memory access is only through the strata MCP tools `strata_read_perspective`,
`strata_contribute`, and `strata_rejudge` (and their read-only siblings)
exposed to this session — never run `strata start` or talk to its HTTP
backend yourself; that process serves the human's Console UI only and is
not part of your job.

**Two hard rules:**
- Never read or write files under `.strata/` directly (its database,
  session files, or summaries) — that bypasses binding and judgment. All
  memory access goes through the strata tools above.
- If any strata tool returns the not-bound error, stop and ask the user
  which scope to act as before completing your answer; an answer produced
  without the project's memory is incomplete.

Your scope and skill are bound when the session starts — from
`STRATA_AGENT_SCOPE` and `STRATA_AGENT_SKILL` where they are set, or
automatically when the fleet has a single scope — so do not hardcode them.
The session id is derived per session by the server, so
leave `STRATA_AGENT_SESSION_ID` blank (an id exported in your shell would reach
the Stop hook but not the server, and split one session in two).
<!-- strata:end -->
