"""Explicit adapters for the frontend API; the standalone CLI stays dependency-free."""

import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .export import export_package
from .placement import build_placement
from .server import Geocoder

_geocoder = Geocoder()


def geocode(address, *, allow_network=True):
    from app.geocode import VENUES, _parse_coordinates
    query = " ".join(address.strip().split())
    direct = _parse_coordinates(query)
    if direct is not None:
        return direct.as_dict()
    for venue in VENUES:
        if query.casefold() in {str(venue[key]).casefold() for key in ("key", "name", "venue", "address")}:
            return {"latitude": venue["lat"], "longitude": venue["lon"], "displayName": venue["address"],
                    "source": "prepared-venue", "confidence": 0.6,
                    "note": "Prepared approximate venue center; review placement."}
    failure = {"latitude": 0, "longitude": 0, "displayName": query, "source": "none", "confidence": 0.0}
    if not allow_network:
        return {**failure, "note": "Offline: enter coordinates or choose a prepared venue."}
    try:
        candidates = _geocoder.search(query)
    except Exception:
        return {**failure, "note": "Search unavailable. Enter coordinates or choose a prepared venue."}
    if len(candidates) != 1:
        return {**failure, "candidates": candidates, "note": "Choose a matching address." if candidates else "No match; enter coordinates or try a more specific address."}
    hit = candidates[0]
    return {**hit, "displayName": hit["label"], "confidence": 0.7, "note": "Address match; review the location on the map."}


def validate_document(raw):
    """Validate our supported geometry conventions without dropping frontend extensions."""
    from app.contracts import normalize_placement
    placement = normalize_placement(raw)
    if placement.schemaVersion != 1:
        raise ValueError("Only placement schema version 1 is supported")
    build_placement(placement.assetId, placement.name, placement.sourceAddress,
                    {**placement.location.model_dump(), **placement.transform.model_dump()})
    return placement.with_index(placement.spatialIndex.precision if placement.spatialIndex else 8)


def package(raw, provenance, coverage, *, era="2026", source_dir, export_root):
    """Create an immutable export snapshot and retain frontend metadata."""
    from app.contracts import validate_placement
    from app.export import world_uri
    if era not in ("2026", "2426"):
        raise ValueError("Unsupported era")
    placement = validate_document(raw)
    core = build_placement(placement.assetId, placement.name, placement.sourceAddress,
                           {**placement.location.model_dump(), **placement.transform.model_dump()})
    # Only refer to files actually copied; inferred footprint LODs aren't substitutes for reconstructed meshes.
    source = Path(source_dir)
    lod = source / "building-lod.glb"
    if lod.is_file():
        core["models"]["low"] = "building-lod.glb"
    else:
        lod = None
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    extensions = {k: v for k, v in placement.model_dump().items() if k not in core}
    extensions.update(era=era, exportedAt=stamp)
    provenance = {**provenance, "schemaVersion": 1, "generatedAt": stamp}
    if era == "2426":
        provenance["eraTreatment"] = "Viewer-only appearance treatment; exported geometry is unchanged."
    token = uuid.uuid4().hex
    root = Path(export_root) / token
    problems = validate_placement(placement)
    files = ["building.glb", "thumbnail.webp", "placement.json", "provenance.json", "coverage.json"]
    if lod:
        files.append("building-lod.glb")
    manifest = {"assetId": placement.assetId, "exportId": token, "era": era, "files": sorted(files),
                "cell": placement.spatialIndex.cell, "worldforgeUri": world_uri(placement, era),
                "exportPath": str(root / placement.assetId), "problems": problems, "valid": not problems,
                "provider": "geospatial"}
    with tempfile.TemporaryDirectory(prefix="worldforge-provenance-") as temporary:
        provenance_path = Path(temporary) / "provenance.json"
        provenance_path.write_text(json.dumps(provenance, allow_nan=False))
        export_package(root, core, source / "building.glb", source / "thumbnail.webp", provenance_path,
                       lod_path=lod, extensions=extensions, documents={"coverage.json": coverage, "manifest.json": manifest})
    return manifest
