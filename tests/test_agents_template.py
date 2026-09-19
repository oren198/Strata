"""The AGENTS.md block Strata seeds for Codex: the write-back norm, and honest
identity guidance (the session id stays blank for Codex)."""

from __future__ import annotations

from strata import install

# The block as shipped through M2 (before the never-end-silent line and the
# identity fix). A project registered then still carries exactly this, and must be
# updated in place by re-register and removed cleanly by unregister.
_M2_BLOCK = "<!-- strata:begin -->\n## Strata memory\n\nStrata is a shared memory layer this project's agents read from and write to\nacross sessions.\n\n- **Read before working.** At the start of a session, pull your scope's\n  perspective before you act on anything.\n- **Contribute what the next agent needs.** A decision, a finding, a gap —\n  write it back. Nothing you don't contribute survives past this session.\n- **Expect the judge's verdict.** Every contribution is reviewed by that\n  scope's manager before it counts as memory — propose freely, but the\n  scope-manager decides what sticks.\n\nMemory access is only through the strata MCP tools `strata_read_perspective`,\n`strata_contribute`, and `strata_rejudge` (and their read-only siblings)\nexposed to this session — never run `strata start` or talk to its HTTP\nbackend yourself; that process serves the human's Console UI only and is\nnot part of your job.\n\n**Two hard rules:**\n- Never read or write files under `.strata/` directly (its database,\n  session files, or summaries) — that bypasses binding and judgment. All\n  memory access goes through the strata tools above.\n- If any strata tool returns the not-bound error, stop and ask the user\n  which scope to act as before completing your answer; an answer produced\n  without the project's memory is incomplete.\n\nYour scope and role identity are bound through environment variables\n(`STRATA_AGENT_SCOPE`, `STRATA_AGENT_SKILL`, `STRATA_AGENT_SESSION_ID`) set\nbefore this session starts — do not hardcode them.\n<!-- strata:end -->\n"  # noqa: E501


def test_the_template_says_never_end_silent() -> None:
    block = install._shipped_agents_md_block()

    assert (
        "Before you finish, either contribute what you learned or call "
        "`strata_session_closeout` with a reason \u2014 never end silent."
    ) in block


def test_the_template_no_longer_claims_identity_comes_from_env_vars_set_before_start() -> None:
    block = install._shipped_agents_md_block()

    assert "set\nbefore this session starts" not in block
    assert "leave `STRATA_AGENT_SESSION_ID` blank" in block


def test_the_m2_block_is_recognised_as_a_stale_shipped_version() -> None:
    text = "# My project\n\n" + _M2_BLOCK + "\nMy own notes.\n"

    assert install.classify_agents_md_drift(text) == "stale"


def test_re_register_updates_the_m2_block_in_place_and_is_then_idempotent() -> None:
    text = "# My project\n\n" + _M2_BLOCK + "\nMy own notes.\n"

    updated, status = install.self_update_agents_md_block(text)
    again, status_again = install.self_update_agents_md_block(updated)

    assert status == "stale"
    assert "never end silent" in updated
    assert updated.startswith("# My project\n\n")
    assert updated.endswith("\nMy own notes.\n")
    assert (again, status_again) == (updated, "match")


def test_unregister_still_matches_the_m2_block() -> None:
    text = "# My project\n\n" + _M2_BLOCK

    remaining, status = install.remove_agents_md(text)

    assert status == "removed"
    assert "strata:begin" not in remaining
