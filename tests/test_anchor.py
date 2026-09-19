"""Does the georeferencing actually recover the numbers photogrammetry cannot?

Each test builds a synthetic "scan" of a building whose real size, orientation
and position are known exactly, then checks that `anchor_model` recovers them
from the mesh plus an OpenStreetMap footprint. A scan here carries the things a
real one does: arbitrary units, an arbitrary origin, an arbitrary ground height,
a slab of sidewalk and a parked car.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest
import trimesh
from shapely.geometry import Polygon as ShapelyPolygon

from geospatial import latlon_from_enu
from reconstruction import anchor

ANCHOR_LAT, ANCHOR_LON = 40.8135, -74.0745

# The building, in the real world.
LONG_M, SHORT_M, HEIGHT_M = 40.0, 20.0, 15.0

# The scan's arbitrary local space.
METERS_PER_UNIT = 10.0
GROUND_UNITS = 3.7                 # the scan's floor is nowhere near zero
OFFSET_UNITS = (17.0, 9.0)         # and its origin is nowhere near the building


# A rectangle is its own 180-degree rotation, so its heading genuinely cannot be
# recovered from a footprint alone. Real buildings rarely are; this notch is what
# lets overlap distinguish "facing north" from "facing south".
NOTCH = 0.35


def _plan(notched: bool) -> list[tuple[float, float]]:
    """Footprint corners in the building's own frame: +x along the long axis.

    Centred on the polygon's own area centroid, because that is where
    `anchor_model` puts the geocoded coordinate — so a test comparing the two
    rings measures the placement, not this helper's choice of origin.
    """
    hl, hs = LONG_M / 2, SHORT_M / 2
    if not notched:
        return [(hl, hs), (hl, -hs), (-hl, -hs), (-hl, hs)]
    corners = [
        (hl, hs), (hl, -hs), (-hl, -hs), (-hl, hs),
        (-hl + LONG_M * NOTCH, hs),
        (-hl + LONG_M * NOTCH, hs - SHORT_M * NOTCH),
        (-hl + LONG_M * NOTCH * 2, hs - SHORT_M * NOTCH),
        (-hl + LONG_M * NOTCH * 2, hs),
    ]
    centre = ShapelyPolygon(corners).centroid
    return [(x - centre.x, y - centre.y) for x, y in corners]


def osm_footprint(bearing_deg: float, notched: bool = True) -> list[tuple[float, float]]:
    """The real building's footprint, long axis at `bearing_deg` from true north."""
    b = math.radians(bearing_deg)
    # (east, north) is right-handed while bearings run clockwise, so the across
    # axis sits at bearing B-90. Using B+90 mirrors the building instead of
    # rotating it, which no rotation can then match.
    along = np.array([math.sin(b), math.cos(b)])
    across = np.array([-math.cos(b), math.sin(b)])
    corners = [along * x + across * y for x, y in _plan(notched)]
    return [tuple(latlon_from_enu(e, n, (ANCHOR_LAT, ANCHOR_LON))) for e, n in corners]


def scan_mesh(*, with_clutter: bool = True, notched: bool = True) -> trimesh.Trimesh:
    """A Y-up scan of the building in arbitrary units, with the street around it.

    The building's long axis runs along model +X, which in glTF's frame is east,
    so its model-space bearing is 90 degrees. Everything else about the mesh —
    scale, origin, ground height — is arbitrary, exactly as RealityScan leaves it.
    """
    # glTF has -Z pointing north, so the building's own +y (across) maps to -z.
    plan = [(x / METERS_PER_UNIT, -y / METERS_PER_UNIT) for x, y in _plan(notched)]
    body = trimesh.creation.extrude_polygon(
        ShapelyPolygon(plan), height=HEIGHT_M / METERS_PER_UNIT,
    )
    # extrude_polygon builds in the XY plane with +Z up; stand it up glTF-style.
    body.apply_transform(np.array([
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]))
    body.apply_translation([OFFSET_UNITS[0], GROUND_UNITS, OFFSET_UNITS[1]])
    if not with_clutter:
        return body

    # Sidewalk: dense, as photogrammetric ground always is, and the thing that
    # makes the lowest *dense* band the right definition of the ground plane.
    slab = trimesh.creation.box(extents=[9.0, 0.04, 9.0]).subdivide().subdivide().subdivide()
    slab.apply_translation([OFFSET_UNITS[0] - 8.0, GROUND_UNITS - 0.02, OFFSET_UNITS[1]])

    # A parked car: wide and low. It out-volumes nothing, but a naive "biggest
    # component" rule would still be at risk from the slab, which this outranks.
    car = trimesh.creation.box(extents=[0.45, 0.15, 0.2])
    car.apply_translation([OFFSET_UNITS[0] - 6.0, GROUND_UNITS + 0.075, OFFSET_UNITS[1] + 3.0])

    return trimesh.util.concatenate([body, slab, car])


