// ─────────────────────────────────────────────────────────────────────
// Strata · operator actions (P5).
//
// Judgment stays automatic; these two modals are the one exception, on
// exception: an in-person correction to a scope's own summary, exercising
// the operator's authority in place of the standing delegate (the
// scope-manager). No standing "operator mode" toggle — each action sits
// behind its own confirm-shaped modal, and is gone the moment it closes.
//
// Both call straight into the same library functions the command line
// calls (`strata operator supersede` / `strata operator retire`), under
// the same cross-process per-scope lock — see strata/app.py.
// ─────────────────────────────────────────────────────────────────────

function SupersedeModal({ open, scopeId, directive, onClose, onDone, onFlash }) {
  const [content, setContent] = React.useState("");
  const [subject, setSubject] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);

  React.useEffect(() => {
    if (open && directive) {
      setContent(directive.content || "");
      setSubject(directive.subject || "");
      setError(null);
    }
  }, [open, directive]);

  if (!open || !directive) return null;

  const blank = !content.trim();

  async function handleSubmit() {
    setBusy(true);
    setError(null);
    try {
      await STRATA_STORE.supersedeDirective(scopeId, directive.id, { content, subject });
      if (onFlash) onFlash("Directive replaced.");
      if (onDone) onDone();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <window.Modal
      open={open}
      onClose={onClose}
      title="Replace this directive"
      footer={<>
        <button className="at-btn at-btn-secondary at-btn-sm" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button
          className="at-btn at-btn-sm"
          onClick={handleSubmit}
          disabled={blank || busy}
          style={{ background: "var(--at-primary)", color: "#fff" }}
        >
          Replace directive
        </button>
      </>}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div>
          <div style={{
            fontSize: 11, fontWeight: 600, color: "var(--at-muted)",
            textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 6,
          }}>
            Current directive
          </div>
          <div style={{
            fontSize: 13, color: "var(--at-ink-soft)", lineHeight: 1.5,
            whiteSpace: "pre-wrap",
            background: "var(--at-bg)", border: "1px solid var(--at-rule)",
            borderRadius: 8, padding: "10px 12px",
          }}>
            {directive.content}
          </div>
        </div>
        <div style={{ fontSize: 12, color: "var(--at-muted)", lineHeight: 1.5 }}>
          This text will be replaced. The original stays in the record forever.
        </div>

        <window.Field label="New content">
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            rows={5}
            className="at-input"
            style={{ resize: "vertical", fontFamily: "inherit" }}
          />
        </window.Field>

        <window.Field label="Subject (optional)">
          <input
            type="text"
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            className="at-input"
          />
        </window.Field>

        <div style={{ fontSize: 12, color: "var(--at-muted)", lineHeight: 1.5 }}>
          Your words are written in exactly as typed. Nothing rewrites them.
        </div>

        {error && (
          <div style={{ fontSize: 13, color: "var(--at-bear)" }}>
            {error}
          </div>
        )}
      </div>
    </window.Modal>
  );
}

function RetireModal({ open, scopeId, directive, onClose, onDone, onFlash }) {
  const [reason, setReason] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);

  React.useEffect(() => {
    if (open) {
      setReason("");
      setError(null);
    }
  }, [open, directive]);

  if (!open || !directive) return null;

  async function handleSubmit() {
    setBusy(true);
    setError(null);
    try {
      await STRATA_STORE.retireDirective(scopeId, directive.id, { reason });
      if (onFlash) onFlash("Directive retired.");
      if (onDone) onDone();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <window.Modal
      open={open}
      onClose={onClose}
      title="Retire this directive"
      footer={<>
        <button className="at-btn at-btn-secondary at-btn-sm" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button
          className="at-btn at-btn-sm"
          onClick={handleSubmit}
          disabled={busy}
          style={{ background: "var(--at-bear)", color: "#fff" }}
        >
          Retire directive
        </button>
      </>}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{
          fontSize: 13, color: "var(--at-ink-soft)", lineHeight: 1.5,
          whiteSpace: "pre-wrap",
          background: "var(--at-bg)", border: "1px solid var(--at-rule)",
          borderRadius: 8, padding: "10px 12px",
        }}>
          {directive.content}
        </div>

        <div style={{ fontSize: 12, color: "var(--at-muted)", lineHeight: 1.5 }}>
          This directive stops binding agents. It is not deleted — the record keeps it,
          and a retirement is recorded alongside.
        </div>

        <window.Field label="Reason (optional)">
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Why? (optional)"
            className="at-input"
          />
        </window.Field>

        {error && (
          <div style={{ fontSize: 13, color: "var(--at-bear)" }}>
            {error}
          </div>
        )}
      </div>
    </window.Modal>
  );
}

window.SupersedeModal = SupersedeModal;
window.RetireModal = RetireModal;
