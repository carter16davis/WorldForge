"""Turn a RealityScan mesh into a map-ready asset.

RealityScan returns a mesh floating in arbitrary local space. It does not know
where on Earth the building is, which way north is, how many meters tall it is,
where the ground plane sits, or where the origin should be. It also hands back
the sidewalk, a parked car and half the neighbour's wall. That is a scan, not an
asset, and the distance between the two is this file.

Six things happen here, in order:

    1. ground plane   the lowest dense horizontal band of vertices
    2. isolation      keep the connected body standing on that plane, drop the rest
    3. footprint      the mesh's true silhouette above the kerb line
    4. scale          model units -> meters, from a reference measurement or an
                      OSM footprint match
    5. heading        the mesh's principal axis rotated onto the OSM footprint's,
                      with the 180-degree flip resolved by polygon overlap
    6. re-origin      translate so ground is y=0 and the footprint centre is x=z=0,
                      and hand back a Y-up glTF mesh whatever came in

Every step records what it did, how confident it is, and what it could not
determine, into the `AnchorReport` that ends up in provenance.json. A step that
cannot be solved from evidence returns None and says so; it never guesses a
number and presents it as a measurement. An unsolved scale or heading is a
control the user moves in the placement editor, which is exactly what that
editor is for.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import trimesh
from shapely.geometry import Polygon
from trimesh.path.polygons import projected as projected_outline

try:
    from geospatial import enu_offset, latlon_from_enu
except ImportError:      # running the CLI from inside reconstruction/
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from geospatial import enu_offset, latlon_from_enu

# Vertices within this fraction of the total model height of the lowest dense
# band count as "on the ground". Photogrammetric ground is never flat.
GROUND_BAND_FRACTION = 0.02

# A ground plane supported by less than this share of the mesh's lowest region
# is reported as unreliable rather than used silently.
MIN_GROUND_INLIERS = 0.05

# Footprint hull is taken from vertices above this height over the ground, so
# that kerbs, steps and scanner noise around the base do not inflate it.
FOOTPRINT_LIFT_FRACTION = 0.10

# A connected body must hold at least this share of the scan's surface area
# before it can be called "the building". Face count cannot do this job: a
# photogrammetric sidewalk is thousands of tiny triangles and a building wall is
# a handful of large ones. Area is also what rejects the specks that would
# otherwise win on height alone.
MIN_BODY_AREA_SHARE = 0.02

# A kept body holding less than this share of the scan is not the whole
# building: the scan was shredded rather than cleanly multi-body, so the cluster
# rule below is used instead of keeping that one body.
WHOLE_BODY_AREA_SHARE = 0.25

# A body holding a substantial share of the scan's area in no more than this many
# triangles is a *sheet*: a few enormous flat polygons. Automatic reconstruction
# regions produce them constantly — the ground plane under a drone orbit, and the
# backdrop stitched across the sky behind the subject. They are never the
# building, and because area is how a body earns the right to be considered here,
# leaving them in lets a twenty-triangle plane outrank four hundred thousand
# triangles of actual reconstruction: that is what reduced a MetLife Stadium scan
# to a 23-vertex sliver.
#
# The threshold is absolute rather than relative to the rest of the scan, because
# the comparison that would make it relative is the one being subverted. Any
# surface a photogrammetric reconstruction actually measured is described in
# thousands of small triangles, whatever else the scan contains.
SHEET_MAX_FACES = 100

# A scan that splits into more bodies than this did not split into
# subject-and-clutter; it shattered. A clean capture yields a handful of bodies —
# the building, the sidewalk, a car — and the tallest substantial one is the
# building. Hundreds of bodies means photogrammetry fragmented the subject
# itself, and no single one of them is the building.
SHREDDED_BODY_COUNT = 20


ENU_TO_GLTF = np.array([
    [1.0, 0.0, 0.0, 0.0],   # east  -> +X
    [0.0, 0.0, 1.0, 0.0],   # up    -> +Y
    [0.0, -1.0, 0.0, 0.0],  # north -> -Z
    [0.0, 0.0, 0.0, 1.0],
])

GLTF_TO_ENU = np.linalg.inv(ENU_TO_GLTF)
"""Everything inside this module works in one frame: +X east, +Y north, +Z up.

