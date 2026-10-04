# Proposals from below and their adoption

The philosopher's contract lines (2026-10-03). The minimal adoption act ships in v1.17.0; the rest is planned for v1.18.

**Context.** Since v1.17, a contribution from a session not bound to a scope can't change that scope's directives. A child's upward contribution lands in the parent as an engine-written attributed proposal line: "X (scope) proposes: … — directive <id> stands". The child's contribution does one thing: it places attributed evidence where the authority will read it. Everything after that is the parent's act on the parent's memory.

1. **The adoption act** is an ordinary own-scope contribution by a parent-bound session, carrying a required link `adopted_from → <proposal contribution id>`, a relation like `acted_on`. It isn't a new act kind. The link exists for two reasons:
   - **provenance:** if the rule proves wrong, the record shows its source, so removal by source stays possible;
   - **no-echo:** an adopted parent directive must never later count as independent corroboration of the same child's claim.
2. **What the adopting session reads.** The proposal line is sufficient ground; breadth is evidence that is shown, never a requirement.
   - Show what bears on generality, because adoption widens reach to every child: how many children proposed or corroborated the same thing, whether their provenance is independent, and any standing derived from outcomes. It is shown, not counted. Ratification is judgment, never a vote.
   - The parent doesn't read the child's interior, only what the child sent up and what it published. If the parent wants more, it asks.
3. **No notice back, in either direction.**
   - If the parent adopts, the new directive reaches the child by inheritance; that is the notice.
   - If the parent rejects or ignores it, nothing the child holds was corrected, and rejecting a proposal is not a correction.
   - Build no reply path. A parent that wants to explain itself publishes or directs through the ordinary channels.
4. **Stale proposals** are context in the parent and fade as context does. They are unexamined until adopted or until outcomes bear on them, so they're among the first to go under budget pressure, and they stay in the record.
   - No pending state, no expiry clock.
   - A re-proposal from the same child is not corroboration, since it has the same provenance.
   - An explicit rejection is just the parent session superseding the proposal line in its own context ("considered and declined, because …"), and it fades the same way.

## Shipped in v1.17.0

- `contributions.adopted_from` (migration 0022), on MCP `strata_contribute` and `POST /contribute`. It is validated: the target must be a proposal in this scope's record, from another position, held by the position gate, and the adopting contributor must be bound to the scope.
- The adopted directive records "adopted from proposal <id>". The judge sees the adopted proposal only when the link is set; every other judge input is unchanged.
- No reply path.

## Planned for v1.18

- **No-echo, enforced mechanically:** any corroboration counting must exclude a same-provenance source, so an adopted directive plus the child's re-proposal never count as two.
- **A "proposals from below" view** for parent-bound sessions, with an independence summary (distinct contributing scopes and sessions), shown and never counted.
- **An adoption link for context.** A parent that turns a child's plain-context report, rather than a held proposal, into a directive gets no adoption link today.
