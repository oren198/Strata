// ─────────────────────────────────────────────────────────────────────
// Strata · small shared atoms. Icon, Field, Button-likes.
// ─────────────────────────────────────────────────────────────────────

function Icon({ name, size = 16, style = {}, className = "" }) {
  return (
    <i
      data-lucide={name}
      className={className}
      style={{ width: size, height: size, display: "inline-flex", flexShrink: 0, ...style }}
    />
  );
}

function Field({ label, hint, children, style }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 6, ...style }}>
      <span style={{ fontSize: 12, fontWeight: 500, color: "var(--at-muted)", letterSpacing: "0.04em", textTransform: "uppercase" }}>
        {label}
      </span>
      {children}
      {hint && <span style={{ fontSize: 12, color: "var(--at-muted)", lineHeight: 1.45 }}>{hint}</span>}
    </label>
  );
}

function IconBtn({ name, label, onClick, danger, style }) {
  const [hover, setHover] = React.useState(false);
  return (
    <button
      onClick={onClick}
      title={label}
      aria-label={label}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        width: 30, height: 30,
        display: "inline-flex", alignItems: "center", justifyContent: "center",
        background: hover ? (danger ? "rgba(200,54,42,0.10)" : "var(--at-highlight)") : "transparent",
        border: "1px solid " + (hover ? (danger ? "rgba(200,54,42,0.25)" : "var(--at-rule)") : "transparent"),
        color: danger ? "var(--at-bear)" : "var(--at-ink-soft)",
        borderRadius: 6, cursor: "pointer",
        transition: "background 120ms, border-color 120ms",
        ...style,
      }}
    >
      <Icon name={name} size={14} />
    </button>
  );
}

function Tag({ children, color, onClick, active }) {
  const bg = color ? `${color}22` : "var(--at-highlight)";
  const fg = color || "var(--at-ink-soft)";
  const border = color ? `${color}55` : "var(--at-rule-soft)";
  return (
    <span
      onClick={onClick}
      style={{
        display: "inline-flex", alignItems: "center", gap: 6,
        padding: "3px 10px", borderRadius: 999,
        background: active ? color : bg,
        color: active ? "#fff" : fg,
        border: `1px solid ${border}`,
        fontSize: 12, fontWeight: 500,
        cursor: onClick ? "pointer" : "default",
        userSelect: "none",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

function Toast({ text, onDone }) {
  React.useEffect(() => {
    const id = setTimeout(onDone, 2400);
    return () => clearTimeout(id);
  }, [text, onDone]);
  return (
    <div style={{
      position: "fixed", left: "50%", bottom: 24, transform: "translateX(-50%)",
      background: "var(--at-primary)", color: "#fff",
      padding: "10px 16px", borderRadius: 10,
      boxShadow: "var(--at-shadow-pop)",
      fontSize: 13, fontWeight: 500,
      display: "flex", alignItems: "center", gap: 10,
      zIndex: 1000,
      animation: "strata-toast-in 200ms ease-out",
    }}>
      <Icon name="check" size={14} />
      {text}
    </div>
  );
}

// Lightweight modal shell.
function Modal({ open, onClose, title, children, footer, width = 520 }) {
  if (!open) return null;
  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0,
        background: "rgba(14, 17, 22, 0.42)",
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: 20, zIndex: 500,
        animation: "strata-fade-in 140ms ease-out",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--at-surface)",
          border: "1px solid var(--at-rule)",
          borderRadius: 14,
          boxShadow: "var(--at-shadow-pop)",
          width: "100%", maxWidth: width,
          maxHeight: "88vh", display: "flex", flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <div style={{
          padding: "16px 18px",
          borderBottom: "1px solid var(--at-rule)",
          display: "flex", alignItems: "center", justifyContent: "space-between",
        }}>
          <div style={{ fontSize: 15, fontWeight: 600, color: "var(--at-ink)" }}>{title}</div>
          <IconBtn name="x" label="Close" onClick={onClose} />
        </div>
        <div style={{ padding: 18, overflowY: "auto", flex: 1 }}>{children}</div>
        {footer && (
          <div style={{
            padding: "12px 18px",
            borderTop: "1px solid var(--at-rule)",
            display: "flex", justifyContent: "flex-end", gap: 10,
            background: "var(--at-bg)",
          }}>
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

function Confirm({ open, title, message, confirmLabel = "Delete", danger = true, onConfirm, onCancel }) {
  return (
    <Modal
      open={open} onClose={onCancel} title={title} width={420}
      footer={<>
        <button className="at-btn at-btn-secondary at-btn-sm" onClick={onCancel}>Cancel</button>
        <button
          className="at-btn at-btn-sm"
          onClick={onConfirm}
          style={{ background: danger ? "var(--at-bear)" : "var(--at-primary)", color: "#fff" }}
        >{confirmLabel}</button>
      </>}
    >
      <div style={{ fontSize: 14, color: "var(--at-ink-soft)", lineHeight: 1.5 }}>{message}</div>
    </Modal>
  );
}

Object.assign(window, { Icon, Field, IconBtn, Tag, Toast, Modal, Confirm, TypeSegmented });

// Segmented control: directive | context. Used in memory editors.
function TypeSegmented({ value, onChange }) {
  const opts = [
    { id: "directive", label: "Directive", icon: "shield" },
    { id: "context",   label: "Context",   icon: "book-open" },
  ];
  return (
    <div style={{
      display: "inline-flex",
      background: "var(--at-highlight)",
      border: "1px solid var(--at-rule-soft)",
      borderRadius: 8,
      padding: 2,
      gap: 2,
      width: "fit-content",
    }}>
      {opts.map((o) => {
        const active = value === o.id;
        return (
          <button
            key={o.id}
            onClick={() => onChange(o.id)}
            style={{
              display: "inline-flex", alignItems: "center", gap: 6,
              padding: "6px 12px",
              borderRadius: 6,
              background: active ? "var(--at-surface)" : "transparent",
              color: active ? "var(--at-ink)" : "var(--at-muted)",
              border: "none",
              fontSize: 12.5,
              fontWeight: active ? 600 : 500,
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
// Re-export now that it's defined.
window.TypeSegmented = TypeSegmented;
