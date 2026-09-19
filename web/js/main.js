/* Wiring. Every user action ends in one `update()` on the store; every view
   re-renders from the store. The rail, the map and the 3D stage therefore
   cannot drift out of sync, which is the bug that would be most obvious and
   least explicable on stage. */

import { api } from "./api.js";
import { MapView } from "./mapview.js";
import { applyBundle, era as eraUtil, state, subscribe, update } from "./store.js";
import { Viewer } from "./viewer3d.js";

const $ = (id) => document.getElementById(id);

const STATUS_COLOR = {
  verified: "#45c08a",
  partial: "#e8a33d",
  uncaptured: "#566074",
  synthetic: "#a26cf0",
};

const ERA_NOTES = [
  [0.02, "2026 — prepared footprint model with estimated height. Real photo reconstruction is not connected yet."],
  [0.35, "Early drift. Same geometry, same placement — only light, material and vegetation change."],
  [0.75, "The Scorched Nebraska treatment takes over. Nothing measured has been altered."],
  [1.01, "2426 — Scorched Nebraska. The export carries both eras under one spatial identity."],
];

let viewer;
let mapView;
let validateTimer;

/* ───────────────────────────── boot ───────────────────────────── */

async function boot() {
  viewer = new Viewer($("three-host"));
  mapView = new MapView($("map-host"), {
    onPick: (lat, lon) => placeAt(lat, lon),
    onTileTrouble: (which) => {
      $("basemap-btn").textContent = "Offline";
      toast(`${which} tiles are not loading, so the map switched to offline mode. ` +
            `Cells, footprint and heading are local data and still work.`, "warn");
    },
    onStatus: (text) => mapBadge(text),
    onFault: (message) => {
      mapBadge("map unavailable");
      toast(`The map could not start: ${message} The 3D view and the export are unaffected.`, "error");
    },
  });

  renderLegend();
  bindControls();
  subscribe(render);

  try {
    const session = await api.session();
    update({ capabilities: session.capabilities, venues: session.venues }, "session");
    applyBundle(session.asset, "session");
    await loadAsset();
    toast("Prepared venue loaded. Enter an address or drop photos to start.", "ok");
  } catch (err) {
    badge3d("could not load");
    toast(err.message, "error");
  }
}

/** Load the current asset into both viewports. */
async function loadAsset() {
  const { placement, coverage, modelUrl, cells } = state;
  if (!placement) return;

  badge3d("loading model…");
  try {
    const stats = await viewer.setAsset({ modelUrl, placement, coverage });
    badge3d(stats
      ? `${stats.widthM.toFixed(0)} × ${stats.depthM.toFixed(0)} × ${stats.heightM.toFixed(0)} m` +
        ` · ${stats.triangles.toLocaleString()} tri`
      : "model loaded");
  } catch (err) {
    badge3d("model failed to load");
    toast(`The model did not load: ${err.message}`, "error");
  }

  mapBadge("waiting for the map…");
  // If the map never comes up, the rest of the app still has to work — the 3D
  // view, the editor and the export do not depend on it.
  const mapUp = await Promise.race([
    mapView.ready.then(() => true),
    new Promise((r) => setTimeout(() => r(false), 8000)),
  ]);
  if (!mapUp) {
    mapBadge("map unavailable");
    return;
  }
  mapBadge("fetching World Cells…");
  const geometry = await api
    .cells(placement.location.latitude, placement.location.longitude,
           placement.spatialIndex?.precision || 8)
    .catch(() => null);
  mapView.setAsset(placement, cells, geometry);
  mapView.flyToAsset();
  syncTransformInputs(placement.transform);
}

/* ───────────────────────── user actions ───────────────────────── */

async function placeAt(lat, lon, address = "") {
  if (!state.placement) return;
  update({ busy: true }, "busy");
  try {
    const result = await api.place({
      assetId: state.placement.assetId,
      address,
      latitude: lat,
      longitude: lon,
      transform: state.placement.transform,
    });
    if (result.error) {
      update({ resolved: result.resolved, busy: false }, "resolved");
      toast(result.error, "warn");
      return;
    }
    update({ resolved: result.resolved }, "resolved");
    applyBundle(result.asset, "placed");
    await loadAsset();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    update({ busy: false }, "busy");
  }
}

