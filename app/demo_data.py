"""The prepared World Cup demo asset, and the World Cell coverage around it.

AGENTS.md: "The live demo must use a prepared reconstruction so it does not
depend on cloud processing finishing during judging." This module is that
preparation. `ensure_demo_asset()` is idempotent and takes about a second, so
a clean checkout is demo-ready without a build step.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import geohash
from app.assets import ASSET_DIR, DATA_DIR, build_asset, centroid_latlon
from app.contracts import CellCoverage, CoverageReport, Placement, normalize_placement

DEMO_ASSET_ID = "venue-metlife-001"

# MetLife Stadium's exterior is three seating tiers behind a louvered screen.
# Not measured by us — flagged as an estimate in provenance so nobody mistakes
# it for a survey figure.
DEMO_HEIGHT_M = 47.0
DEMO_ELEVATION_M = 2.4          # Meadowlands grade, approximate


def _footprint() -> dict:
    return json.loads((DATA_DIR / "metlife_footprint.json").read_text())


def _coverage_report(asset_id: str) -> dict:
    """A hand-authored stand-in for a real coverage agent.

    The four facades are the story the UI tells: two verified, one partial, one
    uncaptured with a concrete recapture instruction. When a real coverage
    agent lands, `pipeline.coverage_for` prefers it and this is never read.
    """
    return CoverageReport(
        assetId=asset_id,
        overallConfidence=0.62,
        facades=[
            {"name": "north", "headingDegrees": 0, "status": "verified",
             "observationCount": 14, "confidence": 0.91,
             "note": "Dense overlap from the north plaza."},
            {"name": "east", "headingDegrees": 90, "status": "verified",
             "observationCount": 11, "confidence": 0.84,
             "note": "Good coverage; mild sun flare on three frames."},
            {"name": "south", "headingDegrees": 180, "status": "partial",
             "observationCount": 4, "confidence": 0.41,
             "note": "Four usable frames, all from one standpoint — no parallax."},
            {"name": "west", "headingDegrees": 270, "status": "uncaptured",
             "observationCount": 0, "confidence": 0.0,
             "note": "No usable frames. Height and openings are inferred from symmetry."},
            {"name": "roof", "headingDegrees": 0, "status": "uncaptured",
             "observationCount": 0, "confidence": 0.0,
             "note": "No overhead capture. The roof is closed off from the footprint, not observed."},
        ],
        recommendations=[
            "The west facade is unobserved. Capture five overlapping photos while "
            "walking from the north corner toward the west corner.",
            "The south facade has no parallax. Re-shoot from two standpoints at "
            "least 30 m apart.",
        ],
    ).model_dump()


def _cell_coverage(centre_cell: str) -> list[dict]:
    """Coverage states for the demo cell and its eight neighbours.

    Synthetic, and labelled as such in the UI legend — it exists so the World
    Cells idea is visible rather than an internal implementation detail.
    """
    ring = geohash.neighbours(centre_cell)
    seeded = {
        "n": ("partial", 1, 0.45),
        "ne": ("uncaptured", 0, 0.0),
        "e": ("verified", 3, 0.88),
        "se": ("uncaptured", 0, 0.0),
        "s": ("partial", 2, 0.52),
        "sw": ("synthetic", 1, 0.30),
        "w": ("uncaptured", 0, 0.0),
        "nw": ("verified", 2, 0.79),
    }
    cells = [CellCoverage(cell=centre_cell, status="partial", assetCount=1, confidence=0.62)]
    for direction, cell in ring.items():
        status, count, conf = seeded[direction]
        cells.append(CellCoverage(cell=cell, status=status, assetCount=count, confidence=conf))
    return [c.model_dump() for c in cells]


def ensure_demo_asset(force: bool = False) -> Placement:
    """Build (or reuse) the prepared asset under `web/assets/<id>/`."""
    out_dir = ASSET_DIR / DEMO_ASSET_ID
    placement_file = out_dir / "placement.json"

    if placement_file.exists() and (out_dir / "building.glb").exists() and not force:
        return normalize_placement(json.loads(placement_file.read_text()))

    fp = _footprint()
    outer = [tuple(p) for p in fp["outer"]]
    holes = [[tuple(p) for p in h] for h in fp["holes"]]
    centre = centroid_latlon(outer)
    cell = geohash.encode(centre[0], centre[1], 8)

    built = build_asset(
        asset_id=DEMO_ASSET_ID,
        name="New York New Jersey Stadium",
        address="1 MetLife Stadium Dr, East Rutherford, NJ 07073",
        outer=outer,
        holes=holes,
        height_m=DEMO_HEIGHT_M,
        elevation_m=DEMO_ELEVATION_M,
        facade_colour="#9aa3ad",
        material="steel_louvre",
        provenance_notes={
            "footprintSource": fp["source"],
            "heightSource": "Estimated from three seating tiers — not measured.",
            "licenseNotes": (
                "Footprint © OpenStreetMap contributors, ODbL 1.0. "
                "Geometry is an extrusion of that footprint, not a photogrammetric "
                "reconstruction. No photographic source media."
            ),
            "observedCoveragePercent": 0.0,
            "syntheticUse": "none",
        },
        coverage=_coverage_report(DEMO_ASSET_ID),
        out_dir=out_dir,
    )

    (out_dir / "cells.json").write_text(
        json.dumps(_cell_coverage(cell), indent=2) + "\n"
    )
    return built.placement


def demo_cells(placement: Placement) -> list[dict]:
    cell = geohash.encode(placement.location.latitude, placement.location.longitude,
                          placement.spatialIndex.precision if placement.spatialIndex else 8)
    cells_file = ASSET_DIR / placement.assetId / "cells.json"
    if cells_file.exists():
        saved = json.loads(cells_file.read_text())
        if saved and saved[0]["cell"] == cell:
            return saved
    return _cell_coverage(cell)


def demo_coverage(placement: Placement) -> dict:
    f = ASSET_DIR / placement.assetId / "coverage.json"
    return json.loads(f.read_text()) if f.exists() else _coverage_report(placement.assetId)
