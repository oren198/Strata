// ─────────────────────────────────────────────────────────────────────
// Strata · View as. The Console's "see exactly what an agent bound to a
// scope reads" proof surface (Task 4 of the local-console plan): compose
// any scope's perspective exactly as compose_perspective + the operator
// and publication readers hand it to an agent, one collapsible card per
// layer, each showing a rough token weight and its share of the whole.
//
// Token weights here are a UI-only estimate (characters / 4) — never a
// tokenizer, never used to judge anything. Standing rule carried over from
// the hosted console's account meter: never render a forecast, a
// burn-down, a run-rate, or "days remaining". Nothing local has a budget,
// so an account-meter's quota concept and role="progressbar" are not
// ported here — this bar only ever answers "what does this scope actually
// read, and how heavy is each part."
// ─────────────────────────────────────────────────────────────────────

const RELATION_META = {
  self: { label: "This scope", color: "var(--at-primary)" },
  ancestor: { label: "Inherited from above", color: "var(--at-ink-soft)" },
  operator: { label: "Set by you, the operator", color: "#7c3aed" },
  peer_reference: { label: "Published by a scope this one references", color: "var(--at-muted)" },
  extra_context: { label: "Extra context", color: "var(--at-rule)" },
};

function relationMeta(relation) {
  return RELATION_META[relation] || { label: relation, color: "var(--at-muted)" };
}

function ViewAsView({ state, scopeId, onSelectScope }) {
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
    STRATA_STORE.fetchPerspective(scopeId)
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
        <h1 className="at-h1" style={{ marginBottom: 4 }}>View as</h1>
        <div className="at-body-sm" style={{ marginBottom: 16 }}>
          Exactly what an agent bound to this scope receives when it reads. Layers arrive
          in this order, top first.
        </div>

        {loading && (
          <div className="at-caption">Loading…</div>
        )}

        {!loading && error && (
          <div>
            <div className="at-caption">Could not load this scope's perspective.</div>
            <div style={{ fontFamily: "var(--font-mono)", color: "var(--at-bear)", fontSize: 12, marginTop: 4 }}>
              {error}
            </div>
          </div>
        )}

        {!loading && !error && data && (
          <>
            <TokenWeightBar layers={data.layers} total={data.token_estimate_total} />

            <div style={{ marginTop: 16 }}>
              {data.layers.map((layer, i) => (
                <LayerCard
                  key={`${layer.scope_id}-${layer.stratum_id}-${i}`}
                  layer={layer}
                  total={data.token_estimate_total}
                  state={state}
                />
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function TokenWeightBar({ layers, total }) {
  if (!total) {
    return (
      <div>
        <div className="pulse-stack" role="img" aria-label="Nothing to read yet.">
          <div className="pulse-stack-part" style={{ width: "100%", background: "var(--at-rule)" }} />
        </div>
        <div className="at-caption" style={{ marginTop: 6 }}>Nothing to read yet.</div>
      </div>
    );
  }

  const ariaLabel = layers
    .map((l) => `${l.scope_id} — ${l.token_estimate} est. tokens`)
    .join(", ");

  return (
    <div>
      <div className="pulse-stack" role="img" aria-label={ariaLabel}>
        {layers.map((l, i) => {
          const meta = relationMeta(l.relation);
          const pct = (l.token_estimate / total) * 100;
          return (
            <div
              key={`${l.scope_id}-${l.stratum_id}-${i}`}
              className="pulse-stack-part"
              title={`${l.scope_id} — ${l.token_estimate} est. tokens`}
              style={{ width: `${pct}%`, background: meta.color }}
            />
          );
        })}
      </div>
      <div className="at-caption" style={{ marginTop: 6 }}>
        {formatNumber(total)} est. tokens in total — a rough estimate, not a tokenizer count.
      </div>
    </div>
  );
}

function LayerCard({ layer, total, state }) {
  const [open, setOpen] = React.useState(layer.relation === "self");
  const scope = (state.scopes || []).find((s) => s.id === layer.scope_id);
  const scopeName = scope ? scope.name : layer.scope_id;
  const meta = relationMeta(layer.relation);
  const pct = total > 0 ? Math.round((layer.token_estimate / total) * 100) : 0;

  return (
    <div className="activity-row">
      <button
        className="activity-row-toggle"
        aria-expanded={open}
        onClick={() => setOpen((prev) => !prev)}
      >
        <span style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <span style={{ fontWeight: 600, color: "var(--at-ink)" }}>{scopeName}</span>
          <Tag color={meta.color}>{meta.label}</Tag>
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 12, flexShrink: 0 }}>
          <span
            className="at-caption"
            style={{ color: layer.binding ? "var(--at-ink)" : "var(--at-muted)" }}
          >
            {layer.binding ? "Binds this agent" : "Context only — does not bind"}
          </span>
          <span className="at-caption">
            {formatNumber(layer.token_estimate)} est. tokens · {pct}% of what the agent reads
          </span>
        </span>
      </button>

      {open && (
        <div className="activity-row-detail">
          <LayerBody layer={layer} />
        </div>
      )}
    </div>
  );
}

function LayerBody({ layer }) {
  if (layer.summary) {
    return <SummaryBody summary={layer.summary} />;
  }
  if (layer.publication) {
    return <PublicationBody publication={layer.publication} />;
  }
  if (layer.operator_memory) {
    return <OperatorMemoryBody operatorMemory={layer.operator_memory} />;
  }
  return null;
}

function SummaryBody({ summary }) {
  if (!summary.exists) {
    return <div className="at-caption" style={{ fontStyle: "italic" }}>This scope has no summary yet.</div>;
  }
  const directives = summary.directives || [];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {directives.length === 0 ? (
        <div className="at-caption" style={{ fontStyle: "italic" }}>No directives.</div>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 6 }}>
          {directives.map((d, i) => (
            <li key={d.id || i} className="at-body-sm">
              {d.subject ? `${d.subject}: ${d.content}` : d.content}
            </li>
          ))}
        </ul>
      )}
      <p className="at-body-sm" style={{ margin: 0, whiteSpace: "pre-wrap" }}>
        {summary.context || ""}
      </p>
    </div>
  );
}

function PublicationBody({ publication }) {
  const items = publication.items || [];
  if (items.length === 0) {
    return <div className="at-caption" style={{ fontStyle: "italic" }}>This scope has published nothing.</div>;
  }
  return (
    <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 6 }}>
      {items.map((it, i) => (
        <li key={it.id || i} className="at-body-sm">
          <span className="at-caption" style={{ textTransform: "uppercase", marginRight: 6 }}>{it.kind}</span>
          {it.content}
        </li>
      ))}
    </ul>
  );
}

function OperatorMemoryBody({ operatorMemory }) {
  const directives = operatorMemory.directives || [];
  const context = operatorMemory.context || [];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {directives.length === 0 ? (
        <div className="at-caption" style={{ fontStyle: "italic" }}>No operator directives.</div>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 6 }}>
          {directives.map((d, i) => (
            <li key={d.id || i} className="at-body-sm">
              {d.subject ? `${d.subject}: ${d.content}` : d.content}
            </li>
          ))}
        </ul>
      )}
      {context.length > 0 && (
        <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 6 }}>
          {context.map((c, i) => (
            <li key={c.id || i} className="at-body-sm">
              {c.content}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

window.ViewAsView = ViewAsView;
window.TokenWeightBar = TokenWeightBar;
window.LayerCard = LayerCard;
