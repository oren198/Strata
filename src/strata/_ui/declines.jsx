// ─────────────────────────────────────────────────────────────────────
// Strata · Declines view. The Console's "turned down" proof surface (Task 1
// of the local-console plan): every contribution the scope-manager judged
// and refused, with the reason it gave. UI-only — this view's fetch never
// runs on the contribute/judge path.
//
// Layout ported from GateActivityTile's decline block (pulse-row /
// pulse-row-head / pulse-quote, "What judgment kept out"), promoted from a
// 3-row teaser to the whole page.
// ─────────────────────────────────────────────────────────────────────

function DeclinesView({ state, scopeId, onSelectScope }) {
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [data, setData] = React.useState(null);

  React.useEffect(() => {
    if (!scopeId) {
      setLoading(false);
      setData(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    STRATA_STORE.fetchScopeDeclines(scopeId)
      .then((body) => {
        if (cancelled) return;
        setData(body);
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err.message || String(err));
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, [scopeId]);

  function loadMore() {
    if (!data || !data.page.next_before_id) return;
    STRATA_STORE.fetchScopeDeclines(scopeId, { before_id: data.page.next_before_id })
      .then((next) => {
        setData((prev) => ({
          ...next,
          declines: [...(prev ? prev.declines : []), ...next.declines],
        }));
      })
      .catch((err) => setError(err.message || String(err)));
  }

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
      }}>
        <h1 className="at-h1" style={{ marginBottom: 4 }}>Turned down</h1>
        <div className="at-body-sm" style={{ marginBottom: 16 }}>
          Contributions the scope-manager judged and refused, with the reason it gave.
          Nothing here entered this scope's memory.
        </div>

        {loading && (
          <div className="at-caption">Loading…</div>
        )}

        {!loading && error && (
          <div>
            <div className="at-caption">Could not load turned-down contributions.</div>
            <div style={{ fontFamily: "var(--font-mono)", color: "var(--at-bear)", fontSize: 12, marginTop: 4 }}>
              {error}
            </div>
          </div>
        )}

        {!loading && !error && data && (
          <>
            <div className="pulse-row-head">
              <div>
                <span className="at-num-md">{formatNumber(data.page.total)}</span>
                <div className="at-caption">turned down by judgment</div>
              </div>
              <div style={{ textAlign: "right" }}>
                <div className="at-caption">
                  {formatNumber(data.mechanical_declines.sessions_that_read_and_recorded_nothing)} sessions read this and recorded nothing
                </div>
                <div className="at-caption" style={{ opacity: 0.8 }}>
                  last {data.mechanical_declines.window_days} days — these are not record entries
                </div>
              </div>
            </div>

            <div className="at-section-eyebrow">What judgment kept out</div>

            {data.declines.length === 0 ? (
              <div className="at-caption">Nothing has been turned down in this scope yet.</div>
            ) : (
              <div>
                {data.declines.map((entry) => (
                  <DeclineCard key={entry.contribution_id} entry={entry} />
                ))}
              </div>
            )}

            {data.page.next_before_id && (
              <button
                className="at-btn at-btn-ghost at-btn-sm"
                onClick={loadMore}
                style={{ marginTop: 10 }}
              >
                Load more
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function DeclineCard({ entry }) {
  return (
    <div className="pulse-row">
      <div className="pulse-row-head">
        <span style={{ fontFamily: "var(--font-mono)" }} className="at-caption">
          {entry.contributor.scope_id}
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <time dateTime={entry.created_at} title={absoluteTime(entry.created_at)}>
            {humanAgo(entry.created_at)}
          </time>
          <span className="at-pill at-pill-bear">Declined</span>
        </span>
      </div>

      {entry.subject && <Tag>{entry.subject}</Tag>}

      <p className="pulse-quote">{entry.content}</p>

      <div className="at-caption" style={{ color: "var(--at-muted)" }}>
        <strong>Why it was turned down</strong>{" "}
        {entry.reason ? entry.reason : "No reason recorded."}
      </div>

      <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--at-muted)" }}>
        {entry.contributor.scope_id} · {entry.contributor.skill || "no skill"} · {entry.contributor.session_id}
      </div>
    </div>
  );
}

window.DeclinesView = DeclinesView;
window.DeclineCard = DeclineCard;
