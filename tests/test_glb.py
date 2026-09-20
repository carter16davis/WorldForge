"""The exported model has to be the size it was on screen.

WorldForge keeps the placement transform in `placement.json` because that is
the contract, but a GLB is also a file people open on its own — dropped into
Blender, into an engine importer, into a glTF viewer. If the scale the user
solved in the editor lives only in a sidecar JSON, every one of those loads the
building at the wrong size and nothing says so. So the scale is baked into the
container before the package is written, and these tests are the check that
baking does not change anything else.
"""

from __future__ import annotations

import json
import struct

import numpy as np
import pytest
import trimesh

from app import glb


def _box_glb(path, extents=(4.0, 3.0, 2.0)):
    mesh = trimesh.creation.box(extents=list(extents))
    path.write_bytes(trimesh.Scene(mesh).export(file_type="glb"))
    return mesh


def test_a_scaled_export_measures_what_the_editor_showed(tmp_path):
    source, baked = tmp_path / "in.glb", tmp_path / "out.glb"
    _box_glb(source)

    report = glb.bake_file(source, baked, scale=2.5)

    assert report["extentsModelUnits"] == [4.0, 3.0, 2.0]
    assert report["extentsMeters"] == [10.0, 7.5, 5.0]
    assert report["applied"]["metersPerModelUnit"] == 2.5
    # The real check is what a consumer measures, not what the report claims.
    assert trimesh.load(baked, force="mesh").extents == pytest.approx([10.0, 7.5, 5.0])


def test_the_vertices_are_untouched(tmp_path):
    """Baking must stay reversible. A node transform is a declaration about the
    same measurement; rewriting the vertex buffer would be a new one."""
    source, baked = tmp_path / "in.glb", tmp_path / "out.glb"
    _box_glb(source)

    glb.bake_file(source, baked, scale=7.0)

    before_doc, before_bin = glb.read_glb(source.read_bytes())
    after_doc, after_bin = glb.read_glb(baked.read_bytes())
    assert after_bin[:len(before_bin)] == before_bin
    assert after_doc["accessors"] == before_doc["accessors"]
    assert after_doc["meshes"] == before_doc["meshes"]


def test_a_z_up_scan_is_laid_down_for_gltf_consumers(tmp_path):
    """ENU is +Z up. glTF is +Y up. A consumer that believes the container gets
    a building on its side unless the correction is in the file."""
    source, baked = tmp_path / "in.glb", tmp_path / "out.glb"
    _box_glb(source, extents=(4.0, 3.0, 2.0))      # east 4, north 3, up 2

    glb.bake_file(source, baked, scale=1.0, up_axis="Z")

    # east stays X, up becomes Y, north becomes -Z.
    assert trimesh.load(baked, force="mesh").extents == pytest.approx([4.0, 2.0, 3.0])


def test_scale_and_frame_bake_together(tmp_path):
    source, baked = tmp_path / "in.glb", tmp_path / "out.glb"
    _box_glb(source, extents=(4.0, 3.0, 2.0))

    glb.bake_file(source, baked, scale=3.0, up_axis="Z")

    assert trimesh.load(baked, force="mesh").extents == pytest.approx([12.0, 6.0, 9.0])


def test_nothing_to_bake_leaves_the_file_alone(tmp_path):
    source, baked = tmp_path / "in.glb", tmp_path / "out.glb"
    _box_glb(source)

    report = glb.bake_file(source, baked, scale=1.0)

    assert report["applied"] is None
    assert baked.read_bytes() == source.read_bytes()


def test_bounds_follow_the_node_hierarchy(tmp_path):
    """`scene_bounds` reads accessor min/max, which are in each node's own
    space. Ignoring the nodes above them would report a building's size as the
    size of whichever part happened to be modelled at the origin."""
    source = tmp_path / "in.glb"
    mesh = trimesh.creation.box(extents=[2.0, 2.0, 2.0])
    scene = trimesh.Scene()
    scene.add_geometry(mesh, node_name="a", transform=np.eye(4))
    moved = np.eye(4)
    moved[:3, 3] = [10.0, 0.0, 0.0]
    scene.add_geometry(mesh, node_name="b", transform=moved)
    source.write_bytes(scene.export(file_type="glb"))

    low, high = glb.scene_bounds(glb.read_glb(source.read_bytes())[0])
    assert high[0] - low[0] == pytest.approx(12.0)      # -1 .. 11


def test_a_file_that_is_not_a_glb_is_refused(tmp_path):
    with pytest.raises(glb.NotGlb):
        glb.read_glb(b"this is not a GLB, it is a sentence")


def test_a_nonsense_scale_is_refused(tmp_path):
    document = {"scenes": [{"nodes": [0]}], "nodes": [{}]}
    for bad in (0, -2.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            glb.bake_transform(dict(document), scale=bad)


def test_the_rewritten_container_is_still_a_valid_glb(tmp_path):
    """Chunk lengths, padding and the declared file length all move when the
    JSON grows by a node. A viewer that checks them is the one that matters."""
    source, baked = tmp_path / "in.glb", tmp_path / "out.glb"
    _box_glb(source)

    glb.bake_file(source, baked, scale=1.5)
    data = baked.read_bytes()

    magic, version, declared = struct.unpack("<4sII", data[:12])
    assert (magic, version, declared) == (b"glTF", 2, len(data))
    json_length, kind = struct.unpack("<II", data[12:20])
    assert kind == glb.JSON_CHUNK
    assert json_length % 4 == 0
    document = json.loads(data[20:20 + json_length])
    assert document["nodes"][-1]["name"] == glb.BAKE_NODE_NAME
    assert document["scenes"][document.get("scene", 0)]["nodes"] == [len(document["nodes"]) - 1]
