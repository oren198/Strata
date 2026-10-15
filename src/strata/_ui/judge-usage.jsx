// Judge usage — read-only. The numbers are the same ones `strata stats judge` prints.
// Nothing on this tab writes, and nothing here calls the judge.

function JudgeUsageView() {
  const [report, setReport] = React.useState(null);
  const [error, setError] = React.useState(null);

  React.useEffect(() => {
    let cancelled = false;
    STRATA_STORE.fetchJudgeUsage()
      .then((data) => {
        if (!cancelled) setReport(data);
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
  if (report === null) {
    return <div style={{ color: "var(--at-muted)", fontSize: 14 }}>Loading judge usage…</div>;
  }

  const today = report.today || { input_tokens: 0, output_tokens: 0 };
  const total = (today.input_tokens || 0) + (today.output_tokens || 0);
  const cap = report.cap === null || report.cap === undefined ? "off" : String(report.cap);
  const rows = report.rows || [];

  return (
    <div>
      <h1 className="at-h1" style={{ marginBottom: 4 }}>Judge usage</h1>
      <div style={{ color: "var(--at-muted)", fontSize: 13, marginBottom: 8 }}>
        {report.price_note}
      </div>
      <div style={{ fontSize: 14, marginBottom: 16 }}>
        Daily token cap: {cap}. Today: {total} tokens
        {" "}({today.input_tokens || 0} input, {today.output_tokens || 0} output).
        {report.over_cap ? " New judgments are refused until the cap resets." : ""}
      </div>
      {rows.length === 0 && (
        <div style={{ color: "var(--at-muted)", fontSize: 14 }}>No judge calls recorded.</div>
      )}
      {rows.length > 0 && (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--at-muted)" }}>
              <th style={{ padding: "6px 8px" }}>Day</th>
              <th style={{ padding: "6px 8px" }}>Scope</th>
              <th style={{ padding: "6px 8px" }}>Call kind</th>
              <th style={{ padding: "6px 8px" }}>Calls</th>
              <th style={{ padding: "6px 8px" }}>Input</th>
              <th style={{ padding: "6px 8px" }}>Output</th>
              <th style={{ padding: "6px 8px" }}>Cost</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.day + (row.scope_id || "") + row.call_kind} style={{ borderTop: "1px solid var(--at-rule)" }}>
                <td style={{ padding: "6px 8px" }}>{row.day}</td>
                <td style={{ padding: "6px 8px", fontFamily: "var(--font-mono)" }}>
                  {row.scope_id || "(no scope)"}
                </td>
                <td style={{ padding: "6px 8px" }}>{row.call_kind}</td>
                <td style={{ padding: "6px 8px" }}>{row.calls}</td>
                <td style={{ padding: "6px 8px" }}>{row.input_tokens}</td>
                <td style={{ padding: "6px 8px" }}>{row.output_tokens}</td>
                <td style={{ padding: "6px 8px" }}>
                  {row.unpriced_calls || row.cost === null || row.cost === undefined
                    ? "no price set"
                    : row.cost}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
