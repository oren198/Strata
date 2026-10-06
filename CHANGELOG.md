# Changelog

This repository had no changelog file before v1.17.0. It is now the source
the release PR (`dev` → `main`) and the GitHub Release body are built from
(`docs/releasing.md`, step 3) — write entries here as work lands, and the
"Unreleased" section is cleared once a release ships.

## Unreleased (v1.18.0)

### Behaviour change

- **A child's own context can no longer undercut an inherited directive.** Until
  now a context note such as "for hotfix builds we don't need the whole test
  suite" rested on the judge alone, and the inherited rule and the contrary note
  then sat side by side in the perspective. After judgment, when a session bound
  to a scope has its context admitted and the scope inherits a directive (from an
  ancestor or the operator), the engine makes one compact extra call that asks how
  the note relates to the rule, then verifies the answer itself. A note that states
  what may or does happen instead of the rule (permitted, or a standing practice) is
  declined, with a reason naming the two legitimate routes: report a specific
  occurrence of following the rule (admitted, raised to the issuer with `acted_on`),
  or propose the exception to the scope that issued the rule. A report of one
  past act of following the rule, or of departing from it, stays admitted, dated
  or not ("I paged the sev-1 through the primary rotation and it went wrong"); the
  record names a departure as a departure from the directive, never as
  permission, and suggests `acted_on`. A report becomes general, and is declined,
  when its act or condition carries a habitual marker ("every time", "whenever",
  "we would", "we skip", "these days") or a generic class ("on hotfixes", "for
  repeat jobs"), or when it adds a generalising clause ("... so we don't need
  it"); `acted_on` never waives that check. A permission widened to "any" or
  "anyone" ("any engineer can approve migrations") is an exception marker. When
  the answer says unrelated, the note is declined only if it also touches an
  inherited directive's subject and carries an exception marker; when the answer
  is unreadable, any exception marker declines. Otherwise the note is admitted,
  and each such fallback is counted in the judgment's `inherited_relation` trace.
  Measured without a key: the three-level drift set 48 of 48, a new held-out set
  of 36 context items 36 of 36, J1 and J4 unchanged or better, and 228 of 228
  forged report answers on exceptions declined
  ([evidence](docs/evidence/v1.18-242-context-exception-2026-10-10.md)).
  Stated limits:
  - An exception phrased with no exception marker is admitted when the extra
    call's answer is unreadable or says unrelated: 6 of the 38 exceptions in the
    held-out measurement. In the live runs no fallback hid an exception (9 of 9
    were plain facts or operator echoes). Better subject matching (#244) is
    expected to shrink this.
  - A batch that declines a member replaces the judge's single context rewrite
    with the previous context plus each remaining member's own text.
  - A report reaches the issuer only if the contributor follows the `acted_on`
    suggestion.

## 1.17.2 (2026-10-06)

### Added

- **`strata register` seeds Claude Code deny rules for `.strata/`** (#173,
  ADR 0013 D6). `permissions.deny` in `.claude/settings.json` gains
  `Read(/.strata/**)` and `Edit(/.strata/**)`, appended beside any rules
  already there and left unchanged on a second register. `strata doctor`
  reports whether they are present. A `Write(path)` rule is not seeded:
  Claude Code accepts it and never consults it. The rules block Claude
  Code's file tools and the shell file commands it recognizes (`cat`,
  `head`, `tail`, `sed`); they do not block a Python or Node process that
  opens the files itself. Codex's sandbox config cannot deny a path inside
  a writable workspace, and its permission profiles do not apply while
  `sandbox_mode` is set, so register does not write a Codex deny. The
  Strata server still reads `.strata/` — it is a process, not a harness tool.

### Fixed

- **The 1.17.1 inherited-directive check no longer holds legitimate child
  directives as context.** Three false holds are fixed: a smaller value under
  an upper bound ("a maximum of", "at most", "no more than", "up to",
  "never above", "below") is now read as stricter, with the direction taken
  from the parent's own words; an added own clause no longer reads as a
  polarity flip when the parent's terms are all kept; and a shared modifier
  with a different head noun ("fuel dock spill kit" under "fuel dock pumps")
  is a different subject. A looser value, an exemption, a softened "all" and
  a narrowing of when the rule applies are still held.
  Measured without a key on a new held-out set (48 items): with the same
  recorded judge answers, the full path went from 44 to 48 correct; the
  check's own catches are unchanged and 117 of 117 forged contradicting admits
  are still held
  ([evidence](docs/evidence/v1.17.2-inherited-false-holds-2026-10-06.md)).

## 1.17.1 (2026-10-05)

### Fixed

- **A child's own directive can no longer change an inherited one.** 1.17.0
  left this to the judge alone, and a weaker judge admitted such directives as
  "team-local practice". The engine now checks after judgment, with no extra
  judge call and no change to what the judge is sent. For a directive admitted
  from a session bound to the judged scope (append, publish or a supersede's
  replacement), single or batch, each rendered inherited directive (ancestor
  and operator) is compared. If the new text is about the same subject it must
  tighten the rule: no exemption, no outdating claim, no softened "all", no
  flipped polarity, no narrowing of when or where it applies, and a value no
  looser. A conflict is admitted as context under the engine note "[Held:
  conflicts with inherited directive <id> (<scope>); a child may tighten an
  inherited rule, not change it.]", and the directive set is unchanged. A
  different subject, a refinement, a tightening, or the rule restated with an
  own constraint passes untouched. Stated limits: "same subject" is a
  leading-noun-phrase match; a tightening the check can't read as stricter (a
  spelled-out number, a "between" range) is held as context rather than
  admitted; a parent with no value, polarity word or marker gives the check
  nothing to compare, so such a contradiction still rests on the judge; in a
  batch the judge's own context rewrite is kept and the held lines are
  appended to it.
  Measured without a key: 117 of 117 forged contradicting admits held; a
  child changing where an inherited fact says a file lives, admitted by the
  judge as a directive, held; held-out refinements and tightenings 24 of 24
  correct; no legitimate admit from earlier pinned runs newly held
  ([evidence](docs/evidence/v1.17.1-inherited-check-2026-10-05.md)).

