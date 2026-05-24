// ─────────────────────────────────────────────────────────────────────
// Strata · Scope detail view.
// Two columns: Backend summary (directives + context) · Scope settings.
// Opened by double-clicking a scope bubble in the graph.
//
// V1 is read-only. Mutations land via the backend bootstrap and CC contribute tool.
// Memory writes flow through `strata.contribute` — see the README.
// ─────────────────────────────────────────────────────────────────────

function ScopeDetail({ scope_id, state, dispatch, onBack, onFlash, embedded = false }) {
  const scope = state.scopes.find((g) => g.id === scope_id);

  // Summary fetched from backend on mount / when scope changes.
  const [summary, setSummary] = React.useState(null);
  const [summaryLoading, setSummaryLoading] = React.useState(false);
  const [summaryError, setSummaryError] = React.useState(null);

  React.useEffect(() => {
    if (!scope_id) return;
    let cancelled = false;
    setSummaryLoading(true);
    setSummaryError(null);
    STRATA_STORE.fetchScopeSummary(scope_id)
      .then((data) => { if (!cancelled) { setSummary(data); setSummaryLoading(false); } })
      .catch((err) => { if (!cancelled) { setSummaryError(err.message); setSummaryLoading(false); } });
    return () => { cancelled = true; };
  }, [scope_id]);

  if (!scope) {
    return (
      <div style={{ padding: 40, color: "var(--at-muted)" }}>
        Scope not found.
        {!embedded && (
          <button className="at-btn at-btn-ghost at-btn-sm" onClick={onBack} style={{ marginLeft: 12 }}>
            Back to graph
          </button>
        )}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, flex: 1, minHeight: 0 }}>
      {/* Header (hidden when embedded in list view) */}
      {!embedded && (
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <button className="at-btn at-btn-secondary at-btn-sm" onClick={onBack}>
            <Icon name="arrow-left" size={12} /> Graph
          </button>
          <span style={{
            width: 14, height: 14, borderRadius: 7,
            background: scope.color, flexShrink: 0,
            border: "2px solid var(--at-surface)",
            boxShadow: "0 0 0 1px var(--at-rule)",
          }} />
          <span className="at-h2" style={{ margin: 0, color: "var(--at-ink)" }}>
            {scope.name}
          </span>
          <span style={{
            fontSize: 11, color: "var(--at-muted)",
            fontFamily: "var(--font-mono)", letterSpacing: "0.06em",
            textTransform: "uppercase",
          }}>
            {state.strata.find((s) => s.id === scope.stratum_id)?.name || "—"}
          </span>
          <div style={{ flex: 1 }} />
        </div>
      )}

      {/* 2-column grid: summary + scope info */}
      <div style={{
        display: "grid",
        gridTemplateColumns: "1fr 320px",
        gap: 14,
        flex: 1,
        minHeight: 0,
      }}>
        {/* Col 1: Backend summary */}
        <Panel title="Scope Summary (from backend)">
          <BackendScopeSummary
            scope={scope}
            summary={summary}
            loading={summaryLoading}
            error={summaryError}
          />
        </Panel>

        {/* Col 2: Scope info (read-only) */}
        <Panel title="Scope info">
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <Field label="ID">
              <div style={{
                fontFamily: "var(--font-mono)", fontSize: 12,
                color: "var(--at-ink-soft)",
                padding: "8px 10px",
                background: "var(--at-bg)",
                border: "1px solid var(--at-rule)",
                borderRadius: 8,
              }}>
                {scope.id}
              </div>
            </Field>
            <Field label="Name">
              <div style={{
                fontSize: 14, color: "var(--at-ink)",
                padding: "8px 10px",
                background: "var(--at-bg)",
                border: "1px solid var(--at-rule)",
                borderRadius: 8,
              }}>
                {scope.name}
              </div>
            </Field>
            <Field label="Stratum">
              <div style={{
                fontSize: 14, color: "var(--at-ink)",
                padding: "8px 10px",
                background: "var(--at-bg)",
                border: "1px solid var(--at-rule)",
                borderRadius: 8,
              }}>
                {state.strata.find((s) => s.id === scope.stratum_id)?.name || scope.stratum_id}
              </div>
            </Field>
            <Field label="Color">
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <span style={{
                  display: "inline-block",
                  width: 28, height: 28, borderRadius: 6,
                  background: scope.color,
                  border: "1px solid var(--at-rule)",
                }} />
                <span style={{ fontFamily: "var(--font-mono)", fontSize: 13, color: "var(--at-ink-soft)" }}>
                  {scope.color}
                </span>
              </div>
            </Field>
            <Field label="Relations">
              <ScopeRelations scope={scope} state={state} />
            </Field>

            {/* Read-only notice */}
            <div style={{
              fontSize: 12, color: "var(--at-muted)", fontStyle: "italic",
              padding: "10px 12px",
              background: "var(--at-bg)",
              border: "1px dashed var(--at-rule)",
              borderRadius: 8,
              lineHeight: 1.5,
            }}>
              V1 is read-only. Scope and memory mutations flow through the backend bootstrap
              and <code style={{ fontFamily: "var(--font-mono)" }}>strata.contribute</code>.
            </div>
          </div>
        </Panel>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// BackendScopeSummary — renders the directives + context from the backend.
// ─────────────────────────────────────────────────────────────────────
function BackendScopeSummary({ scope, summary, loading, error }) {
  if (loading) {
    return (
      <div style={{ color: "var(--at-muted)", fontSize: 13, padding: "20px 0", textAlign: "center" }}>
        <Icon name="loader" size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} />
        Loading summary…
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ color: "var(--at-bear)", fontSize: 13, padding: "20px 0" }}>
        <Icon name="alert-circle" size={14} style={{ marginRight: 6, verticalAlign: "-2px" }} />
        {error}
      </div>
    );
  }

  if (!summary) {
    return (
      <div style={{
        fontSize: 13, color: "var(--at-muted)", fontStyle: "italic",
        padding: "28px 12px", textAlign: "center",
        border: "1px dashed var(--at-rule)", borderRadius: 10,
      }}>
        No summary available.
      </div>
    );
  }

  const { directives = [], context = "", updated_at } = summary;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      {/* Directives */}
      <div>
        <div style={{
          fontSize: 11, fontWeight: 600, color: "var(--at-muted)",
          textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 8,
        }}>
          Directives ({directives.length})
        </div>
        {directives.length === 0 ? (
          <div style={{
            fontSize: 13, color: "var(--at-muted)", fontStyle: "italic",
            padding: "12px", border: "1px dashed var(--at-rule)", borderRadius: 8,
          }}>
            No directives in this scope yet.
          </div>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
            {directives.map((d, i) => (
              <li key={d.id || i} style={{
                background: "var(--at-bg)",
                border: "1px solid var(--at-rule)",
                borderLeft: `3px solid ${scope.color}`,
                borderRadius: 8, padding: "10px 12px",
              }}>
                {d.subject && (
                  <div style={{ fontSize: 12, fontWeight: 600, color: "var(--at-ink)", marginBottom: 4 }}>
                    {d.subject}
                  </div>
                )}
                <div style={{ fontSize: 13, color: "var(--at-ink-soft)", lineHeight: 1.5, whiteSpace: "pre-wrap" }}>
                  {d.content || d}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Context */}
      <div>
        <div style={{
          fontSize: 11, fontWeight: 600, color: "var(--at-muted)",
          textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 8,
        }}>
          Context
        </div>
        <div style={{
          background: "var(--at-bg)",
          border: "1px solid var(--at-rule)",
          borderRadius: 8, padding: "12px 14px",
          fontSize: 13, lineHeight: 1.6,
          color: context ? "var(--at-ink-soft)" : "var(--at-muted)",
          fontStyle: context ? "normal" : "italic",
          minHeight: 60,
          whiteSpace: "pre-wrap",
        }}>
          {context || "No context yet."}
        </div>
      </div>

      {updated_at && (
        <div style={{ fontSize: 11, color: "var(--at-muted)", fontFamily: "var(--font-mono)" }}>
          Updated {new Date(updated_at).toLocaleString()}
        </div>
      )}
    </div>
  );
}

function Panel({ title, action, children }) {
  return (
    <section style={{
      background: "var(--at-surface)",
      border: "1px solid var(--at-rule)",
      borderRadius: 12,
      display: "flex", flexDirection: "column",
      minHeight: 0, overflow: "hidden",
    }}>
      <div style={{
        padding: "10px 14px",
        borderBottom: "1px solid var(--at-rule)",
        display: "flex", alignItems: "center", justifyContent: "space-between",
        background: "var(--at-bg)",
      }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: "var(--at-muted)", textTransform: "uppercase", letterSpacing: "0.1em" }}>
          {title}
        </div>
        {action}
      </div>
      <div style={{ padding: 14, overflowY: "auto", flex: 1, minHeight: 0 }}>{children}</div>
    </section>
  );
}

Object.assign(window, { ScopeDetail, Panel: window.Panel });

// ─────────────────────────────────────────────────────────────────────
// ScopesRail — left sidebar listing every scope, grouped by stratum.
// Used in List view for navigation.
// ─────────────────────────────────────────────────────────────────────
function ScopesRail({ state, selectedId, onSelect }) {
  return (
    <aside style={{
      background: "var(--at-surface)",
      border: "1px solid var(--at-rule)",
      borderRadius: 12,
      display: "flex", flexDirection: "column",
      minHeight: 0, overflow: "hidden",
    }}>
      <div style={{
        padding: "10px 14px",
        borderBottom: "1px solid var(--at-rule)",
        display: "flex", alignItems: "center", justifyContent: "space-between",
        background: "var(--at-bg)",
      }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: "var(--at-muted)", textTransform: "uppercase", letterSpacing: "0.1em" }}>
          Scopes
        </div>
      </div>
      <div style={{ flex: 1, overflowY: "auto", padding: 8 }}>
        {state.strata.length === 0 && (
          <div style={{ padding: "16px 10px", fontSize: 13, color: "var(--at-muted)", fontStyle: "italic" }}>
            No scopes. Run the backend bootstrap to add some.
          </div>
        )}
        {state.strata.map((s, i) => {
          const scopesHere = state.scopes.filter((g) => g.stratum_id === s.id);
          return (
            <div key={s.id} style={{ marginBottom: 12 }}>
              <div style={{
                padding: "6px 8px 4px",
                fontSize: 10, fontWeight: 600,
                color: "var(--at-muted)", textTransform: "uppercase",
                letterSpacing: "0.08em",
                display: "flex", justifyContent: "space-between",
              }}>
                <span>L{i + 1} · {s.name}</span>
                <span style={{ fontFamily: "var(--font-mono)", letterSpacing: 0 }}>{scopesHere.length}</span>
              </div>
              {scopesHere.length === 0 ? (
                <div style={{ padding: "8px 10px", fontSize: 12, color: "var(--at-muted)", fontStyle: "italic" }}>
                  No scopes
                </div>
              ) : (
                <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 2 }}>
                  {scopesHere.map((g) => {
                    const active = selectedId === g.id;
                    return (
                      <li key={g.id}>
                        <button
                          onClick={() => onSelect(g.id)}
                          style={{
                            width: "100%", textAlign: "left",
                            background: active ? "var(--at-highlight)" : "transparent",
                            border: "1px solid " + (active ? g.color + "55" : "transparent"),
                            borderRadius: 8,
                            padding: "8px 10px",
                            display: "flex", alignItems: "center", gap: 10,
                            cursor: "pointer",
                          }}
                        >
                          <span style={{ width: 10, height: 10, borderRadius: 5, background: g.color, flexShrink: 0 }} />
                          <span style={{
                            flex: 1, minWidth: 0,
                            fontSize: 13, fontWeight: active ? 600 : 500,
                            color: "var(--at-ink)",
                            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                          }}>
                            {g.name}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          );
        })}
      </div>
    </aside>
  );
}

// ─────────────────────────────────────────────────────────────────────
// ListView — Scopes rail + the scope detail. Combined.
// ─────────────────────────────────────────────────────────────────────
function ListView({ state, dispatch, selectedId, onSelect, onFlash }) {
  const scope = state.scopes.find((g) => g.id === selectedId);
  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: "240px 1fr",
      gap: 14,
      flex: 1, minHeight: 0,
    }}>
      <ScopesRail
        state={state}
        selectedId={selectedId}
        onSelect={onSelect}
      />
      {scope ? (
        <ScopeDetail
          scope_id={selectedId}
          state={state}
          dispatch={dispatch}
          onFlash={onFlash}
          embedded
        />
      ) : (
        <div style={{
          background: "var(--at-surface)",
          border: "1px dashed var(--at-rule)",
          borderRadius: 12,
          display: "flex", alignItems: "center", justifyContent: "center",
          color: "var(--at-muted)", fontSize: 14,
        }}>
          Pick a scope on the left.
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// SummaryExpandModal — full-summary view triggered from the graph hover.
// ─────────────────────────────────────────────────────────────────────
function SummaryExpandModal({ scopeId, state, dispatch, onClose, onOpenDetail, onFlash }) {
  const open = !!scopeId;
  const scope = state.scopes.find((g) => g.id === scopeId);
  const [summary, setSummary] = React.useState(null);
  const [summaryLoading, setSummaryLoading] = React.useState(false);

  React.useEffect(() => {
    if (!scopeId) { setSummary(null); return; }
    let cancelled = false;
    setSummaryLoading(true);
    STRATA_STORE.fetchScopeSummary(scopeId)
      .then((data) => { if (!cancelled) { setSummary(data); setSummaryLoading(false); } })
      .catch(() => { if (!cancelled) setSummaryLoading(false); });
    return () => { cancelled = true; };
  }, [scopeId]);

  if (!open) return null;
  if (!scope) return null;

  const directives = summary?.directives || [];

  return (
    <Modal
      open={open} onClose={onClose}
      title={scope.name}
      width={620}
      footer={<>
        <button className="at-btn at-btn-secondary at-btn-sm" onClick={onClose}>Close</button>
        <button className="at-btn at-btn-primary at-btn-sm" onClick={() => { onClose(); onOpenDetail(scopeId); }}>
          Open details <Icon name="arrow-right" size={12} />
        </button>
      </>}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <span style={{ width: 12, height: 12, borderRadius: 6, background: scope.color }} />
        <span style={{
          fontSize: 11, color: "var(--at-muted)", fontFamily: "var(--font-mono)",
          letterSpacing: "0.06em", textTransform: "uppercase",
        }}>
          {state.strata.find((s) => s.id === scope.stratum_id)?.name}
        </span>
        <span style={{ fontSize: 12, color: "var(--at-muted)" }}>·</span>
        <span style={{ fontSize: 12, color: "var(--at-muted)" }}>
          {directives.length} directive{directives.length === 1 ? "" : "s"}
        </span>
      </div>
      <BackendScopeSummary
        scope={scope}
        summary={summary}
        loading={summaryLoading}
        error={null}
      />
    </Modal>
  );
}

Object.assign(window, { ScopeDetail, ScopesRail, ListView, SummaryExpandModal });

// ─────────────────────────────────────────────────────────────────────
// ScopeRelations — list existing edges for a scope (read-only).
// V1 is read-only; edge mutations are not supported in the UI.
// ─────────────────────────────────────────────────────────────────────
function ScopeRelations({ scope, state }) {
  const connected = state.edges
    .map((e) => {
      if (e.from === scope.id) return { otherId: e.to, edge: e };
      if (e.to === scope.id) return { otherId: e.from, edge: e };
      return null;
    })
    .filter(Boolean);

  if (connected.length === 0) {
    return (
      <div style={{
        fontSize: 12, color: "var(--at-muted)", fontStyle: "italic",
        padding: "10px 12px",
        border: "1px dashed var(--at-rule)", borderRadius: 8,
      }}>
        No relations.
      </div>
    );
  }

  return (
    <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 4 }}>
      {connected.map(({ otherId }) => {
        const other = state.scopes.find((g) => g.id === otherId);
        if (!other) return null;
        const otherStratum = state.strata.find((s) => s.id === other.stratum_id);
        const sameStratum = other.stratum_id === scope.stratum_id;
        return (
          <li key={otherId} style={{
            display: "flex", alignItems: "center", gap: 8,
            background: "var(--at-bg)",
            border: "1px solid var(--at-rule)",
            borderRadius: 8, padding: "6px 10px",
          }}>
            <span style={{ width: 10, height: 10, borderRadius: 5, background: other.color, flexShrink: 0 }} />
            <span style={{ flex: 1, minWidth: 0, fontSize: 13, color: "var(--at-ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {other.name}
            </span>
            <span style={{
              fontSize: 10, fontWeight: 500,
              color: "var(--at-muted)",
              fontFamily: "var(--font-mono)",
              letterSpacing: "0.06em", textTransform: "uppercase",
              padding: "2px 6px", borderRadius: 4,
              background: sameStratum ? "var(--at-highlight)" : "transparent",
              border: sameStratum ? "none" : "1px dashed var(--at-rule)",
            }} title={sameStratum ? "Same stratum" : "Adjacent stratum"}>
              {sameStratum ? "same" : (otherStratum?.name || "±1")}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

window.ScopeRelations = ScopeRelations;
