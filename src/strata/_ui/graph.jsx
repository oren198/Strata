// ─────────────────────────────────────────────────────────────────────
// Strata · Memory Graph (swimlane).
// V1 is read-only. Mutations land via the backend bootstrap and CC contribute tool.
// ─────────────────────────────────────────────────────────────────────

function MemoryGraph({ state, tweaks, onOpenScope, onExpandSummary }) {
  const wrapRef = React.useRef(null);
  const svgRef = React.useRef(null);
  const [size, setSize] = React.useState({ w: 900, h: 560 });
  const [hoverScopeId, setHoverScopeId] = React.useState(null);
  const [selectedScopeId, setSelectedScopeId] = React.useState(null);
  const hoverTooltipRef = React.useRef(null);

  // ─── Resize ────────────────────────────────────────────────────
  React.useEffect(() => {
    if (!wrapRef.current) return;
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) {
        const r = e.contentRect;
        setSize({ w: Math.max(420, r.width), h: Math.max(360, r.height) });
      }
    });
    ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, []);

  // ─── Layout constants ──────────────────────────────────────────
  const padX = 18;
  const padY = 18;
  const labelGutter = 100;
  const innerW = size.w - labelGutter - padX;
  const innerH = size.h - padY * 2;
  const bandH = Math.max(140, innerH / Math.max(state.strata.length, 1));

  function scopeRadius(scope_id) {
    // In V1 we don't have per-scope memory counts in state; use a fixed base.
    return 40;
  }

  const stratumIndex = React.useMemo(() => {
    const m = {};
    state.strata.forEach((s, i) => { m[s.id] = i; });
    return m;
  }, [state.strata]);

  const scopeById = React.useMemo(() => {
    const m = {};
    state.scopes.forEach((g) => { m[g.id] = g; });
    return m;
  }, [state.scopes]);

  // Structural signature — only changes when scopes are added/removed
  // or move between strata.
  const structuralSig = React.useMemo(
    () => state.scopes.map((g) => g.id + ":" + g.stratum_id).sort().join("|"),
    [state.scopes]
  );
  const strataSig = React.useMemo(
    () => state.strata.map((s) => s.id).join("|"),
    [state.strata]
  );

  function laneCenterY(stratum_id) {
    const i = stratumIndex[stratum_id] ?? 0;
    return padY + i * bandH + bandH / 2;
  }

  // ─── Force sim ─────────────────────────────────────────────────
  const nodesRef = React.useRef(new Map());
  const ticksRef = React.useRef(0);
  const settledRef = React.useRef(false);
  const [, setTick] = React.useState(0);
  const draggingRef = React.useRef(null);
  const hoverCloseTimer = React.useRef(null);

  function holdHover(id) {
    if (hoverCloseTimer.current) {
      clearTimeout(hoverCloseTimer.current);
      hoverCloseTimer.current = null;
    }
    setHoverScopeId(id);
  }
  function scheduleHoverClose() {
    if (hoverCloseTimer.current) clearTimeout(hoverCloseTimer.current);
    hoverCloseTimer.current = setTimeout(() => {
      setHoverScopeId(null);
      hoverCloseTimer.current = null;
    }, 180);
  }

  function kickSim(n = 100) {
    ticksRef.current = Math.max(ticksRef.current, n);
    settledRef.current = false;
  }

  // Rebuild nodes only on structural change.
  React.useEffect(() => {
    const prev = nodesRef.current;
    const next = new Map();
    for (const g of state.scopes) {
      const prior = prev.get(g.id);
      const yTarget = laneCenterY(g.stratum_id);
      if (prior) {
        next.set(g.id, { ...prior, stratum_id: g.stratum_id });
      } else {
        next.set(g.id, {
          id: g.id,
          stratum_id: g.stratum_id,
          x: labelGutter + padX + innerW * (0.2 + 0.6 * Math.random()),
          y: yTarget + (Math.random() - 0.5) * 30,
          vx: 0, vy: 0,
          pinned: false,
        });
      }
    }
    nodesRef.current = next;
    kickSim();
    setTick((t) => t + 1);
  }, [structuralSig, strataSig, size.w, size.h]);

  // Kick on layout-affecting changes.
  React.useEffect(() => { kickSim(); }, [state.edges.length, state.options.graph_charge, state.options.graph_link_distance]);

  // ─── Force loop ───────────────────────────────────────────────
  React.useEffect(() => {
    let raf;
    const charge = state.options.graph_charge || 800;
    const linkDist = state.options.graph_link_distance || 120;
    const damping = 0.74;
    const yPull = 0.22;
    const innerLeft = labelGutter + padX;
    const innerRight = size.w - padX;

    function step() {
      if (ticksRef.current <= 0 && !draggingRef.current) {
        raf = requestAnimationFrame(step);
        return;
      }

      const list = Array.from(nodesRef.current.values());
      if (list.length === 0) { raf = requestAnimationFrame(step); return; }

      for (const n of list) { n.fx = 0; n.fy = 0; }

      for (let i = 0; i < list.length; i++) {
        for (let j = i + 1; j < list.length; j++) {
          const a = list[i], b = list[j];
          let dx = a.x - b.x, dy = a.y - b.y;
          let d2 = dx * dx + dy * dy;
          if (d2 < 1) { d2 = 1; dx = Math.random() - 0.5; dy = Math.random() - 0.5; }
          const d = Math.sqrt(d2);
          const sameLane = a.stratum_id === b.stratum_id;
          const ra = scopeRadius(a.id), rb = scopeRadius(b.id);
          const minD = ra + rb + 32;
          let k = sameLane ? charge : charge * 0.6;
          if (d < minD) k *= 4;
          const f = k / d2;
          const fx = (dx / d) * f, fy = (dy / d) * f;
          a.fx += fx; a.fy += fy;
          b.fx -= fx; b.fy -= fy;
        }
      }

      for (const e of state.edges) {
        const a = nodesRef.current.get(e.from);
        const b = nodesRef.current.get(e.to);
        if (!a || !b) continue;
        const dx = b.x - a.x, dy = b.y - a.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const ra = scopeRadius(a.id), rb = scopeRadius(b.id);
        const restLen = Math.max(linkDist, ra + rb + 60);
        const stretch = d - restLen;
        const f = stretch * 0.06;
        const fx = (dx / d) * f, fy = (dy / d) * f;
        a.fx += fx; a.fy += fy;
        b.fx -= fx; b.fy -= fy;
      }

      for (const n of list) {
        const yT = laneCenterY(n.stratum_id);
        n.fy += (yT - n.y) * yPull;
      }

      let maxV = 0;
      for (const n of list) {
        if (n.pinned) { n.vx = 0; n.vy = 0; continue; }
        n.vx = (n.vx + n.fx) * damping;
        n.vy = (n.vy + n.fy) * damping;
        if (Math.abs(n.vx) < 0.04) n.vx = 0;
        if (Math.abs(n.vy) < 0.04) n.vy = 0;
        const sp = Math.sqrt(n.vx * n.vx + n.vy * n.vy);
        if (sp > 22) { n.vx = (n.vx / sp) * 22; n.vy = (n.vy / sp) * 22; }
        n.x += n.vx; n.y += n.vy;

        const r = scopeRadius(n.id);
        if (n.x < innerLeft + r) { n.x = innerLeft + r; n.vx = Math.abs(n.vx) * 0.4; }
        if (n.x > innerRight - r) { n.x = innerRight - r; n.vx = -Math.abs(n.vx) * 0.4; }
        if (n.y < padY + r) { n.y = padY + r; n.vy = Math.abs(n.vy) * 0.3; }
        if (n.y > size.h - padY - r) { n.y = size.h - padY - r; n.vy = -Math.abs(n.vy) * 0.3; }

        if (sp > maxV) maxV = sp;
      }

      ticksRef.current -= 1;
      if (maxV < 0.08) {
        ticksRef.current = 0;
        settledRef.current = true;
        for (const n of list) { n.vx = 0; n.vy = 0; }
      }

      // Hard collision-resolution pass.
      for (let pass = 0; pass < 3; pass++) {
        let collided = false;
        for (let i = 0; i < list.length; i++) {
          for (let j = i + 1; j < list.length; j++) {
            const a = list[i], b = list[j];
            const ra = scopeRadius(a.id), rb = scopeRadius(b.id);
            const minGap = ra + rb + 20;
            let dx = b.x - a.x, dy = b.y - a.y;
            let d = Math.sqrt(dx * dx + dy * dy);
            if (d < minGap) {
              collided = true;
              if (d < 0.001) { dx = 1; dy = 0; d = 1; }
              const overlap = (minGap - d) / 2;
              const ux = dx / d, uy = dy / d;
              if (!a.pinned) { a.x -= ux * overlap; a.y -= uy * overlap; }
              if (!b.pinned) { b.x += ux * overlap; b.y += uy * overlap; }
            }
          }
        }
        if (!collided) break;
        for (const n of list) {
          const r = scopeRadius(n.id);
          if (n.x < innerLeft + r) n.x = innerLeft + r;
          if (n.x > innerRight - r) n.x = innerRight - r;
          if (n.y < padY + r) n.y = padY + r;
          if (n.y > size.h - padY - r) n.y = size.h - padY - r;
        }
      }

      setTick((t) => (t + 1) % 1e9);
      raf = requestAnimationFrame(step);
    }
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [state.edges, state.options.graph_charge, state.options.graph_link_distance, size.w, size.h]);

  // ─── Drag (view-only — no stratum reassignment in V1) ──────────
  function svgPoint(evt) {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0 };
    const r = svg.getBoundingClientRect();
    return { x: evt.clientX - r.left, y: evt.clientY - r.top };
  }
  function onNodeMouseDown(e, id) {
    e.stopPropagation();
    if (e.detail >= 2) return;
    const n = nodesRef.current.get(id);
    if (!n) return;
    draggingRef.current = { id, moved: false };
    n.pinned = true;
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
  }
  function onMouseMove(e) {
    const drag = draggingRef.current;
    if (!drag) return;
    const n = nodesRef.current.get(drag.id);
    if (!n) return;
    const p = svgPoint(e);
    if (!drag.moved && Math.hypot(p.x - n.x, p.y - n.y) > 3) drag.moved = true;
    n.x = p.x; n.y = p.y; n.vx = 0; n.vy = 0;
    setTick((t) => (t + 1) % 1e9);
  }
  function onMouseUp() {
    const drag = draggingRef.current;
    window.removeEventListener("mousemove", onMouseMove);
    window.removeEventListener("mouseup", onMouseUp);
    if (!drag) return;
    const n = nodesRef.current.get(drag.id);
    if (n) n.pinned = false;

    if (!drag.moved) {
      setSelectedScopeId(drag.id);
    } else if (n) {
      kickSim();
    }
    draggingRef.current = null;
  }
  function onNodeDoubleClick(id) { onOpenScope(id); }

  // ─── Render ────────────────────────────────────────────────────
  return (
    <div
      ref={wrapRef}
      style={{
        position: "relative", flex: 1,
        background: "var(--at-surface)",
        border: "1px solid var(--at-rule)",
        borderRadius: 14,
        overflow: "hidden",
        minHeight: 500,
      }}
    >
      <svg
        ref={svgRef}
        width={size.w} height={size.h}
        style={{ display: "block", cursor: "default" }}
        onMouseDown={() => { setSelectedScopeId(null); }}
      >
        <defs>
          <marker
            id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
            markerWidth="6" markerHeight="6" orient="auto-start-reverse"
          >
            <path d="M0,0 L10,5 L0,10 z" fill="var(--at-ink-soft)" opacity={0.7} />
          </marker>
        </defs>

        {/* Lanes */}
        {state.strata.map((s, i) => {
          const y = padY + i * bandH;
          return (
            <g key={s.id}>
              {i % 2 === 0 && (
                <rect
                  x={labelGutter} y={y}
                  width={size.w - labelGutter - padX} height={bandH}
                  fill="var(--at-bg)" opacity={0.5}
                />
              )}
              {i > 0 && (
                <line
                  x1={labelGutter} y1={y}
                  x2={size.w - padX} y2={y}
                  stroke="var(--at-rule)" strokeDasharray="4 4"
                />
              )}
              <foreignObject x={padX} y={y} width={labelGutter - padX - 6} height={bandH}>
                <div xmlns="http://www.w3.org/1999/xhtml" style={{
                  height: "100%", display: "flex", flexDirection: "column",
                  justifyContent: "center", paddingRight: 8,
                }}>
                  <div style={{
                    fontSize: 11, color: "var(--at-muted)",
                    fontFamily: "var(--font-mono)", letterSpacing: "0.08em",
                    textTransform: "uppercase", marginBottom: 2,
                  }}>L{i + 1}</div>
                  <div style={{
                    fontSize: 14, fontWeight: 600, color: "var(--at-ink)",
                    lineHeight: 1.2, overflow: "hidden",
                    textOverflow: "ellipsis", whiteSpace: "nowrap",
                  }}>{s.name}</div>
                </div>
              </foreignObject>
            </g>
          );
        })}

        {/* Edges */}
        <g>
          {state.edges.map((e, i) => {
            const a = nodesRef.current.get(e.from);
            const b = nodesRef.current.get(e.to);
            if (!a || !b) return null;
            const ga = scopeById[e.from], gb = scopeById[e.to];
            if (!ga || !gb) return null;
            const ra = scopeRadius(e.from), rb = scopeRadius(e.to);
            const dx = b.x - a.x, dy = b.y - a.y;
            const d = Math.sqrt(dx * dx + dy * dy) || 1;
            const ux = dx / d, uy = dy / d;
            const x1 = a.x + ux * ra, y1 = a.y + uy * ra;
            const x2 = b.x - ux * (rb + 4), y2 = b.y - uy * (rb + 4);
            const ia = stratumIndex[ga.stratum_id] ?? 0;
            const ib = stratumIndex[gb.stratum_id] ?? 0;
            const sameLane = ia === ib;
            return (
              <line
                key={i}
                x1={x1} y1={y1} x2={x2} y2={y2}
                stroke="var(--at-ink-soft)"
                strokeOpacity={sameLane ? 0.55 : 0.45}
                strokeWidth={1.6}
                strokeDasharray={sameLane ? "0" : "5 4"}
                markerEnd="url(#arrow)"
              />
            );
          })}
        </g>

        {/* Scope bubbles */}
        <g>
          {state.scopes.map((g) => {
            const n = nodesRef.current.get(g.id);
            if (!n) return null;
            const r = scopeRadius(g.id);
            const isHover = hoverScopeId === g.id;
            const isSelected = selectedScopeId === g.id;

            const fill = g.color + "26";
            const stroke = g.color;

            return (
              <g
                key={g.id}
                transform={`translate(${n.x}, ${n.y})`}
                style={{ cursor: "pointer", transition: "opacity 200ms" }}
                onMouseDown={(ev) => onNodeMouseDown(ev, g.id)}
                onDoubleClick={() => onNodeDoubleClick(g.id)}
                onMouseEnter={() => holdHover(g.id)}
                onMouseLeave={() => scheduleHoverClose()}
              >
                <circle
                  r={r}
                  fill={fill}
                  stroke={stroke}
                  strokeWidth={isSelected || isHover ? 3 : 2}
                  style={{ transition: "stroke-width 120ms" }}
                />
                {/* Scope name */}
                <text
                  textAnchor="middle"
                  dominantBaseline="middle"
                  style={{
                    fontFamily: "var(--font-display)",
                    fontSize: r > 50 ? 14 : 12.5, fontWeight: 600,
                    fill: "var(--at-ink)",
                    pointerEvents: "none", userSelect: "none",
                  }}
                >
                  {g.name.length > Math.floor(r / 4) + 4
                    ? g.name.slice(0, Math.floor(r / 4) + 3) + "…"
                    : g.name}
                </text>
              </g>
            );
          })}
        </g>
      </svg>

      {/* Toolbar (read-only: only Open button) */}
      {selectedScopeId && (
        <div style={{
          position: "absolute", top: 14, right: 14, zIndex: 4,
          display: "flex", gap: 8, alignItems: "center",
        }}>
          <button
            className="at-btn at-btn-sm at-btn-primary"
            onClick={() => onOpenScope(selectedScopeId)}
          >
            <Icon name="maximize-2" size={12} /> <span>Open</span>
          </button>
        </div>
      )}

      {/* Hover preview */}
      {hoverScopeId && !draggingRef.current && (() => {
        const n = nodesRef.current.get(hoverScopeId);
        const g = scopeById[hoverScopeId];
        if (!n || !g) return null;
        const r = scopeRadius(g.id);
        const wantW = 280;
        let left = n.x + r + 12;
        if (left + wantW > size.w - 8) left = n.x - r - wantW - 12;
        left = Math.max(8, left);
        let top = n.y - 40;
        top = Math.max(8, Math.min(top, size.h - 180));
        return (
          <div
            ref={hoverTooltipRef}
            onMouseEnter={() => holdHover(g.id)}
            onMouseLeave={() => scheduleHoverClose()}
            style={{
              position: "absolute", left, top, width: wantW,
              background: "var(--at-surface)",
              border: "1px solid var(--at-rule)",
              borderRadius: 12,
              boxShadow: "var(--at-shadow-pop)",
              padding: 14, zIndex: 3,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <span style={{ width: 12, height: 12, borderRadius: 6, background: g.color }} />
              <span style={{ fontSize: 14, fontWeight: 600, color: "var(--at-ink)", flex: 1 }}>{g.name}</span>
              <span style={{ fontSize: 10, color: "var(--at-muted)", fontFamily: "var(--font-mono)", letterSpacing: "0.06em", textTransform: "uppercase" }}>
                {state.strata.find((s) => s.id === g.stratum_id)?.name}
              </span>
            </div>
            <div style={{ fontSize: 11, color: "var(--at-muted)", fontFamily: "var(--font-mono)", marginBottom: 8 }}>
              {g.id}
            </div>
            <div style={{ marginTop: 8, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
              <button
                className="at-btn at-btn-ghost at-btn-sm"
                onClick={() => onExpandSummary && onExpandSummary(g.id)}
              >
                <Icon name="maximize-2" size={12} /> Expand
              </button>
              <button
                className="at-btn at-btn-secondary at-btn-sm"
                onClick={() => onOpenScope(g.id)}
              >
                Open details <Icon name="arrow-right" size={12} />
              </button>
            </div>
          </div>
        );
      })()}

      {/* Empty state */}
      {state.scopes.length === 0 && (
        <div style={{
          position: "absolute", inset: 0,
          display: "flex", alignItems: "center", justifyContent: "center",
          flexDirection: "column", gap: 12,
          color: "var(--at-muted)",
          pointerEvents: "none", zIndex: 1,
        }}>
          <Icon name="circle-dashed" size={28} />
          <div style={{ fontSize: 14 }}>No scopes yet. Run the backend bootstrap to populate the fleet.</div>
        </div>
      )}
    </div>
  );
}

Object.assign(window, { MemoryGraph });
