// ─────────────────────────────────────────────────────────────────────
// Strata · root app. Reducer + routing + backend polling.
// Screens: graph (default) · scope detail (drill-in) · settings.
//
// V1 is read-only. Mutations land via the backend bootstrap and CC contribute tool.
// ─────────────────────────────────────────────────────────────────────

const PALETTE = ["#c026d3", "#16a34a", "#ca8a04", "#7c3aed", "#2563eb", "#dc2626", "#0891b2", "#65a30d", "#db2777"];
function pickColor(i) { return PALETTE[i % PALETTE.length]; }

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "filter": "all",
  "nodeSize": 9
} /*EDITMODE-END*/;

function reducer(state, action) {
  const next = { ...state };
  switch (action.type) {
    case "set_option":
      // UI display options are still allowed (dark mode, graph physics).
      next.options = { ...state.options, [action.key]: action.value };
      // Persist display preferences locally.
      STRATA_STORE.savePrefs({ ...(state.options || {}), [action.key]: action.value });
      return next;

    // Backend-hydration action: replace fleet state from API response.
    case "hydrate":
      return {
        ...state,
        strata: action.data.strata,
        scopes: action.data.scopes.map((s, i) => ({
          ...s,
          color: s.color || pickColor(i),
        })),
        memories: action.data.memories || [],
        edges: action.data.edges,
        loading: false,
        error: null,
      };

    case "set_loading":
      return { ...state, loading: action.value };

    case "set_error":
      return { ...state, error: action.message, loading: false };

    default:
      return state;
  }
}

function makeInitialState() {
  const prefs = STRATA_STORE.loadPrefs();
  return {
    strata: [],
    scopes: [],
    memories: [],
    edges: [],
    options: {
      dark_mode: prefs.dark_mode || false,
      graph_charge: prefs.graph_charge || 800,
      graph_link_distance: prefs.graph_link_distance || 120,
    },
    loading: true,
    error: null,
  };
}

