// ─────────────────────────────────────────────────────────────────────
// Strata · Record trail view. The Console's "the record as a browsable
// trail" proof surface (Task 3 of the local-console plan): one scope's
// append-only record, newest first, every entry in plain words — including
// what was refused. UI-only — this view's fetch never runs on the
// contribute/judge path.
//
// Layout: accordion rows, one open at a time. Retirements are left out —
// this page renders one scope's contribution record only.
// ─────────────────────────────────────────────────────────────────────

const DECISION_META = {
  accept_as_directive: { label: "Accepted as directive", chipClass: "at-pill at-pill-ok", icon: "check-circle" },
  accept_as_context: { label: "Accepted as context", chipClass: "at-pill", icon: "check-circle" },
  decline: { label: "Declined", chipClass: "at-pill at-pill-bear", icon: "x-circle" },
};

const FALLBACK_META = { label: "Awaiting judgment", chipClass: "at-pill", icon: "clock" };

const JUDGE_FAILED_META = { label: "Judgment failed", chipClass: "at-pill at-pill-warn", icon: "alert-triangle" };

// ADR 0014 pin 4: a manager-refresh contribution still awaiting its refresh's
// judgment is NOT a judge outage — it is an input change that has not been
// processed yet. Same "clock" family as FALLBACK_META (nothing has failed),
// distinct label so it never reads as an ordinary stuck judgment.
const REFRESH_PENDING_META = { label: "Refresh pending", chipClass: "at-pill", icon: "clock" };

// Pure — decides the plain-language label, chip class, and icon for one
// contribution's state. The single place these words are decided.
// `attempts` is accepted for signature parity with the judge_failed
// sentence builder below but does not affect the chip itself. `subject` is
// the contribution's own `subject` field (ADR 0014 pin 4): a
// "manager-refresh" contribution still without a verdict is refresh-pending,
// never plain "Awaiting judgment" — an operator scanning this list for
// judge outages must not count it as one.
function stateWords(stateEntry, attempts, subject) {
  if (!stateEntry || stateEntry.state === "pending") {
    return subject === "manager-refresh" ? REFRESH_PENDING_META : FALLBACK_META;
  }
  if (stateEntry.state === "judge_failed") return JUDGE_FAILED_META;
  if (stateEntry.state === "judged") {
    return DECISION_META[stateEntry.decision] ?? FALLBACK_META;
  }
  return FALLBACK_META;
}

// The extra sentence rendered under a judge_failed row.
function judgeFailedSentence(stateEntry, attempts) {
  const list = attempts || [];
  const n = list.length;
  const newest = list.reduce((best, a) => {
    if (!best) return a;
    return (a.attempted_at || "") > (best.attempted_at || "") ? a : best;
  }, null);
  const errorClass = (newest && newest.error_class) || (stateEntry && stateEntry.error_class) || "unknown error";
  return `The judge errored, so no verdict was reached — ${errorClass} after ${n} attempt${n === 1 ? "" : "s"}. The contribution stays in the record until it is judged again.`;
}

