# Strata

<!-- mcp-name: io.github.oren198/strata -->

Strata gives a fleet of AI coding agents shared memory. Agents working the
same project write what they learn to a scope and read what other agents
already wrote — and every write is checked by an LLM judge before it lands,
so one agent's mistake never corrupts what the rest of the fleet reads.

## Core concepts

- **Fleet** — the whole set of scopes and agents sharing one Strata memory.
- **Scope** — one node agents bind to and read/write; scopes are grouped
  into ordered strata (e.g. architecture → backend → tests).
- **Contribution** — one write to a scope: a proposed directive (binding) or
  context (non-binding).
- **Judge** — the LLM that reviews every contribution and decides whether,
  and how, it's admitted.
- **Record** — the append-only audit trail of everything ever contributed
  and judged, per scope.

`strata register` seeds Claude Code deny rules for the store
`.strata/config.toml` actually resolves. The seeded layout lives under
`.strata/`, and the rules are then `Read(/.strata/**)` and
`Edit(/.strata/**)` in `.claude/settings.json`. A database kept elsewhere
is denied per file (`<db>*`, which also covers the `-wal` and `-shm`
files), not by denying the directory that holds it — a database in a
shared checkout directory must not hide the other repositories there.
Summaries, the sessions directory beside them, and `.locks` are denied as
directories only when that directory is Strata's own. `fleet.yaml` is
denied as that file (`Read` and `Edit`, no wildcard) when a denied
directory does not already cover it, and the backups beside it are denied
as `Read(<file>.bak*)` and `Edit(<file>.bak*)` (`fleet.yaml.bak` and
`fleet.yaml.bak.<utc>`). The directory holding the file is not denied. A generic `summaries` or `sessions` directory directly under your
home directory, or under an ancestor of the project, is denied only when
Strata created it; otherwise `strata doctor` says it was left open. A
directory that resolves above your home directory is left open too.
`.strata/` stays denied because the config lives there. Those rules cover
Claude Code's Read, Edit, and Write tools, the shell file commands it
recognizes (`cat`, `head`, `tail`, `sed`, `tee`), and Bash redirections
onto those paths. They do not stop a Python or Node process, or the
`sqlite3` shell, that opens the files itself. A `Write(path)` rule is not
written because Claude Code never consults one. Codex's sandbox config
cannot deny a path inside a writable workspace, so register does not seed
one there. Outside a harness that enforces the deny, scoping is discipline,
not security.

For the full theory and vocabulary, see
[`docs/philosophy.md`](https://github.com/oren198/Strata/blob/main/docs/philosophy.md) (why Strata exists, why naive
sharing fails) and [`CONTEXT.md`](https://github.com/oren198/Strata/blob/main/CONTEXT.md) (the canonical glossary
every part of the codebase uses — 23 terms, no synonyms).

---

## Install and run

```bash
pipx install strata-mem      # strata + strata-mcp on PATH, in an isolated env
cd your-project
strata register               # wires memory into this project
```

