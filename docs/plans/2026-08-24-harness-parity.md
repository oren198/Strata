# Multi-Harness Register/Unregister + Default Harness Implementation Plan

> **STATUS: DELIVERED** (verified 2026-08-31). All seven tasks shipped during
> the 1.10.x line — `detect_harnesses` (`install.py`), repeatable `--harness`
> on both register and unregister, `strata set-default-harness`, `launch`
> reading the recorded default via `read_default_harness`, AGENTS.md marker
> seeding for Codex, and the README section. Kept as the design record for how
> harness parity was reasoned about, not as work to pick up.
>
> One item from Task 5 remains open and is tracked as issue #171: the Codex
> freshness Stop hook is schema-verified and installed, but has never been
> observed firing in a live Codex session.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One identical user journey across harnesses: `strata register` wires every installed harness by default, `strata unregister` reverses the same way, `--harness` narrows either, and `strata set-default-harness` records which harness `strata launch` uses.

**Architecture:** A small detection layer (`detect_harnesses`) drives register/unregister fan-out over the existing per-harness wiring functions (Claude Code's settings/skills/hook installers; the Codex config mergers/removers from the codex-harness branch). The default harness lives in `.strata/config.toml`. `launch` reads it; the Codex launch path stays gated behind live verification with an honest error.

**Tech Stack:** Python 3.11, argparse (existing `__main__.py` patterns), tomllib for reads, the existing marker-based merge/remove helpers in `src/strata/install.py`.

**Spec:** Operator decisions 2026-08-24 (this conversation): no journey difference between harnesses; register default = all installed; unregister symmetric with the same flags; `set-default-harness` exists. Base branch: `feature/codex-harness` (needs its merged codex helpers).

## Global Constraints

- Everything works with no backend running (all of this is file wiring).
- This repo's public surfaces name no consumer of the engine.
- Plain language in all output and docs; every failure/skip line says why or what to do.
- Strictly additive and reversible: user-authored config always survives register AND unregister (marker-based detection, byte-match removal — the established `install.py` conventions).
- Backward compatible: on a machine where detection finds nothing (CI, containers), `strata register` still wires claude-code exactly as today, with a one-line notice.
- TDD: failing test first, commit per green step. Run tests as `.venv/bin/python -m pytest` in the worktree.

---

### Task 1: Harness detection

**Files:**
- Modify: `src/strata/install.py`
- Test: `tests/test_harness_detection.py` (new)

**Interfaces:**
- Produces: `KNOWN_HARNESSES: tuple[str, ...] = ("claude-code", "codex")` and `detect_harnesses(home: Path | None = None, path_env: str | None = None) -> list[str]` — returns the subset of `KNOWN_HARNESSES` present on this machine, in `KNOWN_HARNESSES` order. Detection rule per harness: `claude-code` if `shutil.which("claude", path=path_env)` or `(home)/.claude` exists; `codex` if `shutil.which("codex", path=path_env)` or `(home)/.codex` exists. `home` defaults to `Path.home()`; parameters exist so tests never depend on the real machine.

- [ ] **Step 1: Failing tests** — cases: empty tmp home + empty PATH → `[]`; `~/.claude` dir only → `["claude-code"]`; both dirs → both, claude first; `codex` binary on PATH only → `["codex"]`.

```python
def test_detects_nothing_on_bare_machine(tmp_path):
    assert detect_harnesses(home=tmp_path, path_env=str(tmp_path)) == []

def test_detects_claude_by_home_dir(tmp_path):
    (tmp_path / ".claude").mkdir()
    assert detect_harnesses(home=tmp_path, path_env=str(tmp_path)) == ["claude-code"]
```

- [ ] **Step 2:** Run → FAIL (no `detect_harnesses`). **Step 3:** Implement. **Step 4:** Run → PASS. **Step 5:** Commit `feat(install): detect installed harnesses`.

### Task 2: `--harness` becomes repeatable; register fans out over all detected

**Files:**
- Modify: `src/strata/__main__.py` (`cmd_register`, the register argparse block)
- Test: `tests/test_register_multi_harness.py` (new; reuse fixtures/idioms from the codex-harness tests in `tests/test_install.py` / `tests/test_cli.py`)

**Interfaces:**
- Consumes: `detect_harnesses` (Task 1); existing claude-code wiring steps and the codex branch of `cmd_register` (`__main__.py:1776` / `:1880` on the base branch).
- Produces: `strata register [--harness NAME ...]` — flag now `action="append"`, `choices=KNOWN_HARNESSES`. Resolution: explicit flags → exactly those; no flags → `detect_harnesses()`; detection empty → `["claude-code"]` plus the notice `"no harness detected on this machine — wiring claude-code (the default)"`. The common scaffolding (steps 1–5: `.strata/`, config.toml, gitignore, fleet.yaml) runs once; the per-harness wiring loop runs once per resolved harness with a `== claude-code ==` / `== codex ==` header line before each block. `--diff` reports per harness the same way.

- [ ] **Step 1: Failing tests** — (a) both-harness machine (fake home with `.claude` and `.codex`, monkeypatched into detection) + plain `register` → claude settings written AND codex config merged; (b) `--harness codex` only → `.claude/settings.json` untouched; (c) bare machine → claude-code wired + notice in output; (d) re-run is idempotent per harness (all skip lines). Monkeypatch `strata.install.detect_harnesses` (or pass a seam — pick the seam `cmd_register` calls and patch there).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Restructure `cmd_register`: extract the current claude-code wiring block and the codex wiring block into `_register_claude_code(...)` / `_register_codex(...)` local helpers (pure moves — do not change their bodies), then loop `for harness in resolved:`. Keep `--bootstrap-venv` semantics: applies to claude-code; if codex is in the resolved set, keep the existing skip-notice. **Step 4:** PASS + full suite. **Step 5:** Commit `feat(register): wire every installed harness by default`.

### Task 3: unregister symmetric

**Files:**
- Modify: `src/strata/__main__.py` (`cmd_unregister`, its argparse block)
- Test: `tests/test_register_multi_harness.py` (extend)

**Interfaces:**
- Produces: `strata unregister [--harness NAME ...]` — same repeatable flag. Resolution differs from register: no flags → reverse EVERY harness whose wiring markers are present (use `stop_hook_present`/`mcp_server_present` for claude-code; `_codex_mcp_present`/`_codex_hook_present` for codex) — what is wired, not what is installed; explicit flags → exactly those. A named harness with nothing wired prints a skip line, exit 0.

- [ ] **Step 1: Failing tests** — (a) register both → plain `unregister` reverses both (claude settings block gone, codex tables gone, user content byte-identical — reuse the round-trip fixtures from the codex tests); (b) `--harness codex` leaves claude wiring intact; (c) unregister on a never-registered dir: skip lines, exit 0.
- [ ] **Step 2:** FAIL. **Step 3:** Same extraction pattern as Task 2 (`_unregister_claude_code` / `_unregister_codex`, pure moves), then the marker-driven loop. **Step 4:** PASS + full suite. **Step 5:** Commit `feat(unregister): reverse every wired harness by default`.

### Task 4: `strata set-default-harness`

**Files:**
- Modify: `src/strata/__main__.py` (new subcommand), `src/strata/project_config.py` (read/write helper)
- Test: `tests/test_default_harness.py` (new)

**Interfaces:**
- Produces: `strata set-default-harness NAME` — validates `NAME in KNOWN_HARNESSES` (exit 2 with the valid list otherwise), requires a registered workspace (`.strata/config.toml` exists — exit 1 with "run 'strata register' first" otherwise), writes `default_harness = "NAME"` under a `[launch]` table in `.strata/config.toml` (create table if absent, replace value if present, preserve every other line — read-modify-write the TOML textually the way the codex config mergers do, or via a minimal targeted edit; tomllib parses for validation only). Also produces `read_default_harness(project_root) -> str | None` in `project_config.py` for Task 5.
- Prints: `default harness: NAME (used by 'strata launch')`.

- [ ] **Step 1: Failing tests** — write-then-read round trip; unknown name → exit 2 + list; unregistered dir → exit 1 + guidance; existing config.toml content preserved byte-for-byte outside the `[launch]` table; running twice replaces the value without duplicating the table.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS + full suite. **Step 5:** Commit `feat(cli): set-default-harness records what 'strata launch' starts`.

### Task 5: launch reads the default; Codex path gated honestly

**Files:**
- Modify: `src/strata/launch.py`, `src/strata/__main__.py` (launch argparse: add `--harness`)
- Test: `tests/test_launch.py` (extend, following its existing test style)

**Interfaces:**
- Consumes: `read_default_harness` (Task 4).
- Produces: launch harness resolution: `--harness` flag → that; else `read_default_harness()` → that; else if exactly one harness is wired (marker check, as in Task 3) → that; else `claude-code` (today's behavior). `claude-code` → existing launch flow, unchanged. `codex` → exit 1 with: `"Codex launch is not wired yet: Codex's MCP env delivery is still being verified live (see README, 'Using Strata with Codex CLI'). Start codex manually after filling in the [mcp_servers.strata.env] values."` — plain, honest, names the doc.

- [ ] **Step 1: Failing tests** — resolution order (flag beats config beats single-wired beats fallback) using monkeypatched seams; codex → exit 1 with the message; claude-code path unchanged (existing launch tests still green).
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS + full suite. **Step 5:** Commit `feat(launch): honor the default harness`.

### Task 6: AGENTS.md seeding for Codex (the skills analogue)

**Files:**
- Create: `src/strata/_templates/AGENTS-strata.md` (package data — a short, plain-language block: what Strata is in one line, the three memory moves — read before working, contribute what the next agent needs, expect the judge's verdict — and the env-binding note; no consumer names; marker-fenced `<!-- strata:begin -->` / `<!-- strata:end -->`)
- Modify: `src/strata/install.py` (`merge_agents_md(existing_text: str) -> tuple[str, bool]` + `remove_agents_md(...)` following the `_append_block`/`_remove_block` marker conventions from the codex helpers), `src/strata/__main__.py` (call from `_register_codex` / `_unregister_codex`, writing the project's `AGENTS.md`)
- Test: `tests/test_agents_md.py` (new)

- [ ] **Step 1: Failing tests** — fresh project → AGENTS.md created with the block; existing AGENTS.md with user content → block appended, user text byte-identical; re-register idempotent; unregister removes only the marker block, "edited" status when the user changed inside it (mirror `remove_gitignore_block` semantics).
- [ ] **Step 2:** FAIL. **Step 3:** Implement (check `MANIFEST.in`/pyproject package-data config so the template ships — mirror how `_templates/minimal.yaml` is declared). **Step 4:** PASS + full suite. **Step 5:** Commit `feat(register): seed AGENTS.md guidance for the codex harness`.

### Task 7: Docs

**Files:**
- Modify: `README.md` — the register/unregister sections describe the new default ("register wires every harness it finds; use --harness to narrow — same for unregister"), `set-default-harness`, and the launch resolution order; the Codex section keeps its verified-claims boundary and gains the AGENTS.md line. `docs/plans/2026-08-24-launch-bar.md` status table: bar-item Codex row updated.

- [ ] **Step 1:** Write against the shipped behavior (read the code from Tasks 1–6 first; the code wins over this plan where they differ). Grep the diff for consumer names. **Step 2:** Full suite + `ruff format --check .` + `ruff check .` (CI runs both — the last two PRs failed on format). **Step 3:** Commit `docs: one journey across harnesses`.

## Sequencing

```
Task 1 ──► Task 2 ──► Task 3 ──► Task 7
                └────► Task 4 ──► Task 5 ─┘
Task 6 after Task 2 (hooks into _register_codex)
```

Sequential on one branch (they all touch `__main__.py`).

## Out of scope (deliberate)

- Codex live verification (operator checklist, needs credentials) and the launch delivery it unlocks — `launch --harness codex` gets a real implementation only after the env-inheritance answer.
- Any new harness beyond these two — the point of `KNOWN_HARNESSES` + the loop is that the next one is a detection rule plus a wiring pair.
