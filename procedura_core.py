"""
procedura_core.py — photo + address -> correctly placed, map-ready 3D building.

This is the deterministic half of the pipeline. Everything an AI model produces
is funnelled through ONE narrow struct (FacadeReading) so the model can be swapped,
mocked, or graded independently of the geometry.

Pipeline:
    address  --geocode-->  anchor (lat, lon)
    anchor   --footprint--> polygon in WGS84            [OSM / Overpass]
    photo    --VLM-------> FacadeReading (floors, roof, material, camera pose)
    polygon + FacadeReading --> ENU mesh --> voxel lattice --> placement manifest

Nothing here needs a GPU, a network call, or a point cloud.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from typing import Iterable

# ----------------------------------------------------------------------------
# 1. Geodesy.  WGS84 -> local ENU metres about an anchor.
#    At building scale (<500 m) the local-tangent-plane approximation is
#    sub-centimetre, so we skip the full ECEF round trip.
# ----------------------------------------------------------------------------

def meters_per_degree(lat_deg: float) -> tuple[float, float]:
    """(metres per degree latitude, metres per degree longitude) at this latitude."""
    phi = math.radians(lat_deg)
    m_lat = (111132.92
             - 559.82 * math.cos(2 * phi)
             + 1.175 * math.cos(4 * phi)
             - 0.0023 * math.cos(6 * phi))
    m_lon = (111412.84 * math.cos(phi)
             - 93.5 * math.cos(3 * phi)
             + 0.118 * math.cos(5 * phi))
    return m_lat, m_lon


@dataclass(frozen=True)
class Anchor:
    """Local tangent plane origin. +X = East, +Y = North, +Z = up. Metres."""
    lat: float
    lon: float
    ground_alt_m: float = 0.0

    def to_enu(self, lat: float, lon: float) -> tuple[float, float]:
        m_lat, m_lon = meters_per_degree(self.lat)
        return ((lon - self.lon) * m_lon, (lat - self.lat) * m_lat)

    def to_wgs84(self, x: float, y: float) -> tuple[float, float]:
        m_lat, m_lon = meters_per_degree(self.lat)
        return (self.lat + y / m_lat, self.lon + x / m_lon)


def bearing_deg(from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> float:
    """Initial great-circle bearing, degrees clockwise from true north."""
    p1, p2 = math.radians(from_lat), math.radians(to_lat)
    dl = math.radians(to_lon - from_lon)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_delta(a: float, b: float) -> float:
    """Smallest signed difference a-b, in (-180, 180]."""
    return (a - b + 180.0) % 360.0 - 180.0


# ----------------------------------------------------------------------------
# 2. The AI boundary.  A vision model fills this in and nothing else.
#    Keep it small: every field here is something a human could also type in,
#    which means every field is something you can unit-test and grade.
# ----------------------------------------------------------------------------

@dataclass
class FacadeReading:
    floors: int                       # storeys visible above grade
    floor_height_m: float = 3.35      # ~11 ft; 4.2 for ground-floor retail
    roof: str = "flat"                # flat | gable | hip | parapet
    roof_rise_m: float = 0.0          # 0 for flat; ridge height above eave otherwise
    parapet_m: float = 0.9
    material: str = "brick"           # drives palette / voxel material id
    facade_color: str = "#8d6551"
    # camera pose — from EXIF if present, else the model's best guess
    cam_lat: float | None = None
    cam_lon: float | None = None
    cam_heading_deg: float | None = None   # direction the lens points, deg from N
    confidence: float = 0.5

    def eave_height_m(self) -> float:
        h = self.floors * self.floor_height_m
        return h + (self.parapet_m if self.roof in ("flat", "parapet") else 0.0)

    def ridge_height_m(self) -> float:
        return self.eave_height_m() + self.roof_rise_m


# ----------------------------------------------------------------------------
# 3. Footprint handling.
# ----------------------------------------------------------------------------

Poly = list[tuple[float, float]]


def signed_area(poly: Poly) -> float:
    a = 0.0
    for i in range(len(poly)):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % len(poly)]
        a += x0 * y1 - x1 * y0
    return a / 2.0


def ensure_ccw(poly: Poly) -> Poly:
    return poly if signed_area(poly) > 0 else poly[::-1]


def centroid(poly: Poly) -> tuple[float, float]:
    a = signed_area(poly)
    if abs(a) < 1e-9:
        n = len(poly)
        return (sum(p[0] for p in poly) / n, sum(p[1] for p in poly) / n)
    cx = cy = 0.0
    for i in range(len(poly)):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % len(poly)]
        cross = x0 * y1 - x1 * y0
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    return (cx / (6 * a), cy / (6 * a))


def simplify(poly: Poly, tol_m: float = 0.35) -> Poly:
    """Douglas-Peucker. OSM footprints carry survey noise; the lattice can't
    represent it anyway, and every stray vertex is a wasted face."""
    if len(poly) < 4:
        return poly

    def dp(pts: Poly) -> Poly:
        if len(pts) < 3:
            return pts
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        dx, dy = x1 - x0, y1 - y0
        norm = math.hypot(dx, dy) or 1e-9
        worst, idx = 0.0, 0
        for i in range(1, len(pts) - 1):
            px, py = pts[i]
            d = abs(dy * px - dx * py + x1 * y0 - y1 * x0) / norm
            if d > worst:
                worst, idx = d, i
        if worst <= tol_m:
            return [pts[0], pts[-1]]
        return dp(pts[:idx + 1])[:-1] + dp(pts[idx:])

    out = dp(list(poly) + [poly[0]])[:-1]
    return out if len(out) >= 3 else poly


def outward_normals(poly: Poly) -> list[float]:
    """Compass bearing of each edge's outward normal, CCW polygon assumed."""
    out = []
    for i in range(len(poly)):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % len(poly)]
        # CCW winding -> outward normal is the edge vector rotated -90 deg
        nx, ny = (y1 - y0), -(x1 - x0)
        out.append((math.degrees(math.atan2(nx, ny)) + 360.0) % 360.0)
    return out