@pytest.mark.parametrize("bearing", [0.0, 30.0, 115.0, 200.0, 305.0])
def test_scale_and_heading_are_recovered_from_a_footprint_match(bearing):
    """The two numbers photogrammetry cannot produce on its own.

    Camera EXIF locates the photographer, not the building, and a mesh has no
    idea how large it is. Both come from matching the scan's own footprint to a
    surveyed one.
    """
    mesh_path = _write(scan_mesh())
    result = anchor.anchor_model(
        mesh_path, ANCHOR_LAT, ANCHOR_LON,
        asset_id="test-building", osm_footprint=osm_footprint(bearing),
    )
    transform = result["placement"]["transform"]

    assert transform["metersPerModelUnit"] == pytest.approx(METERS_PER_UNIT, rel=0.02)

    # The model's long axis lies along east (bearing 90), so the rotation that
    # carries it onto the real bearing is `bearing - 90`.
    expected = (bearing - 90.0) % 360.0
    assert _angle_gap(transform["headingDegrees"], expected) < 2.0

    steps = {s["name"]: s for s in result["report"]["steps"]}
    assert steps["heading"]["solved"] and steps["scale"]["solved"]
    assert steps["heading"]["confidence"] > 0.4
    assert result["report"]["unresolved"] == []


def test_dimensions_are_measured_not_typed():
    """`package --bounds W L H` used to be typed by hand in model units. These
    come off the mesh, in meters."""
    result = anchor.anchor_model(
        _write(scan_mesh()), ANCHOR_LAT, ANCHOR_LON,
        asset_id="test-building", osm_footprint=osm_footprint(30.0),
    )
    bounds = result["report"]["boundsMeters"]
    assert bounds["measured"] is True
    assert bounds["widthMeters"] == pytest.approx(LONG_M, rel=0.03)
    assert bounds["lengthMeters"] == pytest.approx(SHORT_M, rel=0.03)
    assert bounds["heightMeters"] == pytest.approx(HEIGHT_M, rel=0.03)


def test_ground_is_the_dense_band_and_the_model_is_re_origined(tmp_path):
    """The exported mesh must sit on y=0 with its footprint centred on the origin,
    whatever arbitrary space the scan arrived in — otherwise it floats, sinks, or
    lands beside its own coordinates."""
    out = tmp_path / "anchored"
    anchor.anchor_model(
        _write(scan_mesh()), ANCHOR_LAT, ANCHOR_LON,
        asset_id="test-building", osm_footprint=osm_footprint(30.0), out_dir=out,
    )
    written = trimesh.load(out / "building.glb", force="mesh")
    low, high = written.bounds

    assert low[1] == pytest.approx(0.0, abs=0.05)             # ground at y = 0
    centre_x = (low[0] + high[0]) / 2
    centre_z = (low[2] + high[2]) / 2
    assert centre_x == pytest.approx(0.0, abs=0.1)
    assert centre_z == pytest.approx(0.0, abs=0.1)

    report = json.loads((out / "anchor-report.json").read_text())
    ground = next(s for s in report["steps"] if s["name"] == "groundPlane")
    assert ground["solved"]
    assert ground["value"] == pytest.approx(GROUND_UNITS, abs=0.1)


def test_clutter_is_dropped():
    """A reconstruction region crops a box, not a subject. Whatever shared that
    box is still in the mesh, and has to go."""
    cluttered = anchor.anchor_model(
        _write(scan_mesh(with_clutter=True)), ANCHOR_LAT, ANCHOR_LON,
        asset_id="test-building", osm_footprint=osm_footprint(30.0),
    )
    clean = anchor.anchor_model(
        _write(scan_mesh(with_clutter=False)), ANCHOR_LAT, ANCHOR_LON,
        asset_id="test-building", osm_footprint=osm_footprint(30.0),
    )
    # Isolation has to leave the same building behind either way.
    for key in ("widthMeters", "lengthMeters", "heightMeters"):
        assert cluttered["report"]["boundsMeters"][key] == \
            pytest.approx(clean["report"]["boundsMeters"][key], rel=0.03)

    isolation = next(s for s in cluttered["report"]["steps"] if s["name"] == "isolation")
    assert isolation["value"] < 1.0        # something was actually removed


def test_a_tape_measure_beats_a_polygon():
    """An explicit reference measurement overrides the footprint match, and says
    so in the report."""
    result = anchor.anchor_model(
        _write(scan_mesh()), ANCHOR_LAT, ANCHOR_LON,
        asset_id="test-building", osm_footprint=osm_footprint(30.0),
        reference_meters=LONG_M,
    )
    scale = [s for s in result["report"]["steps"] if s["name"] == "scale"][-1]
    assert scale["method"] == "reference-measurement"
    assert scale["confidence"] > 0.9
    assert result["placement"]["transform"]["metersPerModelUnit"] == \
        pytest.approx(METERS_PER_UNIT, rel=0.01)


