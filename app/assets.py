"""Builds the prepared demo asset: footprint -> GLB + placement + provenance.

`reconstruction/` builds real assets from photographs. This module exists
because AGENTS.md is emphatic that the live demo must not depend on a cloud job
finishing during judging: this path is deterministic, offline, and takes about a
second, so there is always something on screen.

Geometry is extruded from a real OSM footprint, so the demo asset is derived
from measured data rather than invented — the provenance file says exactly that.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import trimesh
from shapely.geometry import Polygon

from app import geohash
from app.contracts import Placement, normalize_placement
from geospatial import meters_per_degree

REPO = Path(__file__).resolve().parent.parent
ASSET_DIR = REPO / "web" / "assets"
DATA_DIR = REPO / "app" / "data"


def to_enu(points: list[tuple[float, float]], origin: tuple[float, float]) -> np.ndarray:
    """[(lat, lon), ...] -> Nx2 array of (east, north) metres about `origin`."""
    m_lat, m_lon = meters_per_degree(origin[0])
    return np.array([[(lon - origin[1]) * m_lon, (lat - origin[0]) * m_lat]
                     for lat, lon in points], dtype=float)


def centroid_latlon(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Area-weighted centre of a footprint ring, in degrees."""
    n = len(points)
    mean = (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)
    enu = to_enu(points, mean)
    x, y = enu[:, 0], enu[:, 1]
    xn, yn = np.roll(x, -1), np.roll(y, -1)
    cross = x * yn - xn * y
    a = cross.sum() / 2.0
    if abs(a) < 1e-9:
        return mean
    cx = ((x + xn) * cross).sum() / (6 * a)
    cy = ((y + yn) * cross).sum() / (6 * a)
    m_lat, m_lon = meters_per_degree(mean[0])
    return (mean[0] + cy / m_lat, mean[1] + cx / m_lon)


ENU_TO_GLTF = np.array([
    [1.0, 0.0, 0.0, 0.0],   # east  -> +X
    [0.0, 0.0, 1.0, 0.0],   # up    -> +Y
    [0.0, -1.0, 0.0, 0.0],  # north -> -Z
    [0.0, 0.0, 0.0, 1.0],
])
"""ENU (X east, Y north, Z up) -> glTF (X right, Y up, -Z forward/north).

Every consumer of `building.glb` gets Y-up metres with north at -Z.
Photogrammetry and GIS tools emit raw ENU (+Z up); `import_mesh` applies this on
the way in, and `reconstruction.anchor` bakes it into the asset it writes.
"""


ASSET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
"""What may name a directory under `web/assets`. An assetId reaches this module
from a URL path, so it is matched in full before it is ever joined to a path."""


def asset_dir(asset_id: str) -> Path:
    """The directory holding one published asset. Raises on anything that could
    escape `ASSET_DIR` — `..`, a slash, a leading dot."""
    if not ASSET_ID.fullmatch(asset_id or ""):
        raise ValueError(f"{asset_id!r} is not a valid asset id")
    return ASSET_DIR / asset_id


