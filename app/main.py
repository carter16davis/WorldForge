"""WorldForge demo server.

Serves the viewer and the small API behind it. Deliberately zero-build: the
front end is plain ES modules with three.js and MapLibre vendored under
`web/vendor/`, so a clean checkout runs with `uvicorn app.main:app` and nothing
else. No Node toolchain to fail at 3am, and the whole thing works offline apart
from basemap tiles.

    .venv/bin/uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import geohash, pipeline
from app.assets import ASSET_DIR
from app.contracts import Placement, validate_document, validate_placement
from app.demo_data import DEMO_ASSET_ID, demo_cells, demo_coverage, ensure_demo_asset
from app.export import EXPORT_ROOT, package, world_uri, zip_package
from app.geocode import VENUES
from reconstruction import jobs
from reconstruction.engine import engine_status, select_engine

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worldforge")

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
UPLOAD_ROOT = REPO / "uploads"
JOBS = jobs.JobStore(UPLOAD_ROOT / "jobs")

MAX_UPLOAD_BYTES = 80 * 1024 * 1024
MAX_UPLOAD_FILES = 60


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Building the prepared asset at startup means the demo is ready the moment
    # the page loads, rather than on first click in front of judges.
    try:
        ensure_demo_asset()
        log.info("prepared demo asset ready: %s", DEMO_ASSET_ID)
    except Exception as exc:
        log.error("could not prepare the demo asset: %s", exc)
    yield


app = FastAPI(title="WorldForge", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class GeocodeRequest(BaseModel):
    address: str = ""
    allowNetwork: bool = True


class PlaceRequest(BaseModel):
    assetId: str = DEMO_ASSET_ID
    address: str = ""
    latitude: float | None = Field(default=None, ge=-85, le=85, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    transform: dict[str, Any] = Field(default_factory=dict)


class ValidateRequest(BaseModel):
    placement: dict[str, Any]


class ExportRequest(BaseModel):
    placement: dict[str, Any]
    era: str = "2026"


class ReconstructRequest(BaseModel):
    batchId: str
    address: str = ""
    latitude: float | None = Field(default=None, ge=-85, le=85, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    referenceMeters: float | None = Field(default=None, gt=0, le=2000, allow_inf_nan=False)
    licenseNotes: str = ""


# ---------------------------------------------------------------------------
# Asset bundles
# ---------------------------------------------------------------------------

def _bundle(placement: Placement) -> dict:
    """Everything the viewer needs for one asset, in one round trip."""
    asset_dir = ASSET_DIR / placement.assetId
    provenance_file = asset_dir / "provenance.json"
    coverage = pipeline.coverage_for(placement, demo_coverage(placement))

    return {
        "placement": placement.model_dump(),
        "coverage": coverage,
        "cells": demo_cells(placement),
        "provenance": json.loads(provenance_file.read_text()) if provenance_file.exists() else {},
        "problems": validate_placement(placement),
        "modelUrl": f"/assets/{placement.assetId}/building.glb",
        "thumbnailUrl": f"/assets/{placement.assetId}/thumbnail.webp",
        "worldforgeUri": world_uri(placement),
    }


def _load_placement(raw: dict[str, Any]) -> Placement:
    try:
        return validate_document(raw)
    except Exception as exc:
        raise HTTPException(422, f"Placement did not validate: {exc}") from exc


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "assetReady": (ASSET_DIR / DEMO_ASSET_ID / "building.glb").exists()}


@app.get("/api/session")
def session() -> dict:
    """One call on page load: what is wired, the prepared asset, the venue list."""
    placement = ensure_demo_asset()
    return {
        "capabilities": pipeline.capabilities(),
        "asset": _bundle(placement),
        "venues": [
            {k: v[k] for k in ("key", "name", "venue", "address", "lat", "lon", "country", "capacity")}
            for v in VENUES
        ],
        "demoAssetId": DEMO_ASSET_ID,
    }


@app.post("/api/geocode")
def geocode(req: GeocodeRequest) -> dict:
    return pipeline.geocode_address(req.address, allow_network=req.allowNetwork)


@app.post("/api/place")
def place(req: PlaceRequest) -> dict:
    """Resolve an address (or explicit coordinates) and move the asset there."""
    placement = ensure_demo_asset()
    if req.assetId != placement.assetId:
        raise HTTPException(404, f"Unknown asset {req.assetId!r}")

    if (req.latitude is None) != (req.longitude is None):
        raise HTTPException(422, "Supply both latitude and longitude.")

    if req.latitude is not None and req.longitude is not None:
        lat, lon = req.latitude, req.longitude
        resolved = {"latitude": lat, "longitude": lon, "source": "map-pin",
                    "confidence": 1.0, "displayName": req.address,
                    "note": "Placed directly on the map.",
                    "cell": geohash.encode(lat, lon, 8)}
    else:
        resolved = pipeline.geocode_address(req.address)
        if resolved["confidence"] <= 0:
            return {"resolved": resolved, "asset": None,
                    "error": resolved.get("note") or "Address did not resolve."}
        lat, lon = resolved["latitude"], resolved["longitude"]

    moved = pipeline.relocate(placement, lat, lon, req.address or resolved.get("displayName", ""))
    try:
        if req.transform:
            moved = pipeline.apply_transform(moved, req.transform)
        moved = validate_document(moved.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"resolved": resolved, "asset": _bundle(moved), "error": None}


@app.post("/api/validate")
def validate(req: ValidateRequest) -> dict:
    placement = _load_placement(req.placement)
    problems = validate_placement(placement)
    return {
        "problems": problems,
        "valid": not problems,
        "placement": placement.model_dump(),
        "worldforgeUri": world_uri(placement),
    }


@app.get("/api/cells")
def cells(lat: float, lon: float, precision: int = 8) -> dict:
    """The World Cell containing a point, plus its ring — for the map overlay."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(422, "Coordinates out of range.")
    precision = max(1, min(12, precision))
    cell = geohash.encode(lat, lon, precision)
    ring = geohash.neighbours(cell)
    return {
        "cell": cell,
        "polygon": geohash.cell_polygon(cell),
        "parents": [{"cell": c, "polygon": geohash.cell_polygon(c)} for c in geohash.parents(cell)],
        "neighbours": [
            {"direction": d, "cell": c, "polygon": geohash.cell_polygon(c)}
            for d, c in ring.items()
        ],
    }


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(default=[]),
                 address: str = Form(default="")) -> dict:
    """Intake for photos/video.

    Always returns a report. If a reconstruction module is wired on this
    machine it runs; otherwise the response says plainly that the prepared asset
    is being used, so the demo keeps moving either way.
    """
    if not files:
        raise HTTPException(422, "No files received.")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(413, f"Too many files (limit {MAX_UPLOAD_FILES}).")

    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    batch = Path(tempfile.mkdtemp(prefix="batch-", dir=UPLOAD_ROOT))
    photos = batch / "photos"
    photos.mkdir()
    saved: list[Path] = []
    total = 0
    try:
        for f in files:
            # Flatten any path the browser sent; only the basename is ours to trust.
            dest = photos / Path(f.filename or "upload.bin").name
            with dest.open("wb") as out:
                while chunk := await f.read(1 << 20):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "Upload exceeds 80 MB.")
                    out.write(chunk)
            saved.append(dest)

        report = pipeline.analyse_media(saved)
        report["batchId"] = batch.name
        report["fingerprint"] = pipeline.media_fingerprint(saved)

        # The batch is kept on disk: /api/reconstruct runs against it next, and
        # a reconstruction has to be able to re-read the originals. It used to be
        # deleted here, which is why nothing could ever be reconstructed.
        report["asset"] = None
        report["engines"] = engine_status()
        report["canReconstruct"] = any(e["available"] for e in report["engines"])
        return report
    except Exception:
        shutil.rmtree(batch, ignore_errors=True)
        raise


