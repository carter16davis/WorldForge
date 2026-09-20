"""GLB container surgery: read it, measure it, bake a transform into it.

Three jobs, none of which unpack the geometry.

**Read and write.** A GLB is a 12-byte header followed by a JSON chunk and a
binary chunk. Rewriting the JSON and handing the binary back untouched is the
only safe way to edit a photogrammetric model here: the alternative is loading
half a million triangles into trimesh and re-exporting them, which re-encodes
the texture atlas and quietly changes the measurement.

**Measure.** `scene_bounds` reads the POSITION accessors' declared min/max and
walks them through the node hierarchy, so the size of a 50 MB model costs a
JSON parse rather than a mesh load.

**Bake.** `bake_transform` wraps the scene's root nodes in one new node
carrying the scale the user set in the placement editor, and the Z-up to Y-up
rotation when the producer's mesh is in an ENU frame. That is a glTF node
transform — every consumer applies it, and the vertex data is byte-identical to
what the reconstruction produced. The export is therefore both *correct at
1 m/unit for anything that just loads the GLB* and *still auditable*, because
the baked factor is recorded rather than multiplied into the vertices.
"""

from __future__ import annotations

import io
import json
import math
import struct
from pathlib import Path

import numpy as np

JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942

BAKE_NODE_NAME = "worldforge-placement-scale"


class NotGlb(ValueError):
    """The file is not a GLB this module is willing to touch."""


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------

def read_glb(data: bytes) -> tuple[dict, bytes]:
    """Split a GLB into its JSON document and its binary chunk."""
    if len(data) < 20 or data[:4] != b"glTF":
        raise NotGlb("not a GLB file")
    _, version, declared = struct.unpack("<4sII", data[:12])
    if version != 2:
        raise NotGlb(f"GLB version {version}, expected 2")
    if declared != len(data):
        raise NotGlb("declared length does not match the file size")

    document: dict | None = None
    binary = b""
    offset = 12
    while offset + 8 <= len(data):
        length, kind = struct.unpack("<II", data[offset:offset + 8])
        body = data[offset + 8:offset + 8 + length]
        if kind == JSON_CHUNK:
            document = json.loads(body)
        elif kind == BIN_CHUNK:
            binary = body
        offset += 8 + length + (-length % 4)

    if document is None:
        raise NotGlb("GLB has no JSON chunk")
    return document, binary


def write_glb(document: dict, binary: bytes) -> bytes:
    """Serialise a JSON document and binary chunk back into a GLB."""
    text = json.dumps(document, separators=(",", ":")).encode()
    text += b" " * (-len(text) % 4)
    binary = bytes(binary)
    binary += b"\0" * (-len(binary) % 4)

    total = 12 + 8 + len(text) + (8 + len(binary) if binary else 0)
    out = io.BytesIO()
    out.write(struct.pack("<4sII", b"glTF", 2, total))
    out.write(struct.pack("<II", len(text), JSON_CHUNK))
    out.write(text)
    if binary:
        out.write(struct.pack("<II", len(binary), BIN_CHUNK))
        out.write(binary)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def _node_matrix(node: dict) -> np.ndarray:
    """One node's local transform as a 4x4, from `matrix` or from TRS."""
    if "matrix" in node:
        # glTF stores column-major; numpy reads row-major.
        return np.array(node["matrix"], dtype=float).reshape(4, 4).T

    matrix = np.eye(4)
    if (scale := node.get("scale")) is not None:
        matrix = np.diag([*(float(s) for s in scale), 1.0])
    if (rotation := node.get("rotation")) is not None:
        x, y, z, w = (float(v) for v in rotation)
        rot = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0.0],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0.0],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ])
        matrix = rot @ matrix
    if (translation := node.get("translation")) is not None:
        move = np.eye(4)
        move[:3, 3] = [float(t) for t in translation]
        matrix = move @ matrix
    return matrix


def _default_scene(document: dict) -> dict:
    scenes = document.get("scenes") or []
    if not scenes:
        raise NotGlb("GLB declares no scene")
    index = document.get("scene", 0)
    if not isinstance(index, int) or not 0 <= index < len(scenes):
        index = 0
    return scenes[index]


