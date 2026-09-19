/* The map half of the placement editor.
 *
 * Shows the geohash World Cell the asset lands in, its eight neighbours
 * coloured by coverage state, its parent cells as nested outlines, and the real
 * footprint. Clicking or dragging the pin moves the asset, which is the fast
 * way to fix a geocode that landed on the car park instead of the stadium.
 *
 * Tiles are the only part of the app that needs the network. When they fail the
 * map keeps working — every overlay is drawn from our own data — and the badge
 * says so rather than showing an empty grey square with no explanation.
 */

import { metersPerDegree } from "./geo.js";

const STATUS_COLOR = {
  verified: "#45c08a",
  partial: "#e8a33d",
  uncaptured: "#566074",
  synthetic: "#a26cf0",
};

const BASEMAPS = {
  street: {
    label: "Street",
    tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
    maxzoom: 19,
    attribution: "© OpenStreetMap contributors",
  },
  satellite: {
    label: "Satellite",
    tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
    maxzoom: 19,
    attribution: "Imagery © Esri, Maxar, Earthstar Geographics",
  },
  // No raster at all. Reached automatically when tiles keep failing: MapLibre
  // retries dead tiles indefinitely, which burns the network and never
  // recovers. Every overlay is drawn from our own data, so the map stays
  // genuinely useful without a basemap — which is the point of the offline
  // requirement in AGENTS.md.
  offline: {
    label: "Offline",
    tiles: null,
    attribution: "Basemap unavailable — cells and footprint are local data",
  },
};

const BASEMAP_ORDER = ["satellite", "street", "offline"];

const EMPTY_SOURCE = { type: "FeatureCollection", features: [] };

export class MapView {
  constructor(host, { onPick, onTileTrouble, onStatus, onFault } = {}) {
    this.host = host;
    this.onPick = onPick;
    this.onTileTrouble = onTileTrouble;
    this.onStatus = onStatus;
    this.onFault = onFault;
    this.basemap = "satellite";
    this.cellsVisible = true;
    this.tileFailures = 0;
    this.placement = null;

    this.map = new maplibregl.Map({
      container: host,
      style: this._style(this.basemap),
      center: [-74.0745, 40.8135],
      zoom: 15.4,
      pitch: 42,
      bearing: -18,
      attributionControl: { compact: true },
    });

    this.map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    this.map.dragRotate.enable();

    this.map.on("error", (e) => {
      const message = String(e?.error?.message || e?.error || "");
      // Tile 404s and offline failures both land here. One is worth reporting
      // once; a stream of them is the same fact repeated.
      if (e?.error?.status === 404 || /Failed to fetch|NetworkError|AbortError/i.test(message)) {
        if (++this.tileFailures === 6 && this.basemap !== "offline") {
          const failed = BASEMAPS[this.basemap].label;
          this._setBasemap("offline");
          this.onTileTrouble?.(failed);
        }
        return;
      }
      // Anything else is a real fault — a missing WebGL context, a bad style.
      // Swallowing it leaves the map stuck on "loading" with no explanation.
      if (!this._reportedFault) {
        this._reportedFault = true;
        this.onFault?.(message || "The map failed to initialise.");
      }
    });

    this.map.on("click", (e) => this.onPick?.(e.lngLat.lat, e.lngLat.lng));

    // A style swap drops every source, so anything queued has to go back in.
    this.map.on("styledata", () => { if (this._needsPush) this._pushData(); });

    // `load` waits for a first successful render, which never happens when the
    // tile host is unreachable — and an unreachable tile host is precisely the
    // case the offline overlays exist for. `styledata` always fires, and our
    // own sources are all we need to add.
    this.ready = new Promise((resolve) => {
      const init = () => {
        if (this._layersAdded) return;
        this._layersAdded = true;
        this._addLayers();
        resolve(this);
      };
      this.map.on("styledata", init);
      this.map.on("load", init);
      if (this.map.isStyleLoaded()) init();
    });

    this._buildMarker();

    // MapLibre only listens for window resizes, but this container is a grid
    // cell that settles after first paint — without this the canvas keeps
    // whatever height it was measured at (98px, in practice).
    this._resizeObserver = new ResizeObserver(() => this.map.resize());
    this._resizeObserver.observe(host);
  }