## 1.17.0 (2026-10-04)

### Security

- Security: a child scope could change its parent's decisions by contributing upward. 1.17.0 enforces that only a session bound to a scope changes that scope's decisions; anything from below arrives as a proposal. Reported by Adam (@Adam13y). (GHSA-w7vh-r35h-8fvr)

### Behaviour change

- **Only a session bound to a scope changes that scope's directives (the
  position gate).** The engine enforces this after judgment; it doesn't rest on
  the judge. A contribution from any other position that would add, replace or
  retire a directive in a scope is admitted as an attributed proposal instead,
  "<skill> (<scope>) proposes: … — directive <id> stands", and the directive
  set stays byte-identical. Examples are a child writing upward and an outcome
  raised from below. Own-scope authority is unchanged. A child's own
  contribution that contradicts an inherited directive is declined by the
  judge. That is judge-only: the engine doesn't yet check an admitted child
  directive against the inherited ones (declined 48 of 48 on the default judge;
  at least one other judge has admitted such a case).
  Measured with a pinned judge on child-to-parent attempts: the parent's
  directives changed in 41 of 84 attempts before and 0 of 84 after (qwen), and
  31 of 56 before and 0 of 56 after (Haiku)
  ([evidence](docs/evidence/v1.17-position-gate-2026-10-04.md)). Stated limit:
  this holds where the contributor's position comes from the session's binding
  (the MCP path). The local HTTP API takes it from the request body and must
  not be exposed.

