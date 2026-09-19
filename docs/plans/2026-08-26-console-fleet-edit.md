# Console Fleet Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Edit the fleet from the Console: open the current `fleet.yaml`, validate it server-side, save it safely, and see the graph update — instead of editing the file blind and restarting.

**Architecture:** A text-first editor. The Console shows the raw YAML (comments and all), the backend validates with the exact same code `strata bootstrap` uses, and a save is: validate → back up → atomic write → hot-swap this backend's in-memory fleet. Embedded agent sessions still load the fleet at startup (ADR 0002's restart-required model is unchanged) — the UI says so after every save. A content-hash guard makes two-tabs/last-write-wins impossible.

**Tech Stack:** FastAPI routes in `src/strata/app.py`, the existing `FleetConfig` validation, the Console's no-build JSX (`src/strata/_ui/`).

**Spec:** Operator request 2026-08-26 ("suggest the user run the console for fleet editing, not solely the yaml file") + this plan's design decisions D1–D5 at the bottom.

## Global Constraints

- Backend serves the UI only: no engine flow may call the new routes.
- Validation is the engine's own (`FleetConfig` load path / `strata bootstrap`) — never a parallel reimplementation.
- The on-disk file is the source of truth (ADR 0002). Saving edits the file a user could also edit by hand; comments and formatting the user typed in the editor are preserved verbatim (raw-text save, no YAML round-tripping).
- Plain language in all copy; no consumer product names.
- TDD; `ruff format --check .` and `ruff check .` clean before every commit; full suite via `.venv/bin/python -m pytest tests/ -q --deselect tests/test_start_guard.py`.

---

### Task 1: Read + validate endpoints

**Files:**
- Modify: `src/strata/app.py`
- Test: `tests/test_app_fleet_edit.py` (new; copy the `client` fixture pattern from `tests/test_app.py:129-161`)

**Interfaces (produces):**
- `GET /fleet` → `{"yaml": "<raw file text>", "etag": "<sha256 of that text>", "path": "<resolved fleet.yaml path>", "scopes": <int>, "edges": <int>}` — raw read of the resolved fleet file; counts come from the currently loaded FleetConfig so a stale-on-disk edit is visible.
- `POST /fleet/validate` with body `{"yaml": "..."}` → 200 `{"ok": true, "scopes": N, "edges": N}` on a loadable fleet; 422 `{"error": "invalid_fleet", "detail": "<the same message strata bootstrap prints>"}` when `FleetConfig` rejects it (YAML syntax errors included — they surface through the same load path). Nothing is written in either case.

- [ ] **Step 1: Failing tests** — GET returns the seeded file text + matching sha256 etag; POST validate with the seeded YAML → ok with correct counts; POST with a broken fleet (duplicate scope id; unparseable YAML) → 422, `invalid_fleet`, detail non-empty; POST never modifies the file (mtime/content unchanged).
- [ ] **Step 2:** Run → FAIL (404s). **Step 3:** Implement — find how `create_app` loads the fleet and where the path is resolved (the same resolution `strata start` uses); validation = construct `FleetConfig` from the submitted text via the existing loader (write to a temp file only if the loader demands a path). **Step 4:** PASS + full suite. **Step 5:** Commit `feat(console): read and validate the fleet over HTTP`.

### Task 2: Save endpoint — backup, atomic write, hot swap, conflict guard

**Files:**
- Modify: `src/strata/app.py`
- Test: `tests/test_app_fleet_edit.py` (extend)

**Interfaces (produces):**
- `PUT /fleet` with body `{"yaml": "...", "etag": "<etag from GET>"}` →
  - 200 `{"saved": true, "backup": "<path>.bak", "scopes": N, "edges": N, "note": "running agent sessions keep the fleet they started with — restart them to pick this up"}`
  - 422 `invalid_fleet` (same shape as validate) — nothing written.
  - 409 `{"error": "fleet_changed", "detail": "the fleet file changed since you loaded it — reload and reapply your edit"}` when the submitted etag doesn't match the current file content — nothing written.