async function geocodeAndPlace(address) {
  if (!address.trim()) return;
  $("address-candidates").replaceChildren();
  update({ busy: true }, "busy");
  try {
    const result = await api.place({
      assetId: state.placement.assetId,
      address,
      transform: state.placement.transform,
    });
    update({ resolved: result.resolved }, "resolved");
    for (const candidate of result.resolved?.candidates || []) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "address-candidate";
      button.textContent = candidate.label;
      button.addEventListener("click", () => {
        $("address-candidates").replaceChildren();
        $("address-input").value = candidate.label;
        placeAt(candidate.latitude, candidate.longitude, candidate.label);
      });
      $("address-candidates").append(button);
    }
    if (result.error) {
      toast(result.error, "warn");
      return;
    }
    applyBundle(result.asset, "placed");
    await loadAsset();
    toast(`Placed at ${result.resolved.displayName || address}.`, "ok");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    update({ busy: false }, "busy");
  }
}

async function handleFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  update({ busy: true }, "busy");
  toast(`Analysing ${files.length} file${files.length > 1 ? "s" : ""}…`);
  try {
    const report = await api.upload(files, $("address-input").value);
    update({
      intake: report,
      batchId: report.batchId,
      engines: report.engines || [],
      job: null,
    }, "intake");
    toast(`Analysed ${report.totalCount} file(s). ` +
          (report.canReconstruct
            ? "Enter the address, then reconstruct."
            : "No reconstruction engine on this machine — see the note below."),
          report.canReconstruct ? "ok" : "warn");

    const gps = report.files.find((f) => f.gps);
    if (gps && !$("address-input").value.trim()) {
      $("address-input").value = `${gps.gps.latitude}, ${gps.gps.longitude}`;
      toast("Found GPS in the photo EXIF — pre-filled the address field.", "ok");
    }
  } catch (err) {
    toast(err.message, "error");
  } finally {
    update({ busy: false }, "busy");
  }
}

/* ─────────────────────── reconstruction job ─────────────────────── */

async function startReconstruction() {
  if (!state.batchId) return;
  const address = $("address-input").value.trim();
  if (!address) {
    toast("Enter the building's address first. A mesh with nowhere to go is not " +
          "map-ready, and reconstruction takes minutes — better to fail now.", "warn");
    $("address-input").focus();
    return;
  }
  try {
    const { jobId } = await api.reconstruct({ batchId: state.batchId, address });
    update({ job: { id: jobId, status: "queued", progress: 0, stage: "queued", log: [] } }, "job");
    pollJob(jobId);
  } catch (err) {
    toast(err.message, "error");
  }
}

/* Polls until the job settles. Backs off while the slow stage runs, because a
   reconstruction is minutes long and a one-second poll for ten minutes is just
   noise in the log. */
async function pollJob(jobId) {
  let delay = 1000;
  for (;;) {
    await new Promise((r) => setTimeout(r, delay));
    let job;
    try {
      job = await api.job(jobId);
    } catch (err) {
      update({ job: { ...state.job, status: "failed", error: err.message } }, "job");
      toast(`Lost contact with the job: ${err.message}`, "error");
      return;
    }
    update({ job }, "job");

    if (job.status === "done") {
      if (job.asset) {
        applyBundle(job.asset, "reconstructed");
        await loadAsset();
      }
      toast("Reconstruction complete. This model is measured from your photos — " +
            "check heading and scale before exporting.", "ok");
      return;
    }
    if (job.status === "failed") {
      toast(job.error || "Reconstruction failed.", "error");
      return;
    }
    delay = job.stage === "reconstruct" ? 5000 : 1500;
  }
}