def published_assets() -> list[dict]:
    """Every asset on disk under `web/assets`, newest first.

    A reconstruction is written here by `reconstruction.jobs` and stays here. The
    job that made it is a transient thing — the browser tab that started it polls
    until it finishes and then has the bundle — but the asset outlives both, so
    the app has to be able to find it again on the next page load. Without this
    the demo can only ever show you the model you just made, and a reload loses
    ten minutes of photogrammetry to a directory nobody reads.

    Cards only: enough to list and pick an asset. `GET /api/assets/{id}` returns
    the full bundle for the one that gets picked.
    """
    if not ASSET_DIR.is_dir():
        return []

    cards = []
    for directory in ASSET_DIR.iterdir():
        placement_file = directory / "placement.json"
        if not directory.is_dir() or not placement_file.is_file():
            continue
        try:
            placement = json.loads(placement_file.read_text())
        except (OSError, ValueError):
            continue          # a half-written asset is not a reason to fail the list

        model = directory / (placement.get("models", {}).get("high") or "building.glb")
        provenance = _read_json(directory / "provenance.json")
        report = provenance.get("anchorReport") or {}
        location = placement.get("location") or {}
        tool = provenance.get("reconstructionTool", "")

        # `reconstruction.anchor` writes no spatial index — `validate_document`
        # derives one on the way in, and this listing does not go through it. The
        # cell follows the coordinates, so deriving it here is the same answer,
        # not a second source of truth.
        index = placement.get("spatialIndex") or {}
        cell = index.get("cell") or ""
        if not cell and location.get("latitude") is not None:
            cell = geohash.encode(location["latitude"], location["longitude"],
                                  index.get("precision") or 8)

        cards.append({
            "assetId": placement.get("assetId", directory.name),
            "name": placement.get("name") or directory.name,
            "sourceAddress": placement.get("sourceAddress", ""),
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "cell": cell,
            "reconstructionTool": tool,
            # The distinction the capability strip makes, per asset: measured
            # from photographs, or extruded from a footprint for the demo.
            "kind": "prepared" if "footprint-extrusion" in tool else "scan",
            "photoCount": len(provenance.get("sourceMedia") or []),
            "unresolved": report.get("unresolved") or [],
            "dimensions": placement.get("dimensions") or {},
            "modelBytes": model.stat().st_size if model.is_file() else 0,
            "hasModel": model.is_file(),
            "createdAt": provenance.get("generatedAt", ""),
            "updatedAt": datetime.fromtimestamp(
                placement_file.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
            "thumbnailUrl": (f"/assets/{directory.name}/thumbnail.webp"
                             if (directory / "thumbnail.webp").is_file() else None),
        })

    return sorted(cards, key=lambda c: c["updatedAt"], reverse=True)


def _read_json(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


@dataclass
class BuiltAsset:
    assetId: str
    glb_path: Path
    placement: Placement
    provenance: dict
    coverage: dict


def extrude_footprint(outer: list[tuple[float, float]],
                      height_m: float,
                      holes: list[list[tuple[float, float]]] | None = None,
                      origin: tuple[float, float] | None = None) -> trimesh.Trimesh:
    """Footprint rings -> a closed, watertight, Y-up mesh in metres.

    `holes` carves courtyards — for a stadium, the bowl opening. Without it the
    demo model is a solid block and the "is this actually the building?"
    question answers itself badly.
    """
    origin = origin or centroid_latlon(outer)
    shell = to_enu(outer, origin)
    rings = [to_enu(h, origin) for h in (holes or [])]

    poly = Polygon(shell, rings)
    if not poly.is_valid:
        poly = poly.buffer(0)          # heals self-intersections from survey noise
    if poly.is_empty:
        raise ValueError("footprint collapsed to an empty polygon")
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)

    mesh = trimesh.creation.extrude_polygon(poly, height=height_m)
    mesh.apply_transform(ENU_TO_GLTF)
    return mesh


def _material(colour: str, metallic: float = 0.0, rough: float = 0.85):
    rgb = colour.lstrip("#")
    rgba = [int(rgb[i:i + 2], 16) for i in (0, 2, 4)] + [255]
    return trimesh.visual.material.PBRMaterial(
        name="facade", baseColorFactor=rgba,
        metallicFactor=metallic, roughnessFactor=rough,
    )


def write_glb(mesh: trimesh.Trimesh, path: Path, colour: str = "#8d6551") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    mesh = mesh.copy()
    mesh.visual = trimesh.visual.TextureVisuals(material=_material(colour))
    scene = trimesh.Scene(mesh)
    path.write_bytes(scene.export(file_type="glb"))
    return path


def import_mesh(path: Path, up_axis: str = "Y") -> trimesh.Trimesh:
    """Load an OBJ/GLB/PLY and return it in the viewer's Y-up frame."""
    loaded = trimesh.load(path, force="mesh")
    if up_axis.upper() == "Z":
        loaded.apply_transform(ENU_TO_GLTF)
    return loaded


def thumbnail(outer: list[tuple[float, float]],
              holes: list[list[tuple[float, float]]],
              path: Path,
              size: int = 512,
              ink: str = "#e8c46a",
              ground: str = "#161a22") -> Path:
    """A plan-view thumbnail drawn from the footprint itself.

    Deliberately not a 3D render: headless boxes have no GL context, and a plan
    view reads better at card size anyway.
    """
    from PIL import Image, ImageDraw

    origin = centroid_latlon(outer)
    shell = to_enu(outer, origin)
    rings = [to_enu(h, origin) for h in holes]

    pad = size * 0.10
    span = max(np.ptp(shell[:, 0]), np.ptp(shell[:, 1])) or 1.0
    scale = (size - 2 * pad) / span

    def px(pts: np.ndarray) -> list[tuple[float, float]]:
        # north is up on a plan view, so the screen Y axis is flipped
        return [(size / 2 + x * scale, size / 2 - y * scale) for x, y in pts]

    img = Image.new("RGB", (size, size), ground)
    d = ImageDraw.Draw(img)
    for i in range(1, 10):                      # faint reference grid
        v = i * size / 10
        d.line([(v, 0), (v, size)], fill="#1f2531")
        d.line([(0, v), (size, v)], fill="#1f2531")
    d.polygon(px(shell), fill="#2c3342", outline=ink)
    for r in rings:
        d.polygon(px(r), fill=ground, outline=ink)
    d.line([(size / 2, pad * 0.35), (size / 2, pad * 0.9)], fill=ink, width=3)
    d.text((size / 2 - 4, pad * 0.95), "N", fill=ink)

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "WEBP", quality=88)
    return path