@app.get("/api/engines")
def engines() -> dict:
    """Which reconstruction backends can run here, and why the others cannot."""
    status = engine_status()
    return {"engines": status, "canReconstruct": any(e["available"] for e in status)}


@app.post("/api/reconstruct")
def reconstruct(req: ReconstructRequest) -> dict:
    """Start a reconstruction job over an uploaded batch. Returns a job id."""
    if not re.fullmatch(r"batch-[A-Za-z0-9_]+", req.batchId):
        raise HTTPException(400, "Invalid batch id.")
    photos = UPLOAD_ROOT / req.batchId / "photos"
    if not photos.is_dir():
        raise HTTPException(404, "That upload batch is gone. Upload the photos again.")

    if (req.latitude is None) != (req.longitude is None):
        raise HTTPException(422, "Supply both latitude and longitude, or neither.")
    if req.latitude is None and not req.address.strip():
        raise HTTPException(422, "An address or a latitude/longitude is required — "
                                 "a reconstruction with no location is not map-ready.")

    engine = select_engine()
    if engine is None:
        raise HTTPException(
            503,
            "No reconstruction engine is available on this machine. "
            + " ".join(e["detail"] for e in engine_status() if not e["available"]))

    asset_id = f"scan-{req.batchId.removeprefix('batch-')[:12].lower()}"
    job = JOBS.create(address=req.address, assetId=asset_id,
                      photoCount=sum(1 for _ in photos.iterdir()))
    jobs.start(JOBS, job.id, photos_dir=photos, address=req.address,
               asset_root=ASSET_DIR, asset_id=asset_id, engine=engine,
               latitude=req.latitude, longitude=req.longitude,
               reference_meters=req.referenceMeters, license_notes=req.licenseNotes)
    return {"jobId": job.id, "assetId": asset_id, "engine": engine.name}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    """Poll a reconstruction. When it is done, the asset bundle comes with it."""
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(400, "Invalid job id.")
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job.")
    payload = job.as_dict()
    if job.status == "done":
        placement_file = ASSET_DIR / job.assetId / "placement.json"
        if placement_file.exists():
            payload["asset"] = _bundle(validate_document(json.loads(placement_file.read_text())))
    return payload