def test_without_a_reference_scale_and_heading_are_left_unsolved():
    """The honest failure mode. An unsolved number becomes a control the user
    moves in the placement editor — it never becomes a confident guess."""
    result = anchor.anchor_model(
        _write(scan_mesh()), ANCHOR_LAT, ANCHOR_LON, asset_id="test-building",
    )
    assert set(result["report"]["unresolved"]) >= {"scale", "heading"}
    for step in result["report"]["steps"]:
        if step["name"] in ("scale", "heading"):
            assert step["solved"] is False
            assert step["confidence"] == 0.0
            assert "placement editor" in step["note"]

    # Geometry is still cleaned, grounded and re-origined; only the unknowable
    # numbers are withheld.
    assert result["placement"]["transform"]["metersPerModelUnit"] == 1.0
    assert result["placement"]["transform"]["anchor"] == "ground-center"


def test_output_is_always_canonical_whatever_came_in():
    """A Z-up ENU scan and a Y-up glTF scan of the same building must produce the
    same Y-up, ground-center asset — that is what makes the declared convention
    a conversion rather than a warning label."""
    y_up = scan_mesh(with_clutter=False)
    z_up = y_up.copy()
    # Y-up (x east, y up, -z north) -> Z-up ENU (x east, y north, z up)
    z_up.apply_transform(np.array([
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, -1.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]))

    footprint = osm_footprint(30.0)
    from_y = anchor.anchor_model(_write(y_up), ANCHOR_LAT, ANCHOR_LON,
                                 asset_id="b", osm_footprint=footprint, up_axis="Y")
    from_z = anchor.anchor_model(_write(z_up), ANCHOR_LAT, ANCHOR_LON,
                                 asset_id="b", osm_footprint=footprint, up_axis="Z")

    for result in (from_y, from_z):
        assert result["placement"]["transform"]["upAxis"] == "Y"
        assert result["placement"]["transform"]["anchor"] == "ground-center"

    assert from_z["placement"]["transform"]["metersPerModelUnit"] == \
        pytest.approx(from_y["placement"]["transform"]["metersPerModelUnit"], rel=0.02)
    assert _angle_gap(from_z["placement"]["transform"]["headingDegrees"],
                      from_y["placement"]["transform"]["headingDegrees"]) < 2.0
    assert from_z["report"]["boundsMeters"]["heightMeters"] == \
        pytest.approx(from_y["report"]["boundsMeters"]["heightMeters"], rel=0.03)


def test_exported_footprint_lands_on_the_real_building():
    """The footprint in placement.json is the mesh's own outline placed on the
    Earth. It has to agree with the OSM ring it was matched against, or the
    heading is decorative."""
    footprint = osm_footprint(115.0)
    result = anchor.anchor_model(
        _write(scan_mesh()), ANCHOR_LAT, ANCHOR_LON,
        asset_id="test-building", osm_footprint=footprint,
    )
    from shapely.geometry import Polygon

    from geospatial import enu_offset

    origin = (ANCHOR_LAT, ANCHOR_LON)
    produced = Polygon([enu_offset(p, origin) for p in result["placement"]["footprint"]])
    expected = Polygon([enu_offset(p, origin) for p in footprint])
    overlap = produced.intersection(expected).area / produced.union(expected).area
    assert overlap > 0.9, f"footprint overlap only {overlap:.2f}"


# ---------------------------------------------------------------------------

def _write(mesh: trimesh.Trimesh) -> str:
    import tempfile

    handle = tempfile.NamedTemporaryFile(suffix=".glb", delete=False)
    handle.write(trimesh.Scene(mesh).export(file_type="glb"))
    handle.close()
    return handle.name


def _angle_gap(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def test_footprint_file_accepts_both_shapes(tmp_path):
    """The project's own footprint files are {"outer": [...]}, so the one command
    that wants a footprint has to read the one file in the repo that has one."""
    from reconstruction.pipeline import read_footprint

    ring = [[40.1, -74.1], [40.2, -74.1], [40.2, -74.2]]

    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(ring))
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"name": "X", "outer": ring, "holes": [], "source": "OSM"}))

    assert read_footprint(bare) == read_footprint(wrapped) == [(40.1, -74.1), (40.2, -74.1), (40.2, -74.2)]

    for bad in ([[1, 2]], {"outer": "nope"}, [[1, 2, 3], [4, 5, 6], [7, 8, 9]]):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            read_footprint(path)


def test_repo_demo_asset_round_trips_through_anchor(tmp_path):
    """The prepared GLB is already metres, Y-up and ground-centred, so anchoring
    it against its own OSM footprint must return scale 1 and heading 0 — an
    end-to-end check against real repository data, not a synthetic box."""
    from pathlib import Path

    from app.demo_data import ensure_demo_asset
    from reconstruction.pipeline import read_footprint

    placement = ensure_demo_asset()
    model = Path("web/assets") / placement.assetId / "building.glb"
    result = anchor.anchor_model(
        model, placement.location.latitude, placement.location.longitude,
        asset_id=placement.assetId,
        osm_footprint=read_footprint("app/data/metlife_footprint.json"),
        out_dir=tmp_path / "anchored",
    )
    transform = result["placement"]["transform"]
    assert transform["metersPerModelUnit"] == pytest.approx(1.0, rel=0.02)
    assert _angle_gap(transform["headingDegrees"], 0.0) < 2.0
    assert 0 <= transform["headingDegrees"] < 360     # never 360.0
