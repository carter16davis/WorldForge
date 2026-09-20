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

The exported GLB is metres. Whatever scale the user settled on in the
placement editor is baked into the model as a glTF node transform before it is
written, and `placement.json` then declares `metersPerModelUnit: 1`. A consumer
that drops `building.glb` straight into an engine gets a building the size it
was on screen, and a consumer that reads the transform gets the same answer.
The declared up axis is normalised to glTF's Y the same way.

Baking a node transform is not the same as rewriting vertices: the accessor
data is byte-identical to the reconstruction's own output, the factor applied
is recorded in the manifest and in provenance, and dividing it back out
restores the original model exactly. Heading and vertical offset are *not*
baked — they place the building in the world rather than describe its size, and
they stay in placement.json where a map consumer expects them.

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

from app import geohash, glb
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


def check_glb(path: Path) -> dict:
    """Gate the container. Not a full glTF validation, and not a render.

    Every model in the package goes through this: GLB v2 with a declared length
    that matches the file, at least one mesh, and no external buffers or images.
    A package whose model pulls a texture off the author's disk is not portable,
    which is most of what "map-ready" means.
    """
    path = Path(path)
    data = path.read_bytes()
    if len(data) < 20:
        raise ValueError(f"{path.name}: GLB header is incomplete")
    magic, version, length = struct.unpack("<4sII", data[:12])
    if magic != b"glTF" or version != 2 or length != len(data):
        raise ValueError(f"{path.name}: expected a GLB v2 file with a matching declared length")

    try:
        document, _ = glb.read_glb(data)
    except glb.NotGlb as exc:
        raise ValueError(f"{path.name}: {exc}") from exc
    if not document.get("meshes"):
        raise ValueError(f"{path.name}: the GLB carries no meshes")
    for resource in (document.get("buffers") or []) + (document.get("images") or []):
        uri = resource.get("uri") or ""
        if uri and not uri.startswith("data:"):
            raise ValueError(f"{path.name}: references {uri!r} outside the file; "
                             f"export with embedded textures and buffers")
    return document


def check_webp(path: Path) -> None:
    with Path(path).open("rb") as stream:
        header = stream.read(12)
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WEBP":
        raise ValueError(f"{Path(path).name}: thumbnail must be a WebP file")


def bake_placement_scale(stage: Path, models: list[str], placement: Placement) -> dict:
    """Bake the editor's scale (and any Z-up frame) into the staged models.

    Every model in the package gets the *same* factor, so the low-detail copy
    is still the same building as the high-detail one. Returns what was applied
    and what the package now measures, in metres.

    A failure here is not fatal: the un-baked model is already staged and is a
    correct export as long as `placement.json` keeps saying what scale it needs.
    The report carries the reason so the manifest can say so out loud instead of
    shipping a model that is silently the wrong size.
    """
    transform = placement.transform
    result: dict = {
        "applied": None,
        "metersPerModelUnit": transform.metersPerModelUnit,
        "upAxis": transform.upAxis,
        "extentsModelUnits": None,
        "dimensionsMeters": None,
        "boundingBoxMeters": None,
        "note": "",
    }

    # Bake beside each model and only move the results into place once every
    # one of them succeeded. A package where the high model is metres and the
    # low model is model units is worse than one that is honestly un-baked.
    baked: list[tuple[Path, Path]] = []
    pending: Path | None = None
    try:
        for name in models:
            source = stage / name
            pending = stage / f"{name}.baking"
            report = glb.bake_file(source, pending,
                                   scale=transform.metersPerModelUnit,
                                   up_axis=transform.upAxis)
            check_glb(pending)
            baked.append((pending, source))
            pending = None
            if name == "building.glb":
                result["applied"] = report["applied"]
                result["extentsModelUnits"] = report["extentsModelUnits"]
    except (ValueError, OSError) as exc:
        log.warning("could not bake the placement scale (%s); exporting in model units", exc)
        result["note"] = (f"The model could not be rewritten in metres ({exc}), so "
                          f"building.glb is in model units and placement.json keeps "
                          f"metersPerModelUnit {transform.metersPerModelUnit:g}.")
        for done, _ in baked:
            done.unlink(missing_ok=True)
        if pending is not None:
            pending.unlink(missing_ok=True)
        return result

    for pending, final in baked:
        pending.replace(final)

    if result["applied"] is not None:
        result["metersPerModelUnit"] = 1.0
        result["upAxis"] = "Y"
        result["note"] = (
            f"Scale {transform.metersPerModelUnit:g} m/unit"
            + (f" and the {transform.upAxis}-up frame" if transform.upAxis != "Y" else "")
            + " baked into the GLB as a node transform; the export is metres, Y-up."
        )

    box = glb.scene_bounds(glb.read_glb((stage / "building.glb").read_bytes())[0])
    if box is not None:
        low, high = box
        result["boundingBoxMeters"] = {"min": [round(v, 4) for v in low],
                                       "max": [round(v, 4) for v in high]}
        # glTF Y-up: X is width, Y is height, Z is length.
        result["dimensionsMeters"] = {
            "widthMeters": round(high[0] - low[0], 3),
            "heightMeters": round(high[1] - low[1], 3),
            "lengthMeters": round(high[2] - low[2], 3),
        }
    return result


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

        # The size on screen is the size in the box. Everything after this point
        # describes the baked models, not the ones the editor was working on.
        bake = bake_placement_scale(stage, [n for n in written if n.endswith(".glb")],
                                    placement)
        placement = placement.model_copy(update={
            "transform": placement.transform.model_copy(update={
                "metersPerModelUnit": bake["metersPerModelUnit"],
                "upAxis": bake["upAxis"],
            }),
        })
        if bake["dimensionsMeters"]:
            # Measured off the exported bytes, so a scale the user changed after
            # the reconstruction cannot leave a stale dimension behind.
            placement = placement.model_copy(update={
                "dimensions": placement.dimensions.model_copy(
                    update=bake["dimensionsMeters"]),
            })

        placement_doc = placement.model_dump(exclude_none=False)
        placement_doc["era"] = era
        placement_doc["exportedAt"] = stamp
        if bake["boundingBoxMeters"]:
            placement_doc["boundingBoxMeters"] = bake["boundingBoxMeters"]

        provenance_doc = {**provenance, "schemaVersion": 1, "generatedAt": stamp,
                          "exportTransform": bake}
        if era == "2426":
            provenance_doc["eraTreatment"] = (
                "Scorched Nebraska presentation. The 2426 appearance is a rendering "
                "treatment applied in the viewer — geometry, coordinates and scale are "
                "identical to the 2026 export."
            )

        problems = validate_placement(placement)
        if bake["applied"] is None and bake["note"]:
            problems = [*problems, bake["note"]]
        manifest = {
            "assetId": placement.assetId,
            "exportId": export_id,
            "era": era,
            "files": sorted(written + ["placement.json", "provenance.json",
                                       "coverage.json", "manifest.json"]),
            "model": {
                "format": "glb",
                "upAxis": placement.transform.upAxis,
                "metersPerModelUnit": placement.transform.metersPerModelUnit,
                "dimensionsMeters": bake["dimensionsMeters"],
                "boundingBoxMeters": bake["boundingBoxMeters"],
                "bakedTransform": bake["applied"],
                "note": bake["note"],
            },
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