@app.get("/api/jobs")
def job_list() -> dict:
    return {"jobs": JOBS.list()}


@app.post("/api/export")
def export(req: ExportRequest) -> dict:
    placement = _load_placement(req.placement)
    if req.era not in ("2026", "2426"):
        raise HTTPException(422, "era must be '2026' or '2426'.")

    asset_dir = ASSET_DIR / placement.assetId
    provenance_file = asset_dir / "provenance.json"
    provenance = json.loads(provenance_file.read_text()) if provenance_file.exists() else {}
    coverage = pipeline.coverage_for(placement, demo_coverage(placement))

    try:
        manifest = package(placement, provenance=provenance, coverage=coverage,
                           era=req.era, source_dir=asset_dir)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    manifest["downloadUrl"] = f"/api/export/{placement.assetId}/download?export_id={manifest['exportId']}"
    return manifest


@app.get("/api/export/{asset_id}/download")
def download(asset_id: str, export_id: str):
    # Reject anything that could escape the export root.
    if not asset_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "Invalid asset id.")
    if not re.fullmatch(r"[a-f0-9]{32}", export_id):
        raise HTTPException(400, "Invalid export id.")
    try:
        blob = zip_package(asset_id, export_root=EXPORT_ROOT / export_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{asset_id}.zip"'},
    )


@app.exception_handler(Exception)
async def unhandled(request, exc: Exception):
    # The UI shows `detail` verbatim, so it says what broke without quoting the
    # exception: a stack trace on a projector leaks filesystem paths, and an
    # exception string is not a sentence a judge can act on. The real error goes
    # to the server log, where whoever is running the demo can read it.
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong on the WorldForge server. "
                           "The details are in the server log."},
    )


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


ASSET_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=ASSET_DIR), name="assets")
app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