def match_photo_to_wall(poly: Poly, anchor: Anchor, r: FacadeReading) -> tuple[int, float]:
    """Which footprint edge is the photographed facade?

    The wall we can see is the one whose outward normal points back at the
    camera. Returns (edge index, confidence 0-1). This is the single step that
    decides whether the texture lands on the right side of the building, and
    it is worth more demo points than any amount of mesh polish.
    """
    if r.cam_lat is not None and r.cam_lon is not None:
        cx, cy = centroid(poly)
        blat, blon = anchor.to_wgs84(cx, cy)
        # bearing from building back out to the camera
        view_back = bearing_deg(blat, blon, r.cam_lat, r.cam_lon)
    elif r.cam_heading_deg is not None:
        view_back = (r.cam_heading_deg + 180.0) % 360.0
    else:
        return (0, 0.0)  # no pose -> caller should fall back to street-facing edge

    normals = outward_normals(poly)
    scored = [(abs(angle_delta(n, view_back)), i) for i, n in enumerate(normals)]
    err, idx = min(scored)
    return (idx, max(0.0, math.cos(math.radians(err))))


# ----------------------------------------------------------------------------
# 4. Extrusion -> low-poly mesh.
# ----------------------------------------------------------------------------

@dataclass
class Mesh:
    verts: list[tuple[float, float, float]] = field(default_factory=list)
    faces: list[tuple[int, ...]] = field(default_factory=list)
    face_tags: list[str] = field(default_factory=list)   # "wall:3", "roof", "ground"

    def add_face(self, vs: Iterable[tuple[float, float, float]], tag: str) -> None:
        base = len(self.verts)
        vs = list(vs)
        self.verts.extend(vs)
        self.faces.append(tuple(range(base, base + len(vs))))
        self.face_tags.append(tag)


