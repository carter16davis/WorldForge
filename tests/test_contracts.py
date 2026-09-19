"""The normaliser is the integration seam — if it drifts, the viewer and my
teammates' modules stop agreeing about what a placement is."""

import json

import pytest

from app import geohash
from app.contracts import (
    Placement,
    Transform,
    normalize_placement,
    validate_placement,
)

CANONICAL = {
    "schemaVersion": 1,
    "assetId": "venue-metlife-001",
    "name": "New York New Jersey Stadium",
    "sourceAddress": "1 MetLife Stadium Dr, East Rutherford, NJ 07073",
    "location": {"latitude": 40.8135, "longitude": -74.0745, "elevationMeters": 2.4},
    "transform": {"headingDegrees": 87, "metersPerModelUnit": 1,
                  "verticalOffsetMeters": 0, "anchor": "ground-center", "upAxis": "Y"},
    "models": {"high": "building.glb"},
}

# The shape Person 1's branch actually writes.
PROCEDURA_V0 = {
    "schema": "procedura.building/0.1",
    "address": "1200 N St, Lincoln, NE",
    "anchor": {"lat": 40.81363, "lon": -96.7043, "ground_alt_m": 1.5},
    "frame": {"convention": "ENU", "x": "east", "y": "north", "z": "up",
              "units": "metres", "yaw_deg_from_true_north": 12.0},
    "footprint_wgs84": [[40.81363, -96.7043], [40.81363, -96.70396],
                        [40.81377, -96.70396], [40.81377, -96.7043]],
    "dimensions": {"floors": 4, "eave_height_m": 15.5, "ridge_height_m": 17.0,
                   "footprint_area_m2": 616.0},
    "appearance": {"roof": "flat", "material": "red_brick", "facade_color": "#8d4b3a"},
    "provenance": {"footprint": "osm/overpass", "overall_confidence": 0.78},
}


def test_canonical_placement_round_trips():
    p = normalize_placement(CANONICAL)
    assert p.assetId == "venue-metlife-001"
    assert p.transform.headingDegrees == 87
    assert p.location.elevationMeters == 2.4


def test_missing_spatial_index_is_derived_not_rejected():
    p = normalize_placement(CANONICAL)
    assert p.spatialIndex is not None
    assert p.spatialIndex.cell == geohash.encode(40.8135, -74.0745, 8)


def test_procedura_dialect_is_normalised():
    p = normalize_placement(PROCEDURA_V0)
    assert p.location.latitude == pytest.approx(40.81363)
    assert p.location.elevationMeters == pytest.approx(1.5)
    assert p.transform.headingDegrees == pytest.approx(12.0)
    assert p.transform.upAxis == "Z"          # ENU is +Z up
    assert p.transform.anchor == "ground-origin"
    assert p.sourceAddress == "1200 N St, Lincoln, NE"
    assert p.dimensions.heightMeters == pytest.approx(17.0)   # ridge, not eave
    assert p.appearance.facadeColor == "#8d4b3a"
    assert len(p.footprint) == 4


def test_procedura_dialect_is_detected_without_its_schema_tag():
    raw = {k: v for k, v in PROCEDURA_V0.items() if k != "schema"}
    assert normalize_placement(raw).location.latitude == pytest.approx(40.81363)


def test_unknown_fields_survive_a_round_trip():
    raw = dict(CANONICAL, teammateField={"weird": True})
    assert normalize_placement(raw).model_dump()["teammateField"] == {"weird": True}


def test_a_non_object_is_rejected_clearly():
    with pytest.raises(ValueError, match="must be a JSON object"):
        normalize_placement([1, 2, 3])


@pytest.mark.parametrize("given,expected", [(370, 10), (-350, 10), (0, 0), (359.5, 359.5)])
def test_heading_wraps_into_a_single_turn(given, expected):
    assert Transform(headingDegrees=given).headingDegrees == pytest.approx(expected)


def test_non_positive_scale_is_rejected():
    with pytest.raises(ValueError):
        Transform(metersPerModelUnit=0)


def test_out_of_range_coordinates_are_rejected():
    with pytest.raises(ValueError):
        normalize_placement(dict(CANONICAL, location={"latitude": 91, "longitude": 0}))


# ─────────────────────────── validation ───────────────────────────

def test_a_good_placement_has_no_problems():
    assert validate_placement(normalize_placement(CANONICAL)) == []


def test_a_stale_cell_is_caught():
    """The check that matters most: the user nudges the building across a cell
    boundary and the index has to follow, or the export ships a wrong identity."""
    p = normalize_placement(CANONICAL)
    moved = p.model_copy(update={
        "location": p.location.model_copy(update={"latitude": 41.0}),
    })
    problems = validate_placement(moved)
    assert any("does not match the coordinates" in msg for msg in problems)
    assert validate_placement(moved.with_index()) == []


def test_precision_and_cell_length_must_agree():
    p = normalize_placement(CANONICAL)
    p.spatialIndex.precision = 6
    assert any("precision" in msg for msg in validate_placement(p))


def test_null_island_is_called_out():
    p = normalize_placement(dict(CANONICAL, location={"latitude": 0, "longitude": 0}))
    assert any("null island" in msg for msg in validate_placement(p))


def test_an_impossible_height_is_flagged():
    p = normalize_placement(dict(CANONICAL, dimensions={"heightMeters": 2000}))
    assert any("exceeds the tallest building" in msg for msg in validate_placement(p))


def test_a_footprint_the_size_of_a_neighbourhood_is_flagged():
    p = normalize_placement(dict(CANONICAL, footprint=[
        [40.80, -74.10], [40.80, -74.00], [40.86, -74.00], [40.86, -74.10],
    ]))
    assert any("neighbourhood" in msg for msg in validate_placement(p))


def test_ground_center_anchor_must_actually_sit_at_the_footprint_centre():
    far = [[41.0, -75.0], [41.0, -74.999], [41.001, -74.999], [41.001, -75.0]]
    p = normalize_placement(dict(CANONICAL, footprint=far))
    assert any("ground-center" in msg for msg in validate_placement(p))


def test_a_floating_model_is_flagged():
    p = normalize_placement(dict(CANONICAL, transform=dict(
        CANONICAL["transform"], verticalOffsetMeters=500)))
    assert any("float or sink" in msg for msg in validate_placement(p))


def test_validation_never_raises_on_a_half_finished_placement():
    """The UI renders problems; it must not crash on a partial placement."""
    p = Placement(assetId="x", location={"latitude": 1, "longitude": 1})
    assert isinstance(validate_placement(p), list)
