# The Console

The Console is the local, browser-based operator view that ships with Strata
(`strata start`, then open `http://127.0.0.1:8000/`). It runs entirely
against your own machine — no external service, no account, nothing hosted.
Four new tabs sit alongside the existing memory graph and settings tabs:
**Turned down**, **Freshness**, **Record**, and **View as**. A fifth surface,
**Operator corrections** (the "Replace" / "Retire" actions), is not a tab —
it lives in place, as row-level actions on a directive inside the existing
scope drawer (the memory graph's scope detail view). A sixth, **Editing the
fleet**, is also not a persistent tab — it opens from an "Edit fleet" button
in the graph tab's header. All six are described below: what each shows,
where the number comes from, and what it proves.

The Console's endpoints (`GET /scopes/{id}/declines`, `GET /staleness`,
`GET /scopes/{id}/record`, `GET /scopes/{id}/record/{contribution_id}`,
`GET /scopes/{id}/perspective`, `POST /scopes/{id}/directives/{id}/supersede`,
`POST /scopes/{id}/directives/{id}/retire`, `GET /fleet`,
`POST /fleet/validate`, `PUT /fleet`) exist to serve the Console.
Nothing in the engine — the contribute/judge path, the MCP server, the CLI —
depends on any of them. They are additive reads (and, for the last four, an
explicit in-person write) layered on top of the same stores the engine
already uses.

## Turned down

Every contribution the scope-manager judged and refused for the selected
scope, newest first, with the reason the judge gave. Nothing shown here ever
entered the scope's memory — a decline is not a record entry.

Alongside the judged list, the page also shows a mechanical counter: the
number of sessions that read this scope inside the staleness window and
closed out having recorded nothing. This is **not** a count of declines of
this scope. The underlying data has no way to say that: a session's
"nothing to record" closeout is a fact about the session, not about which
scope it was reading when it decided that. The nearest honest number is
"sessions that had a read receipt for this scope and ended up recording
nothing" — so that is exactly what the label says, and it is reported
separately from the judged declines above it, never folded into the same
count.

## Freshness

Every active scope, ranked worst-first by how many sessions have read it
since anything new was accepted into it. A scope with no summary yet is
marked "No memory yet"; a scope with zero reads since its last accepted
contribution is "Fresh"; anything else is "Stale". A big number here means
agents keep leaning on memory nobody has updated.

The view offers a 7 / 30 / 90 day window and accepts any window of 1 day or
more (there is no upper limit). **Session-state files here currently have no
retention policy** — they are never pruned — so a very large window reaches
back as far as those files go. This is flagged as a known future concern, not
a defect: nothing today deletes old session state, so nothing today bounds
how far back a large window can look.

Above the scope list, a three-way count — **contributions**, **closeouts**,
**silent readers** — summarizes what sessions did inside the window. All
three numbers count **sessions**, not events and not agents: a session that
contributed counts once in "contributions" even if it wrote several
contributions; a session that closed out having nothing to record counts
once in "closeouts"; a session that read the scope and did neither counts
once in "silent readers." The three buckets are disjoint and sum to the
total number of sessions active in the window. (There is no agent registry
in this engine, which is why this differs from a count of distinct agents.)

## Record

One scope's append-only contribution record, newest first, rendered as a
collapsible list rather than a raw table. Each entry is labelled in plain
language — "Accepted as directive," "Accepted as context," "Declined,"
"Judgment failed," or "Awaiting judgment" — and expands to show the full
text, the judge's notes (if any), and, for a failed judgment, the error class
and how many attempts were made. Nothing here is ever edited or deleted;
this view is a read over the same record the engine writes to on every
contribution.

## View as

Composes and displays exactly what an agent bound to the selected scope
would receive on a read — the same composition the MCP server produces,
built from the same `compose_perspective` call, including inherited layers,
operator-set memory, and publications from peer scopes the selected scope
references. Each layer is shown as its own card, in the order an agent
actually receives them, along with a rough weight bar.

The weight next to each layer is an **estimate**, computed as
characters divided by four, and labelled "est." everywhere it appears. It is
not a tokenizer count and it is not a prediction of cost — its only job is
comparing layers against each other ("the operator layer is a third of what
this scope reads"). This view never shows a forecast, a budget, or "days
remaining"; there is no quota concept in this engine to meter against.

## Operator corrections

Two actions — **Replace directive** and **Retire directive** — let the
operator correct a scope's own directive in person, going around the
automatic scope-manager judgment for that one directive. Both are always
available on a directive in the Console; there is no separate "operator
mode" to switch on first, because a local Console has exactly one user, and
that user is the operator.

Each action opens a modal before anything is written, and the modal **is**
the confirmation: it shows the directive being changed, states plainly what
will happen to it (a replace shows the new text you're about to write; a
retire states that the directive stops binding agents but is not deleted),
and requires an explicit click on **Replace directive** or **Retire
directive** inside the modal. Nothing fires from a single click on the row
itself. Both actions call the same library functions the command line uses
(`strata operator supersede` / `strata operator retire`) under the same
cross-process per-scope lock, so a correction made from the Console and one
made from the CLI go through the identical path.

Because the Console can now write, the badge in the top-right corner reads
`operator`, not `read-only` — that badge is the standing signal that this is
a local, single-operator surface that can make changes, not a warning that
something is misconfigured.

Retired directives stay visible on the scope ("retired here"), read from the
same scope summary response as everything else in the drawer, but that list
is capped at the 50 most recent retirements for the scope — older ones stay
in the record and are still retired, they just drop off this convenience
list.

## Editing the fleet

The "Edit fleet" button in the graph tab's header opens a text editor over
the raw `fleet.yaml` — comments, blank lines, and all. This is deliberately
not a form: the file stays the source of truth, and what you type is exactly
what gets written back, byte for byte. Structured add-a-scope forms may come
later, layered on the same endpoints; today, editing the fleet in the
Console and hand-editing `.strata/fleet.yaml` in a text editor produce the
identical file.

**Validate** is a free dry run: it sends the text to `POST
/fleet/validate`, which loads it through the exact same code path `strata
bootstrap` uses — never a separate reimplementation of the invariant checks
— and reports either the resulting scope/edge counts or the first invariant
it violated, in plain language. Nothing is written either way.

**Save** does four things in order, every time:

1. **Validate** the submitted text again (the same check Validate runs) — a
   fleet that fails to load is never written, full stop.
2. **Check the etag** — a hash of the fleet file's bytes taken when you last
   loaded or saved it — against the file's current bytes. A mismatch means
   someone or something else changed `fleet.yaml` since you started editing;
   the save is refused with a "reload and reapply your edit" message rather
   than silently overwriting that other change.
3. **Back up** the current file to `fleet.yaml.bak` before touching it.
4. **Write** the new text to a temp file and atomically replace the real
   `fleet.yaml` with it.

**This Console updates now** — every route that reads the fleet stats
`fleet.yaml` before serving (the same lazy reload this backend has always
used to pick up an out-of-band edit without a restart), so the graph, scope
counts, and everything else reflect the save on their very next read. What
does **not** update automatically is any agent session already running:
each one loaded the fleet at startup (ADR 0002) and keeps that copy until
you restart it — the Console repeats this after every save so it's never a
surprise.