def extrude(poly: Poly, r: FacadeReading) -> Mesh:
    poly = ensure_ccw(simplify(poly))
    eave = r.eave_height_m()
    m = Mesh()
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        m.add_face([(x0, y0, 0.0), (x1, y1, 0.0), (x1, y1, eave), (x0, y0, eave)],
                   f"wall:{i}")

    if r.roof in ("flat", "parapet") or r.roof_rise_m <= 0:
        m.add_face([(x, y, eave) for x, y in poly], "roof:flat")
    else:
        # Gable/hip approximated by a ridge along the long axis of the bbox.
        xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        ridge_z = r.ridge_height_m()
        if (x1 - x0) >= (y1 - y0):      # ridge runs east-west
            a, b = (x0, cy, ridge_z), (x1, cy, ridge_z)
        else:                            # ridge runs north-south
            a, b = (cx, y0, ridge_z), (cx, y1, ridge_z)
        for i in range(n):
            p0 = (*poly[i], eave)
            p1 = (*poly[(i + 1) % n], eave)
            apex = a if math.dist(p0[:2], a[:2]) < math.dist(p0[:2], b[:2]) else b
            m.add_face([p0, p1, apex], f"roof:{r.roof}")
    return m


# ----------------------------------------------------------------------------
# 5. Voxel lattice.
#    Snapping happens on a GLOBAL integer lattice, not a per-building one.
#    That is what stops neighbouring buildings from overlapping or leaving a
#    half-voxel seam when they are imported in separate sessions.
# ----------------------------------------------------------------------------

@dataclass
class VoxelBlock:
    voxel_m: float
    origin_ijk: tuple[int, int, int]       # global lattice index of block corner
    size: tuple[int, int, int]
    solid: set[tuple[int, int, int]]       # local indices
    material: str

    def count(self) -> int:
        return len(self.solid)

    def to_runs(self) -> list[list[int]]:
        """Z-major run-length encoding: [i, j, z_start, run_len] per run.
        Buildings are prismatic, so this is ~2 orders of magnitude smaller
        than a dense grid and maps cleanly onto a diff-based stream."""
        cols: dict[tuple[int, int], list[int]] = {}
        for i, j, k in self.solid:
            cols.setdefault((i, j), []).append(k)
        runs = []
        for (i, j), ks in sorted(cols.items()):
            ks.sort()
            start, prev = ks[0], ks[0]
            for k in ks[1:]:
                if k == prev + 1:
                    prev = k
                    continue
                runs.append([i, j, start, prev - start + 1])
                start = prev = k
            runs.append([i, j, start, prev - start + 1])
        return runs


def point_in_poly(x: float, y: float, poly: Poly) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            xint = (x1 - x0) * (y - y0) / (y1 - y0) + x0
            if x < xint:
                inside = not inside
    return inside


def voxelize(poly: Poly, r: FacadeReading, anchor: Anchor,
             voxel_m: float = 1.0, lattice_origin: Anchor | None = None) -> VoxelBlock:
    poly = ensure_ccw(simplify(poly))
    lattice_origin = lattice_origin or anchor
    # offset of this building's anchor within the global lattice
    ox, oy = lattice_origin.to_enu(anchor.lat, anchor.lon)

    xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
    i0 = math.floor((min(xs) + ox) / voxel_m); i1 = math.ceil((max(xs) + ox) / voxel_m)
    j0 = math.floor((min(ys) + oy) / voxel_m); j1 = math.ceil((max(ys) + oy) / voxel_m)
    eave = r.eave_height_m()
    k_top = math.ceil(eave / voxel_m)

    solid: set[tuple[int, int, int]] = set()
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            # sample at the voxel centre, in building-local metres
            px = (i + 0.5) * voxel_m - ox
            py = (j + 0.5) * voxel_m - oy
            if not point_in_poly(px, py, poly):
                continue
            for k in range(0, k_top):
                solid.add((i - i0, j - j0, k))
            if r.roof_rise_m > 0:
                # crude gable: height falls off from the ridge axis
                half = max((max(xs) - min(xs)), (max(ys) - min(ys))) / 2 or 1.0
                d = abs(py - centroid(poly)[1]) if (max(xs) - min(xs)) >= (max(ys) - min(ys)) \
                    else abs(px - centroid(poly)[0])
                extra = r.roof_rise_m * max(0.0, 1.0 - d / half)
                for k in range(k_top, k_top + math.ceil(extra / voxel_m)):
                    solid.add((i - i0, j - j0, k))

    max_k = max((k for _, _, k in solid), default=0)
    return VoxelBlock(
        voxel_m=voxel_m,
        origin_ijk=(i0, j0, 0),
        size=(i1 - i0 + 1, j1 - j0 + 1, max_k + 1),
        solid=solid,
        material=r.material,
    )