function RecordTrailView({ state, scopeId, onSelectScope }) {
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [data, setData] = React.useState(null);
  const [openId, setOpenId] = React.useState(null);
  const [modalId, setModalId] = React.useState(null);

  React.useEffect(() => {
    if (!scopeId) {
      setLoading(false);
      setData(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setOpenId(null);
    STRATA_STORE.fetchScopeRecord(scopeId)
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
    STRATA_STORE.fetchScopeRecord(scopeId, { before_id: data.page.next_before_id })
      .then((next) => {
        setData((prev) => ({
          ...next,
          contributions: [...(prev ? prev.contributions : []), ...next.contributions],
          judgments: [...(prev ? prev.judgments : []), ...next.judgments],
          judgment_attempts: [...(prev ? prev.judgment_attempts : []), ...next.judgment_attempts],
          contribution_states: [...(prev ? prev.contribution_states : []), ...next.contribution_states],
        }));
      })
      .catch((err) => setError(err.message || String(err)));
  }

  // Build lookup maps once per page, not per row.
  const stateById = React.useMemo(() => {
    const map = new Map();
    (data ? data.contribution_states : []).forEach((s) => map.set(s.contribution_id, s));
    return map;
  }, [data]);
  const judgmentById = React.useMemo(() => {
    const map = new Map();
    (data ? data.judgments : []).forEach((j) => map.set(j.contribution_id, j));
    return map;
  }, [data]);
  const attemptsById = React.useMemo(() => {
    const map = new Map();
    (data ? data.judgment_attempts : []).forEach((a) => {
      const list = map.get(a.contribution_id) || [];
      list.push(a);
      map.set(a.contribution_id, list);
    });
    return map;
  }, [data]);

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
        <h1 className="at-h1" style={{ marginBottom: 4 }}>Record</h1>
        <div className="at-body-sm" style={{ marginBottom: 16 }}>
          Everything this scope has ever been asked to remember, newest first — including
          what was refused. Nothing here is ever edited or deleted.
        </div>

        {loading && (
          <div className="at-caption">Loading…</div>
        )}

        {!loading && error && (
          <div>
            <div className="at-caption">Could not load the record.</div>
            <div style={{ fontFamily: "var(--font-mono)", color: "var(--at-bear)", fontSize: 12, marginTop: 4 }}>
              {error}
            </div>
          </div>
        )}

        {!loading && !error && data && (
          <>
            <div className="at-caption" style={{ marginBottom: 8 }}>
              {formatNumber(data.page.total)} entries
            </div>

            {data.contributions.length === 0 ? (
              <div className="at-caption">This scope's record is empty. Nothing has been contributed yet.</div>
            ) : (
              <div>
                {data.contributions.map((c) => (
                  <RecordEntryRow
                    key={c.id}
                    contribution={c}
                    stateEntry={stateById.get(c.id)}
                    judgment={judgmentById.get(c.id)}
                    attempts={attemptsById.get(c.id) || []}
                    open={openId === c.id}
                    onToggle={() => setOpenId((prev) => (prev === c.id ? null : c.id))}
                    onOpen={setModalId}
                  />
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

      {modalId && (
        <RecordEntryModal scopeId={scopeId} contributionId={modalId} onClose={() => setModalId(null)} />
      )}
    </div>
  );
}

function RecordEntryRow({ contribution, stateEntry, judgment, attempts, open, onToggle, onOpen }) {
  const meta = stateWords(stateEntry, attempts, contribution.subject);
  return (
    <div className="activity-row">
      <button
        className="activity-row-toggle"
        aria-expanded={open}
        onClick={onToggle}
      >
        <span style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          <Icon name={meta.icon} size={14} />
          <span className={meta.chipClass}>{meta.label}</span>
          <span style={{
            display: "-webkit-box", WebkitLineClamp: 1, WebkitBoxOrient: "vertical",
            overflow: "hidden", textAlign: "left", color: "var(--at-ink-soft)", fontSize: 12.5,
          }}>
            {contribution.content}
          </span>
        </span>
        <time
          dateTime={contribution.created_at}
          title={absoluteTime(contribution.created_at)}
          style={{ flexShrink: 0, fontSize: 12, color: "var(--at-muted)" }}
        >
          {humanAgo(contribution.created_at)}
        </time>
      </button>

      {open && (
        <div className="activity-row-detail">
          <p
            style={{
              margin: 0, cursor: "pointer",
              display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden",
            }}
            onClick={() => onOpen(contribution.id)}
          >
            {contribution.content}
          </p>

          {stateEntry && stateEntry.state === "judge_failed" && (
            <div className="at-caption" style={{ color: "var(--at-warn)" }}>
              {judgeFailedSentence(stateEntry, attempts)}
            </div>
          )}

          {judgment && judgment.notes && (
            <div className="at-caption" style={{ color: "var(--at-muted)" }}>
              <strong>What the scope-manager said</strong> {judgment.notes}
            </div>
          )}

          {contribution.supersedes && (
            <div className="at-caption">Replaces <code>{contribution.supersedes}</code></div>
          )}

          <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--at-muted)" }}>
            {contribution.id} · {contribution.contributor.session_id} · {absoluteTime(contribution.created_at)}
          </div>
        </div>
      )}
    </div>
  );
}

function RecordEntryModal({ scopeId, contributionId, onClose }) {
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(null);
  const [entry, setEntry] = React.useState(null);

  React.useEffect(() => {
    if (!contributionId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setEntry(null);
    STRATA_STORE.fetchRecordEntry(scopeId, contributionId)
      .then((body) => {
        if (cancelled) return;
        setEntry(body);
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err.message || String(err));
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, [scopeId, contributionId]);

  const meta = entry
    ? stateWords(entry.state, entry.judgment_attempts, entry.contribution.subject)
    : FALLBACK_META;

  return (
    <Modal open={!!contributionId} onClose={onClose} title="Record entry" width={640}>
      {loading && <div className="at-caption">Loading…</div>}

      {!loading && error && (
        <div style={{ fontFamily: "var(--font-mono)", color: "var(--at-bear)", fontSize: 12 }}>
          {error}
        </div>
      )}

      {!loading && !error && entry && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Icon name={meta.icon} size={14} />
            <span className={meta.chipClass}>{meta.label}</span>
          </div>

          <p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{entry.contribution.content}</p>

          {entry.state && entry.state.state === "judge_failed" && (
            <div className="at-caption" style={{ color: "var(--at-warn)" }}>
              {judgeFailedSentence(entry.state, entry.judgment_attempts)}
            </div>
          )}

          {entry.judgment && (
            <div className="at-caption" style={{ color: "var(--at-muted)" }}>
              <strong>What the scope-manager said</strong> {entry.judgment.notes || "No notes recorded."}
            </div>
          )}

          {entry.judgment_attempts && entry.judgment_attempts.length > 0 && (
            <div>
              <div className="at-section-eyebrow">Failed judge attempts</div>
              {entry.judgment_attempts.map((a) => (
                <div key={a.id} className="at-caption" style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>
                  {absoluteTime(a.attempted_at)} — {a.error_class}{a.message ? `: ${a.message}` : ""}
                </div>
              ))}
            </div>
          )}

          {entry.contribution.supersedes && (
            <div className="at-caption">Replaces <code>{entry.contribution.supersedes}</code></div>
          )}

          <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--at-muted)" }}>
            {entry.contribution.id} · {entry.contribution.contributor.session_id} · {absoluteTime(entry.contribution.created_at)}
          </div>
        </div>
      )}
    </Modal>
  );
}

window.stateWords = stateWords;
window.RecordTrailView = RecordTrailView;
window.RecordEntryRow = RecordEntryRow;
window.RecordEntryModal = RecordEntryModal;
