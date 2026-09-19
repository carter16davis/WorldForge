"""Integration seam between the viewer and my teammates' modules.

Person 1 (reconstruction) and Person 2 (geospatial) are building in parallel.
Rather than wait, every call the UI makes goes through here, and here decides:

    teammate's module if importable  ->  use it
    otherwise                        ->  a local stand-in, clearly labelled

`capabilities()` reports which branch each call took, and the UI shows that in
a status strip. Nobody has to ask "is the real pipeline wired up yet?" — the
app says so on screen.

To wire a module in, expose any of the documented function names in
docs/INTEGRATION.md. Nothing here needs editing.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app import geocode as _geocode
from app import geohash
from app.contracts import (
    CoverageReport,
    Placement,
    Transform,
    normalize_placement,
    validate_placement,
)

log = logging.getLogger("worldforge.pipeline")

# Import candidates, best first. Names match docs/INTEGRATION.md.
RECONSTRUCTION_MODULES = ("worldforge.reconstruction", "reconstruction", "procedura_core")
GEOSPATIAL_MODULES = ("worldforge.geospatial", "geospatial", "placement_module")


def _first_module(names: tuple[str, ...]):
    for name in names:
        try:
            return importlib.import_module(name), name
        except Exception:      # ModuleNotFoundError, or a half-finished module
            continue
    return None, ""


def _attr(module, *names: str) -> Callable | None:
    for n in names:
        fn = getattr(module, n, None) if module else None
        if callable(fn):
            return fn
    return None


@dataclass
class Capability:
    name: str
    wired: bool
    provider: str
    detail: str

    def as_dict(self) -> dict:
        return {"name": self.name, "wired": self.wired,
                "provider": self.provider, "detail": self.detail}


def _modules():
    recon, recon_name = _first_module(RECONSTRUCTION_MODULES)
    geo, geo_name = _first_module(GEOSPATIAL_MODULES)
    return recon, recon_name, geo, geo_name


def capabilities() -> list[dict]:
    """What is actually wired right now. Re-checked on every call so a teammate
    dropping a module into the repo shows up on a page refresh."""
    recon, recon_name, geo, geo_name = _modules()
    recon_fn = _attr(recon, "reconstruct", "build_asset", "build")
    cov_fn = _attr(recon, "coverage_report", "analyze_coverage", "assess")
    geo_fn = _attr(geo, "geocode", "resolve_address")
    pack_fn = _attr(geo, "package", "export_package", "write_package")

    return [
        Capability("Reconstruction", bool(recon_fn),
                   recon_name or "prepared asset",
                   "Person 1's pipeline" if recon_fn
                   else "Using the prepared venue asset — uploads are analysed but not reconstructed.").as_dict(),
        Capability("Coverage agent", bool(cov_fn),
                   recon_name or "built-in",
                   "Person 1's coverage agent" if cov_fn
                   else "Built-in intake checks (EXIF, blur, duplicates) plus the prepared report.").as_dict(),
        Capability("Geocoding", bool(geo_fn),
                   geo_name or "nominatim + venue table",
                   "Person 2's geocoder" if geo_fn
                   else "OpenStreetMap Nominatim, falling back to the offline venue table.").as_dict(),
        Capability("Export packaging", bool(pack_fn),
                   geo_name or "built-in",
                   "Person 2's packager" if pack_fn
                   else "Built-in packager writing the AGENTS.md export contract.").as_dict(),
    ]


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def geocode_address(address: str, *, allow_network: bool = True) -> dict:
    _, _, geo, name = _modules()
    fn = _attr(geo, "geocode", "resolve_address")
    if fn:
        try:
            result = fn(address, allow_network=allow_network)
            return _coerce_geocode(result, provider=name)
        except Exception as exc:
            log.warning("%s.geocode failed (%s); falling back", name, exc)
    return _geocode.geocode(address, allow_network=allow_network).as_dict()


def _coerce_geocode(result: Any, provider: str) -> dict:
    """Accept whatever Person 2 returns — dataclass, dict, or (lat, lon)."""
    if isinstance(result, tuple) and len(result) == 2:
        lat, lon = float(result[0]), float(result[1])
        payload = {"latitude": lat, "longitude": lon, "displayName": "",
                   "confidence": 0.8, "note": ""}
    elif isinstance(result, dict):
        payload = dict(result)
        payload["latitude"] = float(payload.get("latitude", payload.get("lat", 0.0)))
        payload["longitude"] = float(payload.get("longitude", payload.get("lon", 0.0)))
    else:
        payload = {
            "latitude": float(getattr(result, "latitude", getattr(result, "lat", 0.0))),
            "longitude": float(getattr(result, "longitude", getattr(result, "lon", 0.0))),
            "displayName": getattr(result, "displayName", getattr(result, "display_name", "")),
            "confidence": float(getattr(result, "confidence", 0.8)),
            "note": getattr(result, "note", ""),
        }
    payload.setdefault("displayName", "")
    payload.setdefault("confidence", 0.8)
    payload.setdefault("note", "")
    payload.setdefault("source", provider)
    payload["cell"] = geohash.encode(payload["latitude"], payload["longitude"], 8)
    return payload


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

def relocate(placement: Placement, lat: float, lon: float,
             address: str = "", *, move_footprint: bool = True) -> Placement:
    """Move an asset to new coordinates, carrying its footprint along.

    Used when the user geocodes a different address for the prepared model.
    The footprint is translated rigidly rather than re-derived, so the building
    keeps its real shape and the UI stays honest about where the shape came
    from (provenance still names the original OSM source).
    """
    d_lat = lat - placement.location.latitude
    d_lon = lon - placement.location.longitude

    update: dict[str, Any] = {
        "location": placement.location.model_copy(
            update={"latitude": lat, "longitude": lon}
        )
    }
    if address:
        update["sourceAddress"] = address
    if move_footprint and placement.footprint:
        update["footprint"] = [(la + d_lat, lo + d_lon) for la, lo in placement.footprint]

    moved = placement.model_copy(update=update)
    # extension field: holes ride along with the footprint
    holes = getattr(moved, "holes", None) or (moved.model_extra or {}).get("holes")
    if move_footprint and holes:
        shifted = [[[la + d_lat, lo + d_lon] for la, lo in ring] for ring in holes]
        moved = moved.model_copy(update={"holes": shifted})

    return moved.with_index(
        moved.spatialIndex.precision if moved.spatialIndex else 8
    )


def apply_transform(placement: Placement, transform: dict) -> Placement:
    """Apply the editor's corrections. Only `Transform` fields are accepted, so
    UI state can never leak into the exported package."""
    merged = placement.transform.model_dump() | {
        k: v for k, v in (transform or {}).items()
        if k in Transform.model_fields
    }
    return placement.model_copy(update={"transform": Transform(**merged)})


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

def coverage_for(placement: Placement, fallback: dict) -> dict:
    recon, name, _, _ = _modules()
    fn = _attr(recon, "coverage_report", "analyze_coverage", "assess")
    if fn:
        try:
            return CoverageReport.model_validate(fn(placement.assetId)).model_dump()
        except Exception as exc:
            log.warning("%s coverage failed (%s); using prepared report", name, exc)
    return fallback


# ---------------------------------------------------------------------------
# Media intake
#
# Person 1 owns reconstruction. This is the intake half: enough analysis that
# the upload screen tells the truth about the files, and a clean hand-off point
# when the real pipeline arrives.
# ---------------------------------------------------------------------------

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}


def _exif_gps(img) -> tuple[float, float] | None:
    try:
        exif = img.getexif()
        gps = exif.get_ifd(0x8825)
        if not gps:
            return None

        def dms(v, ref):
            d, m, s = (float(x) for x in v)
            val = d + m / 60 + s / 3600
            return -val if ref in ("S", "W") else val

        return dms(gps[2], gps[1]), dms(gps[4], gps[3])
    except Exception:
        return None


def _blur_score(path: Path) -> float | None:
    """Variance of the Laplacian: low means soft or out of focus."""
    try:
        import cv2
        import numpy as np
        data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        return float(cv2.Laplacian(img, cv2.CV_64F).var())
    except Exception:
        return None


def _perceptual_hash(path: Path) -> str | None:
    """8x8 average hash — catches burst-mode near-duplicates."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            small = im.convert("L").resize((8, 8))
        px = list(small.getdata())
        mean = sum(px) / len(px)
        bits = "".join("1" if p > mean else "0" for p in px)
        return f"{int(bits, 2):016x}"
    except Exception:
        return None