function App() {
  const [state, dispatch] = React.useReducer(reducer, undefined, makeInitialState);
  const [tab, setTab] = React.useState("graph"); // graph | settings
  const [view, setView] = React.useState("graph"); // graph | list
  const [openScopeId, setOpenScopeId] = React.useState(null);
  const [expandSummaryId, setExpandSummaryId] = React.useState(null);
  const [toast, setToast] = React.useState(null);
  const [tweaks, setTweak] = useTweaks(TWEAK_DEFAULTS);

  // Expose dispatch for graph.jsx interactions.
  React.useEffect(() => { window.__strataDispatch = dispatch; }, [dispatch]);

  // Dark mode.
  React.useEffect(() => {
    document.documentElement.classList.toggle("dark", !!state.options.dark_mode);
  }, [state.options.dark_mode]);

  // Lucide rebind.
  React.useEffect(() => {
    if (window.lucide?.createIcons) {
      const id = requestAnimationFrame(() => window.lucide.createIcons());
      return () => cancelAnimationFrame(id);
    }
  });

  // Backend polling: hydrate on mount and every REFRESH_INTERVAL_MS.
  React.useEffect(() => {
    let cancelled = false;

    async function refresh() {
      try {
        const data = await STRATA_STORE.fetchFleet();
        if (!cancelled) dispatch({ type: "hydrate", data });
      } catch (err) {
        if (!cancelled) dispatch({ type: "set_error", message: err.message });
      }
    }

    refresh(); // immediate first load
    const interval = setInterval(refresh, STRATA_STORE.REFRESH_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  // If we drilled in but the scope got removed by a backend refresh, bounce back.
  React.useEffect(() => {
    if (openScopeId && !state.scopes.find((g) => g.id === openScopeId)) {
      setOpenScopeId(null);
    }
  }, [state.scopes, openScopeId]);

  function flash(text) { setToast({ text, id: Date.now() }); }

  // Loading / error state.
  if (state.loading) {
    return (
      <div className="at" style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", flexDirection: "column", gap: 14, color: "var(--at-muted)" }}>
        <Icon name="loader" size={24} />
        <div style={{ fontSize: 14 }}>Connecting to Strata backend…</div>
        <div style={{ fontSize: 12, fontFamily: "var(--font-mono)" }}>{STRATA_STORE.getApiBase()}</div>
      </div>
    );
  }

  if (state.error) {
    return (
      <div className="at" style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", flexDirection: "column", gap: 14, color: "var(--at-muted)" }}>
        <Icon name="wifi-off" size={24} style={{ color: "var(--at-bear)" }} />
        <div style={{ fontSize: 14, color: "var(--at-bear)" }}>Could not reach Strata backend</div>
        <div style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--at-muted)" }}>{state.error}</div>
        <div style={{ fontSize: 12, color: "var(--at-muted)" }}>
          Make sure <code style={{ fontFamily: "var(--font-mono)" }}>make run</code> is running at{" "}
          <code style={{ fontFamily: "var(--font-mono)" }}>{STRATA_STORE.getApiBase()}</code>.
          The page will retry automatically.
        </div>
      </div>
    );
  }

  return (
    <div className="at" style={{ minHeight: "100vh", display: "flex", flexDirection: "column" }}>
      <TopBar
        tab={tab}
        onTab={(t) => { setTab(t); setOpenScopeId(null); }}
        dark={!!state.options.dark_mode}
        onToggleDark={() => dispatch({ type: "set_option", key: "dark_mode", value: !state.options.dark_mode })}
      />

      <main style={{
        flex: 1,
        maxWidth: 1500, width: "100%",
        margin: "0 auto",
        padding: "20px 24px 28px",
        display: "flex", flexDirection: "column",
        minHeight: 0,
      }}>
        {tab === "graph" && (
          <>
            <div style={{
              display: "flex", alignItems: "flex-end", justifyContent: "space-between",
              marginBottom: 14, gap: 16, flexWrap: "wrap",
            }}>
              <div>
                <h1 className="at-h1" style={{ marginBottom: 2 }}>Memory graph</h1>
                <div style={{ color: "var(--at-muted)", fontSize: 13 }}>
                  Each lane is a stratum. Scopes (coloured bubbles) hold memories. Double-click a scope to drill in.
                  {" "}<span style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>Read-only — writes flow through the backend.</span>
                </div>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
                <div style={{ fontSize: 12, color: "var(--at-muted)", fontFamily: "var(--font-mono)", display: "flex", gap: 14 }}>
                  <span><b style={{ color: "var(--at-ink)" }}>{state.strata.length}</b> strata</span>
                  <span><b style={{ color: "var(--at-ink)" }}>{state.scopes.length}</b> scopes</span>
                  <span><b style={{ color: "var(--at-ink)" }}>{state.edges.length}</b> edges</span>
                </div>
                <ViewToggle view={view} onChange={setView} />
              </div>
            </div>

            {view === "graph" && (
              <MemoryGraph
                state={state}
                tweaks={tweaks}
                onOpenScope={(id) => { setOpenScopeId(id); setView("list"); }}
                onExpandSummary={(id) => setExpandSummaryId(id)}
              />
            )}
            {view === "list" && (
              <ListView
                state={state}
                dispatch={dispatch}
                selectedId={openScopeId || state.scopes[0]?.id}
                onSelect={(id) => setOpenScopeId(id)}
                onFlash={flash}
              />
            )}
          </>
        )}

        {tab === "settings" && (
          <SettingsScreen state={state} dispatch={dispatch} onFlash={flash} />
        )}
      </main>

      {toast && <Toast text={toast.text} key={toast.id} onDone={() => setToast(null)} />}

      <SummaryExpandModal
        scopeId={expandSummaryId}
        state={state}
        dispatch={dispatch}
        onClose={() => setExpandSummaryId(null)}
        onOpenDetail={(id) => { setOpenScopeId(id); setView("list"); }}
        onFlash={flash}
      />

      <TweaksPanel title="Tweaks">
        <TweakSection label="Graph" />
        <TweakSlider
          label="Repulsion"
          value={state.options.graph_charge}
          min={300} max={1800} step={50}
          onChange={(v) => dispatch({ type: "set_option", key: "graph_charge", value: v })}
        />
        <TweakSlider
          label="Edge length"
          value={state.options.graph_link_distance}
          min={60} max={240} step={5} unit="px"
          onChange={(v) => dispatch({ type: "set_option", key: "graph_link_distance", value: v })}
        />
        <TweakToggle
          label="Dark mode"
          value={!!state.options.dark_mode}
          onChange={(v) => dispatch({ type: "set_option", key: "dark_mode", value: v })}
        />
      </TweaksPanel>
    </div>
  );
}

function TopBar({ tab, onTab, dark, onToggleDark }) {
  return (
    <header style={{
      background: "var(--at-surface)",
      borderBottom: "1px solid var(--at-rule)",
      padding: "12px 24px",
      display: "flex", alignItems: "center", gap: 24,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{
          width: 24, height: 24, borderRadius: 6,
          background: "var(--at-primary)",
          display: "flex", alignItems: "center", justifyContent: "center",
        }}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <rect x="1" y="2" width="12" height="2" rx="1" fill="#fff" opacity="0.55" />
            <rect x="1" y="6" width="12" height="2" rx="1" fill="#fff" opacity="0.75" />
            <rect x="1" y="10" width="12" height="2" rx="1" fill="#fff" />
          </svg>
        </div>
        <span style={{ fontSize: 16, fontWeight: 600, letterSpacing: "-0.01em" }}>Strata</span>
      </div>

      <nav style={{ display: "flex", gap: 4, marginLeft: 8 }}>
        <button
          className={"at-tab" + (tab === "graph" ? " active" : "")}
          onClick={() => onTab("graph")}
        >
          <Icon name="git-fork" size={13} style={{ marginRight: 6, verticalAlign: "-2px" }} />
          Memory graph
        </button>
        <button
          className={"at-tab" + (tab === "settings" ? " active" : "")}
          onClick={() => onTab("settings")}
        >
          <Icon name="settings" size={13} style={{ marginRight: 6, verticalAlign: "-2px" }} />
          Settings
        </button>
      </nav>

      <div style={{ flex: 1 }} />

      {/* Read-only badge */}
      <span style={{
        fontSize: 10, fontFamily: "var(--font-mono)",
        color: "var(--at-muted)",
        padding: "3px 8px", borderRadius: 999,
        border: "1px solid var(--at-rule)",
        letterSpacing: "0.06em", textTransform: "uppercase",
      }}>
        read-only
      </span>

      <button
        onClick={onToggleDark}
        title={dark ? "Switch to light mode" : "Switch to dark mode"}
        style={{
          width: 32, height: 32, borderRadius: 8,
          background: "transparent",
          border: "1px solid var(--at-rule)",
          cursor: "pointer",
          color: "var(--at-ink-soft)",
          display: "inline-flex", alignItems: "center", justifyContent: "center",
        }}
      >
        <Icon name={dark ? "sun" : "moon"} size={14} />
      </button>
    </header>
  );
}

// ─────────────────────────────────────────────────────────────────────
// ViewToggle — pill segmented control between Graph and List views.
// ─────────────────────────────────────────────────────────────────────
function ViewToggle({ view, onChange }) {
  const opts = [
    { id: "graph", label: "Graph", icon: "git-fork" },
    { id: "list",  label: "List",  icon: "list" },
  ];
  return (
    <div style={{
      display: "inline-flex",
      background: "var(--at-highlight)",
      border: "1px solid var(--at-rule-soft)",
      borderRadius: 8, padding: 2, gap: 2,
    }}>
      {opts.map((o) => {
        const active = view === o.id;
        return (
          <button
            key={o.id}
            onClick={() => onChange(o.id)}
            style={{
              display: "inline-flex", alignItems: "center", gap: 6,
              padding: "6px 12px", borderRadius: 6,
              background: active ? "var(--at-surface)" : "transparent",
              color: active ? "var(--at-ink)" : "var(--at-muted)",
              border: "none",
              fontSize: 12.5, fontWeight: active ? 600 : 500,
              cursor: "pointer",
              boxShadow: active ? "0 1px 2px rgba(0,0,0,0.06)" : "none",
              transition: "background 120ms, color 120ms",
            }}
          >
            <Icon name={o.icon} size={12} />
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
window.ViewToggle = ViewToggle;

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
