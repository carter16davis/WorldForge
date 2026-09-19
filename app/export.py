"""Writes the export contract from AGENTS.md and zips it for download.

    exports/<asset-id>/
    ├── building.glb
    ├── building-lod.glb        # when a lower-detail model can be derived
    ├── thumbnail.webp
    ├── placement.json
    ├── provenance.json
    └── coverage.json

The GLB stays canonical: heading, scale and vertical offset live in
placement.json rather than being baked into the geometry, so a correction made
in the editor is reversible and auditable instead of destructive.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app import geohash
from app.assets import ASSET_DIR, extrude_footprint, write_glb
from app.contracts import Placement, validate_placement
from app.pipeline import _attr, _modules

log = logging.getLogger("worldforge.export")

REPO = Path(__file__).resolve().parent.parent
EXPORT_ROOT = REPO / "exports"

LOD_TOLERANCE_M = 4.0
"""Footprint simplification distance for the low-detail model. Four metres
keeps a building readable at street-block zoom while dropping the survey
wiggle that dominates an OSM ring's vertex count."""


def _simplify_latlon(ring: list[tuple[float, float]], tol_m: float) -> list[tuple[float, float]]:
    from shapely.geometry import Polygon

    from app.assets import centroid_latlon, meters_per_degree, to_enu

    origin = centroid_latlon(ring)
    poly = Polygon(to_enu(ring, origin)).buffer(0).simplify(tol_m, preserve_topology=True)
    if poly.is_empty or poly.geom_type != "Polygon":
        return ring
    m_lat, m_lon = meters_per_degree(origin[0])
    coords = list(poly.exterior.coords)[:-1]
    return [(origin[0] + y / m_lat, origin[1] + x / m_lon) for x, y in coords]


def build_lod(placement: Placement, dest: Path) -> str | None:
    """Derive building-lod.glb. Returns the filename, or None with a reason logged."""
    if not placement.footprint or len(placement.footprint) < 4:
        return None
    height = placement.dimensions.heightMeters
    if not height:
        return None

    outer = _simplify_latlon(list(placement.footprint), LOD_TOLERANCE_M)
    if len(outer) < 3 or len(outer) >= len(placement.footprint):
        return None      # nothing gained; shipping it would be dishonest as an "LOD"

    try:
        mesh = extrude_footprint(outer, height, holes=None)
        write_glb(mesh, dest / "building-lod.glb", placement.appearance.facadeColor)
    except Exception as exc:
        log.warning("LOD generation failed (%s); exporting without one", exc)
        return None
    return "building-lod.glb"


def package(placement: Placement, *, provenance: dict, coverage: dict,
            era: str = "2026", source_dir: Path | None = None,
            export_root: Path | None = None) -> dict:
    """Write the package and return a manifest describing what landed."""
    _, _, geo, geo_name = _modules()
    external = _attr(geo, "package", "export_package", "write_package")
    if external:
        return external(placement.model_dump(), provenance, coverage, era=era,
                        source_dir=source_dir or (ASSET_DIR / placement.assetId),
                        export_root=export_root or EXPORT_ROOT)

    # The index must follow the coordinates — the user may have nudged the
    # building across a cell boundary in the editor.
    placement = placement.with_index(
        placement.spatialIndex.precision if placement.spatialIndex else 8
    )

    root = export_root or EXPORT_ROOT
    dest = root / placement.assetId
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    src = source_dir or (ASSET_DIR / placement.assetId)
    written: list[str] = []

    for name in ("building.glb", "thumbnail.webp"):
        candidate = src / name
        if candidate.exists():
            shutil.copy2(candidate, dest / name)
            written.append(name)

    if (lod := build_lod(placement, dest)) is not None:
        written.append(lod)

    models = {"high": "building.glb" if "building.glb" in written else None,
              "low": "building-lod.glb" if "building-lod.glb" in written else None}
    placement = placement.model_copy(update={"models": placement.models.model_copy(
        update={k: v for k, v in models.items() if v is not None})})

    placement_doc = placement.model_dump(exclude_none=False)
    placement_doc["era"] = era
    placement_doc["exportedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    provenance_doc = dict(provenance)
    provenance_doc["generatedAt"] = placement_doc["exportedAt"]
    if era == "2426":
        provenance_doc["eraTreatment"] = (
            "Scorched Nebraska presentation. The 2426 appearance is a rendering "
            "treatment applied in the viewer — geometry, coordinates and scale are "
            "identical to the 2026 export."
        )

    (dest / "placement.json").write_text(json.dumps(placement_doc, indent=2) + "\n")
    (dest / "provenance.json").write_text(json.dumps(provenance_doc, indent=2) + "\n")
    (dest / "coverage.json").write_text(json.dumps(coverage, indent=2) + "\n")
    written += ["placement.json", "provenance.json", "coverage.json"]

    problems = validate_placement(placement)
    manifest = {
        "assetId": placement.assetId,
        "era": era,
        "files": sorted(written),
        "cell": placement.spatialIndex.cell if placement.spatialIndex else "",
        "worldforgeUri": world_uri(placement, era),
        "exportPath": str(dest.relative_to(REPO)) if dest.is_relative_to(REPO) else str(dest),
        "problems": problems,
        "valid": not problems,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def world_uri(placement: Placement, era: str = "2026") -> str:
    """`worldforge://<cell>/<asset>/<era>` — the spatial identity from AGENTS.md."""
    cell = (placement.spatialIndex.cell if placement.spatialIndex
            else geohash.encode(placement.location.latitude, placement.location.longitude, 8))
    return f"worldforge://{cell}/{placement.assetId}/{era}"


def zip_package(asset_id: str, export_root: Path | None = None) -> bytes:
    """Zip a written package for browser download."""
    dest = (export_root or EXPORT_ROOT) / asset_id
    if not dest.is_dir():
        raise FileNotFoundError(f"nothing exported for {asset_id!r} yet")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(dest.rglob("*")):
            if f.is_file():
                z.write(f, f"{asset_id}/{f.relative_to(dest)}")
    return buf.getvalue()