- **Every same-scope directive change carries an engine-written provenance
  line in the record.** When a session bound to a scope changes its
  directives, the engine appends "[Engine: same-scope change by <skill>
  (session <id>), bound to <scope>: <ops>. …]" to the recorded notes. The
  record's account of who changed the rules therefore never rests on the
  judge's wording. On the release tree it was present on 21 of 21 such changes
  and on no other judgment. The judge's prompt also gained one sentence: stated
  reasoning may describe position and what was checked, never authority or
  verification it couldn't establish. Its effect wasn't measurable on the
  default judge (authority wording in 1 of 12 reasons before, 3 of 24 after,
  all describing the scope's own position and none naming an approver). The
  engine line is the guarantee. Judge inputs are otherwise byte-identical: the
  line is stripped from the history later judges see.

- **Recorded verdict notes open with the decision and the applied ops (#238).**
  For example "[accept_as_context; no ops] …" or "[accept_as_directive;
  supersede c_a→c_b; append c_b] …". The record then shows what actually
  happened even when the reasoning describes a different act. The prefix is
  stripped from every judge input; judge failures are recorded unchanged; an
  idempotent rejudge returns the judge's reasoning without it.

- **A scope's own corrected claim is now checked for paraphrased carriers in
  its own publication, not only verbatim ones (#219 C).** When a scope's
  outcome judgment or refresh finds one of its own claims wrong
  (`failed_corrected`), the engine already withdraws published items that
  still carry the claim verbatim (#221). This adds one further owner-judge
  call per correction, deciding `carries`/`does_not_carry` for the scope's
  current published face beyond what the verbatim sweep already caught —
  recorded either way (`claim_carrier_checks`), capped at 20 candidates per
  correction with any overflow recorded, never silently dropped. A
  mechanical observed-value veto can additionally keep an item the judge
  wrongly marked `carries` (recorded `kept_by_guard`) when it states what
  was actually observed rather than the refuted claim. #219 C wrongly
  withdraws about 2–7% of a scope's neighbouring items (measured). Each one
  is listed in the Console's Correction withdrawals view and can be
  restored there by the operator. The owner's own judged restore recovered
  5 of 12 such items in our measurement; the operator path is the reliable
  remedy ([evidence](docs/evidence/v1.17.219c-restore-2026-10-03.md)).

- **A judge failure on `/contribute` now returns HTTP 503, not 500 (#235,
  #236).** A second protocol slip surviving the corrective re-ask — a
  genuine judge-API outage, an auth failure, or a malformed response the
  retry couldn't fix — now fails closed with a dedicated, engine-authored
  decline reasoning instead of propagating the judge's own malformed text
  or mis-tagging the contribution's outcome fields. The API layer maps
  this to `503 {"error": "scope_manager_failure", ..., "retry":
  "strata_rejudge"}`, distinct from a 200 merits decline. Covers both the
  ordinary path (#235) and the batch path (#236), which had no
  forced-decline fallback at all before this.

- **Interior assertions naming a non-entitled scope now get one extra judge
  call, and can be declined where 1.16 admitted them (#225).** When an
  accepted contribution names a fleet scope the current scope is not
  entitled to, the engine now fires a second, targeted judge call that
  classifies the claim's ground (ADR 0016) and verifies the answer against
  the contribution's own text, rather than trusting the judge's optional
  field:
  - A **conduct** claim must be a dealing the contributor was actually part
    of — an attestation or perception frame ("I can tell you…", "I saw…") is
    stripped before the first-person check runs.
  - An **informant** claim must quote a telling event the contributor was
    actually in, as addressee or audience ("told us", "mentioned to me") —
    not just a named teller. A scope's own name offered as its own informant,
    with no telling event, is declined.
  - Three cases are declined **by design**, not promised fixed: an observed
    act written without the contributor in it; a real telling event that
    names no addressee; and an overheard telling that names the scope
    (admissible in theory under ADR 0016 D2, declined here) — each decline
    reason tells the contributor what to say instead.
  - A rule dressed with "our" passes the check and is left to the judge.
  - Overheard tellings that name a scope: the 1.16 judge already declined
    these on its first call (0/10 before and after), so this is unchanged.
  - Not covered by this change: the batch judgment path, and alias
    references to a scope (matching stays id/name only, word-bounded).

  See the README's J4 row and ADR 0016 D2's dated note (2026-10-02, #225) for
  the measured gate and the full rulings.

- **"Discussed with X" now counts as a telling event (#225 widened).** "Discussed
  with X", "agreed with X", "decided with X", "met with X" and "in our sync with
  X", with the contributor present, are telling events in #225's informant and
  conduct checks (the philosopher's ruling, 2026-10-03). A bare "as discussed,
  …" names nobody and still fails. Hearsay from a joint verb is recorded at that
  verb's strength ("In discussion with X: …"), never as "X says".

- **A decline for "no one spoke" now gets one re-check, and can become context
  (attribution over-decline).** When an ordinary judgment declines a
  contribution for manufactured attribution, a second targeted judge call names
  the contribution's actual ground: a rendered directive or publication it
  quotes, a named outside party's publishing act, a telling event the
  contributor was in, or the contributor's own first-hand claim. The engine
  verifies that answer against the contribution's text. Only a verified ground
  reverses the decline, and only ever to context, never to a directive. Every
  other decline ground still stands, and any failure leaves the first decline
  unchanged. A rescue must ground every claim the contribution attributes to its
  source. Not covered: aliases ("the purchasing team"), the batch path, and a
  conduct observation that names another scope
  ([evidence](docs/evidence/v1.17-attribution-recheck-2026-10-03.md)).

### Added

- **A session bound to a scope can adopt a proposal from below
  (`adopted_from`).** It does so with an ordinary own-scope contribution that
  names the held proposal (MCP `strata_contribute` and `POST /contribute`
  parameter `adopted_from`; migration 0022). It is accepted only when:
  - the proposal is in this scope's record, came from another position, and
    was held by the position gate;
  - the adopting session is bound to the scope.
  The resulting directive records "adopted from proposal <id>". No notice goes
  back to the proposer; the inherited rule is the notice.

- **A child's own rule under an inherited one gets one re-check, and can be
  reinstated (#237).** When a decline names an inherited directive as the
  conflict, a second targeted judge call names the relation (contradicts,
  exempts, refines or tightens), and the engine verifies it against both texts.
  A verified refinement or tightening reinstates the judge's original
  classification. Every guard reads the whole contribution, so an exemption,
  a softened quantifier, a narrowed "every day", or a claim that the inherited
  rule is outdated keeps the decline. Stated limits: a refinement whose subject
  shares a word with the inherited rule's subject is held to the tightening
  test, and the batch path is not covered
  ([evidence](docs/evidence/v1.17.237-relation-recheck-2026-10-04.md)).

- **Optional provider pinning for an OpenRouter judge, `JUDGE_PROVIDER` /
  `STRATA_JUDGE_PROVIDER` (#224).** OpenRouter routes the default judge
  across roughly ten backing providers; the same item on the same build
  has measured anywhere from 10/10 to 5/10 across runs, plausibly from the
  provider mix. Setting this pins every judge call to one named provider
  (e.g. `Alibaba`) via `extra_body={"provider": {"order": [...],
  "allow_fallbacks": false}}`, ignored entirely on a non-OpenRouter judge
  endpoint. Not a behaviour change while unset (the default): every judge
  call's request is byte-identical to today's. `strata doctor`'s judge line
  now shows whether a configured provider is actually pinned or ignored.
  Covers the judge's own calls, the freshness Stop-hook evaluator's
  drafter, and doctor's own live probe — the only three places the
  engine ever calls the judge endpoint.

- **A judged `restore` act undoes a published item a correction sweep wrongly
  withdrew, under its ORIGINAL id and bytes (companion to #219 C).** Only a
  sweep withdrawal — the verbatim P4 sweep, or #219 C's own owner-judge
  `carries` decision, at any hop of either one's relay cascade — can be
  restored this way; a deliberate withdrawal is re-published, as before.
  - **Owner path, judged** (`strata_restore`/`propose_restore`): the owning
    scope's own judge re-runs the structural test only — still believed by
    its CURRENT memory, and does not re-assert the refuted claim — given the
    refuted claim, the correcting content, and the item together. A decline
    leaves the item withdrawn.
  - **Operator path, in person** (`strata operator restore <scope> <item_id>`):
    unjudged, operator provenance — the escape when the owner's judge
    declines.
  - **Relays come back mechanically** unless the relaying scope's own judge
    has processed (drained) the correction notice for the item since the
    withdrawal — then that scope gets the correction as evidence only, and
    its own judge decides whether to relay again.
  - **A new `claim_restored` notice** reaches EXACTLY the (reader scope,
    item) pairs that got the original false notice, read from the recorded
    change events by change id and item id — never recomputed from today's
    topology, and never sent to a scope that started reading only after the
    withdrawal. Evidence only: nothing is inserted into a reader's memory on
    its behalf.
  - **Detection surface:** `strata record <scope> --swept` and the Console's
    new "Correction withdrawals" tab (under Publications) both list every
    correction withdrawal in a scope (verbatim, judged `carries`, or relay),
    with the refuted claim and correcting content side by side, how it was
    withdrawn, the reader count, and #219 C's own unresolved/overflow rows,
    flagged. The Console tab adds a Restore button (the operator path) and a
    "keep withdrawn" acknowledge.
  - Not a behaviour change for anything already shipped: unused unless a
    withdrawal is actually restored.

- **A running session learns when memory it already read has moved,
  read signaling (#234).** A deterministic per-scope watermark (self +
  ancestor summary versions, each chain scope's current operator item
  ids, change-event count/newest id) is recorded alongside each read.
  Every MCP tool result now carries `perspective_stale: [scope ids]`
  when a scope this session read has since changed — including a scope
  this session only reads, not just the one it writes to. The
  Stop-hook's freshness evaluator gains an independent read-side clause
  sharing its 2-block strict-mode budget with the existing write-side
  reminder (write-side counted first); default mode surfaces it as a
  non-blocking message, said at most once per session either way.

- **A tolerant judge contract (#231).** `ScopeManager.judge`/`judge_batch`/
  `judge_publication`/`judge_bootstrap_publication` each accept and
  ignore a trailing `**_extra` — a new optional keyword the engine starts
  passing in the future is a no-op at any judge implementation that
  doesn't yet know it, never a `TypeError` (closing the class of incident
  behind #202, the P5 parse/prompt-gate split, and #229 — three
  kwarg-naming slips in two cycles). An AST-derived compatibility test
  checks every real call-site keyword is still a named parameter (a typo
  still surfaces as a missing argument) and that every in-repo test fake
  standing in for `ScopeManager` itself tolerates an unknown keyword.

- **MCP Registry housekeeping (docs-only).** `.github/SECURITY.md` now
  describes the judge as any Anthropic-Messages-compatible endpoint
  (OpenRouter by default), not only the Anthropic API, and names
  `JUDGE_API_KEY` as the secret (with the deprecated `ANTHROPIC_API_KEY`
  noted). `README.md` carries the registry's ownership-verification
  marker (`<!-- mcp-name: io.github.oren198/strata -->`). A new
  `server.json` at the repo root describes the package for the MCP
  Registry.

### Evals

- **A live depth-2 relay item for `claim_corrected` (#230, in
  `strata-evals`, follow-up from #221).** Covers owner → tracked-relay
  child → chain-composed grandchild, plus a reference-edge reader of the
  child, live rather than only pinned offline: the grandchild and the
  reference-edge reader each get exactly one `claim_corrected`, under the
  owner's original wave id, after the relaying child's own refresh is
  judged — whatever that refresh decides.