def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def analyse_media(paths: list[Path], *, blur_threshold: float = 80.0) -> dict:
    """Per-file intake report. Never raises — an unreadable file becomes a
    flagged row, not a failed upload."""
    recon, name, _, _ = _modules()
    fn = _attr(recon, "analyse_media", "analyze_media", "inspect_photos")
    if fn:
        try:
            return fn([str(p) for p in paths])
        except Exception as exc:
            log.warning("%s media analysis failed (%s); using built-in intake", name, exc)

    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()      # iPhone HEIC arrives straight off the camera roll
    except Exception:
        pass

    files: list[dict] = []
    hashes: list[tuple[str, str]] = []
    for p in paths:
        suffix = p.suffix.lower()
        row: dict[str, Any] = {
            "name": p.name,
            "sizeBytes": p.stat().st_size if p.exists() else 0,
            "kind": "video" if suffix in VIDEO_SUFFIXES else
                    "photo" if suffix in IMAGE_SUFFIXES else "other",
            "issues": [],
        }
        if row["kind"] == "photo":
            try:
                from PIL import Image
                with Image.open(p) as im:
                    row["width"], row["height"] = im.size
                    gps = _exif_gps(im)
                if gps:
                    row["gps"] = {"latitude": round(gps[0], 7), "longitude": round(gps[1], 7)}
                    row["cell"] = geohash.encode(gps[0], gps[1], 8)
            except Exception as exc:
                row["issues"].append(f"Could not read image ({type(exc).__name__}).")

            blur = _blur_score(p)
            if blur is not None:
                row["sharpness"] = round(blur, 1)
                if blur < blur_threshold:
                    row["issues"].append("Soft or out of focus — low value for reconstruction.")
            if (h := _perceptual_hash(p)) is not None:
                for other_name, other_hash in hashes:
                    if _hamming(h, other_hash) <= 4:
                        row["issues"].append(f"Near-duplicate of {other_name}.")
                        break
                hashes.append((p.name, h))
        elif row["kind"] == "other":
            row["issues"].append("Unsupported file type — not photo or video.")
        files.append(row)

    usable = [f for f in files if f["kind"] in ("photo", "video") and not f["issues"]]
    located = [f for f in files if f.get("gps")]
    return {
        "files": files,
        "usableCount": len(usable),
        "totalCount": len(files),
        "geotaggedCount": len(located),
        "notes": _intake_notes(files, usable, located),
        "provider": "built-in intake",
    }


