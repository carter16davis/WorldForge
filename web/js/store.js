/* One mutable app state with change notification.

   Small enough that a framework would cost more than it saves, but centralised
   so the map, the 3D view and the rail can never disagree about the placement —
   which was the failure mode worth designing against. */

const listeners = new Set();

export const state = {
  placement: null,      // canonical placement (see app/contracts.py)
  coverage: null,
  cells: [],
  provenance: {},
  problems: [],
  modelUrl: null,
  worldforgeUri: "",
  capabilities: [],
  venues: [],
  resolved: null,       // last geocode result
  intake: null,         // last upload report
  era: 0,               // 0 = 2026, 1 = 2426
  coverageShading: false,
  busy: false,
};

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Merge a patch and notify. `reason` lets subscribers skip work they don't need. */
export function update(patch, reason = "update") {
  Object.assign(state, patch);
  for (const fn of listeners) {
    try {
      fn(state, reason);
    } catch (err) {
      console.error("subscriber failed on", reason, err);
    }
  }
}

/** Apply an asset bundle from /api/session, /api/place or /api/upload. */
export function applyBundle(bundle, reason = "asset") {
  if (!bundle) return;
  update({
    placement: bundle.placement,
    coverage: bundle.coverage,
    cells: bundle.cells || [],
    provenance: bundle.provenance || {},
    problems: bundle.problems || [],
    modelUrl: bundle.modelUrl,
    worldforgeUri: bundle.worldforgeUri || "",
  }, reason);
}

export const era = {
  label: (t) => (t < 0.5 ? "2026" : "2426"),
  year: (t) => Math.round(2026 + t * 400),
};