A Y-up glTF scan is rotated into it on the way in and back out on the way out.
Carrying two conventions through six steps is how sign errors get written, and a
sign error here puts a building's front door on the wrong side of the street."""


@dataclass
class Step:
    """One solved (or unsolved) quantity, with the evidence behind it."""

    name: str
    solved: bool
    value: float | None = None
    method: str = ""
    confidence: float = 0.0
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnchorReport:
    assetId: str
    upAxisIn: str
    steps: list[dict] = field(default_factory=list)
    boundsMeters: dict = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    generatedAt: str = ""

    def add(self, step: Step) -> Step:
        self.steps.append(step.as_dict())
        if not step.solved:
            self.unresolved.append(step.name)
        return step


# ---------------------------------------------------------------------------
# 0. Is the declared frame the mesh's actual frame?
# ---------------------------------------------------------------------------

AXIS_NAMES = ("east", "north", "up")


def check_up_axis(mesh: trimesh.Trimesh, up: int, declared: str) -> Step:
    """Measure which axis the mesh's flat surfaces actually stand on.

    Everything after this assumes the mesh is upright: the ground plane is the
    lowest dense band *along the up axis*, isolation ranks bodies by how high
    they reach, and the footprint is the silhouette cast *down* the up axis. Feed
    it a mesh lying on its side and every one of those is measured across the
    building instead of through it — and the result is a plausible-looking asset
    that is wrong in a way no number in the report admits to.

    That is not hypothetical either. RealityScan exports in its own Z-up survey
    frame, not glTF's Y-up, and anchoring its output as Y-up turned a stadium
    into a 79 x 12 metre slab standing 38 metres tall.

    This does not rotate anything. The engine that produced the file knows what
    frame it writes and says so; guessing an axis from geometry would trade a
    declaration that can be fixed once for a heuristic that fails on tall narrow
    buildings, where the walls out-area the roof. What it does is check the
    declaration against the mesh and put the disagreement in the report.
    """
    normals = mesh.face_normals
    weights = mesh.area_faces
    total = float(weights.sum()) or 1.0
    flatness = [float((weights * np.abs(normals[:, axis])).sum()) / total
                for axis in range(3)]

    dominant = int(np.argmax(flatness))
    runner_up = max(f for axis, f in enumerate(flatness) if axis != dominant)
    margin = flatness[dominant] / runner_up if runner_up > 0 else float("inf")

    if dominant == up:
        return Step("upAxis", True, float(up), "surface-normals",
                    round(min(0.95, flatness[up]), 3),
                    f"Declared {declared}-up, and the mesh agrees: most surface area "
                    f"faces along the up axis ({flatness[up] * 100:.0f}% against "
                    f"{runner_up * 100:.0f}% for the next).")

    return Step(
        "upAxis", True, float(up), "surface-normals", 0.1,
        f"Declared {declared}-up, but the mesh does not look like it. Most surface "
        f"area faces along the {AXIS_NAMES[dominant]} axis "
        f"({flatness[dominant] * 100:.0f}% against {flatness[up] * 100:.0f}% for the "
        f"declared up axis, {margin:.1f}x). If the model comes out lying on its "
        f"side, the export is in a different frame than the engine declares — "
        f"re-run with the other up axis. Ground plane, isolation and footprint "
        f"below were all measured against the declared axis.",
    )


# ---------------------------------------------------------------------------
# 1. Ground plane
# ---------------------------------------------------------------------------