  _style(which) {
    const base = BASEMAPS[which];
    const style = {
      version: 8,
      sources: {},
      layers: [{ id: "bg", type: "background", paint: { "background-color": "#0a0d12" } }],
    };
    if (base.tiles) {
      style.sources.basemap = {
        type: "raster",
        tiles: base.tiles,
        tileSize: 256,
        maxzoom: base.maxzoom,
        attribution: base.attribution,
      };
      style.layers.push({
        id: "basemap", type: "raster", source: "basemap",
        paint: { "raster-opacity": 0.92 },
      });
    }
    return style;
  }

  _addLayers() {
    const m = this.map;
    for (const id of ["parents", "cells", "footprint", "heading"]) {
      m.addSource(id, { type: "geojson", data: EMPTY_SOURCE });
    }

    m.addLayer({
      id: "parents-line", type: "line", source: "parents",
      paint: {
        "line-color": "#6fd3e8",
        "line-width": ["interpolate", ["linear"], ["get", "depth"], 0, 1.6, 4, 0.5],
        "line-opacity": 0.32,
        "line-dasharray": [3, 3],
      },
    });

    m.addLayer({
      id: "cells-fill", type: "fill", source: "cells",
      paint: {
        "fill-color": ["get", "color"],
        "fill-opacity": ["case", ["get", "isCentre"], 0.34, 0.16],
      },
    });

    m.addLayer({
      id: "cells-line", type: "line", source: "cells",
      paint: {
        "line-color": ["get", "color"],
        "line-width": ["case", ["get", "isCentre"], 2.2, 0.9],
        "line-opacity": 0.85,
      },
    });

    m.addLayer({
      id: "footprint-fill", type: "fill", source: "footprint",
      paint: { "fill-color": "#e8c46a", "fill-opacity": 0.24 },
    });

    m.addLayer({
      id: "footprint-line", type: "line", source: "footprint",
      paint: { "line-color": "#e8c46a", "line-width": 2 },
    });

    m.addLayer({
      id: "heading-line", type: "line", source: "heading",
      paint: { "line-color": "#ffffff", "line-width": 2, "line-opacity": 0.9 },
    });

    this._pushData();
  }

  _buildMarker() {
    const el = document.createElement("div");
    el.className = "pin";
    el.title = "Drag to correct the placement";
    this.marker = new maplibregl.Marker({ element: el, draggable: true })
      .setLngLat([-74.0745, 40.8135])
      .addTo(this.map);
    this.marker.on("dragend", () => {
      const { lat, lng } = this.marker.getLngLat();
      this.onPick?.(lat, lng);
    });
  }

  /* ───────────────────────────── data ───────────────────────────── */

  setAsset(placement, cells = [], cellGeometry = null) {
    this.placement = placement;
    this.cells = cells;
    this.cellGeometry = cellGeometry;
    this.marker.setLngLat([placement.location.longitude, placement.location.latitude]);
    this._pushData();
  }

  setTransform(transform) {
    if (this.placement) this.placement = { ...this.placement, transform };
    this._pushHeading();
  }

  _pushData() {
    // Deliberately NOT gated on isStyleLoaded(): that stays false while raster
    // tiles are in flight, and on a slow or blocked network it never flips, so
    // gating on it silently threw away every overlay. Sources accept setData as
    // soon as they exist, which is all we actually need.
    if (!this.map.getSource("cells")) {
      this._needsPush = true;
      return;
    }
    const p = this.placement;
    if (!p) return;
    this._needsPush = false;

    const statusFor = new Map((this.cells || []).map((c) => [c.cell, c]));
    const centre = p.spatialIndex?.cell;

    const cellFeatures = [];
    if (this.cellGeometry) {
      const all = [
        { cell: this.cellGeometry.cell, polygon: this.cellGeometry.polygon, isCentre: true },
        ...this.cellGeometry.neighbours.map((n) => ({ ...n, isCentre: false })),
      ];
      for (const entry of all) {
        const info = statusFor.get(entry.cell);
        const status = entry.isCentre ? (info?.status || "partial") : (info?.status || "uncaptured");
        cellFeatures.push({
          type: "Feature",
          properties: {
            cell: entry.cell,
            status,
            color: STATUS_COLOR[status] || STATUS_COLOR.uncaptured,
            isCentre: entry.isCentre || entry.cell === centre,
            assetCount: info?.assetCount ?? 0,
          },
          geometry: { type: "Polygon", coordinates: [entry.polygon] },
        });
      }
    }
    this.map.getSource("cells").setData({ type: "FeatureCollection", features: cellFeatures });

    const parentFeatures = (this.cellGeometry?.parents || []).map((parent, i) => ({
      type: "Feature",
      properties: { cell: parent.cell, depth: i },
      geometry: { type: "Polygon", coordinates: [parent.polygon] },
    }));
    this.map.getSource("parents").setData({ type: "FeatureCollection", features: parentFeatures });

    const rings = [];
    if (p.footprint?.length >= 3) {
      rings.push(closeRing(p.footprint.map(([lat, lon]) => [lon, lat])));
      for (const hole of p.holes || []) {
        if (hole.length >= 3) rings.push(closeRing(hole.map(([lat, lon]) => [lon, lat])));
      }
    }
    this.map.getSource("footprint").setData(
      rings.length
        ? { type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: rings } }
        : EMPTY_SOURCE,
    );

