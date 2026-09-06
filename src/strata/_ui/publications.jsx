// ─────────────────────────────────────────────────────────────────────
// Strata · Publications. The Console's surface for the sideways sharing
// channel (CONTEXT.md § Publication, § Republication — ADR 0013): what a
// scope currently publishes, the history of its publish/withdraw acts, and
// what a given reader actually receives under the one-edge rule. Read-only
// — this browser never publishes or withdraws (constraint G1); writing
// still flows through `strata.publish` / `strata.withdraw`.
//
// Publication travels exactly one edge: a reader receives its chain
// parent's publication and the publications of the scopes it itself
// references — never a grandparent's, and never one reached only through an
// ancestor's own reference edge. Material beyond one hop arrives only by
// republication, which keeps its origin scope and the relay it travelled
// ("according to X, via Y") — every item card below that carries republication
// provenance renders that sentence, never silently merging it into the
// relaying scope's own voice.
// ─────────────────────────────────────────────────────────────────────

const PUB_SUBTABS = [
  { id: "current", label: "Publishes now" },
  { id: "history", label: "Act history" },
  { id: "reader", label: "As a reader" },
];

function scopeName(state, scopeId) {
  const s = (state.scopes || []).find((g) => g.id === scopeId);
  return s ? s.name : scopeId;
}

// Republication attribution line — "according to X, via Y" — or null for an
// item that originated in the scope it is being read from.
function relayAttribution(state, item) {
  if (!item || !item.origin_scope_id) return null;
  const origin = scopeName(state, item.origin_scope_id);
  const relay = item.relay_scope_id ? scopeName(state, item.relay_scope_id) : null;
  return relay && relay !== origin
    ? `According to ${origin}, via ${relay}.`
    : `According to ${origin}.`;
}