- Save sequence, in order: validate the submitted text → etag check against the file as it exists now → copy current file to `<fleet path>.bak` → write submitted text to `<fleet path>.tmp` then `os.replace` onto the real path → swap the app's in-memory FleetConfig (move the fleet the app holds onto mutable state, e.g. `app.state`, so every route reads through it — Task 1's GET counts and `/scopes` must reflect the new fleet immediately).

- [ ] **Step 1: Failing tests** — happy path (save → 200, file content byte-equals submitted text, `.bak` holds the previous content, `GET /scopes` immediately lists the new scope set); invalid fleet → 422 + file untouched + no `.bak`; stale etag → 409 + file untouched; a second GET's fresh etag then saves cleanly; comments/blank lines in the submitted YAML survive byte-for-byte.
- [ ] **Step 2:** FAIL. **Step 3:** Implement (byte-level file I/O — `read_bytes`/`write_bytes` per the CRLF lesson in this repo; the etag is computed over raw bytes). **Step 4:** PASS + full suite. **Step 5:** Commit `feat(console): save the fleet — validated, backed up, atomic, conflict-guarded`.

### Task 3: The editor view

**Files:**
- Create: `src/strata/_ui/fleet-edit.jsx`
- Modify: `src/strata/_ui/graph.jsx` (an "Edit fleet" button in the graph view's header), `src/strata/_ui/app.jsx` (view wiring), `src/strata/_ui/index.html` + `_UI_FILES` (registration — `format.jsx` loads before it, matching the established order), `src/strata/_ui/atlas.css` (styles inside the existing `/* Console proof surfaces */` block)
- Test: extend `tests/test_ui_package_data.py`; babel-parse check of changed JSX

**Interfaces (consumes):** the three routes from Tasks 1–2, exactly as specified there.

- [ ] **Step 1:** Follow the no-build JSX contract exactly as the five existing views do (top-level functions, `window.X = X`, no imports/exports). The view: a monospace `<textarea>` initialized from `GET /fleet` (etag kept in state); **Validate** button → `POST /fleet/validate`, rendering ok-with-counts or the `invalid_fleet` detail in plain language; **Save** button → `PUT /fleet`, then on 200 re-fetch the graph data and show the restart note as a banner; 409 renders the reload-and-reapply message with a **Reload file** action; a dirty flag warns before navigating away with unsaved text (in-app view switch — guard in the view's own navigation handling, no `beforeunload` reliance).
- [ ] **Step 2:** Extend the package-data test for the new file; babel-parse all changed JSX; run the full suite. **Step 3:** Manual check note in the report: exact `strata start` + click path. **Step 4:** Commit `feat(console): edit the fleet in the Console`.

### Task 4: Docs

**Files:**
- Modify: `docs/console.md` (new "Editing the fleet" section: what save does — validate, back up, write, this Console updates now, agent sessions on restart), `README.md` (the fleet-editing paragraph gains "or edit it in the Console (`strata start`, graph tab → Edit fleet)" alongside the editor-and-`strata bootstrap` path — both stay documented)

- [ ] **Step 1:** Write against the shipped behavior (the code wins over this plan where they differ). Grep the diff for consumer names. Full suite + both ruff checks. **Step 2:** Commit `docs: fleet editing in the Console`.

## Sequencing

Task 1 → Task 2 → Task 3 → Task 4, one branch (`feature/console-fleet-edit`), sequential — all touch `app.py` or build on its routes.

## Design decisions

- **D1 Text-first, not forms.** The editor edits the raw YAML — comments survive, the file stays the source of truth, and the operator's existing mental model (it's a file) is preserved. Structured add-a-scope forms are future work layered on the same endpoints.
- **D2 Engine-validated, twice.** Validate is a free dry-run; save re-validates — the file on disk can never become something `FleetConfig` won't load.
- **D3 Restart semantics unchanged** (ADR 0002): only this backend hot-swaps; embedded agent sessions keep their startup fleet until restarted, and the UI says so after every save. No cross-process reload machinery.
- **D4 Conflict guard via content etag.** GET hands out a hash; PUT requires it back; a mismatch is a 409, never a silent overwrite. Cheap, and kills the two-tabs foot-gun.
- **D5 One-level undo.** Every save writes `<fleet>.bak` first. Not a history — the record of fleet evolution is git's job (the fleet file is the user's to commit).