    this._pushHeading();
    this._reportStatus();
  }

  /** A white needle from the placement point along the model's heading. */
  _pushHeading() {
    if (!this.map.getSource?.("heading") || !this.placement) return;
    const p = this.placement;
    const { latitude: lat, longitude: lon } = p.location;
    const heading = Number(p.transform?.headingDegrees ?? 0);
    const reach = Math.max(60, (p.dimensions?.lengthMeters || 120) * 0.72);

    const m = metersPerDegree(lat);
    const rad = (heading * Math.PI) / 180;
    const end = [
      lon + (Math.sin(rad) * reach) / m.lon,
      lat + (Math.cos(rad) * reach) / m.lat,
    ];
    this.map.getSource("heading").setData({
      type: "Feature", properties: {},
      geometry: { type: "LineString", coordinates: [[lon, lat], end] },
    });
  }

  /* ───────────────────────────── view ───────────────────────────── */

  flyToAsset(placement = this.placement) {
    if (!placement) return;
    this.map.flyTo({
      center: [placement.location.longitude, placement.location.latitude],
      zoom: 15.6, pitch: 46, speed: 1.3, essential: true,
    });
  }

  setCellsVisible(on) {
    this.cellsVisible = on;
    for (const id of ["cells-fill", "cells-line", "parents-line"]) {
      if (this.map.getLayer(id)) {
        this.map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
      }
    }
  }

  toggleBasemap() {
    const next = BASEMAP_ORDER[(BASEMAP_ORDER.indexOf(this.basemap) + 1) % BASEMAP_ORDER.length];
    this._setBasemap(next);
    return BASEMAPS[next].label;
  }

  _setBasemap(which) {
    this.basemap = which;
    this.tileFailures = 0;
    this._layersAdded = false;
    this.map.setStyle(this._style(which));
    this.map.once("styledata", () => {
      this._layersAdded = true;
      this._addLayers();
      this.setCellsVisible(this.cellsVisible);
    });
  }

  /** The era tint is a canvas filter — the basemap is photography, and we are
      not pretending it is 2426 data. */
  setEra(t) {
    const canvas = this.map.getCanvas();
    canvas.style.filter =
      t < 0.02 ? "none"
               : `sepia(${(t * 0.55).toFixed(2)}) saturate(${(1 + t * 0.5).toFixed(2)}) ` +
                 `hue-rotate(${(-t * 12).toFixed(1)}deg) brightness(${(1 - t * 0.2).toFixed(2)})`;
  }

  _reportStatus() {
    if (!this.onStatus) return;
    const cells = this.map.getSource("cells")?._data?.features?.length ?? 0;
    const ring = this.placement?.footprint?.length ?? 0;
    const tiles = BASEMAPS[this.basemap].label.toLowerCase();
    this.onStatus(`${tiles} · ${cells} cells · ${ring}-vertex footprint`);
  }

  resize() { this.map.resize(); }

  dispose() {
    this._resizeObserver?.disconnect();
    this.map.remove();
  }
}

function closeRing(coords) {
  const ring = [...coords];
  const [fx, fy] = ring[0];
  const [lx, ly] = ring[ring.length - 1];
  if (fx !== lx || fy !== ly) ring.push([fx, fy]);
  return ring;
}
