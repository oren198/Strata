// ─────────────────────────────────────────────────────────────────────
// Strata · Fleet editor (D1: text-first, not forms).
//
// Opens the raw fleet.yaml text (comments and all — nothing here ever
// round-trips through YAML), validates it against the engine's own load
// path, and saves it: validate -> etag conflict guard -> backup -> atomic
// write -> hot swap. This backend picks the new fleet up immediately;
// embedded agent sessions keep the fleet they started with until restarted
// (ADR 0002) — the save banner says so every time.
// ─────────────────────────────────────────────────────────────────────

function FleetEditView({ onFleetSaved, onDirtyChange, onFlash }) {
  const apiBase = STRATA_STORE.getApiBase();

  const [yaml, setYaml] = React.useState("");
  const [etag, setEtag] = React.useState(null);
  const [path, setPath] = React.useState("");
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState(null);
  const [dirty, setDirty] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [validation, setValidation] = React.useState(null); // { ok, scopes, edges } | { ok: false, detail }
  const [saveResult, setSaveResult] = React.useState(null); // { note, scopes, edges }
  const [conflict, setConflict] = React.useState(null); // { detail }

  const load = React.useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const resp = await fetch(`${apiBase}/fleet`);
      if (!resp.ok) throw new Error(`GET /fleet returned ${resp.status}`);
      const data = await resp.json();
      setYaml(data.yaml);
      setEtag(data.etag);
      setPath(data.path);
      setDirty(false);
      setValidation(null);
      setConflict(null);
    } catch (err) {
      setLoadError(err.message);
    } finally {
      setLoading(false);
    }
  }, [apiBase]);

  React.useEffect(() => {
    load();
  }, [load]);

  React.useEffect(() => {
    if (onDirtyChange) onDirtyChange(dirty);
    return () => {
      if (onDirtyChange) onDirtyChange(false);
    };
  }, [dirty, onDirtyChange]);

  function handleChange(e) {
    setYaml(e.target.value);
    setDirty(true);
    setValidation(null);
    setSaveResult(null);
    setConflict(null);
  }

  function _errorDetail(data, fallback) {
    return (data && data.detail && data.detail.detail) || fallback;
  }

  async function handleValidate() {
    setBusy(true);
    setValidation(null);
    setSaveResult(null);
    setConflict(null);
    try {
      const resp = await fetch(`${apiBase}/fleet/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yaml }),
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) {
        setValidation({ ok: false, detail: _errorDetail(data, "This fleet did not validate.") });
      } else {
        setValidation({ ok: true, scopes: data.scopes, edges: data.edges });
      }
    } catch (err) {
      setValidation({ ok: false, detail: err.message });
    } finally {
      setBusy(false);
    }
  }

  async function handleSave() {
    setBusy(true);
    setValidation(null);
    setSaveResult(null);
    setConflict(null);
    try {
      const resp = await fetch(`${apiBase}/fleet`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yaml, etag }),
      });
      const data = await resp.json().catch(() => null);
      if (resp.status === 409) {
        setConflict({
          detail: _errorDetail(data, "The fleet file changed since you loaded it — reload and reapply your edit."),
        });
        return;
      }
      if (!resp.ok) {
        setValidation({ ok: false, detail: _errorDetail(data, "This fleet did not validate.") });
        return;
      }
      setDirty(false);
      setSaveResult({ note: data.note, scopes: data.scopes, edges: data.edges });
      if (onFlash) onFlash("Fleet saved.");
      if (onFleetSaved) onFleetSaved();
      // Pick up the fresh etag/text this save just wrote, so a second save
      // in the same visit doesn't spuriously 409 against its own write.
      const refreshed = await fetch(`${apiBase}/fleet`);
      if (refreshed.ok) {
        const refreshedData = await refreshed.json();
        setYaml(refreshedData.yaml);
        setEtag(refreshedData.etag);
        setPath(refreshedData.path);
      }
    } catch (err) {
      setValidation({ ok: false, detail: err.message });
    } finally {
      setBusy(false);
    }
  }

  function handleReload() {
    if (dirty) {
      const proceed = window.confirm("Discard your unsaved edit and reload the fleet file from disk?");
      if (!proceed) return;
    }
    load();
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14, minHeight: 0, flex: 1 }}>
      <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
        <div>
          <h1 className="at-h1" style={{ marginBottom: 2 }}>Edit fleet</h1>
          <div style={{ color: "var(--at-muted)", fontSize: 13, fontFamily: "var(--font-mono)" }}>
            {path || "fleet.yaml"}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button className="at-btn at-btn-secondary at-btn-sm" onClick={handleReload} disabled={busy || loading}>
            <Icon name="refresh-cw" size={12} style={{ marginRight: 6, verticalAlign: "-2px" }} />
            Reload file
          </button>
          <button className="at-btn at-btn-secondary at-btn-sm" onClick={handleValidate} disabled={busy || loading}>
            <Icon name="check-circle" size={12} style={{ marginRight: 6, verticalAlign: "-2px" }} />
            Validate
          </button>
          <button
            className="at-btn at-btn-sm"
            onClick={handleSave}
            disabled={busy || loading || !dirty}
            style={{ background: "var(--at-primary)", color: "#fff" }}
          >
            <Icon name="save" size={12} style={{ marginRight: 6, verticalAlign: "-2px" }} />
            Save
          </button>
        </div>
      </div>

      {loading && (
        <div style={{ color: "var(--at-muted)", fontSize: 13 }}>Loading fleet.yaml…</div>
      )}

      {loadError && (
        <div style={{
          fontSize: 13, color: "var(--at-bear)",
          background: "var(--at-bg)", border: "1px solid var(--at-rule)",
          borderRadius: 8, padding: "10px 12px",
        }}>
          Could not load fleet.yaml: {loadError}
        </div>
      )}

      {!loading && !loadError && (
        <textarea
          className="fleet-edit-textarea"
          value={yaml}
          onChange={handleChange}
          spellCheck={false}
          style={{
            flex: 1, minHeight: 360, width: "100%", resize: "vertical",
            fontFamily: "var(--font-mono)", fontSize: 13, lineHeight: 1.5,
            padding: 14, whiteSpace: "pre", tabSize: 2,
            background: "var(--at-bg)", color: "var(--at-ink)",
            border: "1px solid var(--at-rule)", borderRadius: 10,
          }}
        />
      )}

      {conflict && (
        <div style={{
          fontSize: 13, color: "var(--at-bear)",
          background: "var(--at-bg)", border: "1px solid var(--at-rule)",
          borderRadius: 8, padding: "10px 12px",
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
        }}>
          <span>{conflict.detail}</span>
          <button className="at-btn at-btn-secondary at-btn-sm" onClick={handleReload}>Reload file</button>
        </div>
      )}

      {validation && !validation.ok && (
        <div style={{
          fontSize: 13, color: "var(--at-bear)",
          background: "var(--at-bg)", border: "1px solid var(--at-rule)",
          borderRadius: 8, padding: "10px 12px", whiteSpace: "pre-wrap",
        }}>
          {validation.detail}
        </div>
      )}

      {validation && validation.ok && (
        <div style={{
          fontSize: 13, color: "var(--at-ink)",
          background: "var(--at-bg)", border: "1px solid var(--at-rule)",
          borderRadius: 8, padding: "10px 12px",
        }}>
          This fleet is valid — {validation.scopes} scope{validation.scopes === 1 ? "" : "s"}, {validation.edges} edge{validation.edges === 1 ? "" : "s"}.
        </div>
      )}

      {saveResult && (
        <div style={{
          fontSize: 13, color: "var(--at-ink)",
          background: "var(--at-bg)", border: "1px solid var(--at-rule)",
          borderRadius: 8, padding: "10px 12px",
        }}>
          Saved — {saveResult.scopes} scope{saveResult.scopes === 1 ? "" : "s"}, {saveResult.edges} edge{saveResult.edges === 1 ? "" : "s"}.
          This Console updates now. {saveResult.note}
        </div>
      )}

      {dirty && !saveResult && (
        <div style={{ fontSize: 12, color: "var(--at-muted)" }}>
          Unsaved changes.
        </div>
      )}
    </div>
  );
}

window.FleetEditView = FleetEditView;