function renderReconstruct(s) {
  const panel = $("reconstruct-panel");
  const button = $("reconstruct-btn");
  const hint = $("reconstruct-hint");
  if (!s.batchId) { panel.hidden = true; return; }
  panel.hidden = false;

  const usable = (s.engines || []).find((e) => e.available);
  const running = s.job && (s.job.status === "queued" || s.job.status === "running");
  button.disabled = !usable || running;
  button.textContent = running ? "Reconstructing…" : "Reconstruct this building";

  hint.textContent = usable
    ? `${usable.name}: ${usable.detail}`
    : "No reconstruction engine here. " +
      (s.engines || []).map((e) => `${e.name} — ${e.detail}`).join("  ");

  const host = $("job");
  if (!s.job) { host.hidden = true; return; }
  host.hidden = false;
  host.dataset.status = s.job.status;
  $("job-stage").textContent = s.job.stage || s.job.status;
  $("job-pct").textContent = `${Math.round((s.job.progress || 0) * 100)}%`;
  $("job-fill").style.width = `${Math.round((s.job.progress || 0) * 100)}%`;
  $("job-detail").textContent = s.job.error || s.job.detail || "";
  $("job-log").textContent = (s.job.log || []).slice(-40).join("\n");
}

function editTransform(patch) {
  if (!state.placement) return;
  const transform = { ...state.placement.transform, ...patch };
  const placement = { ...state.placement, transform };
  update({ placement }, "transform");

  viewer.applyTransform(transform);
  mapView.setTransform(transform);

  clearTimeout(validateTimer);
  validateTimer = setTimeout(async () => {
    try {
      const result = await api.validate(placement);
      update({ problems: result.problems, worldforgeUri: result.worldforgeUri }, "validated");
    } catch { /* validation is advisory; a failed check must not block editing */ }
  }, 320);
}

async function exportPackage() {
  if (!state.placement) return;
  const button = $("export-btn");
  button.disabled = true;
  button.textContent = "Exporting…";
  try {
    const manifest = await api.exportPackage(state.placement, eraUtil.label(state.era));
    renderExport(manifest);
    toast(manifest.valid
      ? "Package exported and validated."
      : "Package exported, but the checks below still fail.", manifest.valid ? "ok" : "warn");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Export package";
  }
}

function setEra(value) {
  const t = value / 100;
  update({ era: t }, "era");
  viewer.setEra(t);
  mapView.setEra(t);
  document.documentElement.dataset.era = eraUtil.label(t);

  const note = ERA_NOTES.find(([limit]) => t < limit)?.[1] ?? "";
  $("era-note").textContent = `${eraUtil.year(t)} — ${note.replace(/^\d{4} — /, "")}`;
  for (const button of document.querySelectorAll(".era-jump")) {
    button.setAttribute("aria-current", String(button.dataset.era === eraUtil.label(t)));
  }
}

/* ───────────────────────────── bindings ───────────────────────────── */

function bindControls() {
  const dropzone = $("dropzone");
  const input = $("file-input");

  dropzone.addEventListener("click", () => input.click());
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
  input.addEventListener("change", () => handleFiles(input.files));

  for (const type of ["dragenter", "dragover"]) {
    dropzone.addEventListener(type, (e) => { e.preventDefault(); dropzone.classList.add("is-over"); });
  }
  for (const type of ["dragleave", "drop"]) {
    dropzone.addEventListener(type, (e) => { e.preventDefault(); dropzone.classList.remove("is-over"); });
  }
  dropzone.addEventListener("drop", (e) => handleFiles(e.dataTransfer.files));
  $("reconstruct-btn").addEventListener("click", startReconstruction);

  $("address-form").addEventListener("submit", (e) => {
    e.preventDefault();
    geocodeAndPlace($("address-input").value);
  });

  $("venue-select").addEventListener("change", (e) => {
    const venue = state.venues.find((v) => v.key === e.target.value);
    if (!venue) return;
    $("address-input").value = venue.address;
    placeAt(venue.lat, venue.lon, venue.address);
  });

  $("heading").addEventListener("input", (e) => editTransform({ headingDegrees: Number(e.target.value) }));
  $("scale").addEventListener("input", (e) => editTransform({ metersPerModelUnit: Number(e.target.value) }));
  $("offset").addEventListener("input", (e) => editTransform({ verticalOffsetMeters: Number(e.target.value) }));

  $("reset-transform").addEventListener("click", () =>
    editTransform({ headingDegrees: 0, metersPerModelUnit: 1, verticalOffsetMeters: 0 }));

  $("coverage-toggle").addEventListener("change", (e) => {
    update({ coverageShading: e.target.checked }, "shading");
    viewer.setCoverageShading(e.target.checked);
  });

  $("era").addEventListener("input", (e) => setEra(Number(e.target.value)));
  for (const button of document.querySelectorAll(".era-jump")) {
    button.addEventListener("click", () => {
      const target = button.dataset.era === "2426" ? 100 : 0;
      animateEra(Number($("era").value), target);
    });
  }

  $("export-btn").addEventListener("click", exportPackage);
  $("frame-btn").addEventListener("click", () => viewer.frame());
  bindToggle("grid-btn", (on) => viewer.setGrid(on));
  bindToggle("human-btn", (on) => viewer.setHuman(on));
  bindToggle("cells-btn", (on) => mapView.setCellsVisible(on));
  $("basemap-btn").addEventListener("click", () => {
    $("basemap-btn").textContent = mapView.toggleBasemap();
  });

  setEra(0);
}

