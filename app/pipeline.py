"""The seam between the viewer and the reconstruction pipeline.

Geocoding (`app.geocode`) and packaging (`app.export`) are always present, so
they are called directly. Reconstruction is not: it drives RealityScan, a
Windows desktop application, and on a machine without it the app must still run
the whole prepared path end to end. So reconstruction alone is probed at call
time, and `capabilities()` reports honestly whether it answered.

The UI shows that report in a status strip. Nobody has to ask "is the real
pipeline wired up yet?" — the app says so on screen, and says which of the two
things you are looking at: a measured reconstruction, or a prepared asset.

Contract for the reconstruction module: see docs/INTEGRATION.md.
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
RECONSTRUCTION_MODULES = ("reconstruction",)


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


def _reconstruction():
    """The reconstruction module, if this machine has a working one."""
    return _first_module(RECONSTRUCTION_MODULES)


def capabilities() -> list[dict]:
    """What is actually wired right now. Re-checked on every call, so installing
    RealityScan and restarting shows up on a page refresh."""
    recon, name = _reconstruction()
    recon_fn = _attr(recon, "reconstruct", "build_asset")
    cov_fn = _attr(recon, "coverage_report", "analyze_coverage")

    return [
        Capability("Reconstruction", bool(recon_fn),
                   name if recon_fn else "prepared asset",
                   "RealityScan photogrammetry, anchored to the geocoded address." if recon_fn
                   else "Using the prepared venue asset — uploads are analysed but not reconstructed.").as_dict(),
        Capability("Coverage agent", bool(cov_fn),
                   name if cov_fn else "built-in",
                   "Per-facade coverage from the reconstruction's own camera poses." if cov_fn
                   else "Built-in intake checks (EXIF, blur, duplicates) plus the prepared report.").as_dict(),
        Capability("Geocoding", True, "nominatim + venue table",
                   "Prepared venue table first, then OpenStreetMap Nominatim.").as_dict(),
        Capability("Export packaging", True, "built-in",
                   "Immutable snapshot packages written to the AGENTS.md export contract.").as_dict(),
    ]


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def geocode_address(address: str, *, allow_network: bool = True) -> dict:
    return _geocode.geocode(address, allow_network=allow_network).as_dict()


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
    recon, name = _reconstruction()
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
# The intake half of reconstruction: enough analysis that the upload screen
# tells the truth about the files, and a clean hand-off point into the real
# pipeline when this machine can run one.
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
    """Variance of the Laplacian: low means soft or out of focus.

    Computed with numpy rather than OpenCV. This used to `import cv2`, which is
    not a project dependency, so every score came back None and the upload screen
    silently never flagged a blurry photo. The same four-neighbour kernel as
    `reconstruction.pipeline.image_quality`, so the web app and the CLI agree
    about which frames are worth reconstructing.
    """
    try:
        import numpy as np
        from PIL import Image, ImageOps

        with Image.open(path) as original:
            original.load()
            grey = ImageOps.exif_transpose(original).convert("L")
            grey.thumbnail((1024, 1024))
            pixels = np.asarray(grey, dtype=np.float64)
        if min(pixels.shape) < 3:
            return None
        centre = pixels[1:-1, 1:-1]
        laplacian = (pixels[:-2, 1:-1] + pixels[2:, 1:-1]
                     + pixels[1:-1, :-2] + pixels[1:-1, 2:] - 4 * centre)
        return float(laplacian.var())
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
    recon, name = _reconstruction()
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
    """Hand off to the reconstruction module. Returns None when it is not wired
    on this machine, which the caller reports as 'falling back to the prepared
    asset'. See docs/INTEGRATION.md for why that is the normal case."""
    recon, name = _reconstruction()
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
