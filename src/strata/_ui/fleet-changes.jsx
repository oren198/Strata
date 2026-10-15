// Fleet changes — pending structure changes the operator can apply or reject,
// and a read-only list of applied acts with the events already recorded.
// Apply and reject are operator-only. The request does not name a scope.
// Nothing in this view emits a change event. Re-emission is `strata fleet reemit`.

function FleetChangesView({ onFlash }) {
  const [rows, setRows] = React.useState(null);
  const [acts, setActs] = React.useState([]);
  const [error, setError] = React.useState(null);
  const [busyId, setBusyId] = React.useState(null);
  const [notices, setNotices] = React.useState({});

  async function load() {
    try {
      const data = await STRATA_STORE.fetchFleetChanges();
      const recorded = await STRATA_STORE.fetchFleetActs();
      setRows(data.changes || []);
      setActs(recorded.acts || []);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }

  React.useEffect(() => { load(); }, []);

  async function act(id, fn) {
    setBusyId(id);
    try {
      const result = await fn(id);
      const lines = result.notices || [];
      setNotices((prev) => ({ ...prev, [id]: lines }));
      if (lines.length) onFlash(lines[0]);
      else onFlash(result.status === "rejected" ? "Rejected." : "Applied.");
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyId(null);
    }
  }

  if (error) {
    return (
      <div style={{ color: "var(--at-bear)", fontSize: 14 }}>{error}</div>
    );
  }
  if (rows === null) {
    return <div style={{ color: "var(--at-muted)", fontSize: 14 }}>Loading fleet changes…</div>;
  }

  return (
    <div>
      <h1 className="at-h1" style={{ marginBottom: 4 }}>Fleet changes</h1>
      <div style={{ color: "var(--at-muted)", fontSize: 13, marginBottom: 16 }}>
        Pending structure changes. Apply and reject act as the operator.
        A change inside a scope's own subtree is applied by that scope and does not wait here.
      </div>
      {rows.length === 0 && (
        <div style={{ color: "var(--at-muted)", fontSize: 14 }}>No pending fleet changes.</div>
      )}
      {rows.map((row) => (
        <div
          key={row.id}
          style={{
            border: "1px solid var(--at-rule)",
            borderRadius: 8,
            padding: "12px 14px",
            marginBottom: 10,
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
            <div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}>
                {row.change_type} · {row.id}
              </div>
              <div style={{ color: "var(--at-muted)", fontSize: 12, marginTop: 4 }}>
                proposer {row.proposer_position} · owner {row.owner_scope_id || "operator"}
              </div>
              <div style={{ fontSize: 12, marginTop: 4 }}>
                {(row.flags && row.flags.length) ? row.flags.join("; ") : "flags: none"}
              </div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, marginTop: 6, color: "var(--at-muted)" }}>
                {JSON.stringify(row.payload)}
              </div>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
              <button
                className="at-btn at-btn-sm"
                disabled={busyId === row.id}
                onClick={() => act(row.id, STRATA_STORE.applyFleetChange)}
              >
                Apply
              </button>
              <button
                className="at-btn at-btn-secondary at-btn-sm"
                disabled={busyId === row.id}
                onClick={() => act(row.id, STRATA_STORE.rejectFleetChange)}
              >
                Reject
              </button>
            </div>
          </div>
          {(notices[row.id] || []).map((line) => (
            <div key={line} style={{ fontSize: 12, marginTop: 8 }}>{line}</div>
          ))}
        </div>
      ))}
      <AppliedActs acts={acts} />
    </div>
  );
}

// Read-only. Each act shows the change events that carry it and every
// conflict line ("Own directive … governs …") those events recorded.
function AppliedActs({ acts }) {
  const list = acts || [];
  return (
    <div style={{ marginTop: 28 }}>
      <h2 className="at-h2" style={{ marginBottom: 4 }}>Applied acts</h2>
      <div style={{ color: "var(--at-muted)", fontSize: 13, marginBottom: 16 }}>
        Read-only. Emitted events and conflict lines for each applied act.
      </div>
      {list.length === 0 && (
        <div style={{ color: "var(--at-muted)", fontSize: 14 }}>No applied acts.</div>
      )}
      {list.map((act) => (
        <div
          key={act.id}
          style={{
            border: "1px solid var(--at-rule)",
            borderRadius: 8,
            padding: "12px 14px",
            marginBottom: 10,
          }}
        >
          <div style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}>
            {act.change_type} · {act.id}
          </div>
          <div style={{ color: "var(--at-muted)", fontSize: 12, marginTop: 4 }}>
            approver {act.approver_position || "—"}
          </div>
          <div style={{ fontSize: 12, fontWeight: 600, marginTop: 10 }}>Emitted events</div>
          {(act.events || []).length === 0 ? (
            <div style={{ color: "var(--at-muted)", fontSize: 12, marginTop: 4 }}>
              No change events for this act.
            </div>
          ) : (
            (act.events || []).map((event) => (
              <div
                key={event.id}
                style={{ fontFamily: "var(--font-mono)", fontSize: 11, marginTop: 4 }}
              >
                {event.created_at} — {event.kind} — {event.scope_id}
              </div>
            ))
          )}
          {(act.conflict_lines || []).map((line) => (
            <div key={line} style={{ fontSize: 12, marginTop: 8 }}>{line}</div>
          ))}
        </div>
      ))}
    </div>
  );
}
