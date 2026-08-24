// ─────────────────────────────────────────────────────────────────────
// Strata · store.
// V1 is read-only. Mutations land via the backend bootstrap and CC contribute tool.
//
// Data model mirrors the backend API shapes:
//   strata  — ordered horizontal lanes. {id, name}
//   scopes  — colored bubbles, each belongs to one stratum.
//             {id, stratum_id, name, color, summary}
//   memories — items inside a scope. {id, scope_id, title, content, type, created}
//             type ∈ "directive" | "context"
//   edges   — between scopes. {from, to}.
//
// API base URL is read from <meta name="strata-api-base" content="..."> in
// index.html. When the tag is absent or its content is empty it defaults to
// window.location.origin — the host and port the Console was served from — so
// `strata start --port 8123` yields a Console that reaches its own backend.
// ─────────────────────────────────────────────────────────────────────

(function () {
  // Resolve the API base URL from a <meta> tag, falling back to the origin the
  // Console is served from. The meta override only matters when the UI is
  // hosted separately from the API.
  function getApiBase() {
    const meta = document.querySelector('meta[name="strata-api-base"]');
    const content = meta && meta.getAttribute("content");
    return (content && content.trim()) || window.location.origin;
  }

  // How often to refresh state from the backend (milliseconds).
  const REFRESH_INTERVAL_MS = 5000;

  // Per-user UI preferences stored locally (theme, graph options).
  // These are NOT modelling state — they stay in localStorage.
  const PREFS_KEY = "strata.ui.prefs";

  function loadPrefs() {
    try {
      const raw = localStorage.getItem(PREFS_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (e) {
      return {};
    }
  }

  function savePrefs(prefs) {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch (e) {
      console.warn("Strata: failed to save UI prefs.", e);
    }
  }

  // Assign a deterministic colour to a scope based on index.
  const PALETTE = ["#c026d3", "#16a34a", "#ca8a04", "#7c3aed", "#2563eb", "#dc2626", "#0891b2", "#65a30d", "#db2777"];
  function pickColor(i) { return PALETTE[i % PALETTE.length]; }

  // Build an empty/loading state while we wait for the backend.
  function makeEmpty() {
    return {
      strata: [],
      scopes: [],
      memories: [],
      edges: [],
    };
  }

  // Fetch the fleet config from GET /scopes and return a normalised state.
  async function fetchFleet() {
    const base = getApiBase();
    const resp = await fetch(`${base}/scopes`);
    if (!resp.ok) throw new Error(`GET /scopes returned ${resp.status}`);
    const data = await resp.json();

    // Backend returns: { strata, scopes, edges }
    // Scopes use stratum_id (snake_case) — keep as-is.
    // We don't fetch memories upfront; the scope-detail panel fetches the
    // summary on demand via GET /scopes/{id}/summary.
    return {
      strata: data.strata || [],
      scopes: (data.scopes || []).map((s, i) => ({
        ...s,
        // Assign a stable colour if the backend doesn't provide one.
        color: s.color || pickColor(i),
      })),
      memories: [], // V1: memory items are not stored in UI state; summaries are fetched per-scope.
      // Backend edges are {from_scope_id, to_scope_id}; the rest of the UI
      // (graph links, scope-detail relations) reads {from, to}. Normalise here
      // so there is a single edge-field contract inside the app — otherwise the
      // lookups silently miss and drill-in shows "No relations" (#65).
      edges: (data.edges || []).map((e) => ({
        from: e.from_scope_id,
        to: e.to_scope_id,
      })),
    };
  }

  // Fetch the summary for a single scope.
  async function fetchScopeSummary(scope_id) {
    const base = getApiBase();
    const resp = await fetch(`${base}/scopes/${encodeURIComponent(scope_id)}/summary`);
    if (!resp.ok) {
      if (resp.status === 404) return null;
      throw new Error(`GET /scopes/${scope_id}/summary returned ${resp.status}`);
    }
    return resp.json(); // { scope_id, directives, context, updated_at }
  }

  // Fetch one page of a scope's declined contributions (UI-only endpoint).
  async function fetchScopeDeclines(scope_id, { limit, before_id } = {}) {
    const base = getApiBase();
    const qs = new URLSearchParams();
    if (limit) qs.set("limit", String(limit));
    if (before_id) qs.set("before_id", before_id);
    const suffix = qs.toString() ? `?${qs}` : "";
    const url = `${base}/scopes/${encodeURIComponent(scope_id)}/declines${suffix}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`GET /scopes/${scope_id}/declines returned ${resp.status}`);
    return resp.json();
  }

  // Fetch the fleet-wide staleness metric (UI-only endpoint).
  async function fetchStaleness({ window_days } = {}) {
    const base = getApiBase();
    const qs = window_days ? `?window_days=${window_days}` : "";
    const resp = await fetch(`${base}/staleness${qs}`);
    if (!resp.ok) throw new Error(`GET /staleness returned ${resp.status}`);
    return resp.json();
  }

  // Fetch one page of a scope's record (newest first). Walk back with before_id.
  async function fetchScopeRecord(scope_id, { limit, before_id } = {}) {
    const base = getApiBase();
    const qs = new URLSearchParams();
    if (limit) qs.set("limit", String(limit));
    if (before_id) qs.set("before_id", before_id);
    const suffix = qs.toString() ? `?${qs}` : "";
    const resp = await fetch(`${base}/scopes/${encodeURIComponent(scope_id)}/record${suffix}`);
    if (!resp.ok) throw new Error(`GET /scopes/${scope_id}/record returned ${resp.status}`);
    return resp.json();
  }

  // Fetch one record entry with its verdict and failed attempts.
  async function fetchRecordEntry(scope_id, contribution_id) {
    const base = getApiBase();
    const resp = await fetch(
      `${base}/scopes/${encodeURIComponent(scope_id)}/record/${encodeURIComponent(contribution_id)}`
    );
    if (!resp.ok) throw new Error(`GET record entry returned ${resp.status}`);
    return resp.json();
  }

  // Compose a scope's perspective as an agent bound to it would receive it.
  async function fetchPerspective(scope_id) {
    const base = getApiBase();
    const resp = await fetch(`${base}/scopes/${encodeURIComponent(scope_id)}/perspective`);
    if (!resp.ok) throw new Error(`GET /scopes/${scope_id}/perspective returned ${resp.status}`);
    return resp.json();
  }

  // Helpers used in graph layout.
  function stratumIndex(state, stratum_id) {
    return state.strata.findIndex((s) => s.id === stratum_id);
  }
  function edgeAllowed(state, fromScopeId, toScopeId) {
    if (fromScopeId === toScopeId) return false;
    const a = state.scopes.find((g) => g.id === fromScopeId);
    const b = state.scopes.find((g) => g.id === toScopeId);
    if (!a || !b) return false;
    const ai = stratumIndex(state, a.stratum_id);
    const bi = stratumIndex(state, b.stratum_id);
    if (ai < 0 || bi < 0) return false;
    return Math.abs(ai - bi) <= 1;
  }

  window.STRATA_STORE = {
    makeEmpty,
    fetchFleet,
    fetchScopeSummary,
    fetchScopeDeclines,
    fetchStaleness,
    fetchScopeRecord,
    fetchRecordEntry,
    fetchPerspective,
    stratumIndex,
    edgeAllowed,
    loadPrefs,
    savePrefs,
    getApiBase,
    REFRESH_INTERVAL_MS,
  };
})();
