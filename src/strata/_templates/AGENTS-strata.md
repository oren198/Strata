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

Your scope and role identity are bound through environment variables
(`STRATA_AGENT_SCOPE`, `STRATA_AGENT_SKILL`, `STRATA_AGENT_SESSION_ID`) set
before this session starts — do not hardcode them.
<!-- strata:end -->