def scene_bounds(document: dict) -> tuple[list[float], list[float]] | None:
    """Axis-aligned bounds of the default scene, in model units.

    Taken from the accessors' own declared min/max, which glTF requires for
    POSITION, so nothing is decoded. Returns None when the document does not
    carry enough information to measure — a caller must not guess a size.
    """
    try:
        scene = _default_scene(document)
    except NotGlb:
        return None
    nodes = document.get("nodes") or []
    meshes = document.get("meshes") or []
    accessors = document.get("accessors") or []

    low = np.full(3, math.inf)
    high = np.full(3, -math.inf)
    seen = set()

    def visit(index: int, parent: np.ndarray) -> None:
        if index in seen or not 0 <= index < len(nodes):
            return                      # a cycle is malformed; refuse to spin
        seen.add(index)
        node = nodes[index]
        world = parent @ _node_matrix(node)

        mesh_index = node.get("mesh")
        if isinstance(mesh_index, int) and 0 <= mesh_index < len(meshes):
            for primitive in meshes[mesh_index].get("primitives") or []:
                position = (primitive.get("attributes") or {}).get("POSITION")
                if not isinstance(position, int) or not 0 <= position < len(accessors):
                    continue
                accessor = accessors[position]
                lo, hi = accessor.get("min"), accessor.get("max")
                if not (isinstance(lo, list) and isinstance(hi, list)
                        and len(lo) == 3 and len(hi) == 3):
                    continue
                # Transform all eight corners: a rotated box's AABB is not the
                # rotation of its AABB's two corners.
                corners = np.array([[lo[0] if bit & 1 else hi[0],
                                     lo[1] if bit & 2 else hi[1],
                                     lo[2] if bit & 4 else hi[2], 1.0]
                                    for bit in range(8)], dtype=float)
                placed = (world @ corners.T).T[:, :3]
                np.minimum(low, placed.min(axis=0), out=low)
                np.maximum(high, placed.max(axis=0), out=high)

        for child in node.get("children") or []:
            visit(child, world)
        seen.discard(index)

    for root in scene.get("nodes") or []:
        visit(root, np.eye(4))

    if not np.isfinite(low).all() or not np.isfinite(high).all():
        return None
    return [float(v) for v in low], [float(v) for v in high]


def extents(document: dict) -> list[float] | None:
    """[x, y, z] size of the default scene in model units, or None."""
    box = scene_bounds(document)
    if box is None:
        return None
    low, high = box
    return [round(h - l, 6) for l, h in zip(low, high)]


# ---------------------------------------------------------------------------
# Baking
# ---------------------------------------------------------------------------

# -90 degrees about +X: ENU (east, north, up) -> glTF (east, up, -north).
# Same rotation as `reconstruction.anchor.ENU_TO_GLTF` and as the viewer's
# frame fix, expressed as the quaternion a glTF node wants.
_Z_UP_TO_Y_UP = [-math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5)]


def bake_transform(document: dict, *, scale: float = 1.0,
                   up_axis: str = "Y") -> dict | None:
    """Wrap the default scene's roots in a node carrying `scale` (and the
    Z-up correction when `up_axis` is "Z"). Mutates and describes the document.

    Returns None when there is nothing to bake, and raises `NotGlb` when the
    document has no scene to wrap. The caller keeps the un-baked file in that
    case rather than shipping a model whose size is a guess.
    """
    if not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
        raise ValueError("scale must be a positive, finite number")
    up_axis = (up_axis or "Y").upper()
    if up_axis not in ("Y", "Z"):
        raise ValueError("up_axis must be 'Y' or 'Z'")

    rotate = up_axis == "Z"
    if math.isclose(scale, 1.0, rel_tol=0, abs_tol=1e-9) and not rotate:
        return None

    scene = _default_scene(document)
    roots = scene.get("nodes")
    if not roots:
        raise NotGlb("the GLB's scene has no nodes to transform")

    nodes = document.setdefault("nodes", [])
    wrapper: dict = {"name": BAKE_NODE_NAME, "children": list(roots)}
    if not math.isclose(scale, 1.0, rel_tol=0, abs_tol=1e-9):
        wrapper["scale"] = [scale, scale, scale]
    if rotate:
        wrapper["rotation"] = list(_Z_UP_TO_Y_UP)
    nodes.append(wrapper)
    scene["nodes"] = [len(nodes) - 1]

    return {
        "metersPerModelUnit": scale,
        "upAxisIn": up_axis,
        "upAxisOut": "Y",
        "method": "gltf-node-transform",
        "node": BAKE_NODE_NAME,
    }


def bake_file(source: Path | str, destination: Path | str, *,
              scale: float = 1.0, up_axis: str = "Y") -> dict:
    """`bake_transform` over a file. Always writes `destination`.

    The report says what was applied and what the model measures afterwards, so
    the export manifest can state the size of the bytes it actually shipped
    rather than the size someone intended.
    """
    source, destination = Path(source), Path(destination)
    document, binary = read_glb(source.read_bytes())

    before = extents(document)
    applied = bake_transform(document, scale=scale, up_axis=up_axis)
    after = extents(document)

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(write_glb(document, binary) if applied
                            else source.read_bytes())

    return {
        "applied": applied,
        "extentsModelUnits": before,
        "extentsMeters": after,
        "bytes": destination.stat().st_size,
    }
