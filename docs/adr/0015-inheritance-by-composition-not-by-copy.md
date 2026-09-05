# 15. Inheritance by composition, not by copy

**Status:** Accepted (2026-09-06 — decided with the operator during the 1.11.0
release gate; supersedes ADR 0011 D4)

**Issue:** #189. Amends ADR 0011 (D4 superseded), ADR 0014 (D2, D6, D7).

## Context

Two mechanisms deliver an ancestor's directive to a descendant, and nobody
reconciled them:

1. **The splice** (ADR 0011 D4): the parent's directive rows are copied,
   byte for byte, into the child's stored summary. Transitive — a child's file
   carries the grandparent's rows because the parent's file already did.
2. **Composition** (ADR 0013 D1): the perspective walks the chain root-first
   and adds each ancestor's directives as that ancestor's own labelled layer.

So a root directive D1 appears in a child's perspective twice: under `g_root`,
and again inside the child's own summary layer. strata-evals G3 flagged the
duplicate the moment reads began to drain (ADR 0014 D6) and so began to
splice. The splice predates ADR 0013; it was the only inheritance path when
the summary was the only thing an agent read. It is not any more.

The operator's ruling, in plain terms: a directive is copied once, that is a
bug; less copying of the same data over and over; the summary holds what the
scope decided; a reader assembles inheritance.

## Decisions

### D1 — A directive lives in exactly one summary: its owner's

A scope's stored summary carries only the directives that scope admitted. No
row is ever copied from an ancestor. The splice — `splice_parent_directives`,
the drain's splice step, the `splice_refresh` judge mode and its MANAGER
REFRESH prompt block, the keyless splice-reconciliation notice — is deleted,
not disabled. ADR 0011 D4 is superseded.

### D2 — One ancestor walk, two consumers

The root-first walk over the chain that `compose_perspective` already performs
becomes one function, and it feeds both consumers:

- **composition** — each ancestor's directives under that ancestor's own layer,
  exactly as ADR 0013 D1 has it;
- **the judge** — the same walk, rendered as one `ANCESTOR DIRECTIVES` block
  per ancestor, root-first, replacing the `parent_summary` input. The judge's
  view of what binds the scope is the agent's view, byte for byte; two walks
  would drift.

`parent_summary` is removed from the judge signature. It is not kept beside
the walk for compatibility: two paths to the same information is the muddle
this ADR ends.

### D3 — Ancestor directive ids are valid anchors and are swept mechanically

An inherited directive binds the scope, so a published item may anchor to it:
anchor validation accepts any id in the scope's own summary or in the ancestor
walk. When an ancestor retires or supersedes a directive, the descendant's
drain — before its judge runs — sweeps the descendant's published items
anchored to that id with the same mechanical rule as a local removal
(ADR 0007 D3, `propagate_directive_removals`), reading the removed id off the
`directive_retired`/`directive_superseded` change event. Mechanical, because
the local case is; a rule that is mechanical for one's own retirements and
judged for an ancestor's would be a second rule.

### D4 — The prompt rule becomes an engine rule

"Never `supersede` or `retire` a parent directive" was a prompt obligation.
With ancestor ids no longer in `current_summary`, an op naming one is an
invalid-target op and is dropped by the existing validation (ADR 0011 D1). The
prompt sentence stays as explanation; the engine is what enforces it.

### D5 — Legacy copies are unspliced, mechanically, once

Stored summaries written before this release carry spliced rows. They were
never judged into that scope — the splice was mechanical — so ADR 0014 D7's
"no stored state rewritten" does not protect them; it protected judged state.
On a scope's first drain after this release, the engine removes every
directive row whose **contribution lives in another scope's record**
(`record_store.get_contribution(id).scope_id != scope.id`) — the exact inverse
of the splice, with a record note naming what was removed and why. That test
is exact: a directive id is a contribution id, and a contribution is appended
to exactly one scope's record. `source_scope_id` is not used for detection —
the HTTP route lets a contributor's scope differ from the target scope.

Rejected: filtering the copies at read time forever (keeps paying for rows
nobody owns), and treating them as the scope's own (they are not).

### D6 — Two judge modes, not three

`ordinary` and `input_change_refresh`. The drain has one job: judge pending
change events. `drain_is_noop` reduces to "nothing pending". A keyless server
has nothing mechanical to do at read time beyond D3's sweep and D5's unsplice,
both of which need no judge.

## Consequences

- A directive has one source of truth. An ancestor's retirement is visible on
  the descendant's next read with no copy to chase.
- A child's word budget stops paying for inherited rows.
- `ScopeSummary.parent_version` loses its purpose; removed if nothing else
  reads it.
- The judge signature changes (`parent_summary` → `ancestor_directives`); the
  stand-in judges in strata-evals adapt, as they did for ADR 0014.
- strata-evals A9 asserts the splice byte for byte. It tests the model this
  ADR retires and must be rewritten to assert D2 instead — the operator's
  repo, the operator's decision, recorded here rather than folded into
  structural fixes.
- G3 passes untouched. That is this change's acceptance test.
- CONTEXT.md § Summary, § Refresh, § Directive; ADR 0011 D4 and ADR 0014 D2/D6/D7
  amendment notes.
