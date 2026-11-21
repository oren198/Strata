// Held for the operator (ADR 0018) — read-only. The data is GET /review/held.
// Nothing on this tab writes, and nothing here calls the judge. Approve and reject are the
// CLI's (`strata operator review`); the Console has no control for them.

function reviewAge(seconds) {
  if (seconds === null || seconds === undefined) return "none";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d`;
}

function ReviewView() {
  const [held, setHeld] = React.useState(null);
  const [error, setError] = React.useState(null);

  React.useEffect(() => {
    let cancelled = false;
    STRATA_STORE.fetchReviewHeld()
      .then((data) => {
        if (!cancelled) setHeld(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return <div style={{ color: "var(--at-bear)", fontSize: 14 }}>{error}</div>;
  }
  if (held === null) {
    return <div style={{ color: "var(--at-muted)", fontSize: 14 }}>Loading held items…</div>;
  }

  const items = held.items || [];

  return (
    <div>
      <h1 className="at-h1" style={{ marginBottom: 4 }}>Held for the operator</h1>
      <div style={{ color: "var(--at-muted)", fontSize: 13, marginBottom: 8 }}>
        Directive changes a human-review scope is holding. A held item is not memory: it was never
        admitted, and the directive it targets keeps binding. Read-only here; decide at the
        command line.
      </div>
      <div style={{ fontSize: 14, marginBottom: 16 }}>
        {held.count} held, oldest {reviewAge(held.oldest_age_seconds)}.
      </div>
      {items.length === 0 && (
        <div style={{ color: "var(--at-muted)", fontSize: 14 }}>Nothing is held.</div>
      )}
      {items.length > 0 && (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--at-muted)" }}>
              <th style={{ padding: "6px 8px" }}>Id</th>
              <th style={{ padding: "6px 8px" }}>Scope</th>
              <th style={{ padding: "6px 8px" }}>Status</th>
              <th style={{ padding: "6px 8px" }}>Content</th>
              <th style={{ padding: "6px 8px" }}>Requested by</th>
              <th style={{ padding: "6px 8px" }}>Held since</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id} style={{ borderTop: "1px solid var(--at-rule)" }}>
                <td style={{ padding: "6px 8px", fontFamily: "var(--font-mono)" }}>{item.id}</td>
                <td style={{ padding: "6px 8px" }}>{item.scope_id}</td>
                <td style={{ padding: "6px 8px" }}>{item.label}</td>
                <td style={{ padding: "6px 8px" }}>{item.content}</td>
                <td style={{ padding: "6px 8px", fontFamily: "var(--font-mono)" }}>
                  {item.requester || "—"}
                </td>
                <td style={{ padding: "6px 8px" }}>{item.held_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

