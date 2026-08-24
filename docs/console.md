# The Console

The Console is the local, browser-based operator view that ships with Strata
(`strata start`, then open `http://127.0.0.1:8000/`). It runs entirely
against your own machine — no external service, no account, nothing hosted.
Five tabs sit alongside the existing memory graph and settings views: **Turned
down**, **Freshness**, **Record**, **View as**, and **Operator corrections**
(labelled "Replace"/"Retire" in the UI). Each is described below: what it
shows, where the number comes from, and what it proves.

The Console's endpoints (`GET /scopes/{id}/declines`, `GET /staleness`,
`GET /scopes/{id}/record`, `GET /scopes/{id}/record/{contribution_id}`,
`GET /scopes/{id}/perspective`, `POST /scopes/{id}/directives/{id}/supersede`,
`POST /scopes/{id}/directives/{id}/retire`) exist to serve the Console.
Nothing in the engine — the contribute/judge path, the MCP server, the CLI —
depends on any of them. They are additive reads (and, for the last two, an
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
more (there is no upper limit). This is a deliberate difference from Strata's
hosted counterpart, which clamps its window to 30 days because its read
receipts are pruned at that age. **Session-state files here currently have no
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