def build_asset(*, asset_id: str, name: str, address: str,
                outer: list[tuple[float, float]],
                holes: list[list[tuple[float, float]]] | None,
                height_m: float,
                elevation_m: float,
                facade_colour: str,
                material: str,
                provenance_notes: dict,
                coverage: dict,
                out_dir: Path) -> BuiltAsset:
    """Write building.glb + thumbnail.webp and return the normalised placement."""
    holes = holes or []
    centre = centroid_latlon(outer)
    mesh = extrude_footprint(outer, height_m, holes, origin=centre)

    out_dir.mkdir(parents=True, exist_ok=True)
    glb = write_glb(mesh, out_dir / "building.glb", facade_colour)
    thumbnail(outer, holes, out_dir / "thumbnail.webp")

    enu = to_enu(outer, centre)
    area = float(Polygon(to_enu(outer, centre), [to_enu(h, centre) for h in holes]).area)

    raw = {
        "schemaVersion": 1,
        "assetId": asset_id,
        "name": name,
        "sourceAddress": address,
        "location": {"latitude": centre[0], "longitude": centre[1],
                     "elevationMeters": elevation_m},
        "spatialIndex": geohash.index(centre[0], centre[1], 8),
        "transform": {"headingDegrees": 0.0, "metersPerModelUnit": 1.0,
                      "verticalOffsetMeters": 0.0, "anchor": "ground-center",
                      "upAxis": "Y"},
        "models": {"high": "building.glb", "low": None},
        "footprint": [[round(la, 7), round(lo, 7)] for la, lo in outer],
        "holes": [[[round(la, 7), round(lo, 7)] for la, lo in h] for h in holes],
        "dimensions": {
            "widthMeters": round(float(np.ptp(enu[:, 0])), 1),
            "lengthMeters": round(float(np.ptp(enu[:, 1])), 1),
            "heightMeters": round(height_m, 1),
            "footprintAreaMeters2": round(area, 1),
        },
        "appearance": {"roof": "open", "material": material,
                       "facadeColor": facade_colour},
    }

    provenance = {
        "schemaVersion": 1,
        "sourceMedia": [],
        "reconstructionTool": "worldforge/footprint-extrusion 0.1",
        "observedCoveragePercent": None,
        "syntheticCoveragePercent": 0.0,
        "syntheticUse": "none",
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **provenance_notes,
    }

    (out_dir / "placement.json").write_text(json.dumps(raw, indent=2) + "\n")
    (out_dir / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    (out_dir / "coverage.json").write_text(json.dumps(coverage, indent=2) + "\n")

    return BuiltAsset(asset_id, glb, normalize_placement(raw), provenance, coverage)