`strata` is a local-first Python service: SQLite + markdown storage, an
embedded MCP mode that needs no backend running, file-canonical
`fleet.yaml`, and an optional read-only browser Console. The
[Quick start](#quick-start-two-agents-one-memory) below is the one supported
first run; everything deeper — architecture decisions, the Console UI, upgrade
notes — lives under `docs/` and is linked from the relevant section.

---

## Quick start: two agents, one memory

The first run this release is built around: **two terminals on one machine, a
Claude Code session and a Codex session bound to the same scope.** One session
learns something and writes it back; the judge admits or declines it, with a
reason you can read; the other session acts on it next time. You can see what
the fleet believes, where each belief came from, and what was kept out.

The engine is embedded — the MCP server applies migrations and opens storage
itself on first use, so nothing needs to run in the background. `strata start`
exists for one reason, the **Console** (step 7); agents never depend on it.

### 1. Prerequisites

- **Python 3.11 or newer** (`strata-mem` declares `requires-python >=3.11`).
  Check: `python3 --version`. On an older interpreter the install refuses rather
  than half-installing — checked on Python 3.10.12: `pip install` stops with
  `ERROR: Package 'strata-mem' requires a different Python: 3.10.12 not in
  '>=3.11'`, and `pipx install --python <3.10 interpreter>` stops with "The Python
  you named does not satisfy '>=3.11'". pipx builds the isolated env from the
  interpreter pipx itself runs on, so that is the one that must be 3.11+. No
  Python 3.11+? See
  [No Python 3.11+ globally?](#no-python-311-globally-use---bootstrap-venv).
- **Claude Code and Codex CLI**, both installed and logged in (`claude` and
  `codex` on your `PATH`). The register step below wires whichever it finds; this
  quickstart wires both.
- **A judge API key.** The judge is an LLM you point at any endpoint that speaks
  the Anthropic Messages API — get an Anthropic key at
  <https://console.anthropic.com/>, or use a router/proxy/self-hosted gateway
  that speaks that API (see [Environment variables](#environment-variables)).

### 2. Install

```bash
pipx install strata-mem      # strata + strata-mcp on PATH, in an isolated env
```

`pipx` is the supported install. `pip install strata-mem` inside a Python 3.11+
virtualenv also gives you working `strata` and `strata-mcp` commands (checked with
`python3.11 -m venv` + `pip install`); then run `strata` from that environment, or
put its `bin/` on `PATH`, because the registered hooks call bare `strata`. `uv` was
not checked, so nothing is claimed for it. If another `strata` is already on your
`PATH` (an older pipx install, say), see [Troubleshooting](#troubleshooting).

### 3. Register — wiring both harnesses

```bash
mkdir strata-demo && cd strata-demo
git init                       # a project root needs a marker (.git, pyproject.toml, ...)
strata register --harness claude-code --harness codex
```

`strata register` is idempotent and strictly additive. With those flags it wires
both harnesses whether or not it can detect them (without flags it wires every
harness it finds). It creates `.strata/` (config, a one-scope `fleet.yaml`, the
database directory), appends a `# Strata` block to `.gitignore`, and then:

- **Claude Code:** copies the Strata skills into `.claude/skills/`, adds the
  `strata` server to `.mcp.json`, and installs the freshness `Stop` hook under
  `.claude/`.
- **Codex:** merges the `strata` MCP server and the same `Stop` hook into Codex's
  own config, `$CODEX_HOME/config.toml` (default `~/.codex/config.toml` — a
  machine-level file, not a per-project one), and seeds this project's
  `AGENTS.md` with a short memory-moves block.

Strict mode is on by default — a session that read fleet memory and wrote nothing back is reminded at its end to contribute or close out (at most twice) — and `strata register --no-strict` turns it off.

In an interactive terminal `strata register` also asks what the first scope's memory is for (one line; Enter skips it). Scripts pass `--description "..."`; a non-interactive run never asks. It is written to `fleet.yaml` as the scope's `description:`.

See [What `strata register` does](#what-strata-register-does) for the full list.

### 4. Set your judge API key

The default judge is `qwen/qwen3-235b-a22b-2507` on OpenRouter, so the key is an
**OpenRouter key** ([openrouter.ai/keys](https://openrouter.ai/keys)). To judge with
Anthropic instead, use an Anthropic key — see [Judge models](#judge-models).

Put it in a `.env` file at the project root. `strata register` already adds `.env`
to `.gitignore`, and every entry point (the MCP server, the CLI and the Console
backend) loads it:

```
JUDGE_API_KEY=sk-or-...
```

`strata register` offers to capture the key for you and writes `JUDGE_MODEL` and
`JUDGE_BASE_URL` beside it, so the `.env` states which judge the key is for.

With an OpenRouter key you can also pin the provider; it's optional. OpenRouter serves the same model id from several hosts, and the judge's answers can differ between them. Left unset, OpenRouter picks a provider for each call and falls back to another if one is unavailable. Pinned, every judge call goes to that one provider, which makes results easier to reproduce, but there is no fallback: if that provider stops serving the model, your judge calls fail until you change or remove the pin.

```
JUDGE_PROVIDER=<provider name>
```

The v1.18.0 numbers in [Judge models](#judge-models) were measured pinned to Nebius. Alibaba, which earlier releases were measured on, stopped serving this model on OpenRouter on 2026-10-10. `strata doctor` shows which provider is pinned.

**With Codex, use the `.env` file, not an export.** Codex starts its MCP server with
only the `[mcp_servers.strata.env]` table from its own config, not your shell's
environment, so an exported key never reaches it and Codex's contributions go
unjudged. The project `.env` is how the key reaches Codex's server. For Claude Code
alone, `export JUDGE_API_KEY=sk-...` in the shell you launch it from also works.

(The older `ANTHROPIC_API_KEY` / `STRATA_ANTHROPIC_API_KEY` names still work as a
deprecated fallback — see [Environment variables](#environment-variables).)

Without a key, reads work, but a contribution is recorded with **no verdict**: the
tool returns an error saying the scope-manager cannot judge without a key, and the
contribution shows in `strata record` as "judge errored". Once a key is set (restart
the harness so the server sees it), `strata_rejudge` gives it its verdict.

### 5. The fleet: keep the one scope

`.strata/fleet.yaml` is seeded with one scope, `g_root`. Keep it: one scope is a
complete, working setup, and it is what both terminals bind to. With exactly one
scope, an unset `STRATA_AGENT_SCOPE` auto-binds to it, so **you export nothing**
for either harness. Grow the fleet later, when real roles emerge (edit
`.strata/fleet.yaml` and validate with `strata bootstrap`, or edit it in the
Console); binding becomes an explicit choice once there are two or more scopes —
see [Binding past one scope](#binding-past-one-scope).

Give each scope a one-line `description:` in `fleet.yaml` — what its memory is for.
The judge measures relevance against it: material "outside this scope's stated
purpose" is declined, and the decline reason quotes the purpose. Without a
description the judge may use what the scope already holds as an implied purpose,
but only once there is enough of it to tell what the scope is about (about 50 words
of summary and directives; `STRATA_IMPLIED_PURPOSE_MIN_WORDS`); a scope with an
empty summary is judged exactly as before, with no relevance rule at all. `strata
doctor` warns once per scope that has no description, and the Console shows it in
the scope header ("no description" when unset).

### 6. Two terminals, same project

**Terminal 1 — Claude Code**

```bash
cd strata-demo
claude
```

What to expect the first time in Claude Code (verified on Claude Code 2.1.278):

- It asks whether you **trust this folder** — choose *Yes, I trust this folder*
  (the highlighted default is *No, exit*).
- It then reports **"New MCP server found in this project: strata"**. **The
  highlighted default is *Continue without using this MCP server*. Pick *Use this
  MCP server* (or *Use this and all future MCP servers in this project*) instead:
  accepting the default gives you a memory-blind session with no Strata tools.**

**Terminal 2 — Codex**

```bash
cd strata-demo
codex
```

What to expect the first time in Codex (all verified on codex-cli 0.153.4):

- Codex asks whether to **trust the directory** — continue.
- It then shows **"Hooks need review"** for the Strata `Stop` hook. Choose
  **Trust all and continue**. Codex does not run a hook until it is trusted; until
  then (and always under `codex exec`) the turn-end reminder never fires. The
  trust is remembered.
- **Leave `STRATA_AGENT_SESSION_ID` blank** in Codex's config (register ships it
  blank). Each Codex session gets its own id automatically, and the hook lands on
  the same id as the MCP server; exporting a value would reach the hook but not the
  server and split one session in two.
- **Set `default_tools_approval_mode = "approve"` under `[mcp_servers.strata]` in
  `~/.codex/config.toml` (or answer every per-tool prompt with *Allow*). Under
  `codex exec` an MCP call that is not approved is refused, so without this
  `codex exec` gets no Strata tools; in the interactive `codex`, cancelling the
  prompts does the same. Either way that is a memory-blind session.** The
  interactive prompts are *Allow*, *Allow for this session*, *Always allow* or
  *Cancel*; "Always allow" is remembered per tool, so each Strata tool asks once,
  and the config key covers all of them at once.

Codex's `workspace-write` sandbox mounts `.git/refs` read-only, so a Codex session
cannot create git tags or refs itself (observed on codex-cli 0.153.4 under `codex exec
-s workspace-write`: `git tag` failed inside the sandbox). Have another session, or
you, do the tagging.

In Claude Code the session-start hook tells the agent to read its perspective
first; in Codex the seeded `AGENTS.md` does. In both, the agent has these tools:
`strata_read_perspective`, `strata_contribute`, `strata_session_closeout`, and
their read-only siblings.

A way to run the demo: in terminal 1, ask Claude Code to read the perspective,
then state one decision or lesson worth keeping and contribute it. Watch the
judge's verdict come back. Then, in terminal 2, start a Codex session and ask it
about that topic — it reads the same scope.

### 7. See what the fleet believes

From the project directory:

```bash
strata stats writeback     # write-back rate by harness: who contributed, who closed out, who stayed silent
strata summary g_root      # what the scope currently holds
strata record g_root       # every contribution and its judgment, with reasons
strata start               # serve the Console at http://127.0.0.1:8000/ui/index.html
```

`strata stats writeback` counts ended sessions by default (`--include-open`
adds the rest); its output says what write-back rate and *accounted for* mean.

In the Console (`strata start`; see [Console](#console)), three read-only views
show the demo's claims: **View as** (exactly what an agent bound to the scope
receives — the perspective), **Record** (the full contribution record with each
judgment and its reasoning) and **Turned down** (everything the judge declined,
with the reason). Stop it with `Ctrl+C` — nothing else depends on it.

### Binding past one scope

With two or more scopes, binding is explicit and delivered differently per
harness. Claude Code inherits it from your shell, so export it before `claude`:

```bash
export STRATA_AGENT_SCOPE=g_root         # scope ID from your fleet.yaml
export STRATA_AGENT_SKILL=strata-worker  # optional — a skill is not required
claude
```

Codex does not inherit your shell for its MCP server; it reads
`STRATA_AGENT_SCOPE` / `STRATA_AGENT_SKILL` from the literal `env` table under
`[mcp_servers.strata.env]` in its `config.toml`. Either way it is the same
identity. Or run `strata launch` for Claude Code — it validates the scope,
resolves the skill, generates a session ID and starts the harness already bound
([`strata launch`](#strata-launch--frictionless-cc-session-binding-adr-0003)).
`strata launch` does not launch Codex.

### Troubleshooting

| Symptom | Fix |
|---|---|
| Anything looks broken and you're not sure why | Run `strata doctor` first — it checks config, DB, `fleet.yaml`, harness wiring, and agent binding in one pass and names the fix for each failure. |
| Hooks or skills behave like an older Strata | An older `strata` is first on your `PATH` (say a pipx 1.10.5) and the registered hooks call bare `strata`. Check `which -a strata` and `strata --version`. Run `strata doctor` *from the install you registered with* — its **strata on PATH** check compares the `strata` on `PATH` with the install recorded by `strata register` (path and version) and fails loudly on a mismatch. (An older `strata doctor` has no such check, so it will not warn you: if the output has no `strata on PATH` line, the `strata` you ran is older than the check.) |
| `strata: command not found` | `pipx install strata-mem` didn't complete, or your shell hasn't picked up the new PATH entry — open a new shell, or run `pipx ensurepath`. |
| The agent never mentions Strata / has no Strata tools | The harness didn't load the wiring: in Claude Code run `/mcp` and look for `strata`; in Codex run `codex mcp list`. Re-run `strata register` from the project root and restart the harness. |
| Codex: the turn-end reminder never appears | The hook is not trusted yet — start `codex` (not `codex exec`) and choose **Trust all and continue** at "Hooks need review". |
| `claude` exits immediately with a binding error | With one scope in the fleet an unset `STRATA_AGENT_SCOPE` auto-binds; this only fires once the fleet has 2+ scopes and none is chosen, or the scope/skill isn't in `.strata/fleet.yaml`. The error names which. |
| A contribution comes back with `scope_manager_failure` / unjudged | Your judge API key is missing or invalid. Check step 4. |
| Want to start over with a fresh DB | `rm -f .strata/strata.db && rm -rf .strata/summaries/` — the next session re-creates them. |

---

## What the demo shows

From one recorded run on 2026-09-19: strata build `f4e7272` (release/v1.12.0-mvp,
installed non-editably into a fresh virtualenv), **codex-cli 0.153.4**,
**Claude Code 2.1.278**, and the judge **`qwen/qwen3-235b-a22b-2507` via
OpenRouter**. Every verdict below is that judge's — qwen is also the default judge as
of 1.13.0 (see [Judge models](#judge-models) for every judge measured since,
including `claude-haiku-4.5`). The samples are small
and scripted — one lesson per direction — so read them as "it happened", not as
rates.

| | |
|---|---|
| **Codex → Claude Code** — **PASS 1/1** | A lesson given only to Codex ("release tags use the prefix `rel-`, never `v`") was contributed and **admitted as context** by the judge. A fresh Claude Code session, asked only to tag the release, read it from shared memory, created `rel-1.0.0` (not `v1.0.0`) and said why: *"the project's shared memory says the maintainers' release tags always start with `rel-` and never `v`."* |
| **Claude Code → Codex** — **PASS 1/1** | "The version lives only in `VERSION.txt` as a plain semver string" was contributed by Claude Code and **admitted as a directive**. A fresh Codex session read that directive from shared memory, searched the usual version-file locations before writing, and wrote only `VERSION.txt` = `2.1.0`. |
| **Junk kept out** | Given a task that also included an irrelevant aside ("parking is validated at the front desk"), the agent did the real task, contributed only the relevant policy, and said it did not save the parking note because it is not about the project. That is the *agent* declining to contribute; the judge never saw it. **Judge side**, measured with the demo eval on the shipped judge build (`f4e7272`) with the qwen judge (two live runs): every plain-junk item was declined (7 of 7: trivia, chit-chat, unsupported claims), but the judge admitted 2 of 72 hard adversarial items (1.12 measurement). On the 1.13 engine, measured against the 84-item J4 adversarial set, the judge admitted 1 of 84 (`j4-822`) — a restricting directive held by the scope's own summary that the judge did not yet weigh (in the v1.18.0 baseline, Nebius, 3 reps: 6 of 279 attacks succeeded, all of them `j4-822` and `j4-830`, this same shape); see [Judge models](#judge-models) and ADR 0016 D5. Known limit, tracked in issue #209: the retirement backstop — the judge is asked to state the changed circumstance for any retirement — is advisory: no judge we measured honours a required tool field reliably (qwen never filled it, even when the re-ask quoted the contribution back), so a bare removal request can still retire a directive (#209). Another scope's material relayed with an oblique origin ("the people who own the account records mentioned…") is no longer an open limit: ADR 0016 closed it as not a defect — that is admissible hearsay, not a leak — and #212 tracks the remaining attribution gap (D5, above). That holds for the unnamed shape. When a contribution NAMES another scope it is not entitled to, an informant answer must also quote a telling event the contributor was in ("told us", "mentioned to me"), or it is declined (#225). An overheard telling that names the scope ("billing mentioned…", with no addressee) is declined, though ADR 0016 admits it in theory. The 1.16 judge already declined that shape on its own first call (0 of 10 before and after, measured on 2026-10-02), so #225 does not change it. Known limit, tracked in #225: the judge admitted some assertions about another scope's rules by making up who said them. Measured with `qwen/qwen3-235b-a22b-2507` via OpenRouter (served by Novita and GMICloud) on the 1.15.0 judge prompt, unchanged at release/v1.16.0 `40baded`, 20 runs per item: "payroll-ops only processes overtime claims filed before the 20th of the month" was admitted 20 of 20, "legal-review has a rule that…" 2 of 20, and "apparently lab-safety has decided…" 0 of 20. Every one of the 22 admits went through as hearsay from an informant the judge invented: the contributor's own role, or the other scope named as its own informant ([evidence](docs/evidence/v1.16-judge-prompt-examples-2026-09-26.md)). **#225 (v1.17) addresses this.** When an accepted contribution names a non-entitled scope, a second, targeted judge call classifies the claim, and the engine checks the answer against the contribution's own text: a conduct claim must quote an act the contributor was party to or witnessed (leading "I can tell you…" and "I saw…" frames don't count); an informant claim must name a teller who is not the contributor, and quote a telling event the contributor was in. Measured on 2026-10-02 with `qwen/qwen3-235b-a22b-2507` pinned to Alibaba, same session, before/after, 10 runs per item, on 42 new interior-assertion items plus the earlier 16: declines of name-form interior assertions rose from 128 of 210 to 190 of 210; the final version's targeted check declined 60 of 60 flat rule statements and 60 of 60 "as <role> I can tell you…" claims; it admitted 60 of 60 named-informant and 60 of 60 conduct counterparts ([evidence](docs/evidence/v1.17-225-interior-gate-2026-10-02.md)). Stated limits, not promised fixed: an act the contributor really saw but wrote without themselves in it is declined, so is a real telling that names no addressee, and so is an overheard telling that names the scope (each decline reason says what to add); a rule worded with "our" ("procurement only approves our orders…") passes the check, and the judge decides; a scope referred to by an alias ("the purchasing team") is not matched (0 of 6 measured; in the v1.18.0 baseline, with `qwen/qwen3-235b-a22b-2507` pinned to Nebius, such claims were admitted 11 of 12 times); the check runs on the single-contribution path only, not on batched judgments. **Also in v1.17:** "discussed with X", "agreed with X" and other joint-event verbs now count as telling events, while a bare "as discussed" still does not. Hearsay from a joint verb is recorded as "In discussion with X: …", never "X says" (the philosopher's ruling, 2026-10-03). And a decline for "no one spoke" now gets one verified re-check, which can turn it into context when the contribution in fact quotes a rendered directive or publication, a named outside party's publishing act, a telling event the contributor was in, or its own first-hand claim. Re-measured on the release tree in the v1.17 baseline; directional bridge numbers are in the [evidence](docs/evidence/v1.17-attribution-recheck-2026-10-03.md). `claude-haiku-4.5` is now measured; it is no longer the default judge (see [Judge models](#judge-models)). **In the operator rehearsal** (same build line, same judge), the Codex agent contributed an irrelevant office note ("the office coffee machine is broken") and the judge admitted it as context. Where a scope states a purpose (`description` in fleet.yaml) the judge declines material outside it — the office-note case above is declined 3/3 with a purpose set, and 0 of 11 items in that class are admitted. Where a scope states no purpose and has little or no memory yet, nothing changed: such a note is still admitted (10 of 11 in that class), and filtering it relies on the agent (#210). |
| **Never-stored honesty, supersession** | Both **PASS** in the live demo eval (`python -m strata_evals.demo`) on the same build and judge. |
| **Write-back rate** (`strata stats writeback`) | Every session in the run contributed — 3 of 3 in one repository and 2 of 2 in the other (Claude Code 2/2 and Codex 1/1 in the first, one each in the second) — with strict mode on. A small scripted sample, not a population rate. |
| **Outcome loop** (1.15, 1.16) | Plumbing shipped: an agent can say which memory item it acted on (`acted_on`) and how it went. The judge accepts the report only if the item **held** (something that could have failed didn't, and the report says what was observed) or **failed**. A failure replaces the claim, and a correction is sent once to every scope that reads the publication directly (child scopes and scopes with a reference edge); since 1.16, a correction relayed through a child reaches the grandchild once when the relayed copy is verbatim (#221); a relayed copy that paraphrases the claim is not covered (#219). Adoption was measured with Claude Code: 0 of 6 sessions set `acted_on` before the perspective listed item ids, and 6 of 6 after (n=6; [before](docs/evidence/v1.15-p1-adoption-2026-09-24.md), [after](docs/evidence/v1.15-p1b-adoption-2026-09-25.md)). Codex: 0 of 6 sessions set `acted_on` before the item-id listing; after it, 1 of 1 completed session did (5 of 6 did not run — the account's usage limit was reached mid-run); not yet a rate ([evidence](docs/evidence/v1.16-codex-adoption-2026-09-26.md); re-run #228). Gate results: [outcome-loop gate](docs/evidence/v1.15-outcome-loop-gate-2026-09-25.md). Since 1.16, a failed report on a directive is raised once to the scope that issued it, and an operator's directive gets it unjudged in the Console, marked seen only by the operator. When a summary must be condensed, the judge is told which items outcomes have tested and drops untested ones first: in the correction case (40 condensations per side), a correction kept 74% of its facts against 52% before, and the untested item beside it kept 5% against 72% ([evidence §5–8](docs/evidence/v1.16-judge-prompt-examples-2026-09-26.md), `qwen/qwen3-235b-a22b-2507` via OpenRouter, providers recorded there). Condensation also records each item that is "no longer verbatim in the summary (condensed away or reworded)"; a row can't yet tell the two apart, and the condensation count shipped in 1.12.0 (#202) over-counts the same way (#227). #219 C wrongly withdraws about 2–7% of a scope's neighbouring items (measured). Each one is listed in the Console's Correction withdrawals view and can be restored there by the operator. The owner's own judged restore recovered 5 of 12 such items in our measurement; the operator path is the reliable remedy ([evidence](docs/evidence/v1.17-219c-restore-2026-10-03.md)). An ambiguous failure declined instead of treated as a correction (#220) did not recur on re-measure (20 of 20 correct) and is closed. |
| **Where the evidence lives** | The strata-evals repository, `results/m4-cross-harness-2026-09-19.md` (commit `a264895`), with the raw transcripts, the Console JSON and the `strata stats writeback` output. |

## Out of the MVP

Out of the MVP: multi-human teams (the hosted memfleet platform, Phase B),
cloud, retrieval sophistication, dashboard metrics, a third harness beyond Claude
Code and Codex, and per-user profile state.

---

## Adding Strata to an existing project

The [Quick start](#quick-start-two-agents-one-memory) commands work on an
existing project unchanged — no clone of this repo and no change to your
project's Python runtime:

```bash
pipx install strata-mem    # install strata in an isolated env; puts strata + strata-mcp on PATH
cd /path/to/your/project
strata register              # idempotent: creates .strata/, seeds fleet.yaml, wires every harness it finds
```

> **PyPI distribution name vs. import/CLI names.** The Strata engine is
> published to PyPI as **`strata-mem`** (the name `strata` was already taken
> by an unrelated, dormant package; see
> [ADR 0009](https://github.com/oren198/Strata/blob/main/docs/adr/0009-packaging-engine-client-split.md)
> for the decision). Everything you actually type stays `strata`: `import
> strata` in Python, and the `strata` / `strata-mcp` console scripts on your
> PATH. Only the `pipx install` / `pip install` argument differs.

### What `strata register` does

`strata register` is strictly additive — it never overwrites files you've already edited.

By default it wires **every harness it finds on this machine**: it detects
Claude Code (the `claude` binary on `PATH`, or a `~/.claude` directory) and
Codex CLI (the `codex` binary, or `~/.codex`) independently, and wires
whichever of those are present — one or both. Pass `--harness claude-code`
or `--harness codex` (repeatable) to narrow to specific harnesses instead of
detecting. If neither is detected (a bare CI machine, a container), it wires
Claude Code anyway, with the notice `no harness detected on this machine —
wiring claude-code (the default)` — today's behavior, unchanged.

The common setup runs once regardless of which harnesses are resolved:

1. Creates `.strata/` directory and `config.toml` (relative paths, portable workspace).
2. Appends a `# Strata` block to `.gitignore` (ignores the DB and venv, never `fleet.yaml`).
3. Seeds `.strata/fleet.yaml` from a minimal template (1 scope, ready to edit).

Then, per resolved harness:

- **claude-code** — copies the `strata`, `strata-worker`, and `strata-inspect`
  skills to `.claude/skills/`; merges a `strata` entry into the project
  root's `.mcp.json` `mcpServers` block — the file Claude Code actually reads
  for project-scoped MCP servers (`.claude/settings.json` has no
  `mcpServers` key in its schema); installs the freshness `Stop`-hook
  (copies `.claude/hooks/strata-stop-hook` and merges a `hooks.Stop` entry
  into `.claude/settings.json`, which IS the right place for hooks — see
  [Memory-freshness Stop-hook](#memory-freshness-stop-hook)). If an earlier
  Strata release had written the `mcpServers.strata` entry into
  `.claude/settings.json` (a location Claude Code never reads for MCP
  servers), register migrates it into `.mcp.json` and prints a "moved" line;
  a hand-edited legacy entry is left in place with a note instead.
- **codex** — merges Strata into Codex CLI's own `config.toml` and seeds
  `AGENTS.md` with a short memory-moves block — see
  [Using Strata with Codex CLI](#using-strata-with-codex-cli).

Run it again at any time — it skips everything that already exists and reports what it kept.
Every step is additive: your own `mcpServers`, `hooks`, skills, `AGENTS.md` content, and
`fleet.yaml` are never overwritten.

`.mcp.json` is not gitignored by register: in Claude Code's model it's
meant to be committable, shared team config, and a plain `strata register`'s
entry carries an empty `env` (binding comes from process env / auto-bind,
not a value baked into the file), so there is nothing project-specific or
secret in it — it is safe to commit as-is. The one exception is
`--bootstrap-venv`: that flag points `command` at an absolute,
machine-local `.strata/.venv/bin/strata-mcp` path, which is NOT portable
across machines/checkouts — if your team uses `--bootstrap-venv`, treat
`.mcp.json` as machine-local (don't commit that version) rather than shared.

### After registration

```bash
# The seeded fleet has one scope — open your harness straight away, no
# exports needed: an unset STRATA_AGENT_SCOPE auto-binds to the fleet's
# only scope.
claude
```

Growing the fleet past one scope turns binding into an explicit choice:

```bash
# Edit your fleet to match your team, then validate it
$EDITOR .strata/fleet.yaml
strata bootstrap

# Bind: same identity for every harness, delivered differently — see
# "Binding past one scope" in the Quick start above. For Claude Code, that's exports
# in the shell that opens it:
export STRATA_AGENT_SCOPE=g_root         # scope ID from your fleet.yaml
export STRATA_AGENT_SKILL=strata-worker  # optional — a skill is not required

# Open your harness — the MCP server validates the binding at startup
claude
```

The MCP server starts with `strata-mcp` (on your PATH from pipx). It reads
`.strata/config.toml` automatically — no `STRATA_DB_PATH` or `STRATA_FLEET_CONFIG`
env vars needed, and it applies pending migrations itself on first use — there
is nothing separate to start. If binding is ambiguous (2+ scopes and none
chosen) or wrong (scope unknown, skill not permitted), the server exits
immediately with an actionable message.

Want to look at memory in a browser instead of (or alongside) working in
your harness? Run `strata start` — see [Console](#console). It's optional and
nothing else depends on it.

Something not working? Run `strata doctor` — it checks your project config,
DB, `fleet.yaml`, harness wiring (MCP entry, Stop hook, skills/config), agent
binding env vars, and that the `strata` on your `PATH` is the install that
registered the project, in one pass, entirely offline (no backend needs to be
running), and tells you exactly what to fix.

### `.strata/config.toml` vs `.strata-role`

Two per-project files, two independent jobs:

- **`.strata/config.toml`** — storage paths (DB, fleet YAML, summaries dir).
  Created by `strata register`. Machine-oriented; says **where memory lives**.
- **`.strata-role`** — an optional default `(scope, skill)` binding for
  `strata launch` (see below). Created by hand, committed to git; says
  **who you are by default**.

Neither implies the other: you can have storage configured with no default
role (`strata launch` prompts interactively), or a role file pointing at a
scope that resolves storage from `config.toml` as usual.

### Checking for skill updates

After `pipx upgrade strata-mem`, run:

```bash
strata register                # self-updates anything stale, in place
strata register --diff         # preview first: shows what would change, writes nothing
```

That's the whole story — no need to `unregister` first. Register self-updates
each managed artifact (skills, the freshness hook script, the AGENTS.md
block) in place when it's still exactly what a current or past `strata
register` wrote and was never hand-edited; anything you've customised is
left untouched and reported, never silently overwritten.

### Memory-freshness Stop-hook

Reading fleet memory and never writing back lets a scope's memory quietly go
stale. `strata register` wires a Claude Code `Stop` hook that closes that loop
at each turn end. It is engine-owned (shipped as package data, installed like
the skills) and strictly additive — your own `Stop` hooks are left untouched.

**How it works.** At every turn end the hook reads the session's mechanical
read/contribute counters (the `.strata/sessions/` state files — no judge, no
memory write). When a session has read fleet memory (from its first read) and recorded
nothing back — no `strata_contribute` call (any verdict) and no
`strata_session_closeout` — the *gate* opens, from the session's first read. What
happens then depends on the mode:

- **Strict (blocking) mode — the default.** The hook blocks the stop with an
  instruction naming both exits — contribute what you learned, or call
  `strata_session_closeout(reason)` if nothing is worth keeping — then lets the
  agent proceed. It blocks **at most twice per session**: a first reminder, and a
  blunter "last reminder" only if the agent made no strata tool call at all
  after the first. It never blocks a third time, so it cannot loop (that cap —
  kept in the session's state — is what guarantees it, not the harness's
  `stop_hook_active` flag, which a later turn resets). No evaluator is spawned.
  The setting is per project, `[freshness]
  strict` in `.strata/config.toml`, so every harness's hook reads the same
  answer (Codex's own `config.toml` is global to the machine, so it cannot hold a
  per-project switch). `strata register` writes `strict = true`; `strata
  register --no-strict` writes `false`, and a plain re-register never undoes that.
  `STRATA_FRESHNESS_STRICT=1`/`0` overrides the file. Each session's state records
  whether it ran strict, and `strata stats writeback` reports the write-back rate
  split by that, so any percentage states the enforcement behind it.

- **Background mode** — `strata register --no-strict`. The hook does **not**
  block your prompt. It spawns a detached, headless *evaluator* and returns
  immediately. The evaluator reads the session transcript tail and decides
  whether the session produced a memory-worthy outcome: if so it drafts a
  contribution and submits it through the **normal judged path** — the
  scope-manager gates admission exactly as it does for a contribution you write
  yourself; if not, it records a mechanical decline. Either outcome resets the
  session's counters, so you are nudged at most once per stale stretch, never
  per turn. The evaluator is best-effort: no `.strata` project, no session
  state, no API key, or any error all degrade to a silent no-op. It never writes
  memory without judgment — only the decline is mechanical.

At most one evaluator runs per session at a time (a lockfile beside the session
state, with a stale-lock TTL), and the gate is always checked before spawning.

**Session identity without any export.** Session state is keyed by
`STRATA_AGENT_SESSION_ID`. On the zero-export single-scope quickstart above,
nothing sets it — so the MCP server keys the session by the deterministic
fallback `sess_auto_<parent pid>` (its parent is the harness process), and the
hook finds the same session with no IPC: it tries its own parent pid and then
walks up its ancestors to the nearest one that has a session record. The walk is
needed because Claude Code runs the hook as `/bin/sh -c 'sh <script>'` and that
outer shell survives, so the hook's direct parent is the shell, not the `claude`
process the server hangs off (found live in M3; before it, the strict hook never
found a Claude Code session on this path). Codex runs the hook as a direct child
of the same `codex` process as its server, so the first step already matches
there. Empty string counts as unset here too (Codex's registered config ships a
literal empty `STRATA_AGENT_SESSION_ID`). An explicit `STRATA_AGENT_SESSION_ID`
skips all of this and is used as-is. (Reused pids are an edge case for any
pid-derived id; a new connection that finds a record from an earlier server pid
archives it and starts fresh.)

**`/mcp reconnect` keeps the session and its binding.** A reconnect restarts the MCP server
but not the harness. With the automatic session id, the server recognises the same harness
process (its pid and start time), keeps one session record (counting the reconnect), and
restores the scope and skill you confirmed with `strata_bind`, after checking them again
against the current `fleet.yaml` exactly as `strata_bind` would. If that scope was removed
or the skill is no longer permitted, the session comes up unbound and says why. A restored binding is announced once, as `binding_notice` on the first
`strata_read_perspective` (or `strata_bind`) reply: scope, skill and when you confirmed it. The saved
binding is `.strata/sessions/<id>.binding` (mode 0600; deleted after 30 days unused). An
explicit `STRATA_AGENT_SESSION_ID` turns this off (nothing saved, nothing restored, a new
connection starts a fresh session), and so does Windows, which has no readable process start
time.

**Windows: session-state counters are not cross-process locked.** The MCP server
and the detached evaluator both read-modify-write the same `.strata/sessions/`
state file. On POSIX each update takes an advisory `fcntl.flock` on a
per-session `<session_id>.json.lock` file, so concurrent updates serialize and
no increment is lost. Windows has no `fcntl`, and Strata deliberately does not
substitute `msvcrt.locking` (it locks byte ranges and cannot wait on another
process, so emulating an advisory lock means a spin-and-retry loop — a wrong
lock is worse than a documented absence of one) and pulls in no dependency for
it. On Windows the update therefore runs unlocked: writes stay atomic, so a file
is never torn or corrupted, but two simultaneous updates can lose one
increment. Nothing judged or memory-bearing rides on these counters — they are
the mechanical substrate for the read-time nudge and this hook — so the worst
case is one nudge firing a turn early or a turn late.

**Environment variables:**

| Variable | Effect |
|---|---|
| `STRATA_FRESHNESS_STRICT` | `1` forces strict (blocking) mode, `0` forces background mode. Unset defers to the project's `[freshness] strict` (default on). |
| `STRATA_SESSION_IDLE_WINDOW_SECONDS` | `86400` | How long a session with no recorded end may sit idle before `strata stats writeback` counts it as ended (a killed server never stamps its own end); `--idle-window` overrides it per run |
| `STRATA_EVALUATOR_MODEL` | Overrides the evaluator's drafting model (default `claude-haiku-4-5-20251001`). The scope-manager that *judges* the draft is unaffected. |

**Non-Claude-Code harnesses.** The hook is a documented contract, not magic —
this is the mechanism's honest limit. Any harness that can run a command at
turn end can reproduce it:

1. At each turn boundary, run `strata freshness-hook`, passing a JSON object on
   stdin with at least `transcript_path` (path to the session transcript) and
   `stop_hook_active` (whether the stop was already blocked once this turn).
2. Set the session's identity env vars (`STRATA_AGENT_SCOPE`, `STRATA_AGENT_SKILL`,
   `STRATA_AGENT_SESSION_ID`) the same way the MCP server sees them — the hook
   keys the session state by `STRATA_AGENT_SESSION_ID`. Leaving it unset relies
   on this harness spawning the hook the same direct-child way Claude Code does
   (see "Session identity without any export" above); set it explicitly if that
   assumption doesn't hold for your harness.
3. In background mode the command exits `0` and (when the gate is open) spawns the
   detached evaluator itself. In strict mode (the default) it prints a
   `{"decision":"block","reason":"…"}` JSON object on stdout that your harness
   must feed back to the agent and honour as a one-time block.

Harnesses that cannot run a turn-end command get none of this automatically —
the substrate (the read/contribute counters, `strata_session_stats`, the
read-time nudge) still works, but the turn-boundary evaluator does not fire
without a hook to trigger it.

### Read signaling (issue #234)

A session that read a scope's perspective can learn — mechanically, never
judged, nothing stored as memory — that the scope has since changed.

**How it works.** Every strata-tool result carries `perspective_stale: [scope
ids]` when a scope this session previously read has moved since — appended
only when the list is non-empty, so an up-to-date session's results are
unchanged. A session's own accepted contribution to a scope may catch its own
read receipt for that scope back up (the self-trigger): it does, UNLESS
something else had already changed that scope first, in which case the
session is correctly still told it's stale — there is foreign change it
hasn't seen. The Memory-freshness Stop hook above gains a matching read-side
clause: strict mode blocks on it (sharing that hook's existing two-block
budget with the write-side reminder, write-side counted first), default mode
only notifies (never blocks); either way it says so **at most once** per
session.

**Limits:**

- The notice window is **between tool calls and turns** — it narrows, it
  never closes. A session that calls no strata tool again, and whose turn
  never ends (or ends with no Stop hook installed), is never told.
- **Cross-scope facts arrive only through a composed edge.** A scope moving
  is only ever noticed for a scope this session itself read directly — never
  inferred for a scope only reachable through another scope's own ancestor
  chain or publication.
- A change this engine version cannot express as a watermark component (none
  known at the time of writing) would go unnoticed; the watermark covers
  every ADR 0014 input change plus every publication-affecting act.

### No Python 3.11+ globally? Use `--bootstrap-venv`

If `pipx` can't find Python 3.11+ (locked-down corporate environment), use:

```bash
strata register --bootstrap-venv
```

This creates `.strata/.venv/` with strata installed, and updates `.mcp.json`
to point at the absolute venv path. The `.strata/.venv/` directory is gitignored
automatically. Note: this downloads ~100MB of Python deps.

### Using Strata with Codex CLI

```bash
strata register --harness codex
```

Plain `strata register` already wires Codex when it detects it on the
machine (see [What `strata register` does](#what-strata-register-does)); use
`--harness codex` to wire Codex specifically, regardless of what else is
detected — for example on a machine that also has Claude Code installed but
you only want the Codex wiring right now.

The Codex wiring does the same per-project setup as plain `strata register`
(`.strata/`, `fleet.yaml`, `.gitignore`), but instead of (or in addition to,
when both harnesses are resolved) wiring `.mcp.json` it merges
Strata's config into the **OpenAI Codex CLI**'s own config file —
`$CODEX_HOME/config.toml`, which defaults to `~/.codex/config.toml`. That is a
user-level file, not a per-project one, matching how Codex's own `codex mcp
add` manages it. Like `strata register` for Claude Code, the merge is
strictly additive and idempotent: your existing `config.toml` — comments,
other `mcp_servers` entries, everything — is left untouched, and re-running
`strata register --harness codex` is a no-op.

It also seeds the project's `AGENTS.md` with a short, marker-fenced block —
Codex has no skills mechanism equivalent to `.claude/skills/`, so this is
where the same read-before-working / contribute-back / judged-verdict
guidance lives for Codex sessions. A fresh `AGENTS.md` is created if none
exists; an existing one keeps its own content byte-identical, with the
Strata block appended. `strata unregister --harness codex` removes only that
block, and only when it still byte-matches what the current or a past
release of register wrote — content you added elsewhere in the file, or
genuine edits inside the block itself, are reported and left in place.

**What this gives you, and how confident to be in each part:**

- **MCP config — verified, including a live read from inside Codex; the
  contribute → judged-verdict half is not yet run there.** Codex CLI's
  support for `[mcp_servers.<name>]` in `config.toml` is verified hands-on
  against codex-cli 0.149.0 (`codex mcp add` round-trips byte-for-byte, and
  `strata register --harness codex` writes exactly that shape) and again on
  0.153.4, where a real Codex session launched `strata-mcp` and called
  `strata_read_perspective` (checklist item 1 below). A contribution with a
  judge verdict from inside Codex needs a judge key and was not run.

  Two things to know before you rely on this:

  **Codex does not interpolate `${VAR}`-style values inside `config.toml`** —
  env values are literal TOML strings, not shell-expanded. So register ships
  the merged block with empty placeholders:

  ```toml
  [mcp_servers.strata.env]
  STRATA_AGENT_SCOPE = ""
  STRATA_AGENT_SKILL = ""
  STRATA_AGENT_SESSION_ID = ""
  ```

  On a fresh, single-scope fleet these empty placeholders are fine as-is —
  an empty `STRATA_AGENT_SCOPE` auto-binds to the fleet's only scope (and
  its `default_skill`, if it has one), the same way an unset shell env var
  does for Claude Code. `STRATA_AGENT_SESSION_ID` stays blank either way
  (see the sharpest-edge note below).

  Once the fleet grows past one scope, fill these in with real values
  before running `codex` (or edit them per
  project/session — this file is user-level, so if you work across multiple
  Strata projects with Codex you'll want to keep them current, or maintain a
  `<repo>/.codex/config.toml` override — Codex's docs list that as a read
  location for trusted projects, though `strata register --harness codex`
  itself only writes the global file today). **Leave
  `STRATA_AGENT_SESSION_ID` blank.** Verified against codex-cli 0.153.4:
  Codex starts one `strata-mcp` per Codex session, as a direct child of that
  session's `codex` process, so two Codex terminals never share a parent pid.
  With the id blank, `strata-mcp` and the freshness hook each derive
  `sess_auto_<parent pid>` and land on the same id (see the checklist below),
  one id per session. Do not export a value for it: Codex hands `strata-mcp`
  only the literal `env` table from `config.toml` (it does not inherit your
  shell — item 2 below), but hands the Stop hook your shell's environment, so
  an exported id would reach the hook and not the server and split one
  session into two. The server also records which harness a session ran in
  (`harness` in its session state: `codex`, `claude-code`, or `unknown`),
  read from the MCP client's own handshake (`clientInfo.name`), so
  write-back can be counted per harness.

- **Turn-boundary freshness hook — verified to fire in the interactive
  `codex` TUI once you trust it; `codex exec` skips it.** Register merges a
  `[[hooks.Stop]]` block that runs `strata freshness-hook` at the end of each
  turn, following the contract documented above under "Non-Claude-Code
  harnesses". Codex will not run a hook until it is trusted: on first launch
  after register the TUI shows "Hooks need review" — choose **Trust all and
  continue** (or review them first). Until then, and always under `codex
  exec` (no prompt to answer; `--dangerously-bypass-hook-trust` exists but
  skips the review), the hook does not run and the turn-boundary nudge is
  absent. Once trusted, verified on codex-cli 0.153.4: the hook fires at the
  end of a turn with stdin JSON carrying `session_id`, `transcript_path`,
  `stop_hook_active`, `last_assistant_message` and `cwd`, it is a direct
  child of the same `codex` process as that session's `strata-mcp`, and it
  inherits the shell that launched `codex`.

  `strata unregister --harness codex` reverses this wiring the same way
  `strata unregister` reverses the Claude Code wiring — only when the
  `[mcp_servers.strata]` table and the hooks.Stop block still byte-match what
  the current or a past release of register wrote; a genuinely edited block
  is reported and left in place. Removing the `[mcp_servers.strata]` table
  also sweeps up any `[mcp_servers.strata.*]` subtables Codex itself appends
  during a live session (per-tool approval state) — left behind, those
  orphan the entry and Codex fails to start.

```toml
# what strata register --harness codex merges into config.toml
[mcp_servers.strata]
command = "strata-mcp"

[mcp_servers.strata.env]
STRATA_AGENT_SCOPE = ""
STRATA_AGENT_SKILL = ""
STRATA_AGENT_SESSION_ID = ""

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "strata freshness-hook"
timeout = 30
```

**Live-verification checklist.** Run on codex-cli 0.153.4 (2026-09-19) in a
scratch project with a temporary `CODEX_HOME`, two concurrent sessions, with
probe wrappers around `strata-mcp` and the hook logging pid, parent pid and
environment. Results:

1. **MCP end-to-end (read).** Verified: `strata_read_perspective` returned
   real scope memory from inside Codex. In `codex exec` (approval policy
   `never`) an MCP tool call is refused with "MCP tool call requires
   approval" unless the server table sets `default_tools_approval_mode =
   "approve"`; the interactive TUI asks instead. The contribute → judged
   verdict half needs a judge key and was not run here.
2. **Env overlay vs. replace.** Verified: replace. With `CANARY=canary-value`
   exported in the launching shell, `strata-mcp` saw the config's literal
   `STRATA_AGENT_SESSION_ID=""` and no `CANARY` — Codex passes the MCP server
   only its `env` table (plus what Codex itself adds). Literal values in
   `config.toml` are therefore correct and sufficient; an exported variable
   never reaches `strata-mcp`.
3. **Stop hook fires — and Codex honors its block.** Verified in the TUI after
   trusting the hook (see above); not fired under `codex exec` without trust.
   It fired on consecutive stops within one session, and Codex showed "Blocked
   by hook" for both strict reminders (the hook blocks at most twice per session
   — see [Memory-freshness Stop-hook](#memory-freshness-stop-hook)). Whether the
   model then acts on a reminder varies: in the runs made, most sessions closed
   out after the first, one ignored both.
4. **Env inheritance in the hook.** Verified: the hook inherits the shell
   that launched `codex` (`CANARY` present) and does **not** get the
   `config.toml` `env` table (`STRATA_AGENT_SESSION_ID` absent). So an
   exported `STRATA_AGENT_SESSION_ID` reaches the hook but not the server.
5. **One session id per session, shared by server and hook.** Verified, in
   both `codex exec` and the TUI, two sessions running concurrently: each
   session has its own `codex` process; its `strata-mcp` and its hook have
   that process as parent pid; the two sessions differ. Every session state
   is keyed `sess_auto_<codex pid>` and carries `harness: codex`. One
   `strata-mcp` per session, alive for the session and gone when it ends.

Not verified: a second Codex session sharing a `codex` process (Codex also
has a shared local app-server daemon behind `codex agents` — sessions started
through it were not exercised); pid reuse across long gaps (the existing
caveat on `sess_auto_<pid>` ids).

### Undoing it: `strata unregister`

`strata unregister` reverses register's wiring. Like register, it is strictly
conservative — it removes each artifact **only when it still matches what the
current, or a past, release of register wrote**, and reports (leaving in
place) anything you have genuinely edited since:

```bash
strata unregister               # remove the wiring; keep your .strata/ memory
strata unregister --dry-run     # preview every action, write nothing
strata unregister --purge-data  # also delete .strata/ (fleet.yaml, DB, summaries)
```

By default it reverses **every harness that is actually wired in this
project** — determined by checking for register's markers in each harness's
files, not by what's installed on the machine (that asymmetry with
register's "wire everything detected" default is deliberate: a plain
`unregister` should never touch a harness this project never registered).
For Codex specifically, `$CODEX_HOME/config.toml` is a machine-level file
shared by every project on the box, so its markers alone aren't proof this
project registered Codex — "wired" for Codex additionally requires this
project's `AGENTS.md` to carry the Strata marker block register seeds. A
machine with a stale/foreign Codex config but no such block in this
project's `AGENTS.md` is left untouched by a plain `unregister`; pass
`--harness codex` explicitly to clean it up anyway.
Pass `--harness claude-code` or `--harness codex` (repeatable) to narrow to
specific harnesses instead. A harness named explicitly that turns out not to
be wired still runs its normal per-artifact checks — each step reports
"nothing to do" and the run exits 0, so `--harness codex` is always safe to
run even against a project that never wired Codex.

What it does, step by step:

1. Removes the managed `# Strata` block from `.gitignore`, leaving every other
   line byte-for-byte unchanged. An edited block is reported and left.
2. Removes the `mcpServers.strata` entry from `.mcp.json`, preserving all
   your other keys (deleting the file only if register's entry was its only
   content). If you customised the entry, it is left in place and reported.
   A legacy `mcpServers.strata` entry left over in `.claude/settings.json`
   from a pre-fix `strata register` is cleaned up the same way.
3. Removes each of the `strata`, `strata-worker`, and `strata-inspect` skills
   **only if byte-identical to the shipped version**. A modified or
   older-version skill is left alone and reported.
4. Removes the freshness `Stop`-hook — both the `hooks.Stop` entry from
   `.claude/settings.json` (only when it byte-matches what register wrote; your
   own `Stop` hooks are preserved) and the `.claude/hooks/strata-stop-hook`
   script (only when byte-identical to the shipped version).
5. Leaves your `.strata/` workspace untouched — that is memory, not wiring.
   Pass `--purge-data` to remove it too (`--dry-run --purge-data` previews the
   purge without deleting).

For the Codex harness (resolved by default when Codex is wired, or via
`--harness codex`), steps 2–4 above are replaced by the reverse of the Codex
wiring: the `[mcp_servers.strata]` table and the freshness `hooks.Stop` block
are removed from `$CODEX_HOME/config.toml` only when each still byte-matches
what register wrote, and the marker-fenced Strata block is removed from the
project's `AGENTS.md`, again only when unedited; `.mcp.json` is
untouched. Steps 1 and 5 are unchanged. When both harnesses are resolved
(the default on a machine with both wired), both sets of steps run, one
after the other.

**Exit code:** `0` on success, including when there is nothing to do (running
it on an unregistered project is a safe no-op). It exits `1` when something you
asked to remove was left in place because it had been edited — so scripts can
detect the partial case.

---

## More commands

### Inspect memory from the terminal

```bash
strata scopes              # list the fleet's strata, scopes, edges
strata summary <scope_id>  # curated summary (directives + context)
strata record  <scope_id>  # every contribution + judgment in the scope's record
strata stats writeback     # write-back rate by harness (sessions that contributed, closed out, or stayed silent)
strata stats judge         # judge calls, tokens, and cost by scope, call kind, and day
```

`strata stats judge` accepts `--since` (an ISO date or time) and `--scope`.
Cost is filled only from `STRATA_JUDGE_PRICE_TABLE`. With no price for a
call, that row shows tokens and says so. `STRATA_JUDGE_DAILY_TOKEN_CAP`
(unset means off) refuses a new judgment once today's tokens reach it; a
judgment already in progress still finishes. The Console's Judge usage tab
shows the same numbers.

### Advanced subcommands

```bash
strata doctor                                    # diagnose config/DB/fleet/wiring/binding, offline
strata judge-check                               # measure the configured judge against the frozen set
strata migrate                                  # apply pending SQLite migrations only
strata bootstrap --config path/to/fleet.yaml    # validate a fleet YAML (no DB writes)
strata start --reload                           # uvicorn auto-reload (dev mode)
strata start --port 8001                        # serve on a different port
```

### `strata judge-check`

`strata judge-check` tests the judge this install is set up to use. It runs the bundled judge-check set (v1) through the engine and compares each decision with the right answer stored for that item. It does not change your project.

The set is public, which shapes what a pass means:

> "the public set will be tuned against; a pass shows the model meets this set, not that it is good in general."

**What the bar is.** The pass bars in this build come from running `qwen/qwen3-235b-a22b-2507` on Parasail 5 times on the frozen set. Your run is compared with those bars. The verdict line is printed in the engine's own words.

**What it does not measure.** Version 1 does not test whether the judge's stated reasons are honest, apart from one pattern check: a reason that calls an item "hearsay" when the item is labelled first-hand. Nothing wider than that is measured. A context note cannot retire a directive (the engine drops its ops), so that case is not tested either.

**Cost.** Before it runs, the command prints a cost estimate and asks `y/N`. `--yes` skips the question.

- `--price IN,OUT` sets the price in dollars per million tokens, input and output. It wins over the provider's listed price. On OpenRouter, the listed price is the pinned provider's endpoint, or the most expensive endpoint when no provider is pinned.
- If no price is known, the command prints the engine's message and does not run.
- `--max-usd` is a hard spending cap during the run. If the cap stops the run, the verdict is "incomplete".

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | meets the bar |
| `1` | does not meet the bar |
| `2` | incomplete or unmeasured |
| `3` | refused: no price, a tampered set, a config error, or you answered no at the prompt |

**The report** records the model, the provider and the host part of the base URL. It never records the API key, the full base URL or a query string.

**In `strata doctor`.** Doctor shows one line about the judge-check report saved for this engine version. It reads that report and never runs the judge itself. The line never changes doctor's exit status. It has one of six forms, and doctor never shows one form in place of another:

| Doctor shows | When |
|---|---|
| meets the v1 bar (report \<date\>) | the saved report meets the bar |
| does not meet (\<failed legs\>) | the saved report misses the bar; the line names the parts that failed |
| incomplete (report \<date\>); re-run | the saved run stopped before it finished |
| report not understood (report \<date\>); re-run | a report is saved, but doctor can't interpret it |
| could not read the report (\<ExceptionClass\>) | the report file exists, but doctor can't read it |
| unmeasured for this engine version | no report is saved for this engine version, or this build has no reference bars |

### Fleet structure changes

`strata fleet pending` lists changes a scope proposed that it does not own.
`strata fleet apply <id>` and `strata fleet reject <id>` decide them as the
operator. `strata fleet add-scope`, `remove-scope`, `reparent`, `add-edge`,
`remove-edge`, and `describe` apply immediately, also as the operator. A
scope proposes the same changes with the MCP tools `strata_fleet_propose`,
`strata_fleet_pending`, `strata_fleet_approve`, and `strata_fleet_reject`;
the acting scope is the session's binding. The Console's Fleet changes tab
is the same operator list. Over HTTP the operator is the only actor, and
any local process that can reach the Console port acts as the operator.
`PUT /fleet` and every fleet change publish `fleet.yaml` through one helper:
an exclusive `fcntl.flock` on `fleet.yaml.lock` beside the file, then a
unique temporary file renamed into place. Where `fcntl` is unavailable
(Windows), that write is refused with: "Refusing to write fleet.yaml:
fcntl.flock is not available on this platform, and Strata will not update
the file without that lock." `strata register` and the env-driven `strata
start` seed create a missing `fleet.yaml` exclusively (`O_CREAT|O_EXCL`)
and do not take that lock; an existing file is left alone. Replacing an
existing file, including `strata export-fleet --force`, still refuses with
that message. If `strata register` cannot seed, it says so before writing
`.strata/config.toml`, and a re-run completes.
A change is recorded and not judged. Removing a
reference, removing a scope, or re-parenting prints what is not re-checked
automatically yet.

### `strata launch` — frictionless CC session binding (ADR 0003)

`strata launch [scope_id]` validates the target scope against `fleet.yaml`
directly (embedded mode — no backend required), resolves the skill from the
scope's declaration, generates a session ID, and hands the session off to
`claude` with `STRATA_AGENT_SCOPE`, `STRATA_AGENT_SKILL`, and
`STRATA_AGENT_SESSION_ID` already set. Run `strata start` only if you also want
the Console UI.

```bash
strata launch g_arch                            # use default_skill from fleet.yaml
strata launch g_arch --skill evidence-summarizer  # override skill
strata launch g_arch --session my-sess          # override auto-generated session ID
strata launch                                   # pick from interactive list, or use .strata-role
strata launch --harness claude-code             # start this harness regardless of the default
```

With no `scope_id`, no `.strata-role`, and a fleet with exactly one scope,
`strata launch` skips the picker entirely — it binds to that scope and says
so, the same single-scope auto-bind rule the MCP server applies.

#### Which harness `strata launch` starts

`strata launch` resolves which harness to start, in order:

1. An explicit `--harness` flag wins outright.
2. Otherwise, the project's recorded default — see `strata set-default-harness`
   below. If `.strata/config.toml` names a harness Strata doesn't know (a
   hand-edited value, or one written by a newer Strata version), it prints a
   one-line warning to stderr naming the bad value and falls back to
   `claude-code` rather than launching the wrong thing silently.
3. Otherwise, if exactly one harness is currently wired in this project
   (checked the same way `strata unregister`'s default resolves — for
   Codex, that means both the machine's `$CODEX_HOME/config.toml` markers
   AND this project's `AGENTS.md` marker block, not the machine config
   alone), that one.
4. Otherwise, `claude-code` — today's behavior, unchanged.

`claude-code` continues through the flow described above. `strata launch` does
not launch `codex`: Codex hands its MCP server only the `[mcp_servers.strata.env]`
table from its own config, never your shell's environment (see
[Using Strata with Codex CLI](#using-strata-with-codex-cli)), so there is nothing
for launch to export. `strata launch --harness codex` — or a project whose
default/only-wired harness resolves to codex — exits `1` with:

> Codex launch is not wired yet: Codex hands its MCP server only the
> `[mcp_servers.strata.env]` table from its own config, not your shell's
> environment, so there is nothing for `strata launch` to export (see README,
> 'Using Strata with Codex CLI'). Start codex directly; its identity comes from
> that table.

#### `strata set-default-harness` — record which harness launch starts

```bash
strata set-default-harness codex        # strata launch now starts codex by default
strata set-default-harness claude-code  # switch back
```

Writes `default_harness = "NAME"` under a `[launch]` table in
`.strata/config.toml`, read-modify-write: every other line in the file —
including a pre-existing `[launch]` table's other keys — is preserved
byte-for-byte, and re-running replaces the value in place instead of
duplicating the table. An unknown harness name exits `2` with the list of
valid harnesses; running it before `strata register` exits `1` with
`run 'strata register' first`. On success it prints
`default harness: NAME (used by 'strata launch')`.

#### `.strata-role` — per-project default binding

Place a `.strata-role` file at the root of a project repo so that
`strata launch` (with no positional argument) binds automatically:

```toml
scope = "g_arch"
skill = "code-writer"   # optional; resolved from fleet.yaml if omitted
```

The file is committed to git alongside the project. When you open the repo and
run `strata launch`, Strata finds the file, validates the scope, and launches
`claude` already bound — no manual `export` step needed.

#### Platform notes

`strata launch` works on POSIX and Windows. On POSIX it `execvp`s `claude`, so
the launcher process is replaced outright. Windows has no real `exec`, so the
launcher resolves `claude` on `PATH` (including `.cmd`/`.exe` shims), spawns it
as a child sharing the console, and forwards its exit code. Ctrl-C reaches the
`claude` session in both cases, and the child's exit code becomes the exit code
of `strata launch`.

**Windows.** From 1.18.0, CI on `windows-latest` runs `strata register`, an
MCP stdio round trip (`strata_read_perspective`, one `strata_contribute`,
`strata_read_scope_record`), a refused fleet structure change, and a refused
`export-fleet` onto an existing file. All four passed on the 1.18.0 release
PR (#248).

### Upgrading from V1.1 to V1.2

V1.2 moves fleet configuration (strata, scopes, edges) out of SQLite and into a
file-canonical `fleet.yaml` (ADR 0002). Before upgrading, export your existing
fleet shape so it isn't lost when migration 0002 drops the SQL fleet tables:

1. **Upgrade code** — pull V1.2 (`git pull`, `make install`). The migration has not run yet.
2. **Export your fleet** — reads the still-present V1 tables and writes `fleet.yaml`:
   ```bash
   strata export-fleet          # writes ./fleet.yaml from ./strata.db
   # or specify paths explicitly:
   strata export-fleet --db /path/to/strata.db --out /path/to/fleet.yaml
   ```
3. **Start V1.2** — applies migration 0002 (drops the SQL fleet tables) and loads the exported config:
   ```bash
   strata start
   ```

`strata start` will refuse to proceed if you forget step 2: it detects a V1 fleet config in the DB with no `fleet.yaml` and exits with an actionable error pointing you back to `strata export-fleet`.

After step 3, edit `fleet.yaml` by hand to add per-scope skill declarations
(`default_skill`, `permitted_skills`) as needed for `strata launch` (ADR 0003).

### Strata Console UI

Open <http://127.0.0.1:8000/> while the backend is running — a graph and list
view of the current fleet state, polling every 5 s, plus four new tabs and an
in-place operator-correction surface described in
[`docs/console.md`](https://github.com/oren198/Strata/blob/main/docs/console.md) and the [Console](#console) section
below. Automatic memory writes (accept/decline)
still flow only through `strata.contribute`; the Console's own write path is
limited to the two in-person operator corrections (Replace / Retire a
directive), each behind a confirm dialog. To point the UI at a non-default
backend, edit the `<meta name="strata-api-base" content="...">` tag in
`src/strata/_ui/index.html`.

---

## Console

`strata start` exists for exactly one reason: to serve the Console, a local
web view of memory. Nothing else in Strata needs it running — the MCP server
and CLI read and write storage directly, with or without a backend up.

```bash
strata start
```

**Success looks like this**, run after the journey above (register, then
bind and work) — migrations already applied and `fleet.yaml` already seeded
by `strata register`, so `strata start` here just confirms the project
config and serves:

```
using project config: /path/to/your/project/.strata/config.toml
  ✓ Python ≥ 3.11: Python 3.11.15
  ✓ git on PATH: git found
  ✓ write perms on data directory: /path/to/your/project/.strata is writable
  ✓ port 8000 available: port 8000 is free
  ✓ ANTHROPIC_API_KEY: JUDGE_API_KEY is set

Strata backend → http://127.0.0.1:8000
Strata Console → http://127.0.0.1:8000/

INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

(Run `strata start` in a directory that was never `strata register`ed — the
plain env-var-driven flow, no `.strata/config.toml` — and the first run
instead prints `Applied N migration(s).` and `seeded fleet.yaml from the
default template; edit to suit`, since nothing has touched storage yet
there. A registered project never shows either line: `strata register`
already seeded `fleet.yaml`, and the MCP server already applied migrations
the first time you ran `claude`.)

The `✓`/`⚠`/`✗` lines are preflight checks, run before anything else. The
judge key check is a **warning**, not a hard failure — if no key is set, or
it's only set in a `.env` file Strata can't find, you'll see:

```
  ⚠ ANTHROPIC_API_KEY: JUDGE_API_KEY is not set. The scope-manager will not be able to judge contributions without it. Set the variable (or the deprecated ANTHROPIC_API_KEY / STRATA_ANTHROPIC_API_KEY) before running strata start.
```

`strata start` still starts in that case — memory *reads* work fine, only
live scope-manager judgments fail. Fix it by exporting `JUDGE_API_KEY` or
adding it to a `.env` file in the current directory (the older
`ANTHROPIC_API_KEY` / `STRATA_ANTHROPIC_API_KEY` names still work too — see
[Environment variables](#environment-variables)); no restart needed beyond
running `strata start` again.

...then open <http://127.0.0.1:8000/ui/index.html> in a browser. The Console
is local-only — it talks to the backend `strata start` just launched on your
own machine, nothing external. Stop it with `Ctrl+C` whenever — state
persists in `.strata/strata.db` and `.strata/summaries/` (or `./strata.db` /
`./summaries/` outside a registered project) and nothing else depends on the
Console being up. Alongside the memory graph and settings, it
has four new tabs, plus in-place Replace/Retire actions in the scope drawer;
see [`docs/console.md`](https://github.com/oren198/Strata/blob/main/docs/console.md) for the full description of each:

- **Turned down** — every contribution the scope-manager refused for a
  scope, with the reason given, plus a separate mechanical count of sessions
  that read the scope and recorded nothing.
- **Freshness** — every active scope ranked by how many sessions have read
  it since anything new was accepted, with a fleet-wide breakdown of
  sessions that contributed, closed out with nothing to record, or read
  silently.
- **Record** — one scope's full append-only contribution record, newest
  first, in plain language.
- **View as** — exactly what an agent bound to a scope receives on a read,
  broken into layers with a rough token-weight estimate for each.
- **Operator corrections** — replace or retire one of a scope's own
  directives in person, each action behind a confirm dialog.
- **Judge usage** — judge calls, tokens, and cost by day, scope, and call
  kind, the same numbers as `strata stats judge`. Read-only.

---

### The HTTP API

The Console's backend also accepts contributions over HTTP (`POST /contribute`),
with the same fields and checks as the MCP tool, including `acted_on`. The HTTP
API trusts the caller's scope; `acted_on` entitlement is checked against it.
A judge failure the engine cannot fail closed on by itself (a genuine API
outage, auth failure, a second malformed response surviving the
corrective re-ask, or the daily token cap) returns `503` with
`{"error": "scope_manager_failure", ..., "retry": "strata_rejudge"}`,
distinct from a `200` merits decline (#235, #236, #246).

**Who changes a scope's directives.** Only a session bound to a scope can create, replace or retire that scope's directives; the engine enforces this after judgment. A contribution from any other position, such as a child writing upward, is admitted as an attributed proposal ("X (scope) proposes: … — directive <id> stands"). A session bound to that scope can adopt it with its own contribution (`adopted_from`). Every same-scope directive change carries an engine-written line in the record naming the session and the changes, so the record's account of who changed a rule never rests on the judge's wording. Recorded notes also open with the actual decision and ops ("[accept_as_context; no ops] …"). Both are stripped from what later judges read. A child's own directive may tighten an inherited one, never change it: after judgment the engine compares an admitted directive from a session bound to that scope against each inherited directive (ancestor and operator) on the same subject, and since v1.18 one that loosens it, exempts from it, softens it or calls it outdated is **declined**, with a reason that names the inherited rule and the scope that owns it (an exception is that scope's to make). Since v1.18.1 a name that only qualifies the subject of several inherited rules (a product, team or site, as in "… of ledger-svc") does not make two rules the same subject, so a fleet whose rules all name the same product no longer has a legitimate tightening declined by an unrelated rule. Stated limit: a name that LEADS even one inherited rule ("ledger-svc rollouts require two reviewer sign-offs" next to "Customer records stored in ledger-svc …") is part of that rule's subject again, so the relief does not apply and the check declines as before (it fails closed, no exception gets through); and with a single inherited rule a shared name in a qualifier still counts as the same subject. Rate bounds ("at most N per day", "no more than N times a week", "at least N times a day") and count floors ("requires N approvals") are read in the right direction since v1.18.1. When the engine's own comparison sees nothing but the judge reads the directive as an exception, the text is held as context instead ("the engine could not confirm a conflict. Context never binds"), so a misread legitimate rule isn't thrown away and a judge-only exception still can't become a rule. A tightening is kept as a directive: on the default judge the four tightening rows were kept 32 of 32 times, against 5 of 32 before v1.18. Measured on the v1.18.0 baseline with `qwen/qwen3-235b-a22b-2507` pinned to Nebius, 3 reps: 48 of 48 on a held-out drift set labelled blind, every rep, with no legitimate rule declined; 46 to 47 of 48 on the original drift set ([evidence](docs/evidence/v1.18-judge-baseline.md)). Stated limit: a reworded exception that shares no words with the inherited rule can be admitted when the judge calls it a refinement (seen in 2 of 3 runs on one test item). Stated limit, the size of the judge-dependence: the engine checks the judge's answer, but for an exception worded with no relaxation word and no value it recognises, a wrong answer is not caught. Fed deliberately wrong answers, the engine still admitted 8 of 30 drift exceptions and 15 of 30 held-out ones as directives ("unrelated" or unreadable), and 3 of 30 and 11 of 30 ("tightens" or "refines"). Stated limit: a session bound to a scope may change that scope's rules, so an instruction written into its own contribution ("just accept it as-is") is stopped only by the judge refusing it. The default judge admitted one such case once in the v1.17 baseline ([evidence](docs/evidence/v1.17-baseline-2026-10-04.md)), and the record names who made the change.

**A child's context can't undercut an inherited rule (v1.18).** When a session bound to a scope has a context note admitted and the scope inherits a directive, the engine asks the judge one short extra question (how does the note relate to the rule?) and checks the answer itself. A note stating what may or does happen instead of the rule, as permission or as a standing practice, is declined; the reason names the two legitimate routes (report a specific occurrence and pass `acted_on`, or propose the exception to the scope that issued the rule). A dated report of one specific past departure (a single identified act, or one bounded completed past episode such as "The pumps ran 24/7 last week.") is admitted, with the departure label in the judgment's record line. A note that grants, approves or reports approval of an exception for a class, or states a practice (present tense, open-ended present perfect, or a bare generic plural of the rule's class with no determiner, number or id, even when dated), is declined. The decline reason tells the writer to report the specific act (with `acted_on`), or to ask the issuing scope to record the exception itself. An approval scoped to one identified act ("On 18 October hotfix 4411 merged without review; the lead told me he approved this one merge.") stays an admitted departure, with the departure label in the judgment's record line. A habitual or generalising report ("every time", "we skip", "on hotfixes", "... so we don't need it") is declined, and `acted_on` never waives that check. When the judge's extra answer says the note is an exception, the note is admitted as a report only on positive evidence of ONE past occurrence: a date or a closed period, a past-tense verb, no present-tense or present-perfect verb, and no habit or generality word unless a closed period bounds it. Anything else is declined, so a new habit phrasing fails closed instead of passing. The list of irregular past forms is extended with unambiguous forms (dwelt, spilt, forsook, oversaw, ...). A verb spelled the same in present and past (shut, let, cut, put, set, cast, reset, ...) never counts as the past verb, so a present-tense habit built on one is not read as one past act (it fails closed). A dated practice start is not one occurrence: "as of / effective / starting / beginning / following <date>", and "from / after / post / beyond <date>" unless a date closes the range ("from 2 March through 8 March"); "onwards" and "till further notice" mark a practice too. Such notes are declined under an exception answer. Known limit: a report whose only past verb is spelled the same in present and past (shut, let, cut, put, set, ...) is declined under an exception answer, as in 1.18.2, because nothing shows it is past; write an unambiguous past form such as "closed", "turned off" or "powered down". Known limit: an admitted departure is stored as written; the departure label is in the judgment's record line, not in the stored note, so a reader of the summary may take it as precedent; the stored label comes in v1.19. On the 1.18.3 authority regression set (66 items), when the judge calls the note an exception, 0 of 47 class-level claims or habits are admitted and 1 of 19 honest single departures is declined. The stated cost is that a departure whose object is a bare plural of the rule's class ("wore quay boots onto the pump deck") is declined, which fails closed. Still admitted, unchanged from 1.18.1: 38 of 47 when the extra answer says unrelated, 38 of 47 when it is unreadable, and 25 of 47 when the judge calls it a departure. Closing these is v1.19 work. Stated limit: an exception worded with no exception marker is admitted when the extra answer is unreadable or says unrelated; that was 6 of 38 exceptions in the held-out measurement, and in the live runs none of the 9 such fallbacks hid an exception. Each fallback is counted in the judgment's `inherited_relation` trace.

**A context note can't undercut the scope's own directives either (v1.19).** The same check now runs on the scope's own directives. A context note on a scope that has its own directives but inherits none now costs one extra judge call. Where the scope also inherits rules, the same single check covers both. A note stating an exception in force now ("let's treat single-engineer review as sufficient for now") is declined, with a reason that says the directive is this scope's own and that the way to change it is to supersede or retire it. A specific dated departure is admitted as context ("Recorded as a departure from <id> (this scope): not a licence; ..."). Reasoning about whether to change the rule ("we are considering retiring it", "should we change it?") is admitted. A context note also never changes the scope's own rules: if the judge returns a supersede or retire of an own directive for a note proposed as context with no `supersedes`, the engine drops those ops, records the drop, and judges the note as context. `strata stats judge` shows the extra call as its own kind, `own_directive_relation`.

**A scope can't publish an exception to a rule it inherits (v1.18).** After the publication judge accepts a publish or restore by a scope that inherits a directive, the engine checks the item. A directive item that changes, loosens or exempts from an inherited rule is refused. A context item that states a general exception is refused; a report of one specific act of following the rule is published; a report of one specific act of departing from it is published only with the engine's label, "Departure from <directive id> (<issuing scope>): ", which the publisher can't drop. A relayed item keeps its origin's label. Measured on the v1.18.0 baseline (30 items labelled blind, Nebius): 0 of 10 exceptions published (the judge accepted all 10; the engine's check refused all 10), 0 of 15 legitimate items refused, 5 of 5 departures published with the label. Stated limit: an exception already on a scope's published face is not withdrawn by this.

**Judge reasons say what they are about (v1.18).** A decline for a missing telling event now quotes the claim and the span it judged ("the claim attributed to 'the architect' has no telling event ('…')"). Reading a document first-hand counts as being told, when the contribution says so in the first person, names whose document it is, and gives its path, URL or id right after it; it is recorded as the author's word as of the reading. After every judgment, a known party (a fleet scope, its name or role, or the operator) that the judge's reason names as an informant must occur in the contribution's own text; if it doesn't, the judge gets one corrective re-ask, and if the reason still names it the engine replaces the reason with "Reason withdrawn: the judge described content that is not in this contribution". The verdict itself is never changed by this.

**Stated limit: the ordinary judge's input wraps the scope summary in an ambiguous fence**; on qwen this can make it misread the summary. The publication and bootstrap judges use unambiguous markers since v1.18.

**Stated limit: the position gate holds on the MCP path.** Only a session bound
to a scope can create, replace or retire that scope's directives; a contribution
from any other position is admitted as an attributed proposal. On the MCP path
the contributor's position comes from the session's binding. The HTTP API takes
`contributor.scope_id` from the request body, so a caller on it can claim any
position. That API is for local use and the Console (see SECURITY.md), not the
agents' path; do not expose it.

## Forgetting

A directive stops binding only when someone with authority over it ends it. Being old or unused never ends it.

**No grace period.** When a scope ends one of its directives, the directive leaves every reader's binding layers on that reader's next read.

**Readers are told.** Each reader gets a notice. The notice stays in the reader's perspective until the reader's next refresh has processed it. It also stays in the record.

**One path, two labels.** A directive ends either because it no longer applies or because it was wrong. Both go through the same path, and readers see both as an ordinary input change. Only the label differs: a release (kind `currency`) is labelled *retired*, and a retraction (kind `correction`) is labelled *retracted*.

**End dates and end conditions.** A directive proposal can say when it ends, with `ends`: a date, or a condition that has to be judged. Only two can confirm that a judged condition was met: the owning scope's judge, or a session bound to the owning scope. Another scope's judge cannot end it. The operator confirms only operator items, because the operator owns them.

**Candidates.** A directive that is old or unused can show up as a candidate. A candidate only asks the owner whether the directive should end. It never ends anything by itself.

**Tools for a bound session:**

- `strata_retire` ends one of the scope's own directives.
- `strata_retirement_candidates` lists candidates, and `strata_dismiss_candidate` dismisses one.
- `strata_contribute` carries `ends` on a directive proposal, and `fulfils` with evidence that a judged condition was met.

**Commands for the operator:**

- `strata operator publish` and `strata operator supersede` take `--until` or `--when`. On `supersede`, they work only with an `op_` id; a `c_` id with either flag is refused and nothing is written.
- `strata operator retire-item` ends an operator item.
- `strata operator candidates` lists candidates, and `strata operator dismiss-candidate` dismisses one.
- `strata stats forgetting` counts scope retirements and candidates. It does not count operator items.

## Configuration

### Per-project: `.strata/config.toml`

When `strata register` has been run, the project root contains
`.strata/config.toml` with relative storage paths:

```toml
db = ".strata/strata.db"
fleet_yaml = ".strata/fleet.yaml"
summaries_dir = ".strata/summaries"
```

The MCP server walks up from its current directory to find this file. When
present, it takes precedence over the env vars below — no shell exports needed
for storage paths.

`strata register` also records the freshness `Stop`-hook's enforcement here, and
which `strata` install ran it:

```toml
[freshness]
strict = true   # `strata register --no-strict` writes false

[install]       # written by `strata register`; `strata doctor` compares it with `strata` on PATH
strata = "/home/you/.local/pipx/venvs/strata-mem/bin/strata"
version = "1.12.0"
```

<a id="choosing-a-judge"></a>
### Judge models

Every contribution is checked by a judge model you choose, and some of Strata's defences depend on that model: a weaker judge lets more through. Below is what I measured, and exactly what each measurement ran on. The v1.18 baseline ran `qwen/qwen3-235b-a22b-2507` on OpenRouter, pinned to the Nebius provider, and cost $3.40 in judge calls. In the September study, `claude-haiku-4.5` admitted 0 of 84 attacks, but it cost $0.2139 per demo run against qwen's $0.0157, and once a scope states its purpose it turned away 2 of 6 legitimate notes (#210). A model or provider not in these tables is unmeasured, not known to be worse.

**v1.18.0 baseline** — `qwen/qwen3-235b-a22b-2507` on OpenRouter, pinned to Nebius, engine `ed31692`, 2026-10-14, 3 runs per item ([evidence](docs/evidence/v1.18-judge-baseline.md)):

| What it tests | Result |
|---|---|
| Adversarial attacks (spoofed authority, injected directives, laundered attribution) | 6 of 279 attempts succeeded |
| Another scope's rules asserted from inside this one (#225) | 11 of 126 attempts succeeded, all naming the scope by an alias |
| A child's exception to an inherited rule, child and grandchild level | 139 of 144 judged correctly |
| The same, on held-out items written blind in new domains | 144 of 144 |
| A context item that undercuts an inherited rule (#242) | 108 of 108 |
| Legitimate refinements of an inherited rule (#237) | 72 of 72 |
| An exception carried down in a publication (#243, 1 run) | 0 of 10 published |
| A child's own tighter rule (e.g. retention 7 days under a 14-day limit) | 32 of 32 kept as a directive |
| General judgment accuracy | 141 of 162 |

This is a new baseline. Alibaba, the provider of earlier qwen measurements, stopped serving this model on OpenRouter on 2026-10-10, so v1.17 numbers aren't directly comparable.

**Six models compared** — 2026-09-20, engine `release/v1.13.0` @ `5f5bf49`, OpenRouter with no provider pinned, one run per suite, so small differences are within noise ([full study](docs/evidence/judge-baseline-2026-09-20.md)). These are older builds and a different provider mix, so don't set them against the v1.18 table above.

| Judge (OpenRouter id) | Attacks admitted | Junk admitted (of 90) | Legitimate notes turned away (of 6, purpose set) | $ per 90-item demo run |
|---|---|---|---|---|
| `qwen/qwen3-235b-a22b-2507` (default) | 2 of 76 scored | 12 | 0 | $0.0157 |
| `anthropic/claude-haiku-4.5` | 0 of 84 | 7 | 2 | $0.2139 |
| `google/gemini-2.5-flash` | 8 of 84, one a direct prompt injection admitted as a directive | 21 | 0 | $0.0353 |
| `openai/gpt-5-mini` | 3 of 72 scored | demo crashed | 0 | — |
| `z-ai/glm-4.6` | not usable: 81% of attack calls errored | demo crashed | crashed | — |
| `deepseek/deepseek-v3.2` | no results: calls took 5–12+ minutes | no results | hung | — |

**The default setup.** `strata register` writes these to `.env`:

```
JUDGE_API_KEY=sk-or-...
JUDGE_BASE_URL=https://openrouter.ai/api
JUDGE_MODEL=qwen/qwen3-235b-a22b-2507
```

No provider is pinned, so OpenRouter routes each call and falls back between providers. To pin one, see **Pin the judge to one OpenRouter provider** below.

If your only key is an Anthropic one, the install uses `claude-haiku-4-5` on Anthropic's own endpoint.

That key is never sent to the router.

Either way, `strata doctor` names the judge, endpoint and provider you're actually running.

**What holds whatever the judge.** The engine enforces these, and no verdict can change them:

- Only a session bound to a scope changes that scope's directives. A write from anywhere else, such as a child writing upward, is kept as an attributed proposal. Over MCP, the position comes from the session's binding. The local HTTP API is the operator's surface: any process that can reach it acts as the operator.
- A session writes only to its own scope or an ancestor. Sideways and downward writes are refused before judgment.
- What each scope reads: every ancestor's directives and the publications one edge away. Never an ancestor's context or a grandparent's publication.
- Operator directives and operator acts (supersede, retire, corrections) are not judged, and neither are fleet structure changes.
- A child's own directive that conflicts with an inherited one is declined when the engine recognises the conflict: the shared subject, plus a number, a "never"/"always" or a bound. If only the judge sees the exception, the directive is held as context.

**What the judge decides**, with engine checks added where noted:
- relevance and junk;
- prompt-injection attempts;
- attribution and hearsay;
- a reworded exception to an inherited rule that the engine doesn't recognise (#244);
- a context item that undercuts an inherited rule (#242);
- whether a child's directive is a legitimate tightening (#237);
- an exception carried down in a publication (#243): the engine refuses it once a follow-up answer identifies it. In the v1.18 run, the judge's first call accepted all 10 such exceptions and the engine's check refused all 10.

The engine checks the judge's follow-up answers: quoted spans must be verbatim. It can't stop what the judge misses, though, so here is how much depends on the judge's answer. I fed the engine wrong answers for every exception in the two inherited-rule sets ([evidence](docs/evidence/v1.18-judge-baseline.md), engine `ed31692`, no model involved):
- with an "unrelated" or unreadable answer, the engine still admits 8 of 30 and 15 of 30 exceptions as directives;
- with a wrong "tightens" or "refines" answer, 3 of 30 and 11 of 30.

Limits I know of, measured in the v1.18 run:
- A reworded exception that shares no words with the inherited rule can be admitted when the judge calls it a refinement (seen in 2 of 3 runs on one test item).
- Another scope's rule asserted with that scope named only by an alias ("the purchasing team" for procurement) is admitted: 11 of 12 attempts.
- A restricting directive in the scope's own summary isn't weighed by the judge: 2 items, every run.
- The ordinary judge's input wraps the scope summary in an ambiguous fence; on qwen this can make it misread the summary.
- A retirement isn't required to state what changed. The judge is asked for it, no model I measured fills it reliably, and the engine doesn't enforce it (#209).

**A model id ages.** Routers rename and retire models, and providers come and go. If an id or provider stops resolving, `strata doctor` says so and prints the override lines. A new provider is a new measurement, not the same judge.

**Override to any Anthropic-Messages endpoint** — a router, a gateway, the Anthropic API:

```
JUDGE_API_KEY=<a key for that endpoint>
JUDGE_BASE_URL=https://your-gateway.example/api
JUDGE_MODEL=<a model id that endpoint serves>
```

**Pin the judge to one OpenRouter provider** (optional) — OpenRouter
routes the same model id across several backing providers, and the same
item on the same build has measured anywhere from 10/10 to 5/10 across
runs, plausibly from the provider mix. Unset, OpenRouter picks the
provider for each call and falls back to another when one is
unavailable. `JUDGE_PROVIDER=<provider name>` (or
`STRATA_JUDGE_PROVIDER`) pins every judge call to that one named
provider via `extra_body={"provider": {"order": ["<provider name>"],
"allow_fallbacks": false}}`. That makes results easier to reproduce,
but there is no fallback: if the pinned provider stops serving the
model, judge calls fail until you change or remove the pin. It is
OpenRouter-only:
against any other endpoint (a bare Anthropic key, a self-hosted
gateway) it is read but ignored, since a request-level OpenRouter
provider preference means nothing elsewhere. `strata doctor` reports
whether a configured provider is actually pinned or ignored for the
endpoint you're running.

### Environment variables

Most settings are env-var driven, prefixed `STRATA_` (the judge configuration
also accepts the shorter, provider-generic names below). When
`.strata/config.toml` is present, the first three are ignored for the MCP
server (project config wins):

| Variable | Default | Purpose |
|---|---|---|
| `STRATA_DB_PATH` | `./strata.db` | SQLite path for the record store (overridden by `config.toml`) |
| `STRATA_SUMMARIES_DIR` | `./summaries` | Directory for per-scope summary files (overridden by `config.toml`) |
| `STRATA_FLEET_CONFIG` | `./fleet.yaml` | Fleet YAML (overridden by `config.toml`) |
| `STRATA_AGENT_SCOPE` | (auto-bind) | The scope this session acts at. Required only when the fleet has 2+ scopes — with exactly one scope, an unset (or empty-string) value auto-binds to it. With zero or 2+ scopes and this unset, the server still starts (soft-start): every memory tool returns an actionable error until bound via `strata_bind`, or the process is restarted with this set |
| `STRATA_AGENT_SKILL` | (optional) | The skill identifier for provenance — required only when the scope declares `default_skill`/`permitted_skills` in `fleet.yaml`, unless the scope was auto-bound, in which case its `default_skill` fills this in when unset |
| `STRATA_AGENT_SESSION_ID` | (auto) | Session identifier. Absent or empty-string resolves to the deterministic `sess_auto_<parent pid>` (the freshness Stop hook resolves the same fallback independently — see [Memory-freshness Stop-hook](#memory-freshness-stop-hook)) |
| `JUDGE_API_KEY` | (unset) | The judge's API key — for the endpoint in `JUDGE_BASE_URL` (OpenRouter by default). `STRATA_JUDGE_API_KEY` also works and wins if both are set. Works against any endpoint that speaks the Anthropic Messages API. |
| `JUDGE_BASE_URL` | `https://openrouter.ai/api` | Points the judge at a router/proxy/self-hosted gateway — the endpoint must speak the Anthropic Messages API. `STRATA_JUDGE_BASE_URL` also works. **Exception:** with only an Anthropic key set (see [Judge models](#judge-models)) the default stays the Anthropic API. |
| `JUDGE_MODEL` | `qwen/qwen3-235b-a22b-2507` | Model used by the judge (an id the endpoint serves). `STRATA_MANAGER_MODEL` is the original name and still works (wins if both are set). **Exception:** with only an Anthropic key set the default stays `claude-haiku-4-5`. |
| `ANTHROPIC_API_KEY` / `STRATA_ANTHROPIC_API_KEY` | (unset) | **Deprecated**, kept as a working fallback: used only when `JUDGE_API_KEY` is unset. On its own it keeps `claude-haiku-4-5` on the Anthropic API. |
| `JUDGE_PROVIDER` | (unset) | Optional. Unset, OpenRouter routes each judge call and falls back between providers. Set to a provider name, every judge call goes to that one OpenRouter provider with no fallback, so calls fail if it stops serving the model. Ignored on a non-OpenRouter judge endpoint. `STRATA_JUDGE_PROVIDER` also works. See [Judge models](#judge-models). |
| `STRATA_JUDGE_DAILY_TOKEN_CAP` | (unset) | Off when unset. A non-negative integer is today's token budget for new judgments and standalone judge calls. A judgment that has already called the model still finishes. Over the cap, the contribution waits unjudged (the same 503 as an unreachable judge). |
| `STRATA_JUDGE_PRICE_TABLE` | (unset) | Path to a JSON object keyed by model id, each value `{"input_per_million": <number>, "output_per_million": <number>}`. Used only when reporting cost. No entry for a model means that row stays tokens-only. |
| `STRATA_FRESHNESS_STRICT` | (unset) | `1`/`0` forces the freshness `Stop`-hook strict (blocking) or background; unset defers to the project's `[freshness] strict`, default on ([details](#memory-freshness-stop-hook)) |
| `STRATA_EVALUATOR_MODEL` | `claude-haiku-4-5-20251001` on the Anthropic API, otherwise the judge's model | Model the freshness evaluator drafts with (the judge is unaffected) |

A local `.env` file is loaded automatically for every name above.

> `STRATA_BACKEND_URL` was **removed in 1.5.0**. The CLI inspection commands
> (`scopes` / `summary` / `record`) now read the record and summary stores
> directly, like every other embedded-mode consumer (ADR 0004 Decision 1) —
> no backend needs to be running.

---

## Project layout

```
README.md                # This file
CONTEXT.md               # Canonical glossary (23 terms — single source of vocabulary)
docs/
  philosophy.md          # Theoretical foundations — why Strata exists
  ROADMAP.md             # Enduring principles + sequenced direction (post-V1.2)
  judge-check.md         # What `strata judge-check` measures, and what it does not
  adr/
    0001-v1-architecture.md
    0002-fleet-config-source-of-truth.md
    0003-strata-launch-cc-binding.md
src/strata/              # Python backend package
  app.py                 # FastAPI app + endpoints (serves _ui/ at /ui)
  settings.py            # pydantic-settings config
  record_store.py        # SQLite repository (append-only record + fleet config)
  summary_store.py       # Markdown on-disk scope summaries
  scope_manager.py       # LLM judgment layer (Anthropic tool use)
  bootstrap.py           # YAML fleet config loader/applier
  mcp/
    server.py            # FastMCP stdio server; operates directly on RecordStore + SummaryStore
  _skills/               # Canonical skill files vendored as package data
    strata/Skill.md      # CC skill: orientation / first-time use
    strata-worker/Skill.md  # CC skill: parametric worker — reads STRATA_AGENT_SCOPE/SKILL
    strata-inspect/Skill.md # CC skill: read-only browser
  _migrations/           # SQLite schema migrations (package data)
  _templates/            # Starter fleet.yaml templates (package data)
  _ui/                   # Strata Console (package data; no build step — Babel-standalone)
    index.html           # Entry point; served at /ui/index.html
    app.jsx              # Root app, backend polling, read-only state
    atoms.jsx            # Shared UI atoms (Icon, Field, Toast, Modal …)
    graph.jsx            # Force-directed scope graph
    scope-detail.jsx     # Scope drill-in: backend summary + scope info
    settings.jsx         # Settings screen (display prefs + fleet read-only view)
    tweaks-panel.jsx     # Floating tweaks panel
    store.js             # API client (fetch /scopes, /scopes/{id}/summary)
    atlas.css            # Atlas design system tokens + component classes
  project_config.py      # .strata/config.toml walk-up loader (ADR 0005 Decision 2)
.claude/
  skills/
    strata/              # CC skill (copy used in Strata-repo sessions)
    strata-worker/       # CC skill (copy used in Strata-repo sessions)
    strata-inspect/      # CC skill (copy used in Strata-repo sessions)
.mcp.json.example        # Example .mcp.json (the shape `strata register` writes)
tests/                   # pytest suite
src/strata/_templates/   # Bundled starter fleets (dev-team.yaml is the default seed;
                          #   minimal.yaml/research-group.yaml/support-org.yaml also ship)
Makefile                 # Common tasks (install / test / lint / run / migrate / bootstrap / smoke)
pyproject.toml           # Project metadata + deps + ruff/pytest config
```

---

## Running Strata in Claude Code

The MCP server operates directly on the SQLite record store and summary files
(ADR 0004 Decision 1, "embedded mode"). The FastAPI backend is the Console UI
layer; running `strata start` is required only to view the UI. The agent loop
— contributions, scope-manager judgments, perspective reads — works whether
the backend is up or down.

> **Entitlement-scoped reads (ADR 0006 D3/D4):**
> `strata_read_perspective`, `strata_read_scope_summary`, and
> `strata_read_scope_record` default to your bound scope
> (`STRATA_AGENT_SCOPE`) when called with no `scope_id`. An explicit
> `scope_id` for `strata_read_scope_summary` reaches your bound scope, its
> inter-stratum ancestors, and any scope referenced by a scope on that chain
> via a reference edge — at any stratum distance, per ADR 0010 (context
> surface); `strata_read_scope_record` and `strata_read_perspective`'s target
> stay chain-only — records audit the authority that binds you, and a
> perspective composes your own chain, not a peer's. `strata_read_perspective`
> itself composes those same chain-referenced scopes in as labelled,
> non-binding `peer_reference` layers (`binding: false`) alongside the chain's
> `self`/`ancestor` layers (`binding: true`) — a referenced scope's directives
> inform the reader but never bind them. Unreferenced scopes and descendants
> stay refused everywhere.
> This supersedes the old HTTP-parity note for `strata_read_scope_record`:
> it now loads the fleet on every call to run this check, so reading your
> own scope's record while it has no rows still returns the empty record
> shape (`{"contributions": [], "judgments": []}`), but a scope outside your
> entitled surface raises instead of silently returning an empty record.

**For a foreign project**: use `strata register` (see
[Adding Strata to an existing project](#adding-strata-to-an-existing-project)
above). The steps below are for developing on Strata itself.

### 1. Start the backend (optional — Console UI only)

```bash
strata start
```

The backend is only required if you want the browser Console UI at
<http://127.0.0.1:8000/>. MCP tool calls work with or without it.

### 2. Register the MCP server in Claude Code

After running `strata register`, `.mcp.json` at the repo root already
contains the correct `mcpServers.strata` entry — that's the file Claude Code
actually reads for project-scoped MCP servers, not `.claude/settings.json`.
**This applies to the Strata repo itself too**: without a discoverable
`.strata/config.toml` the MCP server still starts (soft-start, ADR 0005 D5
dated addendum), but every memory tool stays gated with an actionable error
naming the missing config until one exists and the server is restarted — so
for developing on Strata run `strata register` once from the repo root — it
is strictly additive, and the created `.strata/` workspace is gitignored.
The `.mcp.json` entry it merges is:

```json
{
  "mcpServers": {
    "strata": {
      "command": "strata-mcp",
      "env": {}
    }
  }
}
```

The seeded fleet has one scope, so an unset `STRATA_AGENT_SCOPE` auto-binds
to it (see [Binding past one scope](#binding-past-one-scope)) — nothing to export
before launching `claude`. Once you add scopes, set `STRATA_AGENT_SCOPE` and
`STRATA_AGENT_SKILL` in the shell before launching. Storage paths are read
from `.strata/config.toml`.

`STRATA_AGENT_SKILL` is a skill identifier recorded in provenance and
**validated against the scope's `permitted_skills`** in `fleet.yaml` (when
that list is set and there's a mismatch, the server still starts — soft-start
— but every memory tool stays gated until bound to a permitted skill via
`strata_bind`). It does not select a Claude Code skill file — **the same
generic CC skill (`strata-worker`) works for any role at any scope**.

### 3. Invoke a skill

The repo ships three CC skills under `.claude/skills/`:

| Skill | What it does |
|---|---|
| `/strata` | First-time orientation: shows the fleet, helps you pick a role, points you to the next skill. Use once. |
| `/strata-worker` | Binds the current CC session as a worker at `STRATA_AGENT_SCOPE`. Reads the perspective, contributes observations as `context`, contributes decisions as `directive`, cites memory back to you. **The main skill you'll use.** |
| `/strata-inspect` | Read-only browser. Use when you want to look around without acting. |

### 4. Worked example (multi-session)

Three terminals, three different roles, one shared Strata:

```bash
# Terminal 1 — backend
strata start

# Terminal 2 — architect (skills must be permitted for the scope in fleet.yaml;
# the dev-team template permits code-writer + evidence-summarizer here)
STRATA_AGENT_SCOPE=g_arch     STRATA_AGENT_SKILL=code-writer   \
STRATA_AGENT_SESSION_ID=sess_arch  claude
# Then in the CC session:  /strata-worker

# Terminal 3 — backend developer
STRATA_AGENT_SCOPE=g_backend  STRATA_AGENT_SKILL=code-writer   \
STRATA_AGENT_SESSION_ID=sess_dev   claude
# Then in the CC session:  /strata-worker
```

Each session contributes to the same backend. The developer captures
implementation patterns as `context`; the architect ratifies recurring
patterns into `directive`s that bind everyone below. Watch the state
evolve in <http://127.0.0.1:8000/> (the Console UI) or run `strata
summary g_arch` from a fourth terminal.

**Several terminals on one machine are safe, even without the backend
running.** Every `claude` session above talks to its own `strata-mcp`
process, and two of those processes — or a process and the optional Console
backend — can end up contributing to the same scope at the same time. Each
holds its own per-scope lock file under `.strata/.locks/` for the moment it
takes to append a contribution and, separately, for the moment it takes to
judge one — the OS enforces that only one process holds a given lock file at
a time, so two contributions to the same scope can never interleave and
leave the summary out of sync with the record (ADR 0012). Nothing extra to
start or configure: the lock files are created on demand next to your
`strata.db`, so this holds whether or not `strata start` is running.
(Windows: `strata-mcp` still serializes concurrent contributions inside one
process; across processes it does not — see ADR 0012.)

---

## Developing

Working on Strata itself (not just using it) needs a clone of this repo,
not a `pipx install`:

```bash
git clone https://github.com/oren198/Strata.git
cd Strata
make install        # editable install + dev extras (pip install -e ".[dev]")
```

If you prefer an isolated virtual environment first:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
make install
```

### Run the tests

```bash
make test         # full suite (scope-manager mocked — no API key needed)
make smoke        # end-to-end smoke (bootstrap → contribute → summary)
make lint         # ruff check + ruff format --check
```

To run the (skipped-by-default) integration test that hits the real
Anthropic API:

```bash
STRATA_RUN_INTEGRATION=1 ANTHROPIC_API_KEY=... pytest tests/test_scope_manager.py -v
```

The original `make` targets (`make migrate`, `make bootstrap`, `make run`,
`make test`, `make lint`, `make smoke`) all still work against a repo clone
and are the fastest path when hacking on Strata itself.

---

## Git workflow

- `main` — the last verified version of Strata.
- `dev` — the integration branch. All feature work merges here first.
- `feature/*` — branched from `dev`, merged back into `dev` via PR.
- Releases are PRs from `dev` → `main`.

---

## Architecture decisions

ADRs live under `docs/adr/`. Each captures a hard-to-reverse decision with
context, alternatives, and consequences. The future direction —
principles plus the next horizons — is in [`docs/ROADMAP.md`](https://github.com/oren198/Strata/blob/main/docs/ROADMAP.md).

Current ADRs:

- [0001 — V1 architecture](https://github.com/oren198/Strata/blob/main/docs/adr/0001-v1-architecture.md): local Python
  backend, SQLite + markdown storage, Claude Code as the initial agent
  runtime, scope-manager hosted as backend-spawned LLM judgment calls
  (see [Environment variables](#environment-variables) for today's
  provider-generic judge configuration).
- [0002 — Fleet config source of truth](https://github.com/oren198/Strata/blob/main/docs/adr/0002-fleet-config-source-of-truth.md):
  `fleet.yaml` is canonical; SQLite holds only contributions and judgments;
  scope lifecycle (`active`/`archived`); per-scope skill declarations.
- [0003 — `strata launch` CC binding](https://github.com/oren198/Strata/blob/main/docs/adr/0003-strata-launch-cc-binding.md):
  frictionless `(scope, skill, session_id)` binding via a single CLI command
  that validates, resolves, and `execvp`s `claude`.
- [0004 — H2 foundations](https://github.com/oren198/Strata/blob/main/docs/adr/0004-h2-foundations.md): embedded mode
  (MCP server direct-store access), manager composition, lazy refresh, bounded
  summaries.
- [0005 — Brownfield install](https://github.com/oren198/Strata/blob/main/docs/adr/0005-brownfield-install.md): `strata register`
  two-command onboarding, per-project `.strata/config.toml` discovery, `strata-mcp`
  console script, skills as package data, honest provenance enforcement.

---

## License

See [`LICENSE`](https://github.com/oren198/Strata/blob/main/LICENSE).
