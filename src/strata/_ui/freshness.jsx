// ─────────────────────────────────────────────────────────────────────
// Strata · Freshness view. The Console's "staleness made visible" proof
// surface (Task 2 of the local-console plan): every active scope, how many
// sessions have read it since anything new was accepted, worst first.
// UI-only — this view's fetch never runs on the contribute/judge path; the
// metric itself lives in session_state.compute_fleet_staleness.
//
// Layout ported from the Console's staleness tiles, with the tenant link
// removed and the local "sessions, not agents" session_outcomes shape (see
// task-2 brief).
// ─────────────────────────────────────────────────────────────────────

function FreshnessView({ state, onOpenScope }) {
  const [windowDays, setWindowDays] = React.useState(30);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [data, setData] = React.useState(null);

  React.useEffect(() => {
    let cancelled = false;
    let timer = null;

    async function refresh() {
      try {
        const body = await STRATA_STORE.fetchStaleness({ window_days: windowDays });
        if (!cancelled) {
          setData(body);
          setLoading(false);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err.message || String(err));
          setLoading(false);
        }
      } finally {
        if (!cancelled) timer = setTimeout(refresh, STRATA_STORE.REFRESH_INTERVAL_MS);
      }
    }

    setLoading(true);
    refresh(); // immediate first load for this window, then it re-schedules itself
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [windowDays]);

  return (
    <div style={{
      background: "var(--at-surface)",
      border: "1px solid var(--at-rule)",
      borderRadius: 12,
      padding: "18px 20px",
      overflowY: "auto",
      minHeight: 0,
      flex: 1,
    }}>
      <h1 className="at-h1" style={{ marginBottom: 4 }}>Freshness</h1>
      <div className="at-body-sm" style={{ marginBottom: 14 }}>
        How many sessions have read each scope since anything new was accepted into it.
        A big number means agents keep leaning on memory nobody has updated.
      </div>

      <div style={{ display: "flex", gap: 4, marginBottom: 16 }}>
        {[7, 30, 90].map((d) => (
          <button
            key={d}
            className={"at-tab" + (windowDays === d ? " active" : "")}
            onClick={() => setWindowDays(d)}
          >
            {d} days
          </button>
        ))}
      </div>

      {loading && (
        <div className="at-caption">Loading…</div>
      )}

      {!loading && error && (
        <div>
          <div className="at-caption">Could not load the freshness view.</div>
          <div style={{ fontFamily: "var(--font-mono)", color: "var(--at-bear)", fontSize: 12, marginTop: 4 }}>
            {error}
          </div>
        </div>
      )}

      {!loading && !error && data && (
        <>
          <SessionOutcomes outcomes={data.session_outcomes} windowDays={data.window_days} />

          <div className="at-section-eyebrow" style={{ marginTop: 16 }}>Scopes, worst first</div>

          {data.scopes.length === 0 ? (
            <div className="at-caption">No active scopes yet.</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {data.scopes.map((entry) => (
                <ScopeHealthStrip
                  key={entry.scope_id}
                  entry={entry}
                  onOpen={onOpenScope}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function ScopeHealthStrip({ entry, onOpen }) {
  const n = entry.reads_since_last_contribution;
  const chip = entry.state === "no_memory"
    ? <span className="at-pill">No memory yet</span>
    : entry.state === "stale"
      ? <span className="at-pill at-pill-warn">Stale</span>
      : <span className="at-pill at-pill-ok">Fresh</span>;

  const versionLabel = entry.summary_version === 0
    ? "no scope summary yet"
    : `v${entry.summary_version}`;

  return (
    <button
      className="scope-health-strip"
      onClick={() => onOpen(entry.scope_id)}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span style={{ color: "var(--at-ink)", fontWeight: 500 }}>{entry.name}</span>
          <span style={{ fontFamily: "var(--font-mono)", color: "var(--at-muted)", fontSize: 12 }}>
            {entry.scope_id}
          </span>
          {chip}
        </div>
        <div className="at-body-sm" style={{ marginTop: 2 }}>
          {n} session{n === 1 ? "" : "s"} read since the last accepted contribution
        </div>
        <div className="at-caption" style={{ marginTop: 2 }}>
          <span style={{ fontFamily: "var(--font-mono)" }}>{versionLabel}</span>
          {" · updated "}{humanAgo(entry.summary_updated_at)}
          {" · last accepted contribution "}{humanAgo(entry.last_accepted_contribution_at)}
        </div>
      </div>
    </button>
  );
}

function SessionOutcomes({ outcomes, windowDays }) {
  const { contributions, closeouts, silent_readers } = outcomes;
  const total = contributions + closeouts + silent_readers;

  if (total === 0) {
    return (
      <div className="at-caption">
        No session has read or contributed in the last {windowDays} days.
      </div>
    );
  }

  const parts = [
    { key: "contributions", value: contributions, color: "var(--at-bull)", label: "contributed" },
    { key: "closeouts", value: closeouts, color: "var(--at-accent)", label: "closed out with nothing to record" },
    { key: "silent_readers", value: silent_readers, color: "var(--at-warn)", label: "read, gave nothing back" },
  ];

  const ariaLabel =
    `${contributions} sessions contributed, ${closeouts} closed out with nothing to record, ` +
    `${silent_readers} read and gave nothing back`;

  return (
    <div>
      <div className="pulse-row-head">
        <span className="at-num-md">{formatNumber(contributions)}</span>
        <span className="at-caption">{formatNumber(silent_readers)} silent</span>
      </div>
      <div className="at-caption" style={{ marginBottom: 6 }}>
        sessions that contributed — last {windowDays} days
      </div>

      <div className="pulse-stack" role="img" aria-label={ariaLabel}>
        {parts.map((p) => (
          <div
            key={p.key}
            className="pulse-stack-part"
            style={{ width: `${(p.value / total) * 100}%`, background: p.color }}
          />
        ))}
      </div>

      <ul className="value-lines">
        {parts.map((p) => (
          <li key={p.key}>
            <span className="pulse-key" style={{ background: p.color }} />
            <span style={{ fontFamily: "var(--font-mono)" }}>{formatNumber(p.value)}</span>
            {" "}{p.label}
          </li>
        ))}
      </ul>

      <div className="at-caption" style={{ marginTop: 8, opacity: 0.8 }}>
        A closeout is a session saying plainly that it had nothing to record. A silent
        session read the memory and said neither.
      </div>
    </div>
  );
}

window.FreshnessView = FreshnessView;
window.ScopeHealthStrip = ScopeHealthStrip;
window.SessionOutcomes = SessionOutcomes;
