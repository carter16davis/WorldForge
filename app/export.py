"""Writes the export contract from AGENTS.md and zips it for download.

    exports/<export-id>/<asset-id>/
    ├── building.glb
    ├── building-lod.glb        # when a lower-detail model can be derived
    ├── thumbnail.webp
    ├── placement.json
    ├── provenance.json
    ├── coverage.json
    └── manifest.json

Each export is an immutable snapshot under its own `exportId`, so a download
link handed to someone keeps resolving to the bytes they were shown after the
placement is edited again.

The GLB stays canonical: heading, scale and vertical offset live in
placement.json rather than being baked into the geometry, so a correction made
in the editor is reversible and auditable instead of destructive.

Packaging is staged in a temporary directory and renamed into place, and it
refuses to overwrite an existing package. A half-written export that looks
complete is worse than a failed one.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import struct
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app import geohash
from app.assets import ASSET_DIR, extrude_footprint, write_glb
from app.contracts import Placement, validate_document, validate_placement

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


def build_lod(placement: Placement, dest: Path, source: Path | None = None) -> str | None:
    """Derive building-lod.glb. Returns the filename, or None with a reason logged.

    A reconstruction already has one: `reconstruction.optimize` writes a
    browser-sized copy of the real geometry when the asset is published, and that
    is a far better low-detail model than anything derivable here. Copy it.
    Extruding the footprint is the fallback for the prepared asset, whose GLB is
    a footprint extrusion to begin with.
    """
    published = (source / "building-lod.glb") if source else None
    if published is not None and published.is_file():
        shutil.copy2(published, dest / "building-lod.glb")
        return "building-lod.glb"

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


def check_glb(path: Path) -> None:
    """Header-level sanity check. Not a full glTF validation, and not a render."""
    path = Path(path)
    with path.open("rb") as stream:
        header = stream.read(12)
    if len(header) != 12:
        raise ValueError(f"{path.name}: GLB header is incomplete")
    magic, version, length = struct.unpack("<4sII", header)
    if magic != b"glTF" or version != 2 or length != path.stat().st_size or length < 20:
        raise ValueError(f"{path.name}: expected a GLB v2 file with a matching declared length")


def check_webp(path: Path) -> None:
    with Path(path).open("rb") as stream:
        header = stream.read(12)
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WEBP":
        raise ValueError(f"{Path(path).name}: thumbnail must be a WebP file")


def package(placement: Placement, *, provenance: dict, coverage: dict,
            era: str = "2026", source_dir: Path | None = None,
            export_root: Path | None = None) -> dict:
    """Write an immutable export snapshot and return a manifest describing it."""
    if era not in ("2026", "2426"):
        raise ValueError("era must be '2026' or '2426'")

    # Re-run the shared contract gate: the caller may have edited the transform
    # since it was last validated, and the index must follow the coordinates —
    # the user can nudge a building across a cell boundary in the editor.
    placement = validate_document(placement.model_dump())

    src = source_dir or (ASSET_DIR / placement.assetId)
    export_id = uuid.uuid4().hex
    root = (export_root or EXPORT_ROOT) / export_id
    dest = root / placement.assetId
    if dest.exists() or dest.is_symlink():
        raise FileExistsError(f"Refusing to overwrite {dest}")

    model = src / "building.glb"
    if not model.is_file():
        raise ValueError(f"No building.glb for {placement.assetId!r} — nothing to export.")
    check_glb(model)
    thumb = src / "thumbnail.webp"
    if thumb.is_file():
        check_webp(thumb)

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".worldforge-", dir=root) as temporary:
        stage = Path(temporary) / "package"
        stage.mkdir()
        written = ["building.glb"]
        shutil.copy2(model, stage / "building.glb")
        if thumb.is_file():
            shutil.copy2(thumb, stage / "thumbnail.webp")
            written.append("thumbnail.webp")

        if (lod := build_lod(placement, stage, src)) is not None:
            check_glb(stage / lod)
            written.append(lod)

        models = {"high": "building.glb",
                  "low": "building-lod.glb" if "building-lod.glb" in written else None}
        placement = placement.model_copy(
            update={"models": placement.models.model_copy(update=models)}
        )

        placement_doc = placement.model_dump(exclude_none=False)
        placement_doc["era"] = era
        placement_doc["exportedAt"] = stamp

        provenance_doc = {**provenance, "schemaVersion": 1, "generatedAt": stamp}
        if era == "2426":
            provenance_doc["eraTreatment"] = (
                "Scorched Nebraska presentation. The 2426 appearance is a rendering "
                "treatment applied in the viewer — geometry, coordinates and scale are "
                "identical to the 2026 export."
            )

        problems = validate_placement(placement)
        manifest = {
            "assetId": placement.assetId,
            "exportId": export_id,
            "era": era,
            "files": sorted(written + ["placement.json", "provenance.json",
                                       "coverage.json", "manifest.json"]),
            "cell": placement.spatialIndex.cell if placement.spatialIndex else "",
            "worldforgeUri": world_uri(placement, era),
            "exportPath": str(dest.relative_to(REPO)) if dest.is_relative_to(REPO) else str(dest),
            "problems": problems,
            "valid": not problems,
        }

        for name, doc in (("placement.json", placement_doc),
                          ("provenance.json", provenance_doc),
                          ("coverage.json", coverage),
                          ("manifest.json", manifest)):
            (stage / name).write_text(json.dumps(doc, indent=2, allow_nan=False) + "\n")

        stage.rename(dest)

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
