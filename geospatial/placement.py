"""Dependency-free placement math. Distances are meters; angles are degrees."""

import math
import re


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
    if result.get("anchor") != "ground-center" or result.get("upAxis") != "Y":
        raise ValueError("Only ground-center anchors and Y-up models are supported")
    result["headingDegrees"] %= 360
    return {key: result[key] for key in (
        "latitude", "longitude", "elevationMeters", "headingDegrees",
        "metersPerModelUnit", "verticalOffsetMeters", "anchor", "upAxis",
    )}


def geocode_prepared(address):
    """Resolve only the prepared venue. Never silently place an unknown address there."""
    normalized = " ".join(address.casefold().strip().split())
    aliases = {
        "metlife stadium", "new york new jersey stadium",
        "1 metlife stadium drive, east rutherford, nj 07073",
    }
    if normalized not in aliases:
        raise ValueError("No prepared match. Supply reviewed latitude/longitude or connect a geocoder.")
    return {
        "latitude": 40.8135, "longitude": -74.0745,
        "source": "AGENTS.md prepared demo coordinates; approximate, requires review",
    }


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


def local_to_enu(point, bounds_min, bounds_max, transform):
    """Ground-center a Y-up point, then return east/north/up meters at the anchor.

    Heading 0 points model -Z north, +X east. Positive headings rotate clockwise.
    Up includes vertical offset, not absolute terrain elevation.
    """
    state = validate_transform(transform)
    for label, vector in (("point", point), ("bounds_min", bounds_min), ("bounds_max", bounds_max)):
        if len(vector) != 3:
            raise ValueError(f"{label} must contain three coordinates")
        for component in vector:
            number(component, label)
    if any(lo > hi for lo, hi in zip(bounds_min, bounds_max)):
        raise ValueError("Bounds minimum exceeds maximum")
    scale = state["metersPerModelUnit"]
    x = (point[0] - (bounds_min[0] + bounds_max[0]) / 2) * scale
    y = (point[1] - bounds_min[1]) * scale
    z = (point[2] - (bounds_min[2] + bounds_max[2]) / 2) * scale
    angle = math.radians(state["headingDegrees"])
    return (
        x * math.cos(angle) - z * math.sin(angle),
        -x * math.sin(angle) - z * math.cos(angle),
        y + state["verticalOffsetMeters"],
    )
