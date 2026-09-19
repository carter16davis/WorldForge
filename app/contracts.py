"""The frozen interchange shapes, plus a normaliser that accepts what each
producer actually emits.

AGENTS.md froze `placement.json` (schemaVersion 1). The reconstruction side
emits a different shape (`procedura.building/0.1`) in an ENU frame. Both are
accepted and normalised inward; everything downstream of `normalize_placement`
sees one shape.

A non-canonical frame (Z-up, or a ground-origin anchor) is *recorded*, not
rejected. Refusing it would only teach a producer to declare Y-up and ship a
Z-up mesh, which is the failure this field exists to prevent. Conversion happens
where the geometry is: `reconstruction.anchor` for a real scan,
`app.assets.import_mesh` for a mesh loaded into the viewer.

UI state never leaks into these models. The editor hands corrections back as a
plain `Transform`, exactly the shape the validator already expects.
"""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import geohash
from geospatial import build_placement

SCHEMA_VERSION = 1
Era = Literal["2026", "2426"]


def _slug(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return out or "asset"


class Location(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    elevationMeters: float = 0.0


class SpatialIndex(BaseModel):
    system: Literal["geohash"] = "geohash"
    precision: int = Field(default=8, ge=1, le=12)
    cell: str
    parentCells: list[str] = Field(default_factory=list)
    neighbors: list[str] = Field(default_factory=list)


class Transform(BaseModel):
    """The only shape the UI sends back. The packager consumes it as-is."""

    headingDegrees: float = 0.0
    metersPerModelUnit: float = Field(default=1.0, gt=0)
    verticalOffsetMeters: float = 0.0
    anchor: Literal["ground-center", "ground-origin"] = "ground-center"
    upAxis: Literal["Y", "Z"] = "Y"

    @field_validator("headingDegrees")
    @classmethod
    def _wrap(cls, v: float) -> float:
        # The heading dial is circular; 370 and -350 are the same bearing.
        return v % 360.0

    @property
    def is_canonical(self) -> bool:
        """True when the GLB is already in the frame every consumer expects."""
        return self.anchor == "ground-center" and self.upAxis == "Y"


class Models(BaseModel):
    high: str = "building.glb"
    low: str | None = None


class Dimensions(BaseModel):
    model_config = ConfigDict(extra="allow")

    widthMeters: float | None = None
    lengthMeters: float | None = None
    heightMeters: float | None = None
    floors: int | None = None
    footprintAreaMeters2: float | None = None


class Appearance(BaseModel):
    model_config = ConfigDict(extra="allow")

    roof: str = "flat"
    material: str = "brick"
    facadeColor: str = "#8d6551"


class Placement(BaseModel):
    """placement.json. Optional extension fields (footprint, dimensions,
    appearance) let the viewer draw a building before a GLB exists; a consumer
    that ignores them still gets a valid map-ready placement."""

    model_config = ConfigDict(extra="allow")

    schemaVersion: int = SCHEMA_VERSION
    assetId: str
    name: str = ""
    sourceAddress: str = ""
    location: Location
    spatialIndex: SpatialIndex | None = None
    transform: Transform = Field(default_factory=Transform)
    models: Models = Field(default_factory=Models)

    # --- extensions (optional everywhere) ---
    footprint: list[tuple[float, float]] = Field(default_factory=list)  # [lat, lon]
    dimensions: Dimensions = Field(default_factory=Dimensions)
    appearance: Appearance = Field(default_factory=Appearance)

    def with_index(self, precision: int = 8) -> "Placement":
        """Re-derive the spatial index from the current coordinates.

        Always called before export: the index must follow the coordinates, or
        a user nudging the building across a cell edge silently ships a stale
        cell. See the testing checklist in AGENTS.md.
        """
        block = geohash.index(self.location.latitude, self.location.longitude, precision)
        return self.model_copy(update={"spatialIndex": SpatialIndex(**block)})


class SourceMedia(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str
    kind: Literal["photo", "video", "frame", "synthetic"] = "photo"
    capturedAt: str | None = None
    license: str = ""


class Provenance(BaseModel):
    model_config = ConfigDict(extra="allow")

    schemaVersion: int = SCHEMA_VERSION
    sourceMedia: list[SourceMedia] = Field(default_factory=list)
    reconstructionTool: str = ""
    observedCoveragePercent: float | None = None
    syntheticCoveragePercent: float = 0.0
    syntheticUse: Literal["none", "texture-completion", "view-synthesis"] = "none"
    licenseNotes: str = ""
    generatedAt: str = ""


class FacadeCoverage(BaseModel):
    """One side of the building, as judged by the coverage agent."""

    model_config = ConfigDict(extra="allow")

    name: str                                     # "north", "wall:3", ...
    headingDegrees: float = 0.0                   # outward normal, deg from north
    status: Literal["uncaptured", "partial", "verified", "synthetic"] = "uncaptured"
    observationCount: int = 0
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    note: str = ""


class CoverageReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    schemaVersion: int = SCHEMA_VERSION
    assetId: str = ""
    overallConfidence: float = Field(default=0.0, ge=0.0, le=1.0)
    facades: list[FacadeCoverage] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class CellCoverage(BaseModel):
    """A World Cell on the coverage heatmap."""

    cell: str
    status: Literal["uncaptured", "partial", "verified", "synthetic"] = "uncaptured"
    assetCount: int = 0
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _from_procedura_v0(raw: dict[str, Any]) -> dict[str, Any]:
    """`procedura.building/0.1` (the ENU reconstruction shape) -> canonical."""
    anchor = raw.get("anchor") or {}
    frame = raw.get("frame") or {}
    dims = raw.get("dimensions") or {}
    look = raw.get("appearance") or {}
    address = raw.get("address", "")

    # footprint_wgs84 is [lat, lon] about the anchor, already absolute.
    footprint = [(float(a), float(b)) for a, b in raw.get("footprint_wgs84") or []]

    height = dims.get("ridge_height_m") or dims.get("eave_height_m")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "assetId": raw.get("assetId") or _slug(address)[:48] or "asset",
        "name": raw.get("name") or address,
        "sourceAddress": address,
        "location": {
            "latitude": anchor.get("lat", 0.0),
            "longitude": anchor.get("lon", 0.0),
            "elevationMeters": anchor.get("ground_alt_m", 0.0),
        },
        "transform": {
            "headingDegrees": frame.get("yaw_deg_from_true_north", 0.0),
            "metersPerModelUnit": 1.0,          # the ENU mesh is already in metres
            "verticalOffsetMeters": 0.0,
            # the mesh origin is the geocoded anchor, not the footprint centre
            "anchor": "ground-origin",
            # ENU is +Z up; the viewer rotates on import
            "upAxis": "Z" if frame.get("z") == "up" else "Y",
        },
        "footprint": footprint,
        "dimensions": {
            "floors": dims.get("floors"),
            "heightMeters": height,
            "footprintAreaMeters2": dims.get("footprint_area_m2"),
        },
        "appearance": {
            "roof": look.get("roof", "flat"),
            "material": look.get("material", "brick"),
            "facadeColor": look.get("facade_color", "#8d6551"),
        },
    }


def normalize_placement(raw: dict[str, Any]) -> Placement:
    """Accept any producer's placement dialect; return the canonical model.

    Unknown-but-harmless keys survive (`extra="allow"`), so a field one of us
    adds mid-hackathon does not get silently dropped on a round trip.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"placement must be a JSON object, got {type(raw).__name__}")

    schema = str(raw.get("schema", ""))
    if schema.startswith("procedura.building/"):
        raw = _from_procedura_v0(raw)
    elif "anchor" in raw and "location" not in raw:
        # same shape, schema tag missing or renamed
        raw = _from_procedura_v0(raw)

    placement = Placement.model_validate(raw)
    if placement.spatialIndex is None:
        placement = placement.with_index()
    return placement


def validate_document(raw: dict[str, Any]) -> Placement:
    """Normalise, then gate on the shared contract before anything is written.

    `build_placement` is the schema gate from `geospatial`: it enforces the
    assetId charset (so an assetId can never become a path), the finite-number
    and range rules, and the recognised anchor/up-axis vocabulary. It is called
    for its exceptions; the pydantic model is what we return, because it carries
    the extension fields the viewer needs and `build_placement` does not.
    """
    placement = normalize_placement(raw)
    if placement.schemaVersion != SCHEMA_VERSION:
        raise ValueError(f"Only placement schema version {SCHEMA_VERSION} is supported")
    build_placement(
        placement.assetId, placement.name, placement.sourceAddress,
        {**placement.location.model_dump(), **placement.transform.model_dump()},
    )
    return placement.with_index(
        placement.spatialIndex.precision if placement.spatialIndex else 8
    )


# ---------------------------------------------------------------------------
# Validation — problems a judge would notice, phrased for the UI
# ---------------------------------------------------------------------------

def validate_placement(p: Placement) -> list[str]:
    """Human-readable problems, empty when the package is demo-ready.

    Mirrors the AGENTS.md testing checklist. These are warnings surfaced in the
    UI, not exceptions: a half-finished placement should still render so the
    user can see what is wrong and fix it with the editor.
    """
    problems: list[str] = []

    if p.location.latitude == 0 and p.location.longitude == 0:
        problems.append("Location is null island (0, 0) — the address never resolved.")

    if p.spatialIndex:
        expected = geohash.encode(
            p.location.latitude, p.location.longitude, p.spatialIndex.precision
        )
        if expected != p.spatialIndex.cell:
            problems.append(
                f"World Cell {p.spatialIndex.cell} does not match the coordinates "
                f"(expected {expected}). Re-derive the index before export."
            )
        if p.spatialIndex.precision != len(p.spatialIndex.cell):
            problems.append(
                f"Declared precision {p.spatialIndex.precision} does not match "
                f"cell length {len(p.spatialIndex.cell)}."
            )
    else:
        problems.append("No spatial index — the asset has no World Cell identity.")

    if not 0 <= p.transform.headingDegrees < 360:
        problems.append("Heading must be within [0, 360).")

    if p.transform.metersPerModelUnit <= 0:
        problems.append("metersPerModelUnit must be positive.")

    if not p.transform.is_canonical:
        # A warning, not a rejection. The package is still valid and still says
        # truthfully what frame its mesh is in; it just needs converting before
        # a Y-up engine loads it.
        problems.append(
            f"Model frame is {p.transform.upAxis}-up / {p.transform.anchor}, not the "
            f"canonical Y-up / ground-center. Run it through reconstruction.anchor "
            f"before handing the GLB to an engine that assumes glTF conventions."
        )

    h = p.dimensions.heightMeters
    if h is not None:
        if h <= 0:
            problems.append("Building height must be positive.")
        elif h > 900:
            problems.append(f"Building height {h:.0f} m exceeds the tallest building — check scale.")

    area = p.dimensions.footprintAreaMeters2
    if area is not None and area <= 0:
        problems.append("Footprint area must be positive.")

    if p.footprint:
        if len(p.footprint) < 3:
            problems.append("Footprint needs at least three vertices.")
        else:
            span = max(
                _haversine_m(p.footprint[0], v) for v in p.footprint
            )
            if span > 2000:
                problems.append(
                    f"Footprint spans {span / 1000:.1f} km — that is a neighbourhood, not a building."
                )
            centre_offset = _haversine_m(
                _centroid(p.footprint), (p.location.latitude, p.location.longitude)
            )
            if p.transform.anchor == "ground-center" and centre_offset > 50:
                problems.append(
                    f"Anchor is declared ground-center but the footprint centre is "
                    f"{centre_offset:.0f} m away from the placement coordinate."
                )

    if abs(p.transform.verticalOffsetMeters) > 200:
        problems.append("Vertical offset is beyond ±200 m — the model will float or sink.")

    return problems


def _centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    n = len(points)
    return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371008.8
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))
