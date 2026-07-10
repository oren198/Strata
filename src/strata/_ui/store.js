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
      edges: data.edges || [],
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
    stratumIndex,
    edgeAllowed,
    loadPrefs,
    savePrefs,
    getApiBase,
    REFRESH_INTERVAL_MS,
  };
})();