function PublicationsView({ state, scopeId, onSelectScope }) {
  const [subtab, setSubtab] = React.useState("current");

  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: "240px 1fr",
      gap: 14,
      flex: 1, minHeight: 0,
    }}>
      <ScopesRail state={state} selectedId={scopeId} onSelect={onSelectScope} />

      <div style={{
        background: "var(--at-surface)",
        border: "1px solid var(--at-rule)",
        borderRadius: 12,
        padding: "18px 20px",
        overflowY: "auto",
        minHeight: 0,
        display: "flex", flexDirection: "column",
      }}>
        <h1 className="at-h1" style={{ marginBottom: 4 }}>Publications</h1>
        <div className="at-body-sm" style={{ marginBottom: 14 }}>
          A scope's single curated outward face — the same face for every reader. It
          travels exactly one edge: a chain parent, or a scope this one references.
          Anything further out arrived only because a scope in between chose to
          republish it.
        </div>

        <div style={{ display: "flex", gap: 4, marginBottom: 16, borderBottom: "1px solid var(--at-rule)" }}>
          {PUB_SUBTABS.map((t) => (
            <button
              key={t.id}
              className={"at-tab" + (subtab === t.id ? " active" : "")}
              onClick={() => setSubtab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>

        {subtab === "current" && <PublishesNowPanel state={state} scopeId={scopeId} />}
        {subtab === "history" && <PublicationHistoryPanel state={state} scopeId={scopeId} />}
        {subtab === "reader" && <ReaderReceivesPanel state={state} defaultScopeId={scopeId} />}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// View 1 — Publishes now: this scope's current outward face.
// ─────────────────────────────────────────────────────────────────────
function PublishesNowPanel({ state, scopeId }) {
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [data, setData] = React.useState(null);

  React.useEffect(() => {
    if (!scopeId) { setLoading(false); setData(null); return; }
    let cancelled = false;
    setLoading(true);
    setError(null);
    STRATA_STORE.fetchScopePublication(scopeId)
      .then((body) => { if (!cancelled) { setData(body); setLoading(false); } })
      .catch((err) => { if (!cancelled) { setError(err.message || String(err)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [scopeId]);

  if (loading) return <div className="at-caption">Loading…</div>;
  if (error) {
    return (
      <div>
        <div className="at-caption">Could not load this scope's publication.</div>
        <div style={{ fontFamily: "var(--font-mono)", color: "var(--at-bear)", fontSize: 12, marginTop: 4 }}>
          {error}
        </div>
      </div>
    );
  }
  if (!data) return null;

  const items = data.items || [];
  if (items.length === 0) {
    return (
      <div className="at-caption" style={{ fontStyle: "italic" }}>
        This scope has published nothing. Its readers see an honestly empty face.
      </div>
    );
  }

  return (
    <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
      {items.map((item) => (
        <li key={item.id} className="activity-row">
          <div className="activity-row-detail" style={{ borderTop: "none", paddingTop: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <span className="at-caption" style={{ textTransform: "uppercase" }}>{item.kind}</span>
              {item.subject && <span style={{ fontWeight: 600, color: "var(--at-ink)" }}>{item.subject}</span>}
              <span style={{ flex: 1 }} />
              <time
                dateTime={item.published_at}
                title={absoluteTime(item.published_at)}
                style={{ fontSize: 12, color: "var(--at-muted)" }}
              >
                {humanAgo(item.published_at)}
              </time>
            </div>
            <p style={{ margin: "0 0 8px", whiteSpace: "pre-wrap", color: "var(--at-ink-soft)" }}>
              {item.content}
            </p>
            {relayAttribution(state, item) && (
              <div className="at-caption" style={{ color: "var(--at-muted)", fontStyle: "italic", marginBottom: 6 }}>
                {relayAttribution(state, item)}
              </div>
            )}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {(item.anchors || []).map((a, i) => (
                <Tag key={i}>{a}</Tag>
              ))}
            </div>
            <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--at-muted)", marginTop: 8 }}>
              {item.id}
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}

// ─────────────────────────────────────────────────────────────────────
// View 2 — Act history: publishes and withdrawals, honestly distinguishing
// a judged verdict, a mechanically-cascaded withdrawal, a judge failure, and
// an act still awaiting judgment.
// ─────────────────────────────────────────────────────────────────────

function pubStateWords(act, stateEntry) {
  const isWithdraw = act.act === "withdraw";
  if (!stateEntry) {
    return { label: "Awaiting judgment", chipClass: "at-pill", icon: "clock" };
  }
  if (stateEntry.state === "judged") {
    if (stateEntry.decision === "accept") {
      return isWithdraw
        ? { label: "Withdrawn", chipClass: "at-pill at-pill-ok", icon: "check-circle" }
        : { label: "Published", chipClass: "at-pill at-pill-ok", icon: "check-circle" };
    }
    return isWithdraw
      ? { label: "Withdrawal declined — still published", chipClass: "at-pill", icon: "x-circle" }
      : { label: "Declined", chipClass: "at-pill at-pill-bear", icon: "x-circle" };
  }
  if (stateEntry.state === "mechanical") {
    // A relay-cascade trigger names an upstream published item (pub_...);
    // a directive-removal trigger names a contribution or operator
    // retirement — everything else (ADR 0013 D4b vs. ADR 0007 D3).
    const isRelayCascade = (act.trigger || "").startsWith("pub_");
    return {
      label: isRelayCascade ? "Withdrawn automatically — its source withdrew it" : "Withdrawn automatically",
      chipClass: "at-pill",
      icon: "git-merge",
    };
  }
  if (stateEntry.state === "judge_failed") {
    return { label: "Judgment failed", chipClass: "at-pill at-pill-warn", icon: "alert-triangle" };
  }
  return { label: "Awaiting judgment", chipClass: "at-pill", icon: "clock" };
}

function pubJudgeFailedSentence(attempts) {
  const list = attempts || [];
  const n = list.length;
  const newest = list.reduce((best, a) => {
    if (!best) return a;
    return (a.attempted_at || "") > (best.attempted_at || "") ? a : best;
  }, null);
  const errorClass = (newest && newest.error_class) || "unknown error";
  return `The judge errored, so no verdict was reached — ${errorClass} after ${n} attempt${n === 1 ? "" : "s"}. The act stays in the record, unjudged.`;
}

function pubMechanicalSentence(act) {
  const isRelayCascade = (act.trigger || "").startsWith("pub_");
  if (isRelayCascade) {
    return "Removed mechanically because the item it relayed was withdrawn at its origin — no judge was involved.";
  }
  return "Removed mechanically because the directive it was anchored to left this scope's summary — no judge was involved.";
}

function PublicationHistoryPanel({ state, scopeId }) {
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [data, setData] = React.useState(null);
  const [openId, setOpenId] = React.useState(null);

  React.useEffect(() => {
    if (!scopeId) { setLoading(false); setData(null); return; }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setOpenId(null);
    STRATA_STORE.fetchScopePublicationRecord(scopeId)
      .then((body) => { if (!cancelled) { setData(body); setLoading(false); } })
      .catch((err) => { if (!cancelled) { setError(err.message || String(err)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [scopeId]);

  const actById = React.useMemo(() => {
    const map = new Map();
    (data ? data.acts : []).forEach((a) => map.set(a.id, a));
    return map;
  }, [data]);
  const stateByActId = React.useMemo(() => {
    const map = new Map();
    (data ? data.act_states : []).forEach((s) => map.set(s.act_id, s));
    return map;
  }, [data]);
  const judgmentByActId = React.useMemo(() => {
    const map = new Map();
    (data ? data.judgments : []).forEach((j) => map.set(j.act_id, j));
    return map;
  }, [data]);
  const attemptsByActId = React.useMemo(() => {
    const map = new Map();
    (data ? data.judgment_attempts : []).forEach((a) => {
      const list = map.get(a.act_id) || [];
      list.push(a);
      map.set(a.act_id, list);
    });
    return map;
  }, [data]);

  if (loading) return <div className="at-caption">Loading…</div>;
  if (error) {
    return (
      <div>
        <div className="at-caption">Could not load this scope's publication history.</div>
        <div style={{ fontFamily: "var(--font-mono)", color: "var(--at-bear)", fontSize: 12, marginTop: 4 }}>
          {error}
        </div>
      </div>
    );
  }
  if (!data) return null;

  const acts = [...data.acts].reverse(); // newest first — the list itself is oldest-first.
  if (acts.length === 0) {
    return <div className="at-caption">Nothing has been published from this scope yet.</div>;
  }

  return (
    <div>
      {acts.map((act) => {
        const stateEntry = stateByActId.get(act.id);
        const judgment = judgmentByActId.get(act.id);
        const attempts = attemptsByActId.get(act.id) || [];
        const meta = pubStateWords(act, stateEntry);
        const open = openId === act.id;
        const withdrawnAct = act.withdraws ? actById.get(act.withdraws) : null;

        return (
          <div key={act.id} className="activity-row">
            <button
              className="activity-row-toggle"
              aria-expanded={open}
              onClick={() => setOpenId((prev) => (prev === act.id ? null : act.id))}
            >
              <span style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                <Icon name={meta.icon} size={14} />
                <span className={meta.chipClass}>{meta.label}</span>
                <span style={{
                  display: "-webkit-box", WebkitLineClamp: 1, WebkitBoxOrient: "vertical",
                  overflow: "hidden", textAlign: "left", color: "var(--at-ink-soft)", fontSize: 12.5,
                }}>
                  {act.act === "withdraw"
                    ? (withdrawnAct ? `Withdrew: ${withdrawnAct.content}` : `Withdrew ${act.withdraws}`)
                    : act.content}
                </span>
              </span>
              <time
                dateTime={act.created_at}
                title={absoluteTime(act.created_at)}
                style={{ flexShrink: 0, fontSize: 12, color: "var(--at-muted)" }}
              >
                {humanAgo(act.created_at)}
              </time>
            </button>

            {open && (
              <div className="activity-row-detail">
                {act.act === "publish" && (
                  <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{act.content}</p>
                )}
                {act.act === "withdraw" && withdrawnAct && (
                  <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                    Withdrew <code>{act.withdraws}</code>: {withdrawnAct.content}
                  </p>
                )}

                {relayAttribution(state, act) && (
                  <div className="at-caption" style={{ color: "var(--at-muted)", fontStyle: "italic" }}>
                    Relays material — {relayAttribution(state, act)}
                  </div>
                )}

                {stateEntry && stateEntry.state === "mechanical" && (
                  <div className="at-caption" style={{ color: "var(--at-muted)" }}>
                    {pubMechanicalSentence(act)}
                  </div>
                )}

                {stateEntry && stateEntry.state === "judge_failed" && (
                  <div className="at-caption" style={{ color: "var(--at-warn)" }}>
                    {pubJudgeFailedSentence(attempts)}
                  </div>
                )}

                {judgment && judgment.reasoning && (
                  <div className="at-caption" style={{ color: "var(--at-muted)" }}>
                    <strong>What the scope-manager said</strong> {judgment.reasoning}
                  </div>
                )}

                {act.anchors && act.anchors.length > 0 && (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                    {act.anchors.map((a, i) => <Tag key={i}>{a}</Tag>)}
                  </div>
                )}

                <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--at-muted)" }}>
                  {act.id} · {act.proposer.session_id} · {absoluteTime(act.created_at)}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// View 3 — As a reader: pick a reader scope, see the publications it
// actually receives under the one-edge rule (its chain parent's, and any
// scope it itself references) — never a grandparent's, and never one
// reached only through an ancestor's own reference edge. Reuses the same
// composition GET /scopes/{id}/perspective already serves; this panel only
// filters to the non-binding (publication) layers and renders them.
// ─────────────────────────────────────────────────────────────────────
function ReaderReceivesPanel({ state, defaultScopeId }) {
  const [readerId, setReaderId] = React.useState(defaultScopeId || (state.scopes[0] && state.scopes[0].id) || "");
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [data, setData] = React.useState(null);

  React.useEffect(() => {
    if (defaultScopeId) setReaderId(defaultScopeId);
  }, [defaultScopeId]);

  React.useEffect(() => {
    if (!readerId) { setLoading(false); setData(null); return; }
    let cancelled = false;
    setLoading(true);
    setError(null);
    STRATA_STORE.fetchPerspective(readerId)
      .then((body) => { if (!cancelled) { setData(body); setLoading(false); } })
      .catch((err) => { if (!cancelled) { setError(err.message || String(err)); setLoading(false); } });
    return () => { cancelled = true; };
  }, [readerId]);

  const publicationLayers = (data ? data.layers : []).filter((l) => l.binding === false);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <label className="at-caption" htmlFor="pub-reader-select">Reader scope</label>
        <select
          id="pub-reader-select"
          value={readerId}
          onChange={(e) => setReaderId(e.target.value)}
          style={{
            padding: "6px 10px", borderRadius: 8,
            border: "1px solid var(--at-rule)", background: "var(--at-bg)",
            color: "var(--at-ink)", fontSize: 13,
          }}
        >
          {state.scopes.map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
      </div>

      {loading && <div className="at-caption">Loading…</div>}

      {!loading && error && (
        <div>
          <div className="at-caption">Could not load what this scope receives.</div>
          <div style={{ fontFamily: "var(--font-mono)", color: "var(--at-bear)", fontSize: 12, marginTop: 4 }}>
            {error}
          </div>
        </div>
      )}

      {!loading && !error && data && publicationLayers.length === 0 && (
        <div className="at-caption" style={{ fontStyle: "italic" }}>
          This scope has no chain parent and references no other scope — it receives no
          publications from anywhere.
        </div>
      )}

      {!loading && !error && publicationLayers.map((layer, i) => (
        <ReaderSourceCard key={`${layer.scope_id}-${i}`} state={state} layer={layer} />
      ))}
    </div>
  );
}

function ReaderSourceCard({ state, layer }) {
  const items = (layer.publication && layer.publication.items) || [];
  const sourceLabel = layer.relation === "parent_publication"
    ? "Chain parent"
    : "Referenced scope";

  return (
    <div className="activity-row" style={{ marginBottom: 8 }}>
      <div className="activity-row-detail" style={{ borderTop: "none", paddingTop: 12 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
          <span style={{ fontWeight: 600, color: "var(--at-ink)" }}>{scopeName(state, layer.scope_id)}</span>
          <Tag>{sourceLabel}</Tag>
          <span className="at-caption">{items.length} item{items.length === 1 ? "" : "s"}</span>
        </div>
        {items.length === 0 ? (
          <div className="at-caption" style={{ fontStyle: "italic" }}>Publishes nothing.</div>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
            {items.map((item, i) => (
              <li key={item.id || i} className="at-body-sm">
                <span className="at-caption" style={{ textTransform: "uppercase", marginRight: 6 }}>{item.kind}</span>
                {item.subject ? `${item.subject}: ${item.content}` : item.content}
                {relayAttribution(state, item) && (
                  <div className="at-caption" style={{ color: "var(--at-muted)", fontStyle: "italic" }}>
                    {relayAttribution(state, item)}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

window.PublicationsView = PublicationsView;
window.pubStateWords = pubStateWords;
window.relayAttribution = relayAttribution;
