/* Shared geodesy. The same local-tangent-plane approximation the Python side
   uses (app/assets.py:meters_per_degree), so the browser and the server agree
   on where a vertex sits to well under a centimetre at building scale. */

export function metersPerDegree(latDeg) {
  const phi = (latDeg * Math.PI) / 180;
  return {
    lat: 111132.92 - 559.82 * Math.cos(2 * phi)
         + 1.175 * Math.cos(4 * phi) - 0.0023 * Math.cos(6 * phi),
    lon: 111412.84 * Math.cos(phi) - 93.5 * Math.cos(3 * phi)
         + 0.118 * Math.cos(5 * phi),
  };
}

/** [lat, lon] -> {east, north} metres about `origin` ([lat, lon]). */
export function toENU([lat, lon], origin) {
  const m = metersPerDegree(origin[0]);
  return { east: (lon - origin[1]) * m.lon, north: (lat - origin[0]) * m.lat };
}

/** {east, north} metres -> [lat, lon]. */
export function fromENU({ east, north }, origin) {
  const m = metersPerDegree(origin[0]);
  return [origin[0] + north / m.lat, origin[1] + east / m.lon];
}

/**
 * ENU metres -> the viewer's scene frame.
 * glTF is Y-up with -Z forward, and we point -Z at true north, so a heading in
 * the scene is a clockwise rotation about +Y.
 */
export function toScene([lat, lon], origin) {
  const { east, north } = toENU([lat, lon], origin);
  return [east, 0, -north];
}

export function haversineM(a, b) {
  const R = 6371008.8;
  const p1 = (a[0] * Math.PI) / 180;
  const p2 = (b[0] * Math.PI) / 180;
  const dp = p2 - p1;
  const dl = ((b[1] - a[1]) * Math.PI) / 180;
  const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** Smallest absolute difference between two bearings, in degrees. */
export function bearingDelta(a, b) {
  return Math.abs(((a - b + 540) % 360) - 180);
}

export function pointInRing(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}
