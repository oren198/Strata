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

Your scope and role identity are bound through environment variables
(`STRATA_AGENT_SCOPE`, `STRATA_AGENT_SKILL`, `STRATA_AGENT_SESSION_ID`) set
before this session starts — do not hardcode them.
<!-- strata:end -->