def detect_ground(vertices: np.ndarray, up: int) -> tuple[float, float]:
    """Return (ground height, inlier fraction) in model units.

    The ground is the lowest *dense* horizontal band, not simply the minimum
    vertex: a scan almost always has a few stray points below the real surface,
    and anchoring to those sinks the building.
    """
    heights = vertices[:, up]
    span = float(np.ptp(heights))
    if span <= 0:
        return float(heights.min()), 0.0

    # Look only at the bottom fifth; above that we are into the building.
    low_cut = heights.min() + 0.2 * span
    low = heights[heights <= low_cut]
    if low.size < 8:
        return float(heights.min()), 0.0

    bins = max(8, min(256, low.size // 32))
    counts, edges = np.histogram(low, bins=bins)
    peak = int(np.argmax(counts))
    ground = float((edges[peak] + edges[peak + 1]) / 2)

    band = span * GROUND_BAND_FRACTION
    inliers = float(np.count_nonzero(np.abs(heights - ground) <= band)) / heights.size
    return ground, inliers


# ---------------------------------------------------------------------------
# 2. Isolation
# ---------------------------------------------------------------------------

@dataclass
class _Body:
    """One connected component, described without building it into a mesh."""

    faces: np.ndarray
    areaShare: float        # share of the scan's surface area
    top: float              # highest point, in model units

    @property
    def is_sheet(self) -> bool:
        return len(self.faces) <= SHEET_MAX_FACES and self.areaShare >= MIN_BODY_AREA_SHARE


def _describe_bodies(mesh: trimesh.Trimesh, components: list[np.ndarray],
                     up: int) -> list[_Body]:
    """Measure every component from face indices alone.

    Nothing here builds a submesh, because building one copies the visuals with
    it — see the note in `isolate_building`.
    """
    area = mesh.area_faces
    total = float(area.sum()) or 1.0
    heights = np.asarray(mesh.vertices)[:, up]

    return [
        _Body(
            faces=faces,
            areaShare=float(area[faces].sum()) / total,
            top=float(heights[np.unique(mesh.faces[faces])].max()),
        )
        for faces in components
    ]


def isolate_building(mesh: trimesh.Trimesh, up: int,
                     ground: float) -> tuple[trimesh.Trimesh, Step]:
    """Drop what shared the reconstruction region: backdrop sheets, sidewalk
    slabs, cars and stray islands.

    A reconstruction region in RealityScan crops a *box*, not a subject, and an
    unattended run places that box automatically. What is left inside it is the
    building plus whatever else fitted, in three flavours:

    *Sheets* go first, unconditionally. An automatic region stitches the ground
    under the capture and a backdrop behind it into a handful of enormous
    triangles, and area is how a body earns consideration here — so a
    twenty-triangle plane holding a fifth of the scan's surface outranks the
    building itself. That is not a hypothetical: it is what reduced a MetLife
    Stadium scan to a 23-vertex sliver. See SHEET_AREA_RATIO.

    Then one of two rules, depending on what the split found:

    - **A handful of bodies, or one that is most of the scan**, is a clean
      capture: the building plus what stood next to it. Keep the tallest among
      the substantial ones — a wide, flat road surface can easily out-*area* a
      narrow building, but it cannot out-reach it.
    - **A scan in hundreds of pieces** has no body that is the building.
      Photogrammetry shreds a large or reflective subject into fragments that
      are all genuinely part of it, so keeping any one of them throws the
      building away. Keep every solid piece, and say in the report that nothing
      beyond the sheets was removed. See SHREDDED_BODY_COUNT.

    Returns the mesh and the step describing what happened, including the case
    where splitting could not run at all. Quietly returning the whole scan there
    would hand back the sidewalk and the car inside something labelled
    "isolated", which is worse than saying it did not work.

    The components are chosen from face indices and only the winners are ever
    built into a mesh, in a single `submesh` call. `mesh.split()` would build all
    of them, and building one copies the visuals with it: a RealityScan export
    carries a single 8192x8192 atlas, so each body costs 256 MB of texture
    whether it is the building or a three-triangle speck of scanner noise. A real
    scan splits into hundreds of bodies, which is hundreds of gigabytes of
    texture copies for a mesh whose geometry fits in a few hundred megabytes.
    Under WSL that does not raise MemoryError, it takes the virtual machine down
    with it.
    """
    before = len(mesh.vertices)
    try:
        components = trimesh.graph.connected_components(
            edges=mesh.face_adjacency, nodes=np.arange(len(mesh.faces)), min_len=1)
    except Exception as exc:
        return mesh, Step(
            "isolation", False, 1.0, "unavailable", 0.0,
            f"Could not split the mesh into connected bodies ({type(exc).__name__}): "
            f"{exc}. The scan was left whole, so anything that shared the "
            f"reconstruction region is still in it. Install scipy.",
        )

    if len(components) <= 1:
        return mesh, Step("isolation", True, 1.0, "largest-vertical-body", 0.6,
                          "Scan was a single connected body; nothing removed.")

    bodies = _describe_bodies(mesh, components, up)
    sheets = [b for b in bodies if b.is_sheet]
    solid = [b for b in bodies if not b.is_sheet]
    sheet_area = sum(b.areaShare for b in sheets)
    sheet_note = (
        f"Removed {len(sheets)} flat backdrop sheet(s) holding {sheet_area * 100:.0f}% "
        f"of the scan's surface in {sum(len(b.faces) for b in sheets)} triangles. "
        if sheets else ""
    )

    if not solid:
        # Every body is an enormous flat plane, so there is no building in here
        # to isolate. Saying so beats handing back the biggest sheet.
        return mesh, Step(
            "isolation", False, 1.0, "sheet-rejection", 0.0,
            f"Every one of the {len(components)} bodies in this scan is a flat "
            f"sheet of a few huge triangles — the ground and backdrop an automatic "
            f"reconstruction region stitches in. There is no reconstructed "
            f"building here to isolate. The scan was left whole.",
        )

    # Only bodies substantial enough to *be* a building may win on height: a
    # stray speck of noise sitting above the roof outranks the roof by a hair.
    substantial = [b for b in solid if b.areaShare >= MIN_BODY_AREA_SHARE]
    tallest = max(substantial or solid, key=lambda b: b.top - ground)

    if (len(solid) <= SHREDDED_BODY_COUNT
            or tallest.areaShare >= WHOLE_BODY_AREA_SHARE):
        kept = mesh.submesh([tallest.faces], append=True)
        return kept, Step(
            "isolation", True, round(len(kept.vertices) / before, 4),
            "largest-vertical-body", 0.6,
            f"{sheet_note}Kept {len(kept.vertices)} of {before} vertices as the "
            f"building body, discarding {len(components) - len(sheets) - 1} other "
            f"connected bodies.")

    # Shredded: every solid piece is part of the building, so every solid piece
    # is kept. There is no second rule here on purpose. Any further guess about
    # which fragments are "really" the subject — biggest body, nearest cluster,
    # tallest — is what published a 23-vertex sliver of a stadium, and the cost
    # of guessing wrong is losing the reconstruction the user waited ten minutes
    # for. Keeping a neighbour's wall is a visible, correctable mistake; deleting
    # the building is not.
    largest = max(solid, key=lambda b: b.areaShare)
    kept = mesh.submesh([np.concatenate([b.faces for b in solid])], append=True)
    after = len(kept.vertices)
    kept_area = sum(b.areaShare for b in solid)

    return kept, Step(
        "isolation", True, round(after / before, 4), "sheet-rejection", 0.35,
        f"{sheet_note}The scan is in {len(components)} disconnected pieces with no "
        f"single body larger than {largest.areaShare * 100:.0f}% of it, so "
        f"photogrammetry fragmented the building itself and no one body is the "
        f"subject. All {len(solid)} solid pieces were kept — "
        f"{kept_area * 100:.0f}% of the surface, {after} of {before} vertices — "
        f"so anything else that stood inside the reconstruction region is still "
        f"in the mesh. Check the footprint, scale and heading in the placement "
        f"editor; recapture with more overlap, or place the reconstruction region "
        f"by hand with the prepare/build commands.",
    )


# ---------------------------------------------------------------------------
# 3. Footprint
# ---------------------------------------------------------------------------

def mesh_footprint(mesh: trimesh.Trimesh, ground: float) -> Polygon:
    """The mesh's true silhouette on the ground plane, in (east, north) meters-of-units.

    The silhouette, not a convex hull: the hull of an L-shaped building is a
    rectangle, and a rectangle is its own 180-degree rotation, which throws away
    exactly the asymmetry that lets `solve_scale_and_heading` tell a building
    facing north from the same building facing south.

    Only geometry above the kerb line contributes, so steps, planters and the
    scanner noise that always pools around a building's base do not inflate it.
    """
    span = float(np.ptp(mesh.vertices[:, 2]))
    lift = ground + span * FOOTPRINT_LIFT_FRACTION
    above = (mesh.vertices[mesh.faces][:, :, 2] >= lift).any(axis=1)
    body = mesh.submesh([above], append=True) if above.any() else mesh

    try:
        outline = projected_outline(body, normal=[0, 0, 1])
    except Exception:
        outline = None
    if outline is None or outline.is_empty:
        # A projection can fail on degenerate or non-manifold scan output. The
        # hull is a worse footprint, but a footprint; the caller's overlap test
        # is what decides whether it was good enough to trust.
        outline = Polygon(body.vertices[:, :2]).convex_hull

    if outline.geom_type == "MultiPolygon":
        outline = max(outline.geoms, key=lambda g: g.area)
    if outline.geom_type != "Polygon" or outline.is_empty:
        raise ValueError("Mesh footprint collapsed; the scan has no planar extent.")
    return outline


def principal_rect(poly: Polygon) -> tuple[float, float, float]:
    """(axis angle in degrees CCW from +x, long edge, short edge).

    The minimum-rotated-rectangle axis is defined only modulo 180 degrees. That
    ambiguity is real and is resolved later by overlap, not hidden here.
    """
    rect = poly.minimum_rotated_rectangle
    if rect.geom_type != "Polygon":
        raise ValueError("Footprint has no rectangular extent.")
    x, y = rect.exterior.coords.xy
    edges = [(x[i + 1] - x[i], y[i + 1] - y[i]) for i in range(2)]
    lengths = [math.hypot(dx, dy) for dx, dy in edges]
    longest = 0 if lengths[0] >= lengths[1] else 1
    dx, dy = edges[longest]
    angle = math.degrees(math.atan2(dy, dx)) % 180.0
    return angle, max(lengths), min(lengths)


# ---------------------------------------------------------------------------
# 4 + 5. Scale and heading
# ---------------------------------------------------------------------------

def _rotate(points: np.ndarray, degrees: float) -> np.ndarray:
    """Rotate (east, north) pairs clockwise by `degrees`, the heading convention."""
    a = math.radians(degrees)
    r = np.array([[math.cos(a), math.sin(a)], [-math.sin(a), math.cos(a)]])
    return points @ r.T


def _iou(a: Polygon, b: Polygon) -> float:
    if not a.is_valid:
        a = a.buffer(0)
    if not b.is_valid:
        b = b.buffer(0)
    union = a.union(b).area
    return float(a.intersection(b).area / union) if union > 0 else 0.0


def solve_scale_and_heading(mesh_enu: Polygon, osm_enu: Polygon,
                            report: AnchorReport) -> tuple[float | None, float | None]:
    """Match the mesh footprint to the real one. Returns (meters per unit, heading).

    Both polygons must already be in an (east, north) frame — the mesh's own for
    `mesh_enu`, true north for `osm_enu`. Heading is the clockwise rotation that
    carries the first onto the second, matching
    `placement.transform.headingDegrees`.
    """
    m_angle, m_long, m_short = principal_rect(mesh_enu)
    o_angle, o_long, o_short = principal_rect(osm_enu)

    if m_long <= 0:
        report.add(Step("scale", False, note="Mesh footprint has no extent."))
        report.add(Step("heading", False, note="Mesh footprint has no extent."))
        return None, None

    scale = o_long / m_long
    aspect_mesh = m_short / m_long
    aspect_osm = o_short / o_long
    aspect_error = abs(aspect_mesh - aspect_osm)

    # Rotating clockwise by h takes an axis at CCW angle t to t - h, so aligning
    # the mesh axis with the real one needs h = m_angle - o_angle. The rectangle
    # axis is only defined modulo 180 degrees, hence two candidates.
    base = (m_angle - o_angle) % 180.0
    centred = np.array(mesh_enu.exterior.coords) - np.array(mesh_enu.centroid.coords[0])
    osm_centred = Polygon(np.array(osm_enu.exterior.coords)
                          - np.array(osm_enu.centroid.coords[0]))

    scored = []
    for candidate in (base, base + 180.0):
        placed = Polygon(_rotate(centred * scale, candidate))
        scored.append((_iou(placed, osm_centred), candidate % 360.0))
    scored.sort(reverse=True)
    (best_iou, heading), (second_iou, _) = scored
    # Round before wrapping: 359.9999 rounds to 360.0, which is outside the
    # [0, 360) the contract requires and reads as a different answer than 0.
    heading = round(heading, 3) % 360.0

    # Overlap is what makes this a measurement rather than a coin flip. A
    # near-symmetric building genuinely cannot be disambiguated from footprint
    # alone, and the report says so instead of picking one and looking certain.
    decisiveness = best_iou - second_iou
    if best_iou < 0.45:
        report.add(Step("scale", False, method="footprint-match", confidence=0.0,
                        note=f"Footprint match is too poor to trust "
                             f"(best overlap {best_iou:.2f}). Supply a reference "
                             f"measurement, or set scale in the placement editor."))
        report.add(Step("heading", False, method="footprint-match", confidence=0.0,
                        note=f"Footprint match is too poor to trust "
                             f"(best overlap {best_iou:.2f}). Set heading by eye in "
                             f"the placement editor."))
        return None, None

    scale_confidence = round(min(0.9, best_iou * (1.0 - min(aspect_error * 2, 0.8))), 3)
    report.add(Step("scale", True, round(scale, 6), "footprint-match", scale_confidence,
                    f"Mesh long axis {m_long:.3f} model units matched to "
                    f"{o_long:.1f} m of OpenStreetMap footprint. Aspect ratio "
                    f"differs by {aspect_error:.3f}."))

    if decisiveness < 0.05:
        report.add(Step("heading", True, heading, "footprint-match", 0.25,
                        f"Footprint is close to symmetric: the two opposite "
                        f"orientations score {best_iou:.2f} and {second_iou:.2f}. "
                        f"The 180-degree flip is unresolved — check a known facade "
                        f"in the placement editor."))
    else:
        report.add(Step("heading", True, heading, "footprint-match",
                        round(min(0.9, best_iou + decisiveness), 3),
                        f"Overlap {best_iou:.2f} against {second_iou:.2f} for the "
                        f"opposite orientation."))
    return scale, heading


# ---------------------------------------------------------------------------
# 6. The whole job
# ---------------------------------------------------------------------------

def anchor_model(model_path: str | Path,
                 latitude: float,
                 longitude: float,
                 *,
                 asset_id: str,
                 name: str = "",
                 address: str = "",
                 osm_footprint: list[tuple[float, float]] | None = None,
                 reference_meters: float | None = None,
                 elevation_meters: float = 0.0,
                 up_axis: str = "Y",
                 out_dir: str | Path | None = None) -> dict:
    """Anchor a scan to the Earth. Returns {'placement': ..., 'report': ...}.

    `osm_footprint` is [(lat, lon), ...] for the real building, used to solve
    scale and heading. `reference_meters` is one known real-world length along
    the building's longest horizontal axis, and overrides the footprint match
    for scale because a tape measure beats a polygon.

    Neither is required. Without them the geometry is still cleaned, grounded and
    re-origined, and scale and heading are left explicitly unsolved for the user
    to set in the placement editor.
    """
    if up_axis not in ("Y", "Z"):
        raise ValueError("up_axis must be 'Y' or 'Z'")
    model_path = Path(model_path)
    report = AnchorReport(assetId=asset_id, upAxisIn=up_axis,
                          generatedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    mesh = trimesh.load(model_path, force="mesh")
    if mesh.vertices.shape[0] < 4:
        raise ValueError(f"{model_path.name} has no usable geometry.")

    # Into the one internal frame: +X east, +Y north, +Z up.
    if up_axis == "Y":
        mesh.apply_transform(GLTF_TO_ENU)
    up = 2

    # --- 0. is the declared frame the mesh's actual frame? ---
    report.add(check_up_axis(mesh, up, up_axis))

    # --- 1. ground plane ---
    ground, inliers = detect_ground(mesh.vertices, up)
    report.add(Step("groundPlane", inliers >= MIN_GROUND_INLIERS, round(ground, 6),
                    "lowest-dense-band", round(min(0.95, inliers * 4), 3),
                    f"{inliers * 100:.1f}% of vertices lie within the ground band. "
                    + ("" if inliers >= MIN_GROUND_INLIERS else
                       "That is thin support — check the model does not float or sink.")))

    # --- 2. isolation ---
    mesh, step = isolate_building(mesh, up, ground)
    report.add(step)

    # --- 3. footprint ---
    footprint = mesh_footprint(mesh, ground)
    plane_centre = np.array(footprint.centroid.coords[0])
    # Already in the mesh's own (east, north); centre it on the building.
    footprint_enu = Polygon(np.asarray(footprint.exterior.coords)[:-1] - plane_centre)

    # --- 4 + 5. scale and heading ---
    scale = heading = None
    if osm_footprint and len(osm_footprint) >= 3:
        origin = (latitude, longitude)
        osm_enu = Polygon([enu_offset(p, origin) for p in osm_footprint])
        scale, heading = solve_scale_and_heading(footprint_enu, osm_enu, report)
    else:
        report.add(Step("heading", False, method="none",
                        note="No reference footprint supplied. Set heading in the "
                             "placement editor; photogrammetry cannot recover true "
                             "north on its own."))

    if reference_meters is not None:
        _, m_long, _ = principal_rect(footprint_enu)
        if m_long <= 0:
            raise ValueError("Cannot apply a reference measurement to a degenerate footprint.")
        scale = reference_meters / m_long
        report.add(Step("scale", True, round(scale, 6), "reference-measurement", 0.95,
                        f"{reference_meters:.2f} m measured along the longest "
                        f"horizontal axis ({m_long:.3f} model units)."))
    elif scale is None:
        report.add(Step("scale", False, method="none",
                        note="No reference measurement and no footprint match. "
                             "Model units are arbitrary; set the scale in the "
                             "placement editor against a known dimension."))

    # --- 6. re-origin, then out to glTF's frame ---
    mesh.apply_translation([-plane_centre[0], -plane_centre[1], -ground])
    mesh.apply_transform(ENU_TO_GLTF)

    applied_scale = scale or 1.0
    extents = mesh.extents * applied_scale
    report.boundsMeters = {
        "widthMeters": round(float(extents[0]), 3),
        "heightMeters": round(float(extents[1]), 3),
        "lengthMeters": round(float(extents[2]), 3),
        "measured": scale is not None,
    }

    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "building.glb").write_bytes(
            trimesh.Scene(mesh).export(file_type="glb")
        )
        (out_dir / "anchor-report.json").write_text(
            json.dumps(asdict(report), indent=2) + "\n"
        )

    # The exported footprint is the mesh's own outline placed on the Earth: scaled
    # to meters and rotated by the solved heading, so it can be compared against
    # the OSM ring it was matched to rather than merely asserted.
    world = _rotate(np.asarray(footprint_enu.exterior.coords)[:-1] * applied_scale,
                    heading or 0.0)
    ring = [list(latlon_from_enu(east, north, (latitude, longitude)))
            for east, north in world]

    # The geometry is now canonical whatever came in: ground at y=0, footprint
    # centre at the origin, +Y up. So the placement says so truthfully.
    placement = {
        "schemaVersion": 1,
        "assetId": asset_id,
        "name": name or asset_id,
        "sourceAddress": address,
        "location": {"latitude": latitude, "longitude": longitude,
                     "elevationMeters": elevation_meters},
        "transform": {
            "headingDegrees": heading if heading is not None else 0.0,
            "metersPerModelUnit": applied_scale,
            "verticalOffsetMeters": 0.0,
            "anchor": "ground-center",
            "upAxis": "Y",
        },
        "models": {"high": "building.glb", "low": None},
        "dimensions": {k: v for k, v in report.boundsMeters.items() if k != "measured"},
        "footprint": ring,
    }
    return {"placement": placement, "report": asdict(report)}