def _intake_notes(files: list[dict], usable: list[dict], located: list[dict]) -> list[str]:
    notes: list[str] = []
    if not files:
        notes.append("No files received.")
        return notes
    if not usable:
        notes.append("No usable frames — every file was flagged. Recapture before reconstructing.")
    elif len(usable) < 8:
        notes.append(
            f"Only {len(usable)} usable frames. Photogrammetry wants 20+ with 60–80% overlap."
        )
    if not located:
        notes.append("No GPS in EXIF — placement will rely entirely on the typed address.")
    elif len(located) < len(files) / 2:
        notes.append("Fewer than half the photos are geotagged.")
    if any("Near-duplicate" in i for f in files for i in f["issues"]):
        notes.append("Burst-mode duplicates detected; they add processing time without adding parallax.")
    return notes


def reconstruct(paths: list[Path], address: str) -> Placement | None:
    """Hand off to Person 1. Returns None when their module is not wired yet,
    which the caller reports as 'falling back to the prepared asset'."""
    recon, name, _, _ = _modules()
    fn = _attr(recon, "reconstruct", "build_asset")
    if not fn:
        return None
    try:
        raw = fn([str(p) for p in paths], address)
        if raw is None:
            return None
        if isinstance(raw, (str, Path)):
            raw = json.loads(Path(raw).read_text())
        elif not isinstance(raw, dict):
            raw = getattr(raw, "placement", None) or raw.__dict__
        placement = normalize_placement(raw)
        problems = validate_placement(placement)
        if problems:
            log.warning("%s produced a placement with problems: %s", name, problems)
        return placement
    except Exception as exc:
        log.warning("%s reconstruction failed (%s); using the prepared asset", name, exc)
        return None


def media_fingerprint(paths: list[Path]) -> str:
    """Stable id for an upload batch, so re-uploading the same set reuses work."""
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda q: q.name):
        h.update(p.name.encode())
        h.update(str(p.stat().st_size if p.exists() else 0).encode())
    return h.hexdigest()[:12]
