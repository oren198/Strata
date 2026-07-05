// ─────────────────────────────────────────────────────────────────────
// Strata · Settings page. Display options only.
// V1 is read-only. Fleet config mutations flow through the backend.
// ─────────────────────────────────────────────────────────────────────

function SettingsScreen({ state, dispatch, onFlash }) {
  function setOption(key, value) {
    dispatch({ type: "set_option", key, value });
  }

  const apiBase = STRATA_STORE.getApiBase();

  return (
    <div style={{ maxWidth: 760, margin: "0 auto", paddingBottom: 60 }}>
      <h1 className="at-h1" style={{ marginBottom: 4 }}>Settings</h1>
      <div style={{ color: "var(--at-muted)", fontSize: 14, marginBottom: 28 }}>
        Display preferences. Fleet configuration is managed via the backend bootstrap.
      </div>

      <Section title="Backend" subtitle="Where the Strata API is running.">
        <Field label="API base URL">
          <div style={{
            fontFamily: "var(--font-mono)", fontSize: 13,
            padding: "10px 12px",
            background: "var(--at-bg)",
            border: "1px solid var(--at-rule)",
            borderRadius: 8,
            color: "var(--at-ink-soft)",
          }}>
            {apiBase}
          </div>
          <span style={{ fontSize: 12, color: "var(--at-muted)", lineHeight: 1.45 }}>
            Set the <code style={{ fontFamily: "var(--font-mono)" }}>content</code> attribute on the{" "}
            <code style={{ fontFamily: "var(--font-mono)" }}>&lt;meta name="strata-api-base"&gt;</code> tag
            in <code style={{ fontFamily: "var(--font-mono)" }}>ui/index.html</code> to change this.
          </span>
        </Field>

        <div style={{
          fontSize: 12, color: "var(--at-muted)",
          padding: "10px 12px",
          background: "var(--at-bg)",
          border: "1px dashed var(--at-rule)",
          borderRadius: 8,
          lineHeight: 1.5,
        }}>
          The UI polls the backend every {STRATA_STORE.REFRESH_INTERVAL_MS / 1000}s. No writes are
          possible from the UI in V1 — all memory mutations flow through{" "}
          <code style={{ fontFamily: "var(--font-mono)" }}>strata.contribute</code>.
        </div>
      </Section>

      <Section title="Display &amp; graph" subtitle="These preferences are stored locally in your browser.">
        <Toggle
          label="Dark mode"
          checked={!!state.options.dark_mode}
          onChange={(v) => setOption("dark_mode", v)}
        />
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
          <Field label="Repulsion" hint="Higher pushes scope nodes apart more.">
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <input
                type="range" min="200" max="1600" step="50"
                value={state.options.graph_charge}
                onChange={(e) => setOption("graph_charge", parseInt(e.target.value, 10))}
                style={{ flex: 1 }}
              />
              <span style={{
                fontFamily: "var(--font-mono)", fontSize: 13,
                color: "var(--at-ink)", width: 48, textAlign: "right",
              }}>{state.options.graph_charge}</span>
            </div>
          </Field>
          <Field label="Edge length" hint="Spring rest length between linked scopes.">
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <input
                type="range" min="30" max="180" step="5"
                value={state.options.graph_link_distance}
                onChange={(e) => setOption("graph_link_distance", parseInt(e.target.value, 10))}
                style={{ flex: 1 }}
              />
              <span style={{
                fontFamily: "var(--font-mono)", fontSize: 13,
                color: "var(--at-ink)", width: 48, textAlign: "right",
              }}>{state.options.graph_link_distance}</span>
            </div>
          </Field>
        </div>
      </Section>

      <Section title="Fleet" subtitle="Read-only view of the current fleet configuration.">
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {state.strata.map((s, i) => (
            <div key={s.id} style={{
              display: "flex", alignItems: "center", gap: 10,
              background: "var(--at-bg)",
              border: "1px solid var(--at-rule)",
              borderRadius: 8, padding: "8px 12px",
            }}>
              <span style={{
                fontSize: 11, color: "var(--at-muted)",
                fontFamily: "var(--font-mono)", letterSpacing: "0.08em",
                textTransform: "uppercase", width: 28, flexShrink: 0,
              }}>L{i + 1}</span>
              <span style={{ flex: 1, fontSize: 14, color: "var(--at-ink)" }}>{s.name}</span>
              <span style={{ fontSize: 11, color: "var(--at-muted)" }}>
                {state.scopes.filter((g) => g.stratum_id === s.id).length} scope{state.scopes.filter((g) => g.stratum_id === s.id).length === 1 ? "" : "s"}
              </span>
            </div>
          ))}
          {state.strata.length === 0 && (
            <div style={{ fontSize: 13, color: "var(--at-muted)", fontStyle: "italic" }}>
              No strata yet — run the backend bootstrap.
            </div>
          )}
        </div>
        <div style={{ fontSize: 12, color: "var(--at-muted)" }}>
          To add or modify strata and scopes, edit the bootstrap YAML and re-run{" "}
          <code style={{ fontFamily: "var(--font-mono)" }}>make run</code>.
        </div>
      </Section>
    </div>
  );
}

function Section({ title, subtitle, children }) {
  return (
    <section style={{
      background: "var(--at-surface)",
      border: "1px solid var(--at-rule)",
      borderRadius: 12,
      padding: 22,
      marginBottom: 18,
      display: "flex", flexDirection: "column", gap: 16,
    }}>
      <div>
        <h2 className="at-h3" style={{ marginBottom: subtitle ? 4 : 0 }}>{title}</h2>
        {subtitle && (
          <div style={{ fontSize: 13, color: "var(--at-muted)" }}>{subtitle}</div>
        )}
      </div>
      {children}
    </section>
  );
}

function Toggle({ label, checked, onChange }) {
  return (
    <label style={{
      display: "flex", alignItems: "center", justifyContent: "space-between",
      gap: 12, cursor: "pointer", userSelect: "none",
    }}>
      <span style={{ fontSize: 14, color: "var(--at-ink)" }}>{label}</span>
      <button
        type="button"
        onClick={() => onChange(!checked)}
        style={{
          width: 38, height: 22,
          background: checked ? "var(--at-primary)" : "var(--at-rule)",
          borderRadius: 999, padding: 2,
          border: "none", cursor: "pointer",
          position: "relative",
          transition: "background 160ms",
        }}
        aria-pressed={checked}
      >
        <span style={{
          position: "absolute", top: 2,
          left: checked ? 18 : 2,
          width: 18, height: 18,
          background: "#fff", borderRadius: 999,
          boxShadow: "0 1px 3px rgba(0,0,0,0.18)",
          transition: "left 160ms",
        }} />
      </button>
    </label>
  );
}

Object.assign(window, { SettingsScreen });