function bindToggle(id, fn) {
  const button = $(id);
  button.addEventListener("click", () => {
    const next = button.getAttribute("aria-pressed") !== "true";
    button.setAttribute("aria-pressed", String(next));
    fn(next);
  });
}

/** Ease the era slider so the jump buttons read as a transition, not a cut. */
function animateEra(from, to) {
  const start = performance.now();
  const duration = 1400;
  const step = (now) => {
    const t = Math.min(1, (now - start) / duration);
    const eased = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
    const value = Math.round(from + (to - from) * eased);
    $("era").value = value;
    setEra(value);
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* ───────────────────────────── rendering ───────────────────────────── */

function render(s, reason) {
  if (reason === "session") {
    renderCapabilities(s.capabilities);
    renderVenues(s.venues);
    $("coverage-source").textContent = s.capabilities.find(c => c.name === "Coverage agent")?.wired
      ? "Coverage supplied by the reconstruction pipeline."
      : "Illustrative demo report: counts and confidence below are simulated, not measured from photos.";
  }
  if (["session", "asset", "placed", "reconstructed"].includes(reason)) {
    renderFacts(s);
    renderCoverage(s.coverage);
  }
  if (reason === "resolved") renderResolved(s.resolved);
  if (reason === "intake") renderIntake(s.intake);
  if (reason === "intake" || reason === "job") renderReconstruct(s);
  if (reason === "transform") renderFacts(s);
  renderProblems(s.problems);
  $("world-uri").textContent = s.worldforgeUri || "worldforge://…";
  document.body.style.cursor = s.busy ? "progress" : "";
}

function renderCapabilities(capabilities) {
  $("capabilities").replaceChildren(...capabilities.map((c) => {
    const el = document.createElement("span");
    el.className = "cap";
    el.dataset.wired = String(c.wired);
    el.textContent = c.name;
    el.title = `${c.provider} — ${c.detail}`;
    return el;
  }));
}

function renderVenues(venues) {
  const select = $("venue-select");
  select.replaceChildren(
    Object.assign(document.createElement("option"), { value: "", textContent: "Choose a venue…" }),
    ...venues.map((v) => Object.assign(document.createElement("option"), {
      value: v.key,
      textContent: `${v.name} — ${v.venue}`,
    })),
  );
}

function renderResolved(resolved) {
  const host = $("resolved");
  if (!resolved) { host.hidden = true; return; }
  host.hidden = false;
  host.dataset.confidence =
    resolved.confidence >= 0.7 ? "high" : resolved.confidence > 0 ? "low" : "none";
  host.replaceChildren(
    el("div", "place", resolved.displayName || "No match"),
    el("div", "coords",
       `${resolved.latitude.toFixed(5)}, ${resolved.longitude.toFixed(5)} · ${resolved.cell || ""}`),
    el("div", "src",
       `${resolved.source} · confidence ${(resolved.confidence * 100).toFixed(0)}%` +
       (resolved.note ? ` — ${resolved.note}` : "")),
  );
}

function renderIntake(report) {
  const host = $("intake");
  if (!report) { host.hidden = true; return; }
  host.hidden = false;

  const summary = el("div", "intake-summary");
  summary.append(frag(`<b>${report.usableCount}</b> of ${report.totalCount} usable`));
  if (report.videoFrameEstimate) {
    summary.append(frag(`<b>~${report.videoFrameEstimate}</b> frames from video`));
  }
  summary.append(
    frag(`<b>${report.geotaggedCount}</b> geotagged`),
    frag(`via ${report.provider}`),
  );

  const files = el("div", "intake-files");
  for (const f of report.files) {
    const row = el("div", "intake-file");
    row.dataset.ok = String(!f.issues.length);
    row.append(el("div", "fname", f.name));
    const bits = [`${(f.sizeBytes / 1048576).toFixed(1)} MB`];
    if (f.width) bits.push(`${f.width}×${f.height}`);
    if (f.kind === "video") {
      row.dataset.kind = "video";
      if (f.durationSeconds) bits.push(`${f.durationSeconds.toFixed(1)}s`);
      if (f.frameRate) bits.push(`${f.frameRate.toFixed(0)} fps`);
      if (f.estimatedFrames) {
        bits.push(`~${f.estimatedFrames} frames @ ${f.plannedIntervalSeconds}s`);
      }
    }
    if (f.sharpness !== undefined) bits.push(`sharp ${f.sharpness.toFixed(0)}`);
    row.append(el("div", "meta", bits.join(" · ")));
    for (const issue of f.issues) row.append(el("div", "issue", issue));
    files.append(row);
  }

  const notes = el("div", "intake-notes");
  for (const note of report.notes || []) notes.append(el("div", "quest", note));

  host.replaceChildren(summary, files, notes);
}

function renderFacts(s) {
  const p = s.placement;
  if (!p) return;
  const d = p.dimensions || {};
  const t = p.transform || {};
  const rows = [
    ["Coordinates", `${p.location.latitude.toFixed(5)}, ${p.location.longitude.toFixed(5)}`],
    ["World Cell", p.spatialIndex?.cell || "—"],
    ["Parent cells", (p.spatialIndex?.parentCells || []).join(" ‹ ") || "—"],
    ["Ground elevation", `${(p.location.elevationMeters ?? 0).toFixed(1)} m`],
    ["Footprint", d.footprintAreaMeters2 ? `${Math.round(d.footprintAreaMeters2).toLocaleString()} m²` : "—"],
    ["Extent", d.widthMeters ? `${d.widthMeters.toFixed(0)} × ${d.lengthMeters.toFixed(0)} m` : "—"],
    ["Height", d.heightMeters ? `${d.heightMeters.toFixed(1)} m` : "—"],
    ["Anchor", `${t.anchor || "—"} · ${t.upAxis || "Y"}-up`],
  ];
  $("facts").replaceChildren(...rows.flatMap(([k, v]) => [el("dt", "", k), el("dd", "", v)]));

  // A scale far from 1 means the model units are not metres — worth saying.
  const scale = Number(t.metersPerModelUnit ?? 1);
  const hint = $("scale-hint");
  if (Math.abs(scale - 1) > 0.02) {
    hint.className = "hint warn";
    hint.textContent = `A ${scale.toFixed(2)} m/unit scale means the source model is not in metres. ` +
                       `Height becomes ${((d.heightMeters || 0) * scale).toFixed(1)} m.`;
  } else {
    hint.className = "hint";
    hint.textContent = "Metres of real world per model unit.";
  }
}

function renderCoverage(coverage) {
  if (!coverage) return;
  const confidence = coverage.overallConfidence ?? 0;
  const colour = confidence >= 0.75 ? STATUS_COLOR.verified
               : confidence >= 0.4 ? STATUS_COLOR.partial
               : STATUS_COLOR.uncaptured;

  const head = el("div", "confidence-head");
  head.append(frag("Reconstruction confidence"), frag(`<b>${(confidence * 100).toFixed(0)}%</b>`));
  const bar = el("div", "confidence-bar");
  const fill = document.createElement("i");
  fill.style.width = `${confidence * 100}%`;
  fill.style.background = colour;
  bar.append(fill);
  $("confidence").replaceChildren(head, bar);

  $("facades").replaceChildren(...(coverage.facades || []).map((f) => {
    const li = el("li", "facade");
    const swatch = document.createElement("i");
    swatch.style.background = STATUS_COLOR[f.status] || STATUS_COLOR.uncaptured;
    const who = el("div", "who", f.name);
    who.append(el("span", "why", f.note || f.status));
    li.append(swatch, who, el("div", "obs", `${f.observationCount}📷`));
    return li;
  }));

  $("recommendations").replaceChildren(...(coverage.recommendations || []).map((r) => {
    const box = el("div", "quest");
    box.append(frag("<b>Capture quest</b>"), frag(r));
    return box;
  }));
}

function renderProblems(problems) {
  const host = $("problems");
  if (!problems?.length) {
    host.replaceChildren(el("div", "problem ok",
      "All placement checks pass — coordinates, cell, scale and anchor agree."));
    return;
  }
  host.replaceChildren(...problems.map((p) => el("div", "problem", p)));
}

function renderExport(manifest) {
  const host = $("export-result");
  host.hidden = false;
  const list = document.createElement("ul");
  for (const f of manifest.files) list.append(el("li", "", f));
  const link = document.createElement("a");
  link.href = manifest.downloadUrl;
  link.textContent = `Download ${manifest.assetId}.zip`;
  host.replaceChildren(el("code", "uri", manifest.worldforgeUri), list, link);
}

function renderLegend() {
  const rows = [
    ["verified", "Verified — observed from multiple angles"],
    ["partial", "Partial — some evidence, weak parallax"],
    ["uncaptured", "Uncaptured — nothing observed"],
    ["synthetic", "Synthetic — generated, not measured"],
  ];
  const legend = $("legend");
  legend.replaceChildren(frag("<b>World Cell coverage · demo data</b>"), ...rows.map(([status, label]) => {
    const row = document.createElement("div");
    const swatch = document.createElement("i");
    swatch.style.background = STATUS_COLOR[status];
    row.append(swatch, frag(label));
    return row;
  }));
}

/* ───────────────────────────── helpers ───────────────────────────── */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** Span from trusted, app-authored markup only — never user or server text. */
function frag(html) {
  const span = document.createElement("span");
  span.innerHTML = html;
  return span;
}

function badge3d(text) { $("three-badge").textContent = text; }
function mapBadge(text) { $("map-badge").textContent = text; }

function syncTransformInputs(transform = {}) {
  $("heading").value = transform.headingDegrees ?? 0;
  $("scale").value = transform.metersPerModelUnit ?? 1;
  $("offset").value = transform.verticalOffsetMeters ?? 0;
  $("heading-out").textContent = `${Math.round(transform.headingDegrees ?? 0)}°`;
  $("scale-out").textContent = `${(transform.metersPerModelUnit ?? 1).toFixed(2)} m / unit`;
  $("offset-out").textContent = `${(transform.verticalOffsetMeters ?? 0).toFixed(1)} m`;
}

// Keep the readouts live while dragging, without a store round trip per pixel.
for (const [id, format] of [
  ["heading", (v) => `${Math.round(v)}°`],
  ["scale", (v) => `${Number(v).toFixed(2)} m / unit`],
  ["offset", (v) => `${Number(v).toFixed(1)} m`],
]) {
  document.addEventListener("input", (e) => {
    if (e.target.id === id) $(`${id}-out`).textContent = format(e.target.value);
  });
}

/* Nothing in a demo is worse than a blank panel and a silent console. Anything
   that escapes a handler becomes a visible, readable toast. */
// "ResizeObserver loop completed…" is a benign browser notice, not a fault.
const BENIGN = /ResizeObserver loop/i;

window.addEventListener("error", (e) => {
  if (BENIGN.test(e.message || "")) return;
  toast(`Unexpected error: ${e.message}`, "error");
});
window.addEventListener("unhandledrejection", (e) =>
  toast(`Unexpected error: ${e.reason?.message || e.reason}`, "error"));

function toast(message, kind = "info") {
  const node = el("div", "toast", message);
  node.dataset.kind = kind;
  $("toasts").append(node);
  setTimeout(() => {
    node.style.opacity = "0";
    node.style.transition = "opacity .3s";
    setTimeout(() => node.remove(), 320);
  }, kind === "error" ? 9000 : 5200);
}

// Handles for debugging from the browser console during a hack session.
Object.defineProperty(window, "wf", { value: { state, get viewer() { return viewer; }, get map() { return mapView; } } });

boot();
