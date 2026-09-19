"""Dependency-free placement math and the coordinate conventions it enforces.

Distances are meters; angles are degrees clockwise from true north.

This module is the one place that knows what an *anchor* and an *up axis* mean.
Two conventions are recognised, because two producers exist:

    Y-up / ground-center   glTF's own frame: +X east, +Y up, -Z north, with the
                           model origin at the horizontal centre of the footprint
                           and the ground plane at the mesh's minimum Y. This is
                           what WorldForge writes, and what `building.glb` always
                           is by the time it reaches an export.

    Z-up / ground-origin   ENU as photogrammetry and GIS tools emit it: +X east,
                           +Y north, +Z up, with the origin wherever the tool put
                           it. RealityScan and the procedura reconstruction shape
                           arrive this way.

Declaring a convention is not the same as converting one. `validate_transform`
accepts both so that an honest producer is never forced to lie about its frame;
`app.contracts.to_canonical_transform` does the conversion, and
`app.assets.import_mesh` rotates the geometry to match.
"""

import math
import re

ANCHORS = ("ground-center", "ground-origin")
UP_AXES = ("Y", "Z")


def number(value, name, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} is out of range")
    return value


def validate_transform(value):
    """Accept the flat placement shape returned by the UI; return a normalized copy."""
    result = dict(value)
    for key, low, high in (
        ("latitude", -90, 90), ("longitude", -180, 180),
        ("elevationMeters", None, None), ("headingDegrees", None, None),
        ("metersPerModelUnit", None, None), ("verticalOffsetMeters", None, None),
    ):
        number(result.get(key), key, low, high)
    if result["metersPerModelUnit"] <= 0:
        raise ValueError("metersPerModelUnit must be positive")
    if result.get("anchor") not in ANCHORS:
        raise ValueError(f"anchor must be one of {', '.join(ANCHORS)}")
    if result.get("upAxis") not in UP_AXES:
        raise ValueError(f"upAxis must be one of {', '.join(UP_AXES)}")
    result["headingDegrees"] %= 360
    return {key: result[key] for key in (
        "latitude", "longitude", "elevationMeters", "headingDegrees",
        "metersPerModelUnit", "verticalOffsetMeters", "anchor", "upAxis",
    )}


def build_placement(asset_id, name, address, transform, *, has_lod=False):
    if not isinstance(asset_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", asset_id):
        raise ValueError("assetId must contain only letters, digits, hyphens, and underscores")
    for label, value in (("name", name), ("sourceAddress", address)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be nonempty")
    state = validate_transform(transform)
    return {
        "schemaVersion": 1, "assetId": asset_id, "name": name, "sourceAddress": address,
        "location": {key: state[key] for key in ("latitude", "longitude", "elevationMeters")},
        "transform": {key: state[key] for key in (
            "headingDegrees", "metersPerModelUnit", "verticalOffsetMeters", "anchor", "upAxis",
        )},
        "models": {"high": "building.glb", **({"low": "building-lod.glb"} if has_lod else {})},
    }


def meters_per_degree(lat_deg):
    """Meters per degree of latitude and of longitude at this latitude.

    The standard WGS84 series expansion. Accurate to well under a meter over the
    span of a building, which is the only distance this project measures over.
    """
    phi = math.radians(lat_deg)
    m_lat = (111132.92 - 559.82 * math.cos(2 * phi)
             + 1.175 * math.cos(4 * phi) - 0.0023 * math.cos(6 * phi))
    m_lon = (111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi)
             + 0.118 * math.cos(5 * phi))
    return m_lat, m_lon


def enu_offset(point, origin):
    """(lat, lon) -> (east, north) meters relative to `origin`."""
    m_lat, m_lon = meters_per_degree(origin[0])
    return (point[1] - origin[1]) * m_lon, (point[0] - origin[0]) * m_lat


def latlon_from_enu(east, north, origin):
    """(east, north) meters relative to `origin` -> (lat, lon)."""
    m_lat, m_lon = meters_per_degree(origin[0])
    return origin[0] + north / m_lat, origin[1] + east / m_lon


def model_to_local_enu(point, bounds_min, bounds_max, anchor, up_axis):
    """One model-space point -> unrotated, unscaled (east, north, up) about the anchor.

    This is the whole of what `anchor` and `upAxis` mean, in nine lines. Heading
    and scale are deliberately not applied here; `local_to_enu` layers those on.
    """
    if up_axis == "Y":
        # glTF: +X east, +Y up, -Z north.
        east, north, up = point[0], -point[2], point[1]
        low_e, low_n, low_u = bounds_min[0], -bounds_max[2], bounds_min[1]
        high_e, high_n = bounds_max[0], -bounds_min[2]
    else:
        # ENU: +X east, +Y north, +Z up.
        east, north, up = point[0], point[1], point[2]
        low_e, low_n, low_u = bounds_min[0], bounds_min[1], bounds_min[2]
        high_e, high_n = bounds_max[0], bounds_max[1]

    if anchor == "ground-center":
        east -= (low_e + high_e) / 2
        north -= (low_n + high_n) / 2
        up -= low_u
    return east, north, up


def local_to_enu(point, bounds_min, bounds_max, transform):
    """Anchor a model-space point, then return east/north/up meters at the anchor.

    Heading 0 leaves the model's own north pointing at true north. Positive
    headings rotate the building clockwise seen from above. Up includes the
    vertical offset, not absolute terrain elevation.
    """
    state = validate_transform(transform)
    for label, vector in (("point", point), ("bounds_min", bounds_min), ("bounds_max", bounds_max)):
        if len(vector) != 3:
            raise ValueError(f"{label} must contain three coordinates")
        for component in vector:
            number(component, label)
    if any(lo > hi for lo, hi in zip(bounds_min, bounds_max)):
        raise ValueError("Bounds minimum exceeds maximum")

    east, north, up = model_to_local_enu(
        point, bounds_min, bounds_max, state["anchor"], state["upAxis"]
    )
    scale = state["metersPerModelUnit"]
    east, north, up = east * scale, north * scale, up * scale
    angle = math.radians(state["headingDegrees"])
    return (
        east * math.cos(angle) + north * math.sin(angle),
        -east * math.sin(angle) + north * math.cos(angle),
        up + state["verticalOffsetMeters"],
    )