# ----------------------------------------------------------------------------
# 6. Export.
# ----------------------------------------------------------------------------

def write_obj(mesh: Mesh, path: str) -> None:
    with open(path, "w") as f:
        f.write("# procedura_core extrusion, +X east +Y north +Z up, metres\n")
        for x, y, z in mesh.verts:
            f.write(f"v {x:.4f} {y:.4f} {z:.4f}\n")
        for face, tag in zip(mesh.faces, mesh.face_tags):
            f.write(f"g {tag.replace(':', '_')}\n")
            f.write("f " + " ".join(str(i + 1) for i in face) + "\n")


def placement_manifest(anchor: Anchor, poly: Poly, r: FacadeReading,
                       wall_idx: int, wall_conf: float, vox: VoxelBlock,
                       address: str, source: str) -> dict:
    """Everything the engine needs to drop this in the right spot, and
    everything a reviewer needs to tell whether we guessed or measured."""
    return {
        "schema": "procedura.building/0.1",
        "address": address,
        "anchor": {"lat": anchor.lat, "lon": anchor.lon,
                   "ground_alt_m": anchor.ground_alt_m},
        "frame": {"convention": "ENU", "x": "east", "y": "north", "z": "up",
                  "units": "metres", "yaw_deg_from_true_north": 0.0},
        "footprint_wgs84": [list(anchor.to_wgs84(x, y)) for x, y in poly],
        "dimensions": {
            "floors": r.floors,
            "eave_height_m": round(r.eave_height_m(), 2),
            "ridge_height_m": round(r.ridge_height_m(), 2),
            "footprint_area_m2": round(abs(signed_area(poly)), 1),
        },
        "appearance": {"roof": r.roof, "material": r.material,
                       "facade_color": r.facade_color},
        "photo_alignment": {"wall_index": wall_idx,
                            "confidence": round(wall_conf, 3)},
        "lattice": {"voxel_m": vox.voxel_m, "origin_ijk": list(vox.origin_ijk),
                    "size": list(vox.size), "solid_voxels": vox.count(),
                    "encoding": "z-runs[i,j,k0,len]"},
        "provenance": {
            "footprint": source,
            "height": "vlm-floor-count x floor-height",
            "overall_confidence": round(min(r.confidence, wall_conf or r.confidence), 3),
        },
    }


def build(address: str, anchor: Anchor, footprint_wgs84: Poly,
          reading: FacadeReading, *, voxel_m: float = 1.0,
          footprint_source: str = "osm/overpass") -> dict:
    poly = ensure_ccw(simplify([anchor.to_enu(la, lo) for la, lo in footprint_wgs84]))
    wall_idx, wall_conf = match_photo_to_wall(poly, anchor, reading)
    mesh = extrude(poly, reading)
    vox = voxelize(poly, reading, anchor, voxel_m=voxel_m)
    manifest = placement_manifest(anchor, poly, reading, wall_idx, wall_conf,
                                  vox, address, footprint_source)
    return {"mesh": mesh, "voxels": vox, "manifest": manifest, "poly_enu": poly}